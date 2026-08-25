---
tags: [progress, changelog]
date: 2026-08-25
---
# Changelog

> Обновляется при каждом изменении кодовой базы или vault. Новые записи — сверху. Формат: `## YYYY-MM-DD — заголовок (commit)`.

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
