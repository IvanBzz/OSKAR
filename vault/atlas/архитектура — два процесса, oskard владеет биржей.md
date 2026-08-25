---
tags: [atlas, architecture]
date: 2026-08-25
status: design
---
# Архитектура — два процесса, oskard владеет биржей

## Резидентные процессы (24/7)

**`oskard`** — Python-демон, ЕДИНСТВЕННЫЙ процесс с доступом к ccxt/BingX:
- цикл 30с: данные → SMC-детекция (`smartmoneyconcepts`: FVG/IMB, OB, liquidity, BOS/CHoCH) → скоринг → журнал
- детерминированная риск-лестница (in-process, LLM не имеет канала обхода)
- FastAPI `127.0.0.1:8321` — управляющий API и одновременно **single-instance lock** (v1 сгорел на двух экземплярах: дубль ADA SHORT)
- Telegram: только исходящие карточки (`sendPhoto` + inline-кнопки)
- по событию (score ≥ 70): subprocess `claude -p` → Scout/Trader

**Hermes** — резидентный агент:
- владеет Telegram `getUpdates`: кнопки Approve/Reject → `POST /decisions/{id}/approve|reject`
- скилл oskar-control: /status /positions /pause /panic /pnl
- cron: Sentinel (60м), Analyst (ночь), reconcile-чек, дайджест

## По требованию (не резидентные)

- `claude -p` (Fable/Opus) — Scout/Trader/Analyst
- Claudexor — все изменения кода OSKAR (best-of-N + ревьюеры)
- Ouroboros — супервизор самоэволюции (Фаза 5)

## Fail-closed

- Умер Hermes → карточки уходят, апрувы не приходят, TTL все отклоняет. Ничего не исполняется.
- Умер oskard → ничего не торгуется вообще.
- Каждый ордер: `clientOrderId = OSKAR-{run_id[:8]}-{decision_id}` — точная реконсиляция.
- Panic превыше всего (ст. 6).

## Почему не один процесс

Hermes — чужой код с широкими возможностями; доступ к бирже у него отнят архитектурно, не полиси. Разделение каналов Telegram (oskard шлет, Hermes принимает) безопасно: конфликтует только getUpdates, он один — у Hermes.

## Связи

- [[../knowledge/decisions/BingX VST для paper — реальные цены, известные грабли]]
- [[../../CONSTITUTION|CONSTITUTION.md]]
