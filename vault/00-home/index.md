---
tags: [home, dashboard]
date: 2026-08-25
---
# OSKAR — дашборд

**Orchestrated System Kernel for Autonomous Reasoning / Adaptive Risk** — мультиагентная трейдинговая система v2. Конституция: [[../../CONSTITUTION|CONSTITUTION.md]].

## Статус

- **Фаза: 0 — Фундамент** (репо, конституция, контракты, vault, извлечение знаний)
- Режим: paper (BingX VST) | Профиль: интрадей 15м/1ч | Реал: закрыт гейтом (PF ≥ 1.3 на ≥ 60 сделках)

## Карта

- **atlas/** — анатомия системы: [[../atlas/архитектура — два процесса, oskard владеет биржей|архитектура]]
- **knowledge/** — decisions (почему X, а не Y), debugging (симптом→причина→фикс→урок), patterns, integrations
- **library/** — знания из книг INVEST ZONE (smc/, risk-math/, levels/, patterns/), `reliability: book_claim` пока Analyst не подтвердит данными
- **runs/** — авто: вердикты инвариантов, статистика, реконсиляция по каждому прогону
- **lessons/** — авто от Analyst | **proposals/** — P-NNN предложения самоэволюции
- **progress/** — [[../progress/changelog|changelog]], [[../progress/задачи — Сделано, Не проверено, План|задачи]]
- **reference/** — API reference, реестр скиллов

## Конвенции (унаследованы из v1)

1. Имя файла = вывод, не тема («RR float precision — 1.4999 вместо 1.5, tolerance 0.01»)
2. Каждая заметка завершается `## Связи`
3. Задачи в трех состояниях: Сделано / **Не проверено** / План
4. Frontmatter: tags, date, updated, commit, status (`committed_not_live_validated` ставится хуком, снимается только зеленым инвариантом)
5. Changelog: новые записи сверху, с хешем коммита
6. Коммит в src/ или config/ без записи в changelog отклоняется pre-commit хуком

## Актуальная статистика

_Заполняется автоматически с Фазы 1 (сделки, инварианты, баланс VST)._

## Связи

- [[../progress/changelog]]
- [[../atlas/архитектура — два процесса, oskard владеет биржей]]
