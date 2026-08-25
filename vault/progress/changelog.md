---
tags: [progress, changelog]
date: 2026-08-25
---
# Changelog

> Обновляется при каждом изменении кодовой базы или vault. Новые записи — сверху. Формат: `## YYYY-MM-DD — заголовок (commit)`.

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
