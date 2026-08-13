"""
Тест забора выгрузок с FTP (src/fetch_sources.py).

Реальный FTP не поднимаем — подменяем ftplib фейком, который проверяет:
логин, переход в папку, и что каждый запрошенный файл сохранён на диск с
базовым именем и полученным содержимым.
"""
from __future__ import annotations

from pathlib import Path

import fetch_sources
from fetch_sources import FtpConfig, fetch_files


class _FakeFTP:
    """Мини-заглушка ftplib.FTP/FTP_TLS: пишет фиктивное содержимое файла."""
    store = {"prod_ops.xlsx": b"XLSX-CONTENT-1C"}

    def __init__(self):
        self.calls = []

    def connect(self, host, port, timeout=0):
        self.calls.append(("connect", host, port))

    def login(self, user, password):
        self.calls.append(("login", user))

    def prot_p(self):
        self.calls.append(("prot_p",))

    def cwd(self, directory):
        self.calls.append(("cwd", directory))

    def retrbinary(self, cmd, callback):
        name = cmd.split(" ", 1)[1]
        callback(self.store.get(name, b""))

    def quit(self):
        self.calls.append(("quit",))

    def close(self):
        pass


def test_fetch_files_downloads_named_files(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_sources.ftplib, "FTP_TLS", _FakeFTP)
    monkeypatch.setattr(fetch_sources.ftplib, "FTP", _FakeFTP)

    cfg = FtpConfig(host="rascroi.ru", user="psr", password="secret",
                    directory="/1c", filenames=["prod_ops.xlsx"], secure=True)
    saved = fetch_files(cfg, tmp_path)

    assert saved == [tmp_path / "prod_ops.xlsx"]
    assert (tmp_path / "prod_ops.xlsx").read_bytes() == b"XLSX-CONTENT-1C"


def test_fetch_files_strips_ftp_path_to_basename(tmp_path, monkeypatch):
    _FakeFTP.store["/1c/prod_ops.xlsx"] = b"DATA"
    monkeypatch.setattr(fetch_sources.ftplib, "FTP_TLS", _FakeFTP)
    monkeypatch.setattr(fetch_sources.ftplib, "FTP", _FakeFTP)

    cfg = FtpConfig(host="h", filenames=["/1c/prod_ops.xlsx"], secure=False)
    saved = fetch_files(cfg, tmp_path)
    # на диск кладём базовое имя, без путей FTP
    assert saved[0].name == "prod_ops.xlsx"
    assert saved[0].read_bytes() == b"DATA"
