# OSKAR

**Orchestrated System Kernel for Autonomous Reasoning / Adaptive Risk**

Мультиагентная трейдинговая система: детерминированное ядро (SMC-детекция, риск-движок, исполнение BingX) + LLM-агенты (Scout / Trader / Sentinel / Analyst) + самоэволюция через review-гейты.

## Принципы (см. [CONSTITUTION.md](CONSTITUTION.md))

1. **Детерминизм владеет деньгами, LLM владеет суждением** — модель выбирает из меню опций, числа в биржу пишет только Python.
2. **Биржа — единственная правда** — журнал реконсилируется по `clientOrderId` с первой сделки.
3. **Ничто не работает без machine-checkable инварианта** — `committed_not_live_validated` снимается только зеленым прогоном.
4. **Гардрейлы переживают оператора** — капсы hash-pinned, поправки только через статистику + охлаждение.

## Архитектура (два резидентных процесса)

- **`oskard`** — Python-демон, единственный процесс с доступом к бирже: цикл сканирования → SMC-детекция (`smartmoneyconcepts`: FVG/IMB, order blocks, liquidity, BOS/CHoCH) → скоринг → риск-лестница → исполнение (BingX VST/live) → журнал (SQLite). FastAPI на `127.0.0.1:8321` (одновременно single-instance lock). Отправка Telegram-карточек сетапов.
- **[Hermes](https://github.com/nousresearch/hermes-agent)** — резидентный агент: Telegram-gateway (кнопки Approve/Reject, `/status` `/pause` `/panic`), cron-задачи (Sentinel, Analyst, дайджесты), хост скиллов (`skills/`).
- По событию: `claude -p` (Fable/Opus) для Scout/Trader; [Claudexor](https://github.com/razzant/claudexor) — движок разработки; [Ouroboros](https://github.com/razzant/ouroboros) — супервизор самоэволюции (Фаза 5).

## Статус

**Фаза 0 — фундамент.** Paper trading (BingX VST). Реальные деньги — только через статистический гейт (PF ≥ 1.3 на ≥ 60 сделках, чистая реконсиляция 2 недели).

## Документация

Живая документация — Obsidian vault в [`vault/`](vault/): архитектура (`atlas/`), решения и баги (`knowledge/`), база знаний из книг (`library/`), прогоны (`runs/`), уроки (`lessons/`), предложения самоэволюции (`proposals/`), changelog (`progress/`).

## Запуск (Фаза 1+)

```
uv sync
uv run pytest
uv run oskar sync balance      # read-only окно в биржу — существует ДО первой сделки
uv run oskard                  # демон
```

Секреты — только в `.env` (см. `.env.example`): `BINGX_API_KEY`, `BINGX_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
