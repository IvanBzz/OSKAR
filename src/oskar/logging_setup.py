"""Логирование демона: ротация файла + вычистка секретов на каждом выходе.

Ревью 2026-10-13 (безопасность/данные): токен бота живет в URL Telegram, а ccxt
кладет подписанный URL (signature, ключи) в текст исключений — и то и другое
доходило до data/oskard.log (2026-10-12 токен уже попадал, вычищали руками).
Единственной защитой был httpx на WARNING внутри daemon.main(); лог рос без
ротации. Теперь:

- RotatingFileHandler: 10 МБ x 5 архивов, UTF-8;
- консоль только WARNING+ (stdout/stderr демона — отдельный файл, см. отчет запуска);
- RedactingFilter на ОБОИХ хендлерах: фильтр хендлера видит и записи, пришедшие
  propagate'ом от чужих логгеров (фильтр логгера — только свои);
- httpx/httpcore остаются на WARNING: URL каждого запроса в INFO — шум, даже вычищенный.
"""

import logging
import re
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_OWNED = "_oskar_handler"  # метка наших хендлеров: повторный setup не плодит дубли

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # URL Telegram: https://api.telegram.org/bot<id>:<secret>/getUpdates
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    # тот же токен без префикса bot (переменная окружения, текст ошибки)
    (re.compile(r"(?<![\w<])\d{6,}:[A-Za-z0-9_-]{30,}"), "<redacted>"),
    # подпись ccxt в query/body приватных запросов BingX
    (re.compile(r"(?i)(signature=)[0-9a-f]+"), r"\1<redacted>"),
    # значения ключей: X-BX-APIKEY: v / apiKey=v / 'secret': 'v' / X-Oskar-Token: v
    (
        re.compile(r"""(?i)((?:x-bx-apikey|x-oskar-token|api[-_]?key|secret)["']?\s*[=:]\s*["']?)[^"'\s&,;}]+"""),
        r"\1<redacted>",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Секреты вычищаются из уже собранного сообщения (msg % args), не из шаблона:
    токен обычно приходит в args (`logger.warning("poll failed: %s", e)`)."""

    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - кривые args: handleError хендлера покажет как есть
            return True
        record.msg = redact(message)
        record.args = None
        # трейсбек форматтер допишет из exc_text: рендерим заранее, чтобы вычистить и его
        if record.exc_info and not record.exc_text:
            record.exc_text = self._exc_formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class _RotatingFileHandler(RotatingFileHandler):
    """Windows: os.rename файла, который держит другой процесс (tail в соседнем окне),
    падает PermissionError — штатный хендлер тогда пытается ротировать на КАЖДОЙ
    записи и сыплет трейсбеками в stderr. Фоллбек: копия в архив + усечение."""

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
        except PermissionError:
            shutil.copyfile(source, dest)
            with open(source, "r+b") as fh:
                fh.truncate(0)


def setup_logging(log_path: Path, level: int = logging.INFO) -> list[logging.Handler]:
    """Корневой логгер: файл с ротацией (level) + консоль (WARNING), оба с вычисткой.
    Повторный вызов заменяет ранее поставленные хендлеры, чужие не трогает."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _OWNED, False)]:
        root.removeHandler(old)
        old.close()

    formatter = logging.Formatter(LOG_FORMAT)
    redactor = RedactingFilter()
    file_handler = _RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(level)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    handlers: list[logging.Handler] = [file_handler, console]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        setattr(handler, _OWNED, True)
        root.addHandler(handler)
    root.setLevel(level)
    # httpx пишет в INFO URL каждого запроса, а URL Telegram содержит токен бота
    # (утечка 2026-10-12): вычистка — вторая линия, первая — не писать вовсе
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handlers
