---
name: oskar-approvals
description: Обработка решений по торговым карточкам OSKAR. Используй, когда приходит callback кнопки с данными вида "oskar:approve:ID" / "oskar:reject:ID" / "oskar:snooze:ID", или Иван текстом одобряет/отклоняет сетап («approve SOLUSDT...», «отклони последний сетап»).
version: 1.0.0
---

# oskar-approvals

Карточки сетапов шлет демон oskard; решение Ивана доставляется демону так:

```
python {skill_dir}/scripts/approve.py approve <decision_id>
python {skill_dir}/scripts/approve.py reject <decision_id>
```

## Правила

1. Callback `oskar:approve:<id>` -> `approve.py approve <id>`; `oskar:reject:<id>` -> reject. Никаких подтверждений — нажатие кнопки И ЕСТЬ подтверждение.
2. `oskar:snooze:<id>` — ничего не вызывай (карточка живет до TTL), ответь «отложено».
3. Ответ скрипта сообщи кратко: «Исполняю SOLUSDT SHORT» / «Отклонено».
4. HTTP 409 = TTL истек или уже решено — так и скажи, ничего не повторяй (fail-closed: истекшее не воскрешается).
5. HTTP 423 = система на паузе или в safe mode — approve невозможен, скажи причину.
6. Текстовое одобрение без id: возьми `pending` через скилл oskar-control, найди сетап по символу; если совпадений больше одного — уточни у Ивана.
7. НИКОГДА не вызывай approve по собственной инициативе — только явное действие Ивана (кнопка или текст).
