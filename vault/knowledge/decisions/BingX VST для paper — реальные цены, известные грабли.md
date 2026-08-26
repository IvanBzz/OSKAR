---
tags: [decision, exchange, bingx]
date: 2026-08-25
status: accepted
---
# BingX VST для paper — реальные цены, известные грабли

## Решение

Paper-фаза на BingX VST (Virtual Simulated Trading, `open-api-vst.bingx.com` через ccxt `sandboxMode: true`), не на Bybit testnet и не на своем пейпер-симуляторе (свой — только как fallback `execution/paper.py`).

## Аргументы

- VST дает **реальные рыночные цены** (Bybit testnet в v1 показывал BTC по $900K)
- Иван выбрал BingX как основную биржу (крипта + товарка + форекс, один API)
- Все грабли уже задокументированы v1: hedge mode → `positionSide` обязателен; `reduceOnly` запрещен; SL/TP только отдельными `STOP_MARKET`/`TAKE_PROFIT_MARKET` с `closePosition`; `set_leverage` требует `params={'side': ...}`; `adjustForTimeDifference` от clock drift
- Известный ОТКРЫТЫЙ баг v1: qty в журнале vs на бирже расходился в 6.98x–833x по-разному на символ. Гипотеза: contractSize/precision. Root-cause — блокер реала (тест round-trip на 20 символах)

## Будущее

`execution/base.py` — ABC-шов: DEXAdapter и MT5Adapter добавляются без переписывания ядра.

## Связи

- [[архитектура — два процесса, oskard владеет биржей]]
