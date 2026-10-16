---
name: oskar-control
description: Управление трейдинг-демоном OSKAR (статус, позиции, P&L, режим исполнения, пауза, panic) через локальный API. Используй при вопросах о состоянии OSKAR, его позициях и результатах, командах /status /positions /pnl /mode /pause /resume /panic или просьбах остановить/запустить торговлю, переключить автомат.
version: 1.2.0
---

# oskar-control

Демон oskard слушает `http://127.0.0.1:8321`. Все действия — через скрипт:

```
python {skill_dir}/scripts/oskarctl.py status          # состояние: режимы, флаги, equity, монитор
python {skill_dir}/scripts/oskarctl.py pending         # карточки, ждущие решения
python {skill_dir}/scripts/oskarctl.py positions       # открытые позиции и ждущие лимитки
python {skill_dir}/scripts/oskarctl.py pnl             # P&L закрытых сделок за 24 ч
python {skill_dir}/scripts/oskarctl.py pnl 168         # ... за 168 ч; pnl 0 — за всю историю
python {skill_dir}/scripts/oskarctl.py mode            # режим исполнения сейчас
python {skill_dir}/scripts/oskarctl.py mode auto       # автомат (только paper/VST)
python {skill_dir}/scripts/oskarctl.py mode confirm    # с подтверждением кнопкой
python {skill_dir}/scripts/oskarctl.py pause
python {skill_dir}/scripts/oskarctl.py resume
python {skill_dir}/scripts/oskarctl.py panic
python {skill_dir}/scripts/oskarctl.py panic-reset
```

Что в ответах:
- `positions` — по журналу демона (монитор сверяет его с биржей раз в минуту): symbol, side,
  status (`open` — позиция, `pending` — лимитка ждет цену), entry, sl, tp, qty, leverage.
  Нереализованного P&L здесь нет — его знает только биржа.
- `pnl` — закрытые сделки окна: `pnl_reconciled` (сверено по филлам биржи — правда) и
  `pnl_unreconciled` (оценка монитора, ждет сверки) — всегда называй их раздельно, не
  складывай молча; `n_no_pnl` — закрытые без P&L (закрыты извне, ждут сверки); wins/losses.
- `mode` — `auto` (сделки ставятся сами, карточка приходит уведомлением) или `confirm`
  (каждую подтверждает Иван); `auto_allowed: false` — автомат запрещен средой (не VST).

## Правила

1. `status`, `pending`, `positions`, `pnl`, `mode` без аргумента — свободно, без подтверждений.
2. `pause` / `resume` / `mode auto` / `mode confirm` — только по явной просьбе Ивана.
   Отказ `mode auto` (HTTP 409) — перескажи причину, не повторяй.
3. `panic` — НЕМЕДЛЕННО по любой формулировке намерения остановить всё
   («паника», «стоп всё», «останови торговлю», «panic»). НЕ переспрашивай,
   НЕ уточняй — исполни и доложи (Конституция OSKAR ст. 6: ничто не смеет
   задержать panic). После panic сообщи, что возврат — `panic-reset`, затем
   `resume`, и оба — только явной командой Ивана.
4. `panic-reset` — ТОЛЬКО когда Иван прямо просит снять panic (ст. 6 п. 3).
   Никогда по своей инициативе, по выводам из статуса или «чтобы продолжить
   торговлю». Пауза после него остается — `resume` отдельной просьбой. Он же снимает
   SAFE MODE (в т.ч. «ордера OSKAR чужого запуска — вторая машина?»): сначала убедись
   словами Ивана, что второй демон остановлен.
5. Если API не отвечает (connection refused) — демон не запущен; так и скажи.
   Panic в этом случае НЕ исполнен: ордера на бирже живы — предупреди Ивана.
6. Ответы давай кратко, по-русски, цифры из JSON — без пересказа лишнего.

Без LLM в цепочке: в чате сигнального бота OSKAR `/panic`, `/status`, `/mode`, `/digest`
(сводка за 24 ч) отвечает сам демон; `oskar panic` в терминале — та же функция, что у API.
