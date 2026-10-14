---
name: oskar-control
description: Управление трейдинг-демоном OSKAR (статус, позиции, пауза, panic) через локальный API. Используй при вопросах о состоянии OSKAR, командах /status /positions /pnl /pause /resume /panic или просьбах остановить/запустить торговлю.
version: 1.1.0
---

# oskar-control

Демон oskard слушает `http://127.0.0.1:8321`. Все действия — через скрипт:

```
python {skill_dir}/scripts/oskarctl.py status
python {skill_dir}/scripts/oskarctl.py pending
python {skill_dir}/scripts/oskarctl.py pause
python {skill_dir}/scripts/oskarctl.py resume
python {skill_dir}/scripts/oskarctl.py panic
python {skill_dir}/scripts/oskarctl.py panic-reset
```

## Правила

1. `status` и `pending` — свободно, без подтверждений.
2. `pause` / `resume` — по явной просьбе Ивана.
3. `panic` — НЕМЕДЛЕННО по любой формулировке намерения остановить всё
   («паника», «стоп всё», «останови торговлю», «panic»). НЕ переспрашивай,
   НЕ уточняй — исполни и доложи (Конституция OSKAR ст. 6: ничто не смеет
   задержать panic). После panic сообщи, что возврат — `panic-reset`, затем
   `resume`, и оба — только явной командой Ивана.
4. `panic-reset` — ТОЛЬКО когда Иван прямо просит снять panic (ст. 6 п. 3).
   Никогда по своей инициативе, по выводам из статуса или «чтобы продолжить
   торговлю». Пауза после него остается — `resume` отдельной просьбой.
5. Если API не отвечает (connection refused) — демон не запущен; так и скажи.
   Panic в этом случае НЕ исполнен: ордера на бирже живы — предупреди Ивана.
6. Ответы давай кратко, по-русски, цифры из JSON — без пересказа лишнего.

Panic без LLM в цепочке: `/panic` в чате сигнального бота OSKAR (отвечает сам
демон) и `oskar panic` в терминале — та же функция, что у API.
