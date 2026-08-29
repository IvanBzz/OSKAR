---
tags: [reference, tools]
date: 2026-08-26
---
# Инструменты — oskar CLI и obsidian CLI

## oskar CLI (из корня репо, `uv run oskar ...`)

| Команда | Что делает |
|---|---|
| `oskar caps-verify` | сверка caps.yaml с пином конституции |
| `oskar sync balance` | баланс биржи (VST/USDT) — read-only |
| `oskar sync positions` | открытые позиции на бирже |
| `oskar sync diff` | расхождения журнал vs биржа (exit 1 при грязи — можно в cron) |
| `oskar knowledge index` | пересборка FTS5-индекса библиотеки |
| `oskar knowledge search <слова>` | поиск по заметкам знаний |
| `oskard` | демон (Фаза 1: API; цикл подключается в Фазе 2) |

## obsidian CLI (официальный, встроен с 1.12.4; включен 2026-08-26)

Управляет запущенным Obsidian. Наш vault называется `vault` (имя папки).

| Команда | Что делает |
|---|---|
| `obsidian vault="vault" orphans` | заметки-сироты без связей |
| `obsidian vault="vault" unresolved` | битые wiki-ссылки |
| `obsidian vault="vault" backlinks file="имя"` | кто ссылается на заметку |
| `obsidian vault="vault" search query="текст"` | поиск |
| `obsidian vault="vault" tags counts` | все теги со счетчиками |
| `obsidian vault="vault" property:set file="X" name=status value=Y` | правка frontmatter |

Гигиена после больших правок vault: `orphans` + `unresolved` должны быть пустыми (проверено: 0/0 на 2026-08-26). Нюанс CLI: exit code всегда 0 — парсить вывод, не `$?`.

## Telegram

Бот: `@OSKAR_agnt_bot`. Исходящие карточки шлет oskard (`core/telegram_out.py`), входящие кнопки ловит Hermes (Фаза 2). chat_id добывается: написать боту -> `getUpdates`.

## Связи

- [[index]]
- [[задачи — Сделано, Не проверено, План]]
