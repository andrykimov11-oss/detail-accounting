"""
Оркестратор ядра подетального учёта.

Связывает модули в единый контур:

    .xbir ──► импорт ──► storage(details)
                              │
    скан ──► process_scan ────┤──► storage(scan_events, facts)
                              │
                              └──► расчёт статуса ──► payload для 1С

Ключевое отличие от прямого вызова process_scan: **факт переживает
перезапуск**. Счётчики сканов хранятся в таблице facts и восстанавливаются
в OrderContext при загрузке заказа. До этого модуля факт жил только в
памяти и терялся при остановке процесса.

Применение:
    core = ProductionCore(Storage("prod.db"))
    core.import_xbir([Path("order6564.xbir")])
    ctx = core.load_order(6564)
    result = core.handle_scan(event, ctx)
    statuses = core.order_status(ctx, operations_1c)
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from xbir_parser import Detail, ValidationReport, parse_xbir  # noqa: E402
from operation_plan import OperationPlan, build_order_plan  # noqa: E402
from scan_processor import (  # noqa: E402
    AREAS,
    FactResult,
    FactStatus,
    OrderContext,
    ScanEvent,
    process_scan,
)
from status_calc import OperationStatus, calc_operation_status, to_1c_payload  # noqa: E402
from storage import Storage  # noqa: E402


def qr_of(detail_uid: str) -> str:
    """QR-код детали: MD5(GUID)[:10] (см. docs/qr-format-spec.md)."""
    return hashlib.md5(detail_uid.encode()).hexdigest()[:10]


@dataclass
class ImportResult:
    """Итог импорта .xbir в хранилище."""
    files: int = 0
    details_imported: int = 0
    rows_total: int = 0
    orders: list[int] = field(default_factory=list)
    reports: list[ValidationReport] = field(default_factory=list)
    qr_collisions: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(len(r.errors) for r in self.reports)


class ProductionCore:
    """
    Фасад ядра учёта. Вся работа с данными идёт через Storage,
    бизнес-логика остаётся в чистых модулях (scan_processor, status_calc).
    """

    def __init__(self, storage: Storage):
        self.storage = storage

    # --- Справочник участков -------------------------------------------------

    def seed_areas(self, areas: dict[str, list[str]] | None = None,
                   names: dict[str, str] | None = None) -> int:
        """
        Заполнить справочник «участок → операции» в БД.
        По умолчанию — из константы AREAS (стартовый набор для пилота).
        Дальше технолог правит справочник в БД, без правки кода.
        """
        areas = areas if areas is not None else AREAS
        names = names or {
            "area_cutting": "Раскрой",
            "area_edging": "Кромление",
            "area_drilling": "Сверление",
            "area_packing": "Упаковка",
        }
        n = 0
        for area_id, ops in areas.items():
            for op in ops:
                self.storage.upsert_area_operation(
                    area_id, names.get(area_id, area_id), op
                )
                n += 1
        return n

    def area_operations(self, area_id: str) -> list[str]:
        """Операции участка из справочника БД (fallback — константа AREAS)."""
        ops = self.storage.get_operations_by_area(area_id)
        return ops if ops else AREAS.get(area_id, [])

    # --- Импорт плана из .xbir ----------------------------------------------

    def import_xbir(self, paths: list[Path]) -> ImportResult:
        """
        Распарсить .xbir и загрузить плановый состав деталей в хранилище.
        Попутно контролирует коллизии QR: два разных GUID с одинаковым
        MD5[:10] сделали бы учёт неоднозначным.
        """
        res = ImportResult()
        seen_qr: dict[str, str] = {}   # qr → detail_uid
        orders: set[int] = set()

        for path in paths:
            details, report = parse_xbir(Path(path))
            res.files += 1
            res.rows_total += report.rows_total
            res.reports.append(report)

            for d in details:
                if d.order_num is None:
                    continue
                qr = qr_of(d.detail_uid)

                prev = seen_qr.get(qr)
                if prev is not None and prev != d.detail_uid:
                    res.qr_collisions.append((qr, prev, d.detail_uid))
                seen_qr[qr] = d.detail_uid

                self.storage.upsert_detail({
                    "detail_uid": d.detail_uid,
                    "order_num": d.order_num,
                    "qr_code": qr,
                    "pos_no": d.pos_no,
                    "material_name": d.material_name,
                    "thickness": d.thickness,
                    "length": d.length,
                    "width": d.width,
                    "qty": d.qty,
                    "edge_l1": d.edge_l1,
                    "edge_l2": d.edge_l2,
                    "edge_w1": d.edge_w1,
                    "edge_w2": d.edge_w2,
                    "edge_total_len": d.edge_total_len,
                    "perimeter": d.perimeter,
                    "area": d.area,
                    "source_file": d.source_file,
                })
                res.details_imported += 1
                orders.add(d.order_num)

        res.orders = sorted(orders)
        return res

    # --- Загрузка заказа: план из БД + восстановление факта -----------------

    def load_order(self, order_num: int) -> OrderContext:
        """
        Собрать OrderContext заказа из хранилища и **восстановить накопленный
        факт** из таблицы facts. Это то, что делает учёт устойчивым к
        перезапуску приложения.
        """
        rows = self.storage.get_details_by_order(order_num)
        details = [self._row_to_detail(r) for r in rows]
        ctx = OrderContext.build(order_num, details)

        for f in self.storage.get_facts_by_order(order_num):
            ctx.scanned[(f["operation_1c"], f["detail_uid"])] = f["scanned_count"]

        # Окно дубликатов тоже восстанавливаем: иначе повторный скан сразу
        # после перезапуска (или с соседнего рабочего места) пройдёт как
        # новая деталь и завысит факт.
        for row in self.storage.get_last_scan_times(order_num):
            try:
                ctx.last_scan_time[(row["qr_code"], row["operation_1c"])] = (
                    datetime.fromisoformat(row["last_at"])
                )
            except (TypeError, ValueError):
                continue

        return ctx

    @staticmethod
    def _row_to_detail(r) -> Detail:
        """Строка БД → модель Detail (для чистых модулей логики)."""
        return Detail(
            detail_uid=r["detail_uid"],
            order_num=r["order_num"],
            material_name=r["material_name"] or "",
            thickness=r["thickness"] or 0,
            length=r["length"] or 0,
            width=r["width"] or 0,
            qty=r["qty"] or 0,
            perimeter=r["perimeter"] or 0.0,
            area=r["area"] or 0.0,
            edge_l1=r["edge_l1"] or 0.0,
            edge_l2=r["edge_l2"] or 0.0,
            edge_w1=r["edge_w1"] or 0.0,
            edge_w2=r["edge_w2"] or 0.0,
            edge_total_len=r["edge_total_len"] or 0.0,
            pos_no=r["pos_no"] or "",
            source_file=r["source_file"] or "",
        )

    # --- Обработка скана с сохранением --------------------------------------

    def handle_scan(self, event: ScanEvent, ctx: OrderContext) -> FactResult:
        """
        Обработать событие сканера и сохранить результат.

        Пишется ВСЁ: и принятые факты, и ошибки/дубликаты — scan_events это
        audit log (спецификация, Принцип 6: «все ошибки пишутся в лог,
        не теряются»). В facts попадает только принятый факт.
        """
        area_ops = self.area_operations(event.area_id)
        result = process_scan(event, ctx, area_ops=area_ops)

        self.storage.log_scan_event({
            "scan_id": event.scan_id,
            "qr_code": event.qr_code,
            "area_id": event.area_id,
            "operator_id": event.operator_id,
            "operation_1c": event.operation_1c,
            "scanned_at": event.scanned_at.isoformat(),
            "status": result.status.value,
            "detail_uid": result.detail_uid,
            "scanned_count": result.scanned_count,
            "planned_qty": result.planned_qty,
            "message": result.message,
            "anomaly": result.anomaly,
            "suggest_list": result.suggest_detail_list,
        })

        if result.status == FactStatus.ACCEPTED:
            self.storage.upsert_fact(
                order_num=ctx.order_num,
                operation_1c=result.operation_1c,
                detail_uid=result.detail_uid,
                scanned_count=result.scanned_count,
            )

        return result

    # --- План и статус -------------------------------------------------------

    def order_plan(self, ctx: OrderContext,
                   operations_1c: list[str]) -> tuple[list[OperationPlan], list[str]]:
        return build_order_plan(ctx.details, operations_1c)

    def order_status(self, ctx: OrderContext, operations_1c: list[str],
                     now: datetime | None = None) -> list[OperationStatus]:
        """Статусы всех операций заказа по текущему факту."""
        plans, _ = self.order_plan(ctx, operations_1c)
        return [calc_operation_status(p, ctx, now=now) for p in plans]

    def closing_payloads(self, ctx: OrderContext,
                         operations_1c: list[str]) -> list[dict]:
        """
        Payload'ы для 1С по операциям, готовым к закрытию.
        Отправка — задача Этапа B (HTTP-клиент), здесь только формирование.
        """
        return [
            to_1c_payload(s)
            for s in self.order_status(ctx, operations_1c)
            if s.is_closing
        ]
