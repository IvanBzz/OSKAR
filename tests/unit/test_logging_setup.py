"""Лог демона: ротация + вычистка токенов и подписанных URL (ревью 2026-10-13).

Утечка 2026-10-12: httpx в INFO положил URL getUpdates с токеном бота в
data/oskard.log. ccxt кладет подписанный URL в текст исключений. Фильтр стоит на
хендлерах и чистит уже собранное сообщение и трейсбек.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from oskar.logging_setup import _RotatingFileHandler, redact, setup_logging

BOT_SECRET = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
TOKEN_URL = f"https://api.telegram.org/bot1234567890:{BOT_SECRET}/getUpdates"
SIGNED_URL = ("https://open-api-vst.bingx.com/openApi/swap/v2/trade/order?symbol=SOL-USDT"
              "&timestamp=1791800000000&signature=9f86d081884c7d659a2feaa0c55ad015")


@pytest.fixture
def clean_root():
    """setup_logging трогает корневой логгер: после теста — снять наши хендлеры и уровни."""
    root = logging.getLogger()
    levels = {name: logging.getLogger(name).level for name in ("", "httpx", "httpcore")}
    yield root
    for handler in [h for h in root.handlers if getattr(h, "_oskar_handler", False)]:
        root.removeHandler(handler)
        handler.close()
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def flushed_text(handlers, path: Path) -> str:
    for handler in handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_setup_logging_rotates_and_redacts(tmp_path, clean_root):
    log = tmp_path / "oskard.log"
    handlers = setup_logging(log)
    file_handler, console = handlers
    assert isinstance(file_handler, RotatingFileHandler)
    assert (file_handler.maxBytes, file_handler.backupCount) == (10 * 1024 * 1024, 5)
    assert file_handler.encoding == "utf-8" and file_handler.level == logging.INFO
    assert console.level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING == logging.getLogger("httpcore").level

    logging.getLogger("oskar.buttons").warning("poll failed: %s", TOKEN_URL)
    logging.getLogger("ccxt.base").info("bingx GET %s headers %s", SIGNED_URL, {"X-BX-APIKEY": "KEYabc123"})
    text = flushed_text(handlers, log)
    assert BOT_SECRET not in text and "bot<redacted>/getUpdates" in text
    assert "9f86d081884c7d65" not in text and "signature=<redacted>" in text
    assert "KEYabc123" not in text and "SOL-USDT" in text  # вычищается секрет, не контекст


def test_redaction_covers_tracebacks(tmp_path, clean_root):
    log = tmp_path / "oskard.log"
    handlers = setup_logging(log)
    try:
        raise RuntimeError(f"ConnectError for {TOKEN_URL}")
    except RuntimeError:
        logging.getLogger("oskar.test").exception("poller crashed")
    text = flushed_text(handlers, log)
    assert "Traceback" in text and "poller crashed" in text and BOT_SECRET not in text


def test_setup_logging_twice_keeps_one_pair_of_handlers(tmp_path, clean_root):
    setup_logging(tmp_path / "a.log")
    setup_logging(tmp_path / "b.log")
    owned = [h for h in clean_root.handlers if getattr(h, "_oskar_handler", False)]
    assert len(owned) == 2 and Path(owned[0].baseFilename).name == "b.log"


def test_redact_keeps_ordinary_text():
    plain = "decision SOLUSDTUSDT-1791800000000-long at 2026-10-15 12:34:56,789; BINGX_API_SECRET not set"
    assert redact(plain) == plain
    assert redact("query apiKey=abc123&timestamp=1") == "query apiKey=<redacted>&timestamp=1"
    assert redact("{'apiKey': 'k1', 'secret': 's2'}") == "{'apiKey': '<redacted>', 'secret': '<redacted>'}"


def test_rotation_survives_log_held_by_another_process(tmp_path, monkeypatch):
    """Windows: os.rename занятого файла -> PermissionError; ротация — копией + усечением,
    без трейсбеков на каждой записи."""
    log = tmp_path / "oskard.log"
    real_rename = os.rename

    def rename(src, dst):
        if Path(src) == log:
            raise PermissionError(32, "The process cannot access the file")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    handler = _RotatingFileHandler(log, maxBytes=200, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    failures = []
    monkeypatch.setattr(handler, "handleError", failures.append)
    try:
        for i in range(10):
            line = f"line {i:02d} " + "x" * 40
            handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, line, None, None))
    finally:
        handler.close()
    assert failures == []
    assert (tmp_path / "oskard.log.1").exists() and log.stat().st_size < 200
    assert "line 09" in log.read_text(encoding="utf-8")
