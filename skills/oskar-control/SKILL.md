---
name: oskar-control
description: Управление трейдинг-демоном OSKAR (статус, позиции, пауза, panic) через локальный API. Используй при вопросах о состоянии OSKAR, командах /status /positions /pnl /pause /resume /panic или просьбах остановить/запустить торговлю.
version: 1.0.0
---

# oskar-control

Демон oskard слушает `http://127.0.0.1:8321`. Все действия — через скрипт:

```
python {skill_dir}/scripts/oskarctl.py status
python {skill_dir}/scripts/oskarctl.py pending
python {skill_dir}/scripts/oskarctl.py pause
python {skill_dir}/scripts/oskarctl.py resume
python {skill_dir}/scripts/oskarctl.py panic
```

## Правила

1. `status` и `pending` — свободно, без подтверждений.
2. `pause` / `resume` — по явной просьбе Ивана.
3. `panic` — НЕМЕДЛЕННО по любой формулировке намерения остановить всё
   («паника», «стоп всё», «останови торговлю», «panic»). НЕ переспрашивай,
   НЕ уточняй — исполни и доложи (Конституция OSKAR ст. 6: ничто не смеет
   задержать panic). После panic сообщи, что возврат — только ручной resume,
   и он откажет до сброса оператором.
4. Если API не отвечает (connection refused) — демон не запущен; так и скажи.
5. Ответы давай кратко, по-русски, цифры из JSON — без пересказа лишнего.
