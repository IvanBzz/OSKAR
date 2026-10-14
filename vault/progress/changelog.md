---
tags: [progress, changelog]
date: 2026-08-25
---
# Changelog

> Обновляется при каждом изменении кодовой базы или vault. Новые записи — сверху. Формат: `## YYYY-MM-DD — заголовок (commit)`.

## 2026-10-15 — блок A: защита прикрепляется к входу (живой 109420 на лимитке BERA)

**Живой случай 2026-10-14 12:03** (первая реальная попытка исполнения на VST): Иван нажал «Лимит 0.2292» по BERA long. Лимитка встала, а отдельный STOP_MARKET с `closePosition` BingX отверг: `109420 position not exist` — до исполнения лимитки позиции нет. Система отработала безопасно (сняла лимитку, записала отказ), но лимитная кнопка была мертва по построению. Ровно этот риск Codex отметил в ревью как непроверенный на бирже. За 4 с до этого первое нажатие умерло на таймауте VST в `set_leverage`.

Решение — защита как часть входа:
- **Прикрепленные SL/TP**: `place_entry(stop_loss=, take_profit=)` -> параметры ордера BingX `stopLoss`/`takeProfit` (биржа сама создает SL/TP в момент исполнения — окна «позиция без стопа» нет ни у лимита, ни у маркета). Сборка запроса проверена офлайн на настоящем ccxt (`create_order_request`, без отправки): JSON с округленным `stopPrice`, `quantity`, `workingType: MARK_PRICE`, `positionSide: LONG`.
- **Фоллбек**, если биржа отвергла вход с прикреплением: голый вход; маркет ждет появления позиции (до 6 с) и ставит closePosition-ордера с повтором на 109420; лимитку защищает монитор при исполнении. Не встал TP — не повод закрываться (стоп на месте), не встал SL на маркете — позиция закрывается.
- **Таймаут на входе** (ордер мог пройти): повторять вслепую нельзя — ищем ордер по clientOrderId; нашли — ведем, не нашли — пишем намерение (pending без id, `entry_sent`), монитор разрешит за 3 мин. Карточка закрывается («НЕ нажимай повторно»).
- **Плечо** при сетевом сбое повторяется (идемпотентно) — первое нажатие 12:03:06 прошло бы.
- **Монитор**: быстрый проход pending раз в 5 с (лимитка исполнилась -> open + защита в тот же проход); id прикрепленных SL/TP биржа заранее не сообщает — монитор «усыновляет» их по открытым ордерам (вид — по сырому `info.type`: унифицированный тип ccxt у STOP_MARKET = `market`!), не нашел — ставит сам; 109420 сразу после исполнения — повтор до 3 раз, затем SAFE MODE (для TP — только алерт). Исчезнувшая позиция классифицируется по истории ордеров биржи (sl_hit/tp_hit с ценой исполнения), а не «manual».
- **Опрос входа по биржевому id** (`trades.entry_order_id`), поиск по clientOrderId — запасной: без учета регистра, по открытым + истории (исполненные и СНЯТЫЕ — `fetch_closed_orders` снятые не отдает), «нет ордера» (`OrderNotFoundError`) отделен от сетевых сбоев — только он может пометить вход снятым.
- **Журнал v3 — миграцией, не стиранием**: `ALTER TABLE trades ADD COLUMN entry_order_id/entry_type`; датасет решений и counterfactuals сохраняется.

### Тесты
- test_protection_lifecycle (19): живой сценарий BERA (лимит с прикреплением, ни одного отдельного closePosition), маркет с усыновлением, отказ прикрепления -> голый маркет с ожиданием позиции и повтором 109420, голый лимит -> монитор, SL не встал -> закрытие, таймаут входа найден по cid / не найден -> намерение, таймаут плеча -> повтор; монитор: усыновление после филла, постановка при отсутствии, 109420-повторы до SAFE MODE, классификация исчезнувшей позиции по истории, быстрый проход не трогает open, неизвестный маркет-вход истекает за 3 мин, опрос по entry_order_id; адаптеры: параметры прикрепления, разбор защиты по сырому типу и positionSide, поиск по cid без учета регистра, OrderNotFound; миграция v2->v3 без потери данных. Обновлены 2 старых теста под новую семантику. Итого 290.

### Затронутые файлы
CREATE: tests/unit/test_protection_lifecycle.py; MODIFY: src/oskar/core/{executor,position_monitor}.py, src/oskar/execution/{base,bingx,paper}.py, src/oskar/journal/db.py, tests/unit/{test_position_monitor,test_hardening,test_executor}.py

## 2026-10-13 — ревью агентами + Codex: 25 находок закрыты одним пакетом

По просьбе Ивана код прошел четыре независимых ревью: три Claude-агента (безопасность; торговая логика; данные/инфраструктура), Codex gpt-6-astra через `codex exec` (read-only) и Claudexor deep-scan. Находки сошлись на одних и тех же дырах — ниже закрытые, с источником.

**Критические**
- **Частичное исполнение = двойная позиция** (все четыре). `executor` писал trades только после входа+SL+TP; отказ SL после исполненного входа оставлял голую позицию без записи, а поллер откатывал approve — повторное нажатие открывало вторую. Теперь: вход ушел -> запись в trades ВСЕГДА; защита не встала -> позиция закрывается/лимитка снимается (`_flatten`), исход несет `entry_sent=True`, поллер по нему карточку закрывает с предупреждением, а не оживляет.
- **Лимиты портфеля были мертвы** (все четыре): демон не передавал `open_positions_fn`, лестница видела пустой портфель — `max_open_positions`, `max_total_risk_pct`, `no_duplicate_position` не срабатывали никогда. Теперь `open_positions_from_journal` (open + pending) + повторная проверка лимита/дубля в момент нажатия.
- **id решений всегда `-299-`** (агенты данных/безопасности, Codex): `bar_index` скользящего окна фиксированной длины = 299 -> все кандидаты символа/направления получали один id, `INSERT OR IGNORE` молча терял решения (82/82 в журнале с «-299-»), кулдаун «299-299<2» не истекал никогда. Ключ — таймстамп бара, кулдаун по времени.
- **Кнопка меняла вход, сохраняя sizing** (Codex): план хранит одну qty для маркета и лимита; маркет дальше от стопа раздувал риск ($10 -> $30 в репродукции). `risk_sized_qty`: qty режется вниз до риска плана от фактической цены входа.
- **Частичный филл лимитки на TTL снимал защиту с живой позиции** (торговый агент, Codex): теперь после отмены остатка — повторный опрос; `filled_qty>0` -> open с реальным qty, SL/TP не трогаем.

**Высокие**
- **panic не трогал биржу и терялся после рестарта** (все): `/panic` -> флаги -> `meta.panic_engaged` -> `monitor.panic_flatten()` (снять входы pending, закрыть позиции open с `close_reason=panic`); после рестарта panic держится до `POST /panic/reset` (ст. 6 п. 3), `oskarctl panic-reset`.
- **API без аутентификации** (безопасность, Codex): страница в браузере могла POST-ом снять паузу/нажать panic/approve. `X-Oskar-Token` (пишется в `data/oskard.token`, читают скиллы), `TrustedHostMiddleware` loopback, `/docs` выключен.
- `_find_by_client_id`: ccxt ждет `clientOrderID`, `fetch_closed_orders` не отдает canceled -> отмененный вход был ненаходим, pending висел вечно с живыми SL/TP. Поиск по правильному параметру + open/closed/canceled; «не найден за TTL» = снят.
- Один пустой `positions()` снимал защиту с живой позиции -> подтверждение двумя тиками + перепроверка филлов SL/TP; расхождения reconcile — тоже два тика, затем SAFE MODE.
- Исчезнувший SL/TP при живой позиции переставляется (`-sl-rN`); не удалось -> SAFE MODE (голую позицию молча не держим).
- План на чужом equity ($1000 дефолт до первого ответа биржи) исполнялся на счете $121: `equity_guarded` сверяет риск плана с живым equity и капсом.
- Сетевой чих на границе пересборки вотчлиста = вечный safe_mode: сетевые сбои — отдельная, в 4 раза длиннее серия; тик, где упали ВСЕ символы, считается сбоем; флип safe_mode уходит в Telegram.
- Свежий ccxt без `load_markets` падал на первом `amount_to_precision` (Codex воспроизвел на установленном ccxt) — первое нажатие кнопки умерло бы. Прелоад маркетов в адаптере и при старте.
- Среда исполнения/equity/монитора разошлась (equity с зашитым `sandbox=True`): один источник `settings.exchange.sandbox`; `mode=live` закрыт гейтом Фазы 5; `mode=paper` + `sandbox=false` — отказ старта.
- Несовпадение схемы журнала стирало базу вместе с открытыми сделками: с open/pending — отказ старта.
- Гонка single-instance (пробный бинд закрывался до старта потоков): файловый замок на все время жизни процесса.

**Средние**
- Общее sqlite-соединение на 4 потока: `journal.db.transaction()` (RLock + commit/rollback) вокруг многошаговых записей; approve/reject — условный `UPDATE ... WHERE resolution IS NULL` с проверкой rowcount (гонка «кнопка+API» давала два approve).
- Duration-stop: `close_position` вернул None (SL сработал между опросами) -> не закрываем с чужой причиной; закрытие подтверждается по позициям, иначе SAFE MODE.
- Маркет-вход: цена/комиссия с биржи (`order_status`), не плановая; не исполнился -> pending.
- Allowlist кнопок и по `from.id`, не только по чату; HTML-экранирование текста модели/биржи в карточках и финализации; ошибки биржи в Telegram — код + 120 символов, без подписанных URL.
- Снуз ограничен: карточка живет не дольше 60 мин от создания; reject/expire зеркалятся в `decisions.outcome` (когорты не теряют отклоненные руками).
- `claude -p` без инструментов (`--disallowedTools Bash,Read,Write,...`): унаследованный профиль прав — поверхность для инъекции через текст биржи/модели.
- Scout-промпт содержал win-rate прошлой системы (ст. 3 запрещает performance-статистику торговым агентам) — убран.
- Counterfactuals: лимитный вход сначала должен быть достигнут (TP засчитывался невошедшему сетапу); горизонт 48 баров -> `expired_flat`; дорешиваются символы с висящими исходами, не вотчлист (31 висел с августа).
- `OperationFailed` с любым кодом (100500 и т.п.) — transient, не приговор символу; плечо на BingX целое (floor); карточка и график — из исполняемого плана (Trader мог выбрать не первые SL/TP); `client_order_id` — хэш вместо обрезки; equity после сбоя — повтор через 5 мин, не час.

Проверка после рестарта поймала регресс проводки: Telegram-переменные читались в `main()` до `load_dotenv` -> поллер кнопок и алерты молча OFF. `.env` грузится первым; признак в логе — строка `button poller started`.

**Не сделано, в план**: fee-aware sizing (риск-кап не учитывает комиссии и проскальзывание); ledger валидации по ст. 5 (`ops/invariants.py`); config-drift тест; per-thread соединения вместо замка; проверка на VST, принимает ли BingX `closePosition` без `quantity` до первого филла (Codex: расхождение ccxt и опубликованного API).

### Тесты
- test_hardening (24): по тесту на каждую закрытую находку. Переписаны ожидания монитора (подтверждение двумя тиками), фикстуры кнопок (`from.id`), qty маркета (риск плана).
- test_review_followups (3), из Claudexor deep-scan: кнопка без исполнителя больше не говорит «Беру по рынку» (approve без ордера — честно); `stopPrice` округляется под тик-сайз сами (params обходили `price_to_precision` ccxt — отказ по тику = вход без защиты); `cancel_order("")` = False, монитор не снимает защиту после «отмены» несуществующего id. Итого 271.

### Затронутые файлы
CREATE: tests/unit/test_hardening.py; MODIFY: src/oskar/core/{executor,approvals,position_monitor,shadow_runner,api,daemon,escalation,cycle,telegram_buttons,telegram_out}.py, src/oskar/execution/{base,bingx,paper}.py, src/oskar/data/client.py, src/oskar/detect/options.py, src/oskar/journal/db.py, src/oskar/agents/{claude_cli.py,prompts/scout.md}, src/oskar/cli.py, skills/*/scripts/*.py, tests (buttons, core, executor, position_monitor, bootstrap_shadow)

## 2026-10-13 — монитор позиций: журнал догоняет биржу

До сих пор сделка после кнопки жила в журнале как `open/pending` навсегда: исполнение лимитки, срабатывание SL/TP, закрытие руками и протухшие карточки никто не отслеживал (в задачах висело с сентября). Теперь `core/position_monitor.py` раз в минуту сверяет журнал с биржей ПО СОСТОЯНИЮ ОРДЕРОВ (ст. 1: биржа — правда; свечи не используются):
- **sweeper**: pending_approvals с истекшим TTL -> `expired`, с карточки снимаются кнопки (эскалатор теперь сохраняет `telegram_message_id`, `TelegramSender.remove_buttons` делает editMessageReplyMarkup);
- **лимитный вход**: исполнился -> `open` с ценой/комиссией биржи; снят биржей или старше `limit_entry_ttl_seconds` (новый параметр профиля: 1ч intraday, 4ч swing) -> вход и SL/TP снимаются, `cancelled`, причина в decisions.outcome_detail;
- **открытая позиция**: исполнен SL -> `closed/sl_hit` + отмена TP (и наоборот), realized_pnl по ценам исполнения минус комиссии биржи; позиции нет и защита не срабатывала -> `manual` (exit/pnl NULL — не выдумываем); **duration-stop**: лосер старше `max_loss_hold_seconds` (3ч) закрывается по рынку -> `duration_timeout`;
- **reconcile**: позиции биржи vs open-сделки журнала (допуск 1% по qty) — расхождение в лог и в Telegram один раз на ключ.
- Контракт `ExchangeAdapter` расширен: `order_status(exchange_id | client_order_id)` и `cancel_order` — для BingX (fetch_order; поиск по clientOrderId с фоллбеком по open/closed orders; OrderNotFound = успех) и для Paper (реестр ордеров; mark() переводит SL/TP в filled/canceled).
- `/status` получил блок `trades` (pending/open/closed/cancelled + последний тик монитора). Уведомления — через сигнального бота: «лимит исполнен», «🛑 стоп … P&L», «⚠️ reconcile».

Поллер кнопок: после отзыва токена (Иван перевыпустил его по моей рекомендации) старый демон крутил getUpdates в tight-loop — 684 строки 401 за минуты. Теперь ok=false -> пауза 30с, лог раз в 60 попыток.

### Тесты
- test_position_monitor (14): sweeper + снятие кнопок, филл лимитки, TTL лимитки со снятием защиты, снятие биржей, SL/TP с P&L (long/short), закрытие извне, duration-stop только для старых лосеров, reconcile один раз и повтор после возврата, допуск qty, устойчивость к ошибке одной сделки, /status.trades, жизненный цикл ордеров Paper, маппинг статусов BingX + фоллбек по clientOrderId + OrderNotFound. test_buttons +1 (401 backoff). Итого 241.

### Затронутые файлы
CREATE: src/oskar/core/position_monitor.py, tests/unit/test_position_monitor.py; MODIFY: src/oskar/execution/{base,bingx,paper}.py, src/oskar/core/{daemon,api,escalation,telegram_out,telegram_buttons,bootstrap}.py, config/profiles.yaml, tests/unit/test_buttons.py

## 2026-10-12 — сигнальный бот подключен; токен в логе; кнопка-самопроверка

Иван переиздал ключ BingX VST и создал второго бота (@oskar_signals_bot). Ключ проверен живым балансом (121.71 VST, `equity_source=exchange` — блокировка исполнения снята). Токен бота проверен getMe/getChat (отличается от основного, чат доступен), демон перезапущен — `button poller started (signals bot)`.

**Утечка токена в лог**: httpx на уровне INFO пишет URL каждого запроса, а URL Telegram содержит токен бота — первый же getUpdates поллера положил токен сигнального бота в `data/oskard.log` (3 строки; файл в .gitignore, в git не попал). Лог вычищен (`/bot<redacted>/`), логгеры httpx/httpcore подняты до WARNING в `daemon.py`. Рекомендация Ивану — перевыпустить токен через BotFather (/revoke), раз он побывал в локальном логе и в выводе инструментов сессии.

**Кнопка-самопроверка**: callback `oskar:ping:<id>` — поллер отвечает всплывашкой «Кнопки работают ✅» и снимает кнопки, журнал и биржу не трогает. CLI `oskar buttons-ping` шлет карточку с одной кнопкой через сигнального бота — проверка цепочки Telegram -> поллер после любого перезапуска/переезда на другую машину без фейковых сделок.

Проба конфликта 409 перед запуском гейтвея (long-poll 40с основным токеном): токен свободен, webhook не установлен; гейтвей подключился с первой попытки. Источник вчерашнего конфликта вне этой машины (один профиль Hermes, не запущен) — ждем ответа Ивана, каким токеном пользуется его учебная система.

### Тесты
- test_buttons +1: ping отвечает PING_ANSWER, снимает кнопки, pending_approvals не тронут; send_ping_card шлет корректный callback_data. Итого 226.

### Затронутые файлы
MODIFY: src/oskar/core/{daemon,telegram_buttons}.py, src/oskar/cli.py, tests/unit/test_buttons.py

## 2026-10-11 — корень бана BingX 109429: токенизированные активы в юниверсе

Расследование: в юниверсе BingX 1051 «активный» USDT-перп, из них 614 — токенизированные акции/форекс/сырье/индексы (NCSK*/NCFX*/NCCO*/NCSI*, RWA-перпы). В выходные 270 из них стоят на паузе (status 25); ccxt `active` смотрит на apiStateOpen и паузы не видит. Каждый скан юниверса бил по ним klines -> `109415 "<symbol> is pause currently"` -> после >10 за 900с биржа отвечает `109429 over 10 error code:109415 ... can retry after time: <epoch ms>` — бан kline-эндпоинта на 15 минут. Старый клиент принимал бан за вину символа и хоронил здоровые ON/NIL/BOME в черном списке; `quick_filter` глотал ошибки молча — месяц 109415 не всплывали в логе (нашлись контролируемой пробой с бюджетом 3 ошибки).

Фикс в три слоя (`data/client.py`, `data/scanner.py`, `core/shadow_runner.py`):
- **Юниверс**: status != 1 и RWA-перпы (регулярка по base) отрезаются до скана: 1051 -> 559 крипто-пар (205 паузных + 287 RWA мимо). `include_rwa=True` оставлен как шов.
- **Классификация ошибок** по коду BingX: 109429 -> глобальный cooldown до retry-after биржи (сеть не трогаем, символ не виноват); 100410 «请稍后重试» -> ретрай без приговора; остальное -> черный список с TTL 4ч (вместо «до конца процесса»).
- **Бюджет ошибок**: 6 ошибок класса «символ» за 15 мин -> добровольная пауза до выхода старейшей из окна. Биржевых 10 не достигаем никогда.
- **Сканер** возвращает `ScanSummary` (отскорено/неликвид/ошибки по классам/первые тексты) — одна строка в лог вместо молчания; на cooldown перебор останавливается, топ помечен частичным.
- **Раннер**: во время паузы цикл и пересборка вотчлиста ждут (одна строка в лог, не шторм по 20 символам); прерванный скан старый вотчлист не трогает (частичный топ перекошен к началу алфавита), повтор — сразу после паузы, не через 30 мин. `/status` получил блок `data`: universe/отрезанное, черный список, errors_15m, cooldown.

Не сделано осознанно: персистентность черного списка в журнале — после фильтра юниверса туда попадают единичные делистинги, TTL 4ч достаточно.

Hermes gateway снова упал на `telegram_polling_conflict` (409 — второй потребитель getUpdates на том же токене; 5 ретраев/200с и выход). На этой машине второго процесса нет (проверено по процессам и планировщику) — подозрение на гейтвей на ноутбуке. Проба 50с long-poll после падения: токен свободен -> перезапуск.

### Тесты
- test_data_errors (9): классификация по кодам, retry-after, бан -> cooldown без черного списка, дефолтный cooldown, TTL черного списка, бюджет ошибок -> пауза и возврат, 100410 ретрай, фильтр юниверса (status/RWA/NC-крипта/без info), include_rwa.
- test_scan_summary (2): сводка (scored/illiquid/errors/samples), стоп на cooldown с частичным топом.
- test_runner_cooldown (4): отложенная пересборка и пропуск цикла, прерванный скан хранит старый вотчлист, холодный старт берет частичный, /status.data. Итого 225.

### Затронутые файлы
MODIFY: src/oskar/data/{client,scanner}.py, src/oskar/core/{shadow_runner,api}.py; CREATE: tests/unit/{test_data_errors,test_scan_summary,test_runner_cooldown}.py

## 2026-10-11 — equity только с биржи: протухший ключ BingX больше не маскируется

При первом же живом запуске после паузы демон показал `equity_usd: 1000.0` — дефолт вместо 121.71. Причина: BingX отвечает `100413 Incorrect apiKey` (VST-ключ протух за месяц; в легаси такое уже было в мае), а раннер молча падал на значение по умолчанию. Это класс бага «журнал врет»: сайзинг на выдуманном equity ($10 риска вместо $1.22). Теперь:
- `ShadowRunner.equity_source`: exchange | persisted | default. Успешный ответ биржи персистится в `meta` (equity_last/equity_last_at) и переживает рестарт как persisted; сбой биржи понижает источник и пишет WARNING, а не молчит.
- `executor.equity_guarded()`: кнопки Маркет/Лимит исполняют ТОЛЬКО при source=exchange; иначе честный отказ «проверь API-ключ BingX», карточка остается живой (ст. 1: биржа — единственная правда).
- /status показывает equity_source и equity_at — Hermes-бот отвечает на «статус» с этим полем.

Сеть: матрица прокси/напрямую по всем хостам (GitHub, BingX public/VST, Telegram, Nous, Anthropic) — всё доступно обоими путями; прокси лишь медленнее для BingX (2.9с против 0.5с на запрос — для скана 800 пар это 15 минут против 3). BingX-хосты добавлены в User NO_PROXY и в env запуска демона. Claude CLI OAuth-токен проверен — жив.

**Блокеры на Иване**: (1) переиздать API-ключ BingX VST и обновить .env; (2) токен сигнального бота для кнопок.

### Тесты
- test_equity_guard (5): default на чистом журнале, персистентность через рестарт, деградация источника при сбое биржи, блок исполнения без live-equity, пропуск с live. Итого 210.

### Затронутые файлы
MODIFY: src/oskar/core/{shadow_runner,api,executor,daemon}.py; CREATE: tests/unit/test_equity_guard.py

## 2026-10-11 — возврат после месяца: демон падал на cp1251, git упирался в прокси

**Падение демона на старте**: `smartmoneyconcepts` печатает баннер с эмодзи при импорте; stdout, перенаправленный в файл под Windows, работает в cp1251 -> `UnicodeEncodeError` -> процесс мертв до первого тика. Класс граблей v1 («ASCII-only консоль»), но ударил с неожиданной стороны — из чужой библиотеки. Фикс в корне: импорт обернут в `redirect_stdout` (баннер глушится), плюс `oskar/_io.py::force_utf8_stdio()` в точках входа демона и CLI — вывод больше НИКОГДА не причина падения (errors=replace). Проверено импортом под принудительным cp1251.

**Git и прокси**: в User-окружении появился платный HTTP-прокси (HTTP_PROXY/HTTPS_PROXY); `curl` через него до GitHub доходит, а `git` — стабильно «Proxy CONNECT aborted», при этом напрямую GitHub работает за секунду (не блокируется). Фикс: per-URL обход `http.https://github.com.proxy=""` в глобальном git-конфиге + GitHub-хосты в User NO_PROXY + per-remote пин в репо. Квирк по пути: PowerShell 5.1 молча выбрасывает пустой аргумент `""` у native-команд — `git config key ""` из PS превращается в чтение ключа; ставить такие значения только из bash.

Первый взгляд на накопленный датасет (14 часов shadow 30-31 августа): 62 решения, 29 разрешенных counterfactuals. Когорта risk_rejected (n=25): WR 36%, сумма -5.4R — лестница резала правильно. Когорта scout_pass (n=3): WR 67%, +7.1R — Scout, возможно, слишком осторожен; n мало, вывод отложен.

### Тесты
- 205 (без изменений; добавлена проверка импорта под cp1251 вручную — автоматизировать в следующем коммите)

### Затронутые файлы
CREATE: src/oskar/_io.py; MODIFY: src/oskar/detect/smc.py, src/oskar/core/daemon.py, src/oskar/cli.py

## 2026-09-14 — исполнение одной кнопкой: Маркет / Лимит по найденному уровню

Пожелание Ивана: «кнопка — выставить лимитный ордер по цене, которую нашел, чтобы система сама все сделала». Реализовано:

- **ExecutionPlan** (контракт v1) — готовый к исполнению план, посчитанный ДО показа карточки: обе цены входа (рыночная + лимитный уровень FVG/OB из меню), SL/TP, qty, леверидж, риск, R:R. Нажатие кнопки НЕ запускает расчетов (ст. 2) — оно выбирает только способ входа. Хранится в pending_approvals.card_json вместо сырого кандидата.
- **Кнопки карточки**: `⚡ Маркет` / `⏳ Лимит <цена>` / `❌ Пропустить` / `💤 15 мин`. Лимитная кнопка появляется, только если система нашла уровень (иначе не показывается).
- **core/executor.py** — одно нажатие делает всё: нормализация qty под точность биржи -> set_leverage -> вход (market/limit) -> отдельными ордерами SL и TP (BingX hedge не принимает inline) -> запись в trades со статусом open/pending. Идемпотентность: повторный клик — no-op (класс бага v1 с дублем позиции). Отказ биржи = сделки в журнале нет И решение откатывается, карточка остается живой для повторной попытки (журнал не врет, ст. 1).
- PaperAdapter: SL/TP до исполнения лимитки больше не падают (условный ордер просто висит).

### Тесты
- test_executor (11): маркет/лимит-пути, дубль-клик, отсутствие лимитного уровня, отказ биржи, клавиатура (лимит callback_data <= 64 байт), сквозной путь кнопка -> ордера, откат при отказе. Итого 205.

### Затронутые файлы
CREATE: src/oskar/core/executor.py, tests/unit/test_executor.py, scripts/bootstrap_machine.ps1, vault/knowledge/decisions/две машины*.md; MODIFY: contracts/{schemas,versions,__init__}.py, core/{escalation,telegram_out,telegram_buttons,daemon}.py, execution/paper.py

## 2026-08-30 — фидбек Ивана по первым карточкам: выжимка + рабочие кнопки

Первые 2 живые карточки (ORCA long conf 63, SOL long conf 62; из 6 Scout-вызовов один сетап честно отсеян). Фидбек: стена текста, куча индикаторов, кнопки мертвы.

1. **Кнопки были мертвы архитектурно**: _handle_callback_query адаптера Hermes зашит на его префиксы (ea:/sc:/cl:/...), чужие callback_data молча отбрасывает. Решение — **выделенный сигнальный бот, которым владеет демон**: `core/telegram_buttons.py` (long-poll callback_query, allowlist-чат, мгновенный answer + снятие кнопок + статус в подписи), `core/approvals.py` — единый резолвер для API и кнопок (+snooze продлевает TTL, +эндпоинт /snooze). Нужен второй бот от BotFather (TELEGRAM_SIGNALS_BOT_TOKEN).
2. **Карточка-выжимка**: 6 строк — направление/цены/риск/TTL + tldr одной фразой; вся техника (зоны, RSI, score, полные дебаты) убрана из карточки, живет в журнале (доступ через Hermes). Тест принуждает: len<400, запрещенные слова (score/RSI/FVG-зоны) отсутствуют.
3. **SetupVerdict v2: поле tldr** (<=160, промпт просит <=120) + жесткие лимиты секций в промпте (<=280 символов — Fable писал по 500-713).

### Тесты
- test_buttons (6): резолвер (snooze/safe-mode), поллер на фейковом Bot API (approve-путь, чужой чат молча игнорируется, истекший TTL, offset). Итого 194.

### Затронутые файлы
CREATE: src/oskar/core/{approvals,telegram_buttons}.py, tests/unit/test_buttons.py; MODIFY: contracts/{schemas,versions}.py, agents/{scout.py,prompts/scout.md}, core/{api,daemon,telegram_out}.py, .env.example, тесты

## 2026-08-30 — гейт эскалации перебирает все комбинации меню (урок 12/17)

Первые 1.8 часа shadow-прогона: 165 тиков, 17 кандидатов — и ноль эскалаций. Разбор следов лестницы показал дизайн-баг гейта: он тестировал ОДНУ дефолтную комбинацию (market-вход + первый SL + первый TP), 12/17 срезались по R:R (ZEC 1.833, INJ 1.896 — на волоске), тогда как лимитка от FVG и тейк 2R из того же меню прошли бы. Trader существует ровно для выбора комбинации — гейт обязан проверять достижимость. Фикс: `best_passing_combo()` — детерминированный перебор <= 27 комбинаций через полную лестницу, эскалация при первой проходной, в counterfactual уходит лучшая по R:R.

Бонус первых данных: из 3 разрешившихся отклоненных counterfactuals 2 кончились бы стопом — лестница пока права.

### Тесты
- +test_best_passing_combo_finds_viable_menu_option (живой сценарий: дефолт файлит, лимитка+дальний стоп проходит). Итого 188.

### Затронутые файлы
MODIFY: src/oskar/core/cycle.py, tests/unit/test_core.py

## 2026-08-30 — claude CLI авторизован; черный список мертвых символов (второй бан)

claude CLI 2.1.251 авторизован через `claude setup-token` Ивана (OAuth-токен в .env, из токена вычищены пробелы-переносы из копирования терминала); пробный вызов зеленый. Демон перезапущен с CLI в PATH и токеном — эскалация активна.

**Второй пойманный бан BingX** (>10 x 109415 за 15 мин): quick_filter каждые 30 минут опрашивал ВЕСЬ universe, включая мертвые/полуделистнутые символы, молча глотая ошибки — а биржа копила счетчик нарушений. Фикс: `MarketDataClient.dead_symbols` — символ с биржевой (не сетевой) ошибкой на klines попадает в черный список до конца процесса, повторные запросы до биржи не доходят. Сетевые сбои символ НЕ хоронят. Плюс дедуп шумных повторов cycle error в shadow-логе (AGT спамил каждые 30с одинаковым пропуском).

### Тесты
- +test_exchange_errors_not_retried_and_blacklisted (расширен), +test_network_errors_not_blacklisted. Итого 187.

### Затронутые файлы
MODIFY: src/oskar/data/client.py, src/oskar/core/shadow_runner.py, tests/unit/test_data_client.py

## 2026-08-30 — эскалация к LLM: промпты Scout/Trader, Escalator, карточки с обоснованием

`agents/prompts/{scout,trader}.md` — роль-карты: Scout обязан построить bull/bear/rebuttal (вердикт take только при закрытом bear_case; напоминание об overconfidence v1 прямо в промпте; STATS_MANIFEST-чистота покрыта тестом); Trader — гейткипер с вето, выбирает опции ПО ID, критерии Герчик-стиля («стоп за структурой, не под лоем толпы»).

`agents/{scout,trader}.py` — бюджет ДО вызова, полный промпт+ответ в analysis_log, выход валидируется контрактами (confidence>75 = ошибка, галлюцинированный id опции = ошибка, ответ про чужой кандидат = ошибка); knowledge_for_candidate — топ-3 заметки библиотеки по типу сетапа через FTS5.

`core/escalation.py` — Escalator: Scout -> Trader -> **полная лестница заново на выбранных ценах** (ст. 2: LLM — селектор, не источник чисел; тест: RR<2 на выбранном TP режется финальной проверкой) -> pending_approvals (TTL fail-closed) -> карточка с графиком и секцией «Тезис/Против/Ответ/conf». Интеграция в ShadowCycle: outcome/cohort-маппинг (scout_pass/trader_decline/pending), scout_json/trade_json в decisions. Журнал v2 (новые исходы), демон при несовпадении схемы архивирует старую БД (допустимо только в shadow-фазе).

Установлен claude CLI 2.1.251 (нативный установщик); auth desktop-приложения с CLI не общий — нужен одноразовый `claude setup-token` от Ивана. До авторизации демон работает в чистом shadow (эскалация выключается сама при отсутствии CLI/логина).

### Тесты
- test_scout_trader (8), test_escalation (6). Итого 186.

### Затронутые файлы
CREATE: src/oskar/agents/{scout,trader}.py, src/oskar/agents/prompts/{scout,trader}.md, src/oskar/core/escalation.py, tests/unit/{test_scout_trader,test_escalation}.py; MODIFY: src/oskar/core/{cycle,daemon,telegram_out}.py, src/oskar/journal/db.py

## 2026-08-30 — Фаза 2 собрана: Hermes+Telegram живые; демон в shadow-режиме

**Канал управления работает целиком**: Hermes установлен (финально — через задачу планировщика вне песочницы, после трех съеденных виртуализацией попыток), Nous Portal OAuth подтвержден Иваном (device-код), gateway подключен к @OSKAR_agnt_bot (allowlist = только Иван), модель nousresearch/hermes-4-405b через Nous-подписку. Сквозной тест пройден: «статус» Ивана -> gateway -> Hermes -> ответ в Telegram. Gateway живет в песочнице сессии (решение Ивана: сеть здесь ходит через VPN-туннель, у планировщика — нет); минус: умирает с перезапуском Claude — перезапускается вручную/мной.

**Демон дорос до shadow-режима**: `core/bootstrap.py` (YAML -> типизированные объекты; единственное место парсинга конфигов), `core/shadow_runner.py` — фоновый поток: вотчлист каждые 30 мин (universe -> quick_filter с полом ликвидности), тик цикла по интервалу профиля (30с), дорешивание counterfactuals каждые 5 мин, equity с VST ежечасно (фоллбек $1000), **3 подряд инфраструктурных сбоя -> safe_mode** (правило v1). /status отдает shadow-блок (watchlist/ticks/equity/last_report) — виден боту через скилл.

### Тесты
- test_bootstrap_shadow (10): загрузка капсов/профиля/деривация cycle-конфига (min_score 50, swing 5, cooldown 2 бара), тики раннера, кеш вотчлиста, equity_fn, error-streak -> safe_mode, сброс серии успехом, /status с shadow. Итого 172.

### Затронутые файлы
CREATE: src/oskar/core/{bootstrap,shadow_runner}.py, tests/unit/test_bootstrap_shadow.py; MODIFY: src/oskar/core/{api,daemon}.py

## 2026-08-27 — установка Hermes передана оператору: scripts/setup_hermes.ps1

Три попытки установить Hermes из агентской сессии разбились о MSIX-виртуализацию песочницы Claude Desktop (детали в debugging-заметке): split-brain путей, частичная переустановка без кода, полностью виртуализированное дерево при работающем exe. Решение: `scripts/setup_hermes.ps1` — однокомандная установка из терминала Ивана (официальный установщик + телеграм-конфиг из OSKAR\.env + скиллы oskar-*; секреты в git не попадают — скрипт читает их из .env на лету). Виртуализированные обломки агентских установок вычищены.

### Затронутые файлы
CREATE: scripts/setup_hermes.ps1; MODIFY: vault/knowledge/debugging/MSIX*.md

## 2026-08-26 — скиллы Hermes: oskar-control и oskar-approvals; установка Hermes запущена

`skills/oskar-control` — /status /pending /pause /resume /panic через локальный API (скрипт на stdlib, venv-независим); правило panic по ст. 6: исполнять немедленно по любой формулировке, не переспрашивая. `skills/oskar-approvals` — маппинг callback-кнопок `oskar:approve|reject|snooze:<id>` в POST API; кнопка = подтверждение, 409 = TTL истек (не воскрешаем), запрет approve по собственной инициативе агента.

Официальный установщик Hermes (242KB) скачан и просмотрен перед запуском: хосты легитимны (python.org/nodejs/git/astral/github), schtasks — управление собственным автостартом gateway, Invoke-Expression — опциональный CUA-драйвер (пропускаем: -SkipComputerUse). Запущен с -NonInteractive -SkipSetup.

### Затронутые файлы
CREATE: skills/oskar-control/{SKILL.md,scripts/oskarctl.py}, skills/oskar-approvals/{SKILL.md,scripts/approve.py}

## 2026-08-26 — TradeDecision v2: право вето Trader'а; решение по критику

Решение с Иваном (заметка: критик — сначала наблюдатель...): (1) вето Trader'а внедряется сразу — TradeDecision v2 с `verdict: execute|decline`, model_validator принуждает консистентность (execute = все опции выбраны, без decline_reason; decline = причина >= 20 символов, без опций); (2) LLM-критик — только log-only, право вето лишь после сравнения когорт по PF; (3) параллельные дебаты отклонены при подписочной экономике. Схема пере-экспортирована, версия в реестре 2.

### Тесты
- +2: execute round-trip v2, консистентность decline/execute. Итого 162.

### Затронутые файлы
MODIFY: src/oskar/contracts/{schemas,versions}.py, src/oskar/contracts/json/TradeDecision.schema.json, tests/unit/test_contracts.py; CREATE: vault/knowledge/decisions/критик*.md

## 2026-08-26 — проба рынка №1: калибровка по живым данным (swing 5, пол ликвидности, порог 50)

829 USDT-перпов за 212с. Ответ на главный открытый вопрос: **swing_length=5 на 15м дает поток кандидатов** (114 структурных событий / 8 кандидатов на топ-10 против 42/2 при swing=10). Фиксы по находкам: пол ликвидности в квик-фильтре (средний часовой оборот >= $200k — мемкоины с дутыми RSI-очками отсеяны), порог эскалации 70 -> 50 (живые кандидаты набирают 13-55). Заметка: vault/runs/проба рынка №1. Объявлен инвариант shadow-прогона: >= 5 кандидатов/сутки.

### Тесты
- +test_liquidity_floor_filters_illiquid. Итого 160.

### Затронутые файлы
MODIFY: src/oskar/data/scanner.py, config/settings.yaml, tests/unit/test_scanner.py; CREATE: vault/runs/проба рынка №1*.md

## 2026-08-26 — КРИТИЧЕСКИЙ фикс: limit улетал в since (бан API), сплит market-data

**Баг:** ccxt `fetch_ohlcv(symbol, timeframe, since, limit)` — третий позиционный аргумент since; клиент передавал limit позиционно -> биржа получала `since=201 мс от 1970` -> пустые ответы и ошибки 109415, ретраи копили счетчик -> **15-минутный бан kline-эндпоинта (109429), пойман вживую** первым сканом рынка. Тестовый фейк замаскировал баг СВОЕЙ неправильной сигнатурой — фейк обязан повторять сигнатуру оригинала точно. Фикс: limit только keyword'ом; фейк с ccxt-сигнатурой + assert; регресс-тест.

**Сплит источников данных:** `make_bingx_public_exchange()` — рыночные данные (свечи/markets) идут с публичного продакшн-хоста без sandbox и ключей (VST-цены == реальные, в этом суть VST); sandbox-хост — только приватным вызовам (баланс/ордера). Ретраи только на сетевые ошибки (`_retryable` по mro), биржевые коды не долбим — они копят счетчик нарушений.

Живое подтверждение после фикса: kline лимиты работают до 1000/запрос; **тестовая карточка сетапа с графиком SOL (36.7KB PNG, зоны FVG, линии entry/SL/TP, кнопки) доставлена в Telegram**. Кракозябры первого сообщения — PowerShell Invoke-RestMethod кодирует тело в ISO-8859-1; боевой путь httpx шлет UTF-8 корректно (тот же класс граблей, что cp1251 в v1).

### Тесты
- +test_limit_passed_as_keyword_never_positional, +test_exchange_errors_not_retried. Итого 159.

### Затронутые файлы
MODIFY: src/oskar/data/client.py, tests/unit/test_data_client.py

## 2026-08-26 — поток подтверждений подключен к циклу; Telegram-канал живой

Telegram: chat_id Ивана получен через getUpdates (@OSKAR_agnt_bot, /start), записан в .env; тестовое сообщение от бота доставлено — исходящий канал работает.

`core/cycle.py`: ShadowCycle получил опциональный `card_sink` — с ним (Фаза 2) кандидат, прошедший лестницу, дополнительно пишется в pending_approvals (TTL из конфига, fail-closed) и уходит карточкой; без него — чистый shadow. Падение канала карточек не валит цикл: pending уже создан, TTL защитит, ошибка в отчете. outcome_detail различает shadow_mode/live_pending.

### Тесты
- +3 в test_core: pending+карточка в live-режиме с будущим TTL, чистый shadow без pending, падение sink не ломает цикл. Итого 157.

### Затронутые файлы
MODIFY: src/oskar/core/cycle.py, tests/unit/test_core.py

## 2026-08-26 — telegram_out: карточки сетапов с графиком и кнопками

`core/telegram_out.py` — только ИСХОДЯЩЕЕ (входящие/getUpdates — монополия Hermes, делить long-poll нельзя, а отправка конфликтов не создает). TelegramSender: send_text + send_setup_card (sendPhoto с графиком или sendMessage), inline-кнопки Approve/Reject/Snooze с callback_data `oskar:approve:{id}` — их ловит Hermes-скилл oskar-approvals. render_card_text — вся суть в один экран телефона (символ/направление/тип, score, entry/SL/TP с процентом стопа, R:R/риск/леверидж/qty из RiskVerdict, зоны FVG/OB/sweep, тренд/RSI/TTL). render_chart — mplfinance PNG: 120 свечей + зоны FVG (фиолетовая) и OB (оранжевая) + линии entry/SL/TP.

### Тесты
- test_telegram_out (7): payload кнопок, sendPhoto с multipart, текст карточки, PNG-рендер (магические байты + размер), отказ без кредов. Итого 154.

### Затронутые файлы
CREATE: src/oskar/core/telegram_out.py, tests/unit/test_telegram_out.py

## 2026-08-26 — первое живое подключение к VST: defaultType=swap, валюта VST

Ключи добавлены (перехвачены из .env.example ДО коммита — секреты живут только в .env). Два открытия на живом подключении:
1. **ccxt без `defaultType: swap` уходит в SPOT реального аккаунта даже в sandbox** — первый вызов показал реальный спот-кошелек (пыль TRX), а не демо. Фикс в обеих фабриках (data/client, sync/exchange_sync)
2. **Демо-валюта называется VST, не USDT** (баланс: 121.71 VST — судя по сумме, тот самый счет v1). snapshot_balance/BalanceInfo понимают обе валюты

Живая проверка: `oskar sync balance` = 121.71 VST; `positions` = пусто; **`diff` = DIFF CLEAN — первая реальная реконсиляция журнал==биржа чистая**. Obsidian CLI включен Иваном и подтвердил гигиену vault: 0 orphans, 0 unresolved. Telegram-бот @OSKAR_agnt_bot жив, ждем первого сообщения Ивана для chat_id.

### Тесты
- +test_balance_snapshot_vst_demo. Итого 148.

### Затронутые файлы
MODIFY: src/oskar/data/client.py, src/oskar/sync/exchange_sync.py, src/oskar/execution/bingx.py, src/oskar/cli.py, tests/unit/test_exchange_sync.py

## 2026-08-26 — vault стал полноценным Obsidian-vault: конфиг, регистрация, MOC-хабы

Vault не отображался в Obsidian: не было `.obsidian/` и записи в реестре приложения. Сделано: конфиг на базе легаси-vault (core-plugins, тема) + **цветные группы графа по папкам** (atlas синий, smc фиолетовый, levels оранжевый, risk-math зеленый, knowledge голубой, progress желтый, proposals красный); vault зарегистрирован в `%APPDATA%\obsidian\obsidian.json` и открыт (Obsidian перезапущен).

Связность графа: три MOC-хаба `_карта SMC/уровней/риск-математики` (ссылки на все 35 заметок кластеров, сгенерированы скриптом), заметка-карта [[конституция — 7 статей, капсы hash-pinned]] (сам CONSTITUTION.md вне vault — его читает рантайм); все относительные ссылки `[[../...]]` заменены на резолвящиеся имена (в Obsidian ссылки за пределы vault висели unresolved).

Официальный Obsidian CLI (встроен с 1.12.4): требует однократного включения тумблера Settings -> General -> Command line interface — дальше vault-гигиена через `obsidian orphans/unresolved/backlinks`.

### Затронутые файлы
CREATE: vault/.obsidian/{app,appearance,core-plugins,graph}.json, vault/library/*/_карта*.md (3), vault/atlas/конституция*.md; MODIFY: vault/00-home/index.md, ссылки в 2 заметках

## 2026-08-26 — ядро: демон с port-lock, управляющий API, shadow-цикл

`core/daemon.py` (entry point `oskard`): порядок старта — verify_caps (ст. 4, mismatch = отказ) -> пробный бинд 127.0.0.1:8321 (второй экземпляр = громкая ошибка, урок дубля ADA) -> журнал + RuntimeState(run_id) -> uvicorn. `core/api.py`: /status, /pending, approve/reject (TTL истек = fail-closed 409 + автопометка expired; повторное разрешение = 409; pause блокирует approve, reject разрешен всегда), /pause /resume /panic — **после panic /resume отказывает** (ст. 6.3: только ручной сброс оператором).

`core/cycle.py` — ShadowCycle (Фаза 1, без LLM и ордеров): свечи -> analyze -> мульти-ТФ -> score -> options -> лестница (confidence=68) -> журнал decisions + counterfactuals (cohort shadow/risk_rejected). Кулдаун по барам per-symbol, сбой символа изолирован. resolve_counterfactuals дорешивает исходы ТОЛЬКО по барам, открывшимся после создания кандидата (пойман и убит собственный look-ahead: первый вариант сканировал все свечи окна). В counterfactuals добавлен cohort 'shadow'; journal connect теперь check_same_thread=False (uvicorn worker-потоки).

### Тесты
- test_core (11): API-потоки (approve/reject/TTL/повтор/pause/panic-no-resume), port-lock на живом сокете, shadow-цикл журналирует, кулдаун-идемпотентность, pause, изоляция ошибок. Итого 147.

### Затронутые файлы
CREATE: src/oskar/core/{__init__,state,api,cycle,daemon}.py, tests/unit/test_core.py; MODIFY: src/oskar/journal/db.py, pyproject.toml

## 2026-08-26 — execution-слой: ABC-шов, BingX hedge-mode, paper-симулятор

`execution/base.py`: ExchangeAdapter ABC (normalize_qty / set_leverage / place_entry / place_sl / place_tp / cancel_all / close_position / positions / balance) — архитектурный шов для будущих DEX/MT5. Внутри системы стороны long|short и канонические символы; биржевые условности — забота адаптера.

`execution/bingx.py`: все грабли v1 зашиты и охраняются кодом: positionSide обязателен (guard бросает без него), reduceOnly в списке FORBIDDEN_PARAMS (guard бросает при попытке), SL/TP только отдельными STOP_MARKET/TAKE_PROFIT_MARKET с closePosition=True и stopPrice (qty=None), set_leverage с params side, clientOrderId в каждом ордере, normalize_qty через amount_to_precision (анти-qty-баг).

`execution/paper.py`: in-memory фоллбек — market-вход по инжектируемой цене, mark(high, low) прогоняет бар (SL первым при коллизии, как в реплее), учет PnL в балансе, дубль позиции отвергается по канону символа.

### Тесты
- test_execution (11): параметры каждого типа ордера против фикстур, канонизация на границе, side у set_leverage, close по contracts*contractSize, paper TP/SL-коллизия/дубль. Итого 136.

### Затронутые файлы
CREATE: src/oskar/execution/{__init__,base,bingx,paper}.py, tests/unit/test_execution.py

## 2026-08-26 — ФИКС .gitignore: пакет src/oskar/data не попадал в git

Голый паттерн `data/` в .gitignore (задуманный для runtime-БД) матчил каталоги с именем data НА ЛЮБОМ уровне — весь пакет src/oskar/data/ (symbols, client, candles, scanner) молча не отслеживался с первого коммита. Локально тесты шли (файлы на диске), но клон с GitHub был бы сломан, CI бы упал. Паттерн заякорен к корню (`/data/`, `/runtime/`), пакет добавлен. Урок в копилку: `git ls-files` после добавления нового пакета — обязательная проверка; «тесты зеленые локально» не значит «репозиторий полон».

### Затронутые файлы
MODIFY: .gitignore; ADD: src/oskar/data/{__init__,symbols,client,candles,scanner}.py

## 2026-08-26 — data-слой: ccxt-клиент (закрытые свечи), кеш TTL, двухступенчатый сканер

`data/client.py`: MarketDataClient поверх инжектируемого ccxt-объекта — **последняя (формирующаяся) свеча всегда отбрасывается** (look-ahead защита начинается на границе данных: запрашиваем limit+1, отдаем limit закрытых); retry x2 с бэкоффом; universe() — активные USDT-перпы с кешем 4ч; фабрика make_bingx_exchange (sandbox=VST, adjustForTimeDifference). `data/candles.py`: CandleCache с TTL per-TF (15m=30с, 1h=60с, 4h=5м, 1d=15м), инжектируемые часы, ключ через canonical() (SOL-USDT и SOL/USDT:USDT — один кеш-слот), отдаются копии (мутации потребителя не портят кеш), меньший limit сервится из кеша.

`data/scanner.py`: ступень 1 — quick-скоринг по 1h (объем 30-40, RSI-экстремум 10-20, определенный тренд 20, ATR>=0.5% 10), top-N по убыванию с детерминированным порядком; сбой одного символа = пропуск, не падение (graceful degradation v1). Ступень 2 (полный SMC-анализ) — в цикле демона.

### Тесты
- test_data_client (7): отброс формирующейся свечи, ретраи, universe-фильтры и TTL, кеш-хиты через форматы символов, копии. test_scanner (4): ранжирование, пропуск битых, top-N, пустой universe. Итого 125.

### Затронутые файлы
CREATE: src/oskar/data/{client,candles,scanner}.py, tests/unit/{test_data_client,test_scanner}.py

## 2026-08-26 — библиотека знаний завершена: 35 заметок, SMC-курс извлечен визуально

Финал экстракции INVEST ZONE (воркфлоу 3 экстрактора + 3 верификатора, все зеленые):
- **smc/ 17 заметок** — Smart Money Concept.pdf оказался картиночным (402 слайда без текстового слоя); отрендерен в PNG через PyMuPDF 2x и прочитан визуально, покрыты все 402 страницы. Сетапы: SH->BOS->RTO, premium/discount + OTE 0.705, брейкеры, SFP, AMD-сессии (времена в UTC+3 летнем — агентам учитывать DST), девиация рейнджа, ведение позиции BE/partials, чек-лист POI. Осознанно НЕ извлечено (вторично для Scout): RSI-дивергенции, Эллиотт, SMT/DXY, вайкофф-фазы отдельно
- **levels/ 9 заметок** — верификатор сверил с первоисточником и исправил 2 фактические ошибки: правило стопа — «запас хода минимум 4 стопа до ближайшего уровня» (КАТ с.144-145, не «ATR вмещает 5 стопов») + запрет входа по тренду после расхода 75-80% дневного ATR; импульс 2-3x среднедневного диапазона атрибутирован Герчику с правилом «импульс немедленный или никакой»
- **risk-math/ 9 заметок** — формулы Винса реконструированы по числовым worked-примерам (изображения формул не в текстовом слое)

FTS5-индекс пересобран: **35 заметок**. Оговорка ко всем: pages = страницы PDF-файла, не книжная пагинация; reliability: book_claim до валидации Analyst'ом.

### Затронутые файлы
CREATE: vault/library/smc/*.md (17); MODIFY: vault/library/levels/ (2 фактических фикса)

## 2026-08-26 — STATS_MANIFEST (ст. 3) + актуализация дашборда и задач

`src/oskar/agents/prompts/STATS_MANIFEST.md` — аудируемый манифест статистик в промптах: разрешенные (внешние данные, рыночные фичи, знания), запрещенные навсегда (направление портфеля, WR по направлениям, «prefer shorts» — петля v1), Analyst-only (полная статистика — агенту без права торговать). Обновлены index.md и задачи: Фаза 0 закрыта по знаниям (31 заметка), блокеры на Иване выделены.

### Затронутые файлы
CREATE: src/oskar/agents/prompts/STATS_MANIFEST.md; MODIFY: vault/00-home/index.md, vault/progress/задачи

## 2026-08-26 — claude_cli мост + бюджеты агентов из analysis_log

`agents/claude_cli.py`: subprocess-мост к `claude -p --output-format json` — байтовый stdin UTF-8 (v1: text=True кодировал cp1251 и ломал русский), ретрай, prompt_sha, extract_json_block (голый JSON / ```json-блок / JSON в тексте). Тесты на стаб-процессах (ok/fail/garbage) — реальный CLI не дергается.

`agents/budget.py`: лимиты per-agent считаются COUNT-запросом по analysis_log — никакого скрытого состояния, переживает рестарты. check_budget бросает BudgetExceeded ДО вызова LLM; log_call пишет полный промпт+ответ (ст. 1 п. 4) и этим же движет счетчик.

### Тесты
- test_claude_cli (7): UTF-8 round-trip русского промпта, ретраи, мусорный вывод, JSON-варианты, дневной/часовой бюджет, полный промпт восстановим по sha. Итого 114.

### Затронутые файлы
CREATE: src/oskar/agents/{__init__,claude_cli,budget}.py, tests/unit/test_claude_cli.py

## 2026-08-26 — exchange_sync: read-only окно в биржу, детектор qty-бага

`sync/exchange_sync.py` (ст. 1 п. 3 — существует ДО первой сделки): чистые функции над протоколом ExchangeView (реальный ccxt-BingX VST через фабрику, тесты на фейке). `diff` сверяет открытые сделки журнала с позициями биржи: наличие (фантомы в обе стороны — v1-баг «-$329 vs -$7.56») + qty per symbol+side через `contracts * contractSize` с ratio-отчетом — прямой инструмент против открытого qty-бага v1 (SUSHI 6.98x — тест воспроизводит сценарий). Сравнение символов через canonical() — ADA-USDT == ADA/USDT:USDT не считается расхождением. CLI: `oskar sync balance|positions|diff` (ключи из .env, ASCII-вывод, diff возвращает exit 1 при грязи — можно в cron/CI).

### Тесты
- test_exchange_sync (8): clean/dirty diff, сценарий SUSHI 6.98x, иммунитет к форматам символов, фантомы журнала и биржи, ASCII-чистота вывода. Итого 107.

### Затронутые файлы
CREATE: src/oskar/sync/{__init__,exchange_sync}.py, tests/unit/test_exchange_sync.py; MODIFY: src/oskar/cli.py

## 2026-08-26 — реплей-харнесс: конвейер целиком, бар-за-баром, без LLM

`ops/replay.py`: каждый бар t — analyze(candles[:t]) -> индикаторы -> score -> options -> риск-лестница; исходы разрешаются по high/low последующих баров, коллизия SL+TP в одном баре — консервативно SL первым. Без LLM: дефолтные опции меню (market/свинг-SL/первый TP) — это оценка механического слоя и генератор counterfactual-когорт (executed / risk_rejected), не бэктест Scout'а. Статистика: setups/executed/rejected/WR.

Диагностика на синтетике подтвердила конвейер живым (score-компоненты собираются), но структурные события (BOS/CHoCH) редки — библиотека требует строгий 4-свинговый паттерн с пробоем. На реальных данных Фазы 1 проверить частоту событий на 15м/1ч — возможен тюнинг swing_length.

### Тесты
- test_replay (9): детерминизм (двойной прогон бит-в-бит), префикс-дисциплина (кандидат ссылается только на прошлое, исход только на будущее), полный риск-след, разрешение исходов (long/short/коллизия/open). Итого 99.

### Затронутые файлы
CREATE: src/oskar/ops/replay.py, tests/unit/test_replay.py

## 2026-08-26 — options.py: детерминированное меню entry/SL/TP для Trader

`detect/options.py` — мост SmcSnapshot -> SetupCandidate (ст. 2: Trader выбирает по id, не пишет цены). Entry: market / край FVG / середина OB (лимитки только на откате). SL: за последним подтвержденным свингом против направления / за краем OB / 1.5x ATR — все с люфтом 0.15 ATR («за уровень, не под лой» — из заметок Герчика). TP: ближайший нетронутый пул ликвидности / противоположный FVG / 2R и 3R. Нет свинг-якоря для SL — сетапа нет вообще. Id кандидата детерминирован (symbol-bar-direction) — реплей воспроизводит. setup_type: confluence при >= 2 структурах.

### Тесты
- test_options (5): полное меню short, зеркальная геометрия long, отказ без SL-якоря, детерминизм id, round-trip контракта. Итого 90.

### Затронутые файлы
CREATE: src/oskar/detect/options.py, tests/unit/test_options.py

## 2026-08-26 — индикаторы (pandas-ta) + opportunity score, перевзвешенный в SMC

`detect/indicators.py`: RSI/EMA20-50-200/ATR/MACD-кросс/volume-ratio по закрытым свечам; мульти-ТФ тренд с весами v1 (15m=1, 1h=2, 4h=3.5, 1d=5), направление при согласии >= 60% веса.

`detect/score.py`: score 0-100, перевзвешен в SMC против v1: конфлюэнс 40 (FVG 15 + OB 15 + свежесть структуры 10), тренд 25, объем 15, RSI 10, MACD 10. **Направление выводится из структуры (последний BOS/CHoCH), не из индикаторов** — индикаторы только добавляют очки при согласии. Нет направленной структуры (или протухла, > 40 баров) — эскалации нет вообще. Близость зон к цене — в единицах ATR (2.0), не фиксированный процент.

### Тесты
- test_indicators_score (10): тренды на синтетике, взвешенный мульти-ТФ, направление против индикаторов, полный конфлюэнс = 100, слабый сетап < 70 порога. Итого 85.

### Затронутые файлы
CREATE: src/oskar/detect/{indicators,score}.py, tests/unit/test_indicators_score.py

## 2026-08-26 — detect/smc.py: обертка smartmoneyconcepts с масками look-ahead

Изучен исходник библиотеки (v0.0.27) — найдены все точки утечки будущего:
- `swing_highs_lows`: свинг требует swing_length баров ПОСЛЕ себя (shift(-N) + rolling) — хвост маскируется; библиотека вдобавок ПРИНУДИТЕЛЬНО метит первый и последний бар свингами для альтернации — сфабрикованные метки убиты маской (тест подтверждает, что сырая библиотека их реально ставит)
- `fvg`: разметка на средней свече через shift(-1) — на префиксе последний бар не может нести FVG (тест); MitigatedIndex==0 = «еще не митигирован в пределах данных» — префикс-безопасно
- `bos_choch`: репортуются только пробитые структуры, момент события = BrokenIndex (тест: broken_index > swing_index всегда)
- `ob`: креация на свече пробоя свинга, зона на прошлой свече; `liquidity`: pip_range зависит от глобального диапазона переданных данных (допустимо)

Правило: analyze() видит ТОЛЬКО префикс закрытых свечей; реплей пересчитывает каждый бар. «Перерисовка» структуры (вытеснение свинга более экстремальным) — известное свойство, честно воспроизводится реплеем.

### Тесты
- test_detect_smc (10): маски хвоста/эндпоинтов, префикс-детерминизм, реплей-цикл без утечек индексов, ручной FVG-кейс (top/bottom точно), митигация относительна префиксу. Итого 75.

### Затронутые файлы
CREATE: src/oskar/detect/{__init__,smc}.py, tests/unit/test_detect_smc.py

## 2026-08-26 — журнал (SQLite) + первая волна библиотеки знаний (risk-math, levels)

`src/oskar/journal/db.py`: схема v1 портирована и расширена — trades (без категории close_reason='unknown' В ПРИНЦИПЕ: CHECK принуждает резолвер к явной категории; в v1 76% закрытий были unknown), decisions (все решения включая отклоненные), analysis_log (полный промпт+ответ каждого LLM-вызова — реплей по ст. 1 п. 4), trade_lessons, pending_approvals (TTL fail-closed), **counterfactuals** (когорты take/pass/reject для калибровки без LLM-вызовов), sync_snapshots. `archive.py`: снепшот перед прогоном (политика v1 «старые числа тянут на дно»).

**Пойманный баг:** SQLite `CHECK (x IN (..., NULL))` дает NULL вместо FALSE — ограничение молча пропускало ЛЮБОЕ значение. Тест поймал; исправлено на `x IS NULL OR x IN (...)` в трех таблицах. Ровно тот класс тихих багов, из-за которого v1 наврал о PnL.

**Библиотека знаний, волна 1 (workflow, 3 экстрактора + верификация):** risk-math 9 заметок (Винс: оптимальное f, дробное f, риск разорения, просадка 35-55% времени — норма, одновременные риски со счета), levels 9 заметок (Герчик + ложные пробои: анатомия ЛП/стоп-ханта, БСУ/БПУ-модель, правила стопа за уровень, сила уровня, поджатие). SMC-кластер (52MB PDF) — экстрактор упал на лимите сессии, перезапущен из кеша воркфлоу.

### Тесты
- test_journal (5): схема, CHECK-ограничения (включая регресс на IN+NULL), UNIQUE decision_id, архив. Итого 65.

### Затронутые файлы
CREATE: src/oskar/journal/{__init__,db,archive}.py, tests/unit/test_journal.py, vault/library/{risk-math,levels}/*.md (18 заметок)

## 2026-08-26 — риск-движок: лестница v2 + сайзинг; капс экспозиции переопределен как суммарный риск

`src/oskar/risk/sizing.py`: fee-aware R:R (минус 2x round-trip fee 0.08% до расчета), динамический леверидж `max_lev * conf_factor(0.3->1.0 на 50->100) * vol_factor(min(1, 1.5/sl%))` — восстановлен по worked-примерам v1 (60%/3.0% -> 2.2x; 90%/0.5% -> 8.6x), риск-базированный размер (потеря при SL = ровно risk_pct).

`src/oskar/risk/ladder.py`: 12 шагов с полным следом в RiskVerdict: анти-feedback ассерт (ст. 3) -> safe_mode -> paused -> context (stale НЕ блокирует) -> confidence [60..75] -> SL-дистанция по профилю -> геометрия цен -> fee-aware R:R >= 2.0 (tolerance 0.01, урок v1) -> леверидж -> осуществимость маржи -> суммарный риск <= 5% -> лимит позиций -> дубли (через canonical) -> confluence gate СИММЕТРИЧНО для обоих направлений (v1 закрыл только LONG).

**Смена семантики капса (до первого прогона, не поправка ст. 4):** `max_total_exposure_pct` -> `max_total_risk_pct`. Тест вскрыл: экспозиция по марже при риск-сайзинге недостижима (SL 1.5% дает маржу ~12% equity при капсе 5%). Правильная семантика: сумма risk_usd открытых позиций <= 5% equity (при 1%/сделку и максимум 3 позициях — потолок 3%, запас есть). Маржа проверяется отдельным шагом осуществимости. Хеш caps.yaml перепинен: 80540a6e...

### Тесты
- test_sizing (8): worked-примеры v1, fee съедает тонкий TP, риск-базированный размер
- test_ladder (19): каждый шаг таблично, RR float tolerance, дубль ADA в разных форматах, stale не блокирует, симметрия confluence, сторож анти-feedback полей. Итого 60.

### Затронутые файлы
CREATE: src/oskar/risk/{__init__,sizing,ladder}.py, tests/unit/{test_sizing,test_ladder}.py; MODIFY: config/caps.yaml, CONSTITUTION.md (перепин хеша)

## 2026-08-26 — symbols.py: единая точка нормализации символов

`src/oskar/data/symbols.py`: канон = ccxt-формат перпа `SOL/USDT:USDT`; `canonical()` принимает все форматы, виденные в v1 (SOLUSDT, SOL-USDT, sol/usdt, 1000PEPEUSDT, ONE_USDT...), `to_exchange()` дает нативный BingX `SOL-USDT`. Все границы системы обязаны прогонять символы через canonical() на входе. Прямой ответ на баг v1 «дубль ADA SHORT из-за ADA/USDT:USDT vs ADA-USDT». Спот не поддерживается (только USDT-перпы), settle != quote отклоняется.

### Тесты
- test_symbols (18): параметризованно все v1-форматы, идемпотентность, сценарий дубля ADA, мусор отклоняется. Итого 33.

### Затронутые файлы
CREATE: src/oskar/data/{__init__,symbols}.py, tests/unit/test_symbols.py

## 2026-08-26 — FTS5-индекс знаний + CLI knowledge index/search

`src/oskar/knowledge/index.py`: полнотекстовый индекс vault/library + vault/lessons (SQLite FTS5, unicode61 remove_diacritics — русский поиск работает; без embedding-API по ограничению «только подписки»). Парсинг YAML-frontmatter заметок, индекс — производная (data/knowledge.db, пересборка целиком). CLI: `oskar knowledge index`, `oskar knowledge search <query>`. Пользовательский ввод экранируется в безопасный FTS5-запрос (каждое слово в кавычках, OR).

### Тесты
- test_knowledge_index (4): frontmatter-парсинг, поиск по-русски, по-английски, пустой результат. Итого 15.

### Затронутые файлы
CREATE: src/oskar/knowledge/{__init__,index}.py, tests/unit/test_knowledge_index.py; MODIFY: src/oskar/cli.py

## 2026-08-25 — Фаза 0: фундамент репозитория (initial commit)

Стратегическая сессия: исследованы легаси-vault v1 (48 заметок), легаси-скиллы, INVEST ZONE, локальный исходник Ouroboros v6.109.0, внешние решения (TradingAgents, ai-hedge-fund, smartmoneyconcepts). Утверждён план v2. Ключевая находка: код v1 утерян, vault v1 — спецификация; v1 был одним мозгом (PF 0.12), v2 — настоящая мультиагентность.

Создано:
- `CONSTITUTION.md` — 7 статей: единственная правда (биржа), граница детерминизма (LLM выбирает по id), запрет самоусиления (урок 14/15 SHORT), процедура капсов (hash-pin, ответ на 20x+принудительный флаг v1), дисциплина валидации, Emergency Stop, vault-дисциплина
- `config/` — caps.yaml (hash-pinned: 1519aded...), settings.yaml (paper/BingX VST), profiles.yaml (интрадей 15м/1ч стартовый), invariants.yaml (INV-001..003)
- `src/oskar/contracts/` — frozen-схемы v1 (паттерн Ouroboros): SetupCandidate, MarketContext, ScoutReport, TradeDecision (выбор опций по id!), RiskVerdict, ExecutionReport, Lesson, Proposal + реестр версий + JSON-экспорт
- `src/oskar/ops/caps_guard.py` + CLI `oskar caps-verify` — сверка sha256 капсов с конституцией
- `scripts/hooks/` — pre-commit: src/config без changelog отклоняется; запрещенная строка банится
- `.github/workflows/ci.yml` — ruff + caps-verify + pytest
- vault v2 скелет с конвенциями v1

### Тесты
- test_contracts (7): round-trip, confidence cap 75 на уровне схемы, extra forbid, пустые меню, реестр версий, дрейф экспорта
- test_caps_guard (3): пин валиден, tamper ловится, CRLF-нормализация
- test_constitution_bans (1): запрещенные строки отсутствуют

### Затронутые файлы
Все — CREATE (initial commit).

## Связи

- [[задачи — Сделано, Не проверено, План]]
