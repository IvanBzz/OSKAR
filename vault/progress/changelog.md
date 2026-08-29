---
tags: [progress, changelog]
date: 2026-08-25
---
# Changelog

> Обновляется при каждом изменении кодовой базы или vault. Новые записи — сверху. Формат: `## YYYY-MM-DD — заголовок (commit)`.

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
