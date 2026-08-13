"""
Забор исходных файлов (выгрузки 1С) из внешних источников.

Это адаптер над «швом» получения файлов: скачивает файл во входящую папку,
а дальше его разбирают штатные парсеры (`one_c_loader` и др.). Приложение на
интернет-сервере не видит папок цеха — файлы приходят либо загрузкой через
админку, либо забором отсюда. Ядро учёта от способа доставки не зависит.

Пока реализован источник FTP (на rascroi.ru развёрнут FTP, куда 1С кладёт
выгрузки). Пароль НЕ хранится в веб-настройках — берётся из окружения
(DA_FTP_PASS), в настройках только хост/пользователь/папка/имена файлов.
Если FTP на том же сервере, что и приложение, можно вместо забора просто
указать локальный путь к папке FTP — тогда этот модуль не нужен.
"""
from __future__ import annotations

import ftplib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FtpConfig:
    """Параметры подключения к FTP-источнику выгрузок."""
    host: str
    user: str = "anonymous"
    password: str = ""
    directory: str = ""          # папка на FTP (cwd после логина)
    filenames: list[str] = field(default_factory=list)  # что забирать
    secure: bool = True          # FTPS (TLS) — если сервер поддерживает
    port: int = 21
    timeout: float = 30.0


def _connect(cfg: FtpConfig) -> ftplib.FTP:
    """Открыть соединение: FTPS при secure=True, иначе обычный FTP."""
    ftp = ftplib.FTP_TLS() if cfg.secure else ftplib.FTP()
    ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
    ftp.login(cfg.user, cfg.password)
    if cfg.secure:
        ftp.prot_p()             # защитить и канал данных, не только логин
    if cfg.directory:
        ftp.cwd(cfg.directory)
    return ftp


def fetch_files(cfg: FtpConfig, dest_dir: Path) -> list[Path]:
    """
    Скачать cfg.filenames с FTP в dest_dir. Возвращает пути скачанных файлов.

    Имя на диске = базовое имя файла (без путей FTP). Каждый файл пишется
    целиком; при ошибке одного — исключение (вызывающий решает, что делать).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ftp = _connect(cfg)
    saved: list[Path] = []
    try:
        for name in cfg.filenames:
            local = dest_dir / Path(name).name
            with open(local, "wb") as fh:
                ftp.retrbinary(f"RETR {name}", fh.write)
            saved.append(local)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return saved
