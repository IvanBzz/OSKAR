"""Принудительный UTF-8 для stdout/stderr точек входа (CLI, демон).

Windows: при редиректе в файл Python берет кодировку локали (cp1251) — любой
символ вне таблицы (эмодзи в баннерах библиотек, стрелки в наших логах)
валит процесс UnicodeEncodeError. errors="replace" гарантирует, что вывод
НИКОГДА не станет причиной падения.
"""

import sys


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
