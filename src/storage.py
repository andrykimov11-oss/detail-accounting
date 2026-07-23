"""
Слой хранения данных. Абстракция над БД.

Бизнес-логика (scan_processor, status_calc) НЕ знает, под ней SQLite
или PostgreSQL. Весь доступ к данным — через этот модуль. Смена движка =
смена реализации Storage, остальной код не трогается.

Текущая реализация: SQLite (один файл, без сервера). Достаточно для PoC и
пилота на 1-2 станках. При росте до 3+ станков с параллельной записью —
заменить на PostgreSQL (та же структура, другой драйвер).

Схема (см. docs/storage-schema.png при наличии):
    details      — плановый состав деталей (из .xbir)
    scan_events  — все события сканера, audit log (не удаляется)
    facts        — накопленный факт по операции+деталь
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


SCHEMA_SQL = """
-- Плановый состав деталей заказа (импорт из .xbir)
CREATE TABLE IF NOT EXISTS details (
    detail_uid    TEXT NOT NULL,
    order_num     INTEGER NOT NULL,
    qr_code       TEXT NOT NULL,
    pos_no        TEXT,
    material_name TEXT,
    thickness     INTEGER,
    length        INTEGER,
    width         INTEGER,
    qty           INTEGER NOT NULL,          -- плановое количество экземпляров
    edge_l1       REAL DEFAULT 0,
    edge_l2       REAL DEFAULT 0,
    edge_w1       REAL DEFAULT 0,
    edge_w2       REAL DEFAULT 0,
    edge_total_len REAL DEFAULT 0,
    perimeter     REAL DEFAULT 0,
    area          REAL DEFAULT 0,
    source_file   TEXT,
    imported_at   TEXT NOT NULL,
    PRIMARY KEY (detail_uid, order_num)
);

-- Справочник «участок → операция» (технолог правит без правки кода).
-- Один участок → много операций; одна операция может быть на нескольких участках.
CREATE TABLE IF NOT EXISTS area_operations (
    area_id       TEXT NOT NULL,            -- 'area_edging'
    area_name     TEXT NOT NULL,            -- 'Кромление'
    operation_1c  TEXT NOT NULL,            -- 'Облицовывание кромки 19/0,8'
    PRIMARY KEY (area_id, operation_1c)
);

-- Настройки приложения: пути к источникам данных и параметры.
-- Задаются технологом в веб-админке, не хардкодятся. Пути к .xbir, выгрузке
-- 1С и логам станков определяются удобством станка/Базиса, а не приложением,
-- поэтому должны настраиваться, а не быть зашитыми.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- Реестр связок «заказ Базиса ↔ документ 1С».
-- Числовой номер из .xbir может соответствовать нескольким документам 1С
-- (разные префиксы ПС00/ЛД00/0Ч00). Связка разрешается один раз и хранится
-- здесь: автоматически по имени клиента либо вручную технологом.
CREATE TABLE IF NOT EXISTS order_links (
    order_num      INTEGER PRIMARY KEY,      -- 7936 — число из .xbir
    order_full_num TEXT,                     -- 'ПС00-007936' — документ 1С
    client_name    TEXT,                     -- клиент по данным 1С
    xbir_client    TEXT,                     -- клиент, извлечённый из .xbir
    status         TEXT NOT NULL,            -- unique/client/manual/not_found
    reason         TEXT,                     -- чем разрешено (для аудита)
    candidates     TEXT,                     -- кандидаты через ';'
    confirmed_by   TEXT,                     -- кто подтвердил вручную
    resolved_at    TEXT NOT NULL
);

-- Справочник операторов: физлица из 1С (файл «производственные операции»).
-- Авторизация без пароля: оператор выбирает себя из списка и указывает участок.
CREATE TABLE IF NOT EXISTS operators (
    operator_id   TEXT PRIMARY KEY,         -- 'op_mazein'
    full_name     TEXT NOT NULL,            -- ФИО как в 1С
    is_active     INTEGER DEFAULT 1,
    imported_at   TEXT
);

-- Сессия смены: кто на каком участке работает сейчас.
-- Открывается при регистрации оператора, закрывается в конце смены.
CREATE TABLE IF NOT EXISTS shift_sessions (
    session_id    TEXT PRIMARY KEY,
    operator_id   TEXT NOT NULL,
    area_id       TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT
);

-- Все события сканера (audit log). Ничего не удаляется.
CREATE TABLE IF NOT EXISTS scan_events (
    scan_id      TEXT PRIMARY KEY,
    qr_code      TEXT,
    area_id      TEXT,
    operator_id  TEXT,
    operation_1c TEXT,
    scanned_at   TEXT NOT NULL,
    status       TEXT NOT NULL,              -- accepted/duplicate/overplan/...
    detail_uid   TEXT,                       -- если резолвится
    scanned_count INTEGER,                   -- счётчик на момент события
    planned_qty  INTEGER,
    message      TEXT,
    anomaly      INTEGER DEFAULT 0,
    suggest_list INTEGER DEFAULT 0
);

-- Очередь отправки статуса операции в 1С (гарантия доставки).
-- Статус помечается delivered только после успешного ответа 1С.
-- Недоставленные записи переживают недоступность сервиса.
CREATE TABLE IF NOT EXISTS status_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    op_key       TEXT NOT NULL UNIQUE,       -- operation_id или order|operation
    payload      TEXT NOT NULL,              -- JSON для 1С
    delivered    INTEGER DEFAULT 0,
    attempts     INTEGER DEFAULT 0,
    http_status  INTEGER DEFAULT 0,
    message      TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Накопленный факт: сколько деталей отсканировано по операции+деталь
CREATE TABLE IF NOT EXISTS facts (
    order_num     INTEGER NOT NULL,
    operation_1c  TEXT NOT NULL,
    detail_uid    TEXT NOT NULL,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (operation_1c, detail_uid)
);

CREATE INDEX IF NOT EXISTS idx_details_order ON details(order_num);
CREATE INDEX IF NOT EXISTS idx_details_qr ON details(qr_code);
CREATE INDEX IF NOT EXISTS idx_scan_events_order ON scan_events(operation_1c);
CREATE INDEX IF NOT EXISTS idx_facts_order ON facts(order_num);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON shift_sessions(ended_at);
CREATE INDEX IF NOT EXISTS idx_links_status ON order_links(status);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON status_outbox(delivered);
"""


class Storage:
    """
    Хранилище данных. Текущая реализация — SQLite.

    Везде, где бизнес-логике нужны данные, она обращается сюда, а не к БД
    напрямую. При переходе на PostgreSQL создаётся PostgresStorage с теми
    же методами.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()

    def _connect(self):
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- details (плановый состав) ------------------------------------------

    def upsert_detail(self, d: dict) -> None:
        """Добавить/обновить деталь в плане заказа."""
        self._conn.execute("""
            INSERT INTO details (detail_uid, order_num, qr_code, pos_no,
                material_name, thickness, length, width, qty,
                edge_l1, edge_l2, edge_w1, edge_w2, edge_total_len,
                perimeter, area, source_file, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(detail_uid, order_num) DO UPDATE SET
                qr_code=excluded.qr_code, qty=excluded.qty,
                length=excluded.length, width=excluded.width,
                thickness=excluded.thickness
        """, (
            d["detail_uid"], d["order_num"], d["qr_code"], d.get("pos_no", ""),
            d.get("material_name", ""), d.get("thickness", 0),
            d.get("length", 0), d.get("width", 0), d["qty"],
            d.get("edge_l1", 0), d.get("edge_l2", 0),
            d.get("edge_w1", 0), d.get("edge_w2", 0),
            d.get("edge_total_len", 0), d.get("perimeter", 0),
            d.get("area", 0), d.get("source_file", ""),
            datetime.now().isoformat(),
        ))
        self._conn.commit()

    def get_details_by_order(self, order_num: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM details WHERE order_num=? ORDER BY pos_no",
            (order_num,)
        ).fetchall()

    def get_detail_by_qr(self, qr_code: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM details WHERE qr_code=?", (qr_code,)
        ).fetchone()

    def count_details(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM details").fetchone()[0]

    # --- area_operations (справочник участок → операция) --------------------

    def upsert_area_operation(self, area_id: str, area_name: str,
                              operation_1c: str) -> None:
        """Добавить/обновить связку участок → операция."""
        self._conn.execute("""
            INSERT INTO area_operations (area_id, area_name, operation_1c)
            VALUES (?, ?, ?)
            ON CONFLICT(area_id, operation_1c) DO UPDATE SET
                area_name=excluded.area_name
        """, (area_id, area_name, operation_1c))
        self._conn.commit()

    def get_operations_by_area(self, area_id: str) -> list[str]:
        """Список операций участка."""
        rows = self._conn.execute(
            "SELECT operation_1c FROM area_operations WHERE area_id=? ORDER BY operation_1c",
            (area_id,)
        ).fetchall()
        return [r["operation_1c"] for r in rows]

    def get_all_areas(self) -> list[sqlite3.Row]:
        """Все участки (для списка выбора при регистрации)."""
        return self._conn.execute(
            "SELECT DISTINCT area_id, area_name FROM area_operations ORDER BY area_id"
        ).fetchall()

    def delete_area(self, area_id: str) -> int:
        """Удалить участок целиком (все его операции из справочника)."""
        cur = self._conn.execute(
            "DELETE FROM area_operations WHERE area_id=?", (area_id,))
        self._conn.commit()
        return cur.rowcount

    def delete_area_operation(self, area_id: str, operation_1c: str) -> None:
        """Убрать одну операцию из участка."""
        self._conn.execute(
            "DELETE FROM area_operations WHERE area_id=? AND operation_1c=?",
            (area_id, operation_1c))
        self._conn.commit()

    def get_area_of_operation(self, operation_1c: str) -> Optional[str]:
        """Найти участок операции (первое совпадение)."""
        row = self._conn.execute(
            "SELECT area_id FROM area_operations WHERE operation_1c=? LIMIT 1",
            (operation_1c,)
        ).fetchone()
        return row["area_id"] if row else None

    # --- settings (пути к источникам, параметры) ----------------------------

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                updated_at=excluded.updated_at
        """, (key, value, datetime.now().isoformat()))
        self._conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row and row["value"] is not None else default

    def all_settings(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- order_links (связка Базис ↔ 1С) ------------------------------------

    def upsert_order_link(self, order_num: int, status: str,
                          order_full_num: str = "", client_name: str = "",
                          xbir_client: str = "", reason: str = "",
                          candidates: list[str] | None = None,
                          confirmed_by: str = "") -> None:
        """Записать результат разрешения связки заказа."""
        self._conn.execute("""
            INSERT INTO order_links (order_num, order_full_num, client_name,
                xbir_client, status, reason, candidates, confirmed_by, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_num) DO UPDATE SET
                order_full_num=excluded.order_full_num,
                client_name=excluded.client_name,
                xbir_client=excluded.xbir_client,
                status=excluded.status,
                reason=excluded.reason,
                candidates=excluded.candidates,
                confirmed_by=excluded.confirmed_by,
                resolved_at=excluded.resolved_at
        """, (order_num, order_full_num, client_name, xbir_client, status,
              reason, ";".join(candidates or []), confirmed_by,
              datetime.now().isoformat()))
        self._conn.commit()

    def get_order_link(self, order_num: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM order_links WHERE order_num=?", (order_num,)
        ).fetchone()

    def get_pending_links(self) -> list[sqlite3.Row]:
        """
        Очередь ручного разбора: заказы, связку которых система не смогла
        установить однозначно. Технолог сопоставляет их вручную.
        """
        return self._conn.execute(
            "SELECT * FROM order_links WHERE status IN ('manual','not_found')"
            " ORDER BY order_num"
        ).fetchall()

    def confirm_order_link(self, order_num: int, order_full_num: str,
                           confirmed_by: str) -> None:
        """
        Технолог вручную подтвердил связку. Статус становится 'manual_confirmed' —
        отличается от автоматических, чтобы в аудите было видно решение человека.
        """
        self._conn.execute("""
            UPDATE order_links
               SET order_full_num=?, status='manual_confirmed',
                   confirmed_by=?, resolved_at=?,
                   reason='подтверждено вручную'
             WHERE order_num=?
        """, (order_full_num, confirmed_by, datetime.now().isoformat(), order_num))
        self._conn.commit()

    def is_order_linked(self, order_num: int) -> bool:
        """
        Разрешена ли связка заказа. Заказ без подтверждённой связки не
        допускается к учёту: статус ушёл бы не в тот документ 1С.
        """
        row = self.get_order_link(order_num)
        return bool(row) and row["status"] in (
            "unique", "window", "client", "manual_confirmed"
        ) and bool(row["order_full_num"])

    # --- operators / смены ---------------------------------------------------

    def upsert_operator(self, operator_id: str, full_name: str,
                        is_active: bool = True) -> None:
        """Добавить/обновить оператора (импорт списка физлиц из 1С)."""
        self._conn.execute("""
            INSERT INTO operators (operator_id, full_name, is_active, imported_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(operator_id) DO UPDATE SET
                full_name=excluded.full_name, is_active=excluded.is_active
        """, (operator_id, full_name, int(is_active), datetime.now().isoformat()))
        self._conn.commit()

    def get_operators(self, active_only: bool = True) -> list[sqlite3.Row]:
        """Список операторов для экрана выбора (авторизация без пароля)."""
        sql = "SELECT * FROM operators"
        if active_only:
            sql += " WHERE is_active=1"
        return self._conn.execute(sql + " ORDER BY full_name").fetchall()

    def get_operator(self, operator_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM operators WHERE operator_id=?", (operator_id,)
        ).fetchone()

    def delete_operator(self, operator_id: str) -> None:
        """
        Удалить оператора из справочника. Аудит сканов не страдает: в
        scan_events operator_id хранится строкой и остаётся для истории.
        Для ошибочных записей; штатно оператора лучше выключать (is_active).
        """
        self._conn.execute("DELETE FROM operators WHERE operator_id=?",
                           (operator_id,))
        self._conn.commit()

    def open_shift(self, session_id: str, operator_id: str,
                   area_id: str, started_at: str | None = None) -> None:
        """Оператор заступил на смену на участке."""
        self._conn.execute("""
            INSERT OR REPLACE INTO shift_sessions
                (session_id, operator_id, area_id, started_at, ended_at)
            VALUES (?, ?, ?, ?, NULL)
        """, (session_id, operator_id, area_id,
              started_at or datetime.now().isoformat()))
        self._conn.commit()

    def close_shift(self, session_id: str, ended_at: str | None = None) -> None:
        self._conn.execute(
            "UPDATE shift_sessions SET ended_at=? WHERE session_id=?",
            (ended_at or datetime.now().isoformat(), session_id))
        self._conn.commit()

    def get_open_shift(self, operator_id: str) -> Optional[sqlite3.Row]:
        """Открытая смена оператора — источник area_id для события скана."""
        return self._conn.execute(
            "SELECT * FROM shift_sessions WHERE operator_id=? AND ended_at IS NULL"
            " ORDER BY started_at DESC LIMIT 1", (operator_id,)
        ).fetchone()

    # --- scan_events (audit log) --------------------------------------------

    def log_scan_event(self, event: dict) -> None:
        """Записать событие сканера в audit log (любой статус)."""
        self._conn.execute("""
            INSERT OR REPLACE INTO scan_events
                (scan_id, qr_code, area_id, operator_id, operation_1c,
                 scanned_at, status, detail_uid, scanned_count, planned_qty,
                 message, anomaly, suggest_list)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event["scan_id"], event.get("qr_code", ""), event.get("area_id", ""),
            event.get("operator_id", ""), event.get("operation_1c", ""),
            event["scanned_at"], event["status"], event.get("detail_uid", ""),
            event.get("scanned_count", 0), event.get("planned_qty", 0),
            event.get("message", ""), int(event.get("anomaly", False)),
            int(event.get("suggest_list", False)),
        ))
        self._conn.commit()

    def count_scan_events(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0]

    def count_accepted_events(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM scan_events WHERE status='accepted'"
        ).fetchone()[0]

    def get_last_scan_times(self, order_num: int) -> list[sqlite3.Row]:
        """
        Время последнего засчитанного скана по паре (qr_code, операция).

        Нужно для защиты от дубликатов между перезапусками и между
        рабочими местами: окно дубликатов должно быть общим, а не жить
        в памяти одного процесса.
        """
        return self._conn.execute("""
            SELECT e.qr_code, e.operation_1c, MAX(e.scanned_at) AS last_at
              FROM scan_events e
              JOIN details d ON d.qr_code = e.qr_code
             WHERE d.order_num = ? AND e.status = 'accepted'
             GROUP BY e.qr_code, e.operation_1c
        """, (order_num,)).fetchall()

    # --- facts (накопленный факт) -------------------------------------------

    def upsert_fact(self, order_num: int, operation_1c: str,
                    detail_uid: str, scanned_count: int) -> None:
        """Обновить накопленный счётчик факта по операции+деталь."""
        self._conn.execute("""
            INSERT INTO facts (order_num, operation_1c, detail_uid,
                               scanned_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(operation_1c, detail_uid) DO UPDATE SET
                scanned_count=excluded.scanned_count,
                updated_at=excluded.updated_at
        """, (order_num, operation_1c, detail_uid, scanned_count,
              datetime.now().isoformat()))
        self._conn.commit()

    def get_facts_by_order(self, order_num: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM facts WHERE order_num=?", (order_num,)
        ).fetchall()

    def get_fact_count(self, operation_1c: str, detail_uid: str) -> int:
        row = self._conn.execute(
            "SELECT scanned_count FROM facts WHERE operation_1c=? AND detail_uid=?",
            (operation_1c, detail_uid)
        ).fetchone()
        return row["scanned_count"] if row else 0

    # --- status_outbox (очередь отправки в 1С) ------------------------------

    def enqueue_status(self, op_key: str, payload: dict) -> int:
        """Поставить статус в очередь. Повторный статус операции обновляет запись."""
        import json as _json
        now = datetime.now().isoformat()
        cur = self._conn.execute("""
            INSERT INTO status_outbox (op_key, payload, delivered, attempts,
                                       created_at, updated_at)
            VALUES (?, ?, 0, 0, ?, ?)
            ON CONFLICT(op_key) DO UPDATE SET
                payload=excluded.payload, delivered=0, updated_at=excluded.updated_at
        """, (op_key, _json.dumps(payload, ensure_ascii=False), now, now))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM status_outbox WHERE op_key=?", (op_key,)
        ).fetchone()
        return row["id"]

    def mark_status_sent(self, outbox_id: int, delivered: bool,
                         http_status: int = 0, message: str = "") -> None:
        self._conn.execute("""
            UPDATE status_outbox
               SET delivered=?, attempts=attempts+1, http_status=?,
                   message=?, updated_at=?
             WHERE id=?
        """, (int(delivered), http_status, message,
              datetime.now().isoformat(), outbox_id))
        self._conn.commit()

    def get_pending_statuses(self, limit: int = 100) -> list[sqlite3.Row]:
        """Недоставленные статусы — очередь на повторную отправку."""
        return self._conn.execute(
            "SELECT * FROM status_outbox WHERE delivered=0 ORDER BY id LIMIT ?",
            (limit,)
        ).fetchall()

    def count_delivered_statuses(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM status_outbox WHERE delivered=1"
        ).fetchone()[0]

    # --- отладка -------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "details": self.count_details(),
            "scan_events": self.count_scan_events(),
            "accepted": self.count_accepted_events(),
        }
