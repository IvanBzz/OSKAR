# Супервизор OSKAR 24/7

`scripts/supervise.ps1` держит живыми **oskard** (демон) и **hermes gateway** (Telegram).
Работает отдельно от сессий Claude: раньше оба процесса умирали вместе с сессией (15–16.10 — 19 ч простоя).
Автозапуск при входе в Windows делает задача Task Scheduler «OSKAR Supervisor».

## Что он делает

- Запускает `uv run oskard` (вывод в `data\oskard.stdout.log`) и `hermes.exe gateway run` (вывод в `data\hermes_gateway_sandbox.log`) со своим окружением: `PYTHONUTF8=1`, `NO_PROXY` с loopback и BingX, `HERMES_HOME`.
- Каждые 10 с проверяет процессы. Для oskard каждые 30 с запрашивает `GET 127.0.0.1:8321/status` с `X-Oskar-Token` из `data\oskard.token`, без прокси.
- Если процесс упал, перезапускает его. Задержка растет 10 с → 20 → 40 → … → 5 мин. После 30 мин стабильной работы задержка сбрасывается.
- **Второй экземпляр не запускает.** Если на машине уже есть процесс oskard, порт 8321 слушается или занят `data\oskard.lock`, супервизор только наблюдает за экземпляром. Если oskard уже запущен руками, супервизор его подхватит. Шлюз Hermes обрабатывается так же: второй getUpdates дал бы 409 у бота.
- Шлет алерт в Telegram от сигнального бота при падении и при восстановлении. На каждый процесс уходит не больше одного сообщения в 10 мин, остальные события сворачиваются в одно.
- **Сторож зависания.** Если строка `heartbeat` в `data\oskard.log` старше 15 мин, приходит алерт «oskard завис?». Поток сканера пишет ее раз в 5 мин. Процесс при этом **не убивается**: решение за Иваном.
- **Не дает ПК уснуть**, пока работает: вызывает `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`. Это запрос процесса, а не настройка питания. Экран гаснуть может. Сон ПК 13–14.10 замораживал демон на часы.
- Лог: `data\supervisor.log` (ротация 5 МБ × 3). Токены и пароли прокси в лог не попадают.

## Установить (только на торговом ПК)

Команды выполняются из своего терминала, не из сессии Claude. Админ-права не нужны.

```powershell
cd C:\Users\Ivan\Desktop\Projects\Trading\OSKAR
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
Start-ScheduledTask -TaskName 'OSKAR Supervisor'     # запустить сейчас, не перезаходя
```

Задача стартует при входе пользователя с задержкой 1 мин. Лимита времени у нее нет. При сбое она перезапускается каждую минуту, работает и от батареи. Окно не появляется: используется `conhost --headless`. Флаг `-NoConhost` убирает эту обертку, но в Windows 11 тогда остается пустое окно Windows Terminal, и его закрытие убивает супервизор.

Перед установкой можно проверить план. Команда ничего не запускает и в сеть не ходит:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\supervise.ps1 -DryRun
```

## Проверить, что все живо

```powershell
Get-ScheduledTask -TaskName 'OSKAR Supervisor' | Get-ScheduledTaskInfo   # LastTaskResult 267009 = работает
Get-Content data\supervisor.log -Tail 20 -Wait                          # события супервизора
uv run oskar status                                                     # ответ API демона
Select-String -Path data\oskard.log -Pattern 'heartbeat:' | Select-Object -Last 1   # последний пульс
```

В Telegram сигнальному боту можно написать `/status`. Если демон жив, он ответит.
`data\oskard.log` и `data\oskard.stdout.log` записаны в UTF-8 без BOM, поэтому в PS 5.1 их нужно открывать с `-Encoding UTF8`.

## Остановить

`Stop-ScheduledTask` останавливает **только супервизор**. oskard и hermes продолжают работать в своих скрытых консолях, и следующий запуск супервизора их подхватит.

```powershell
Stop-ScheduledTask -TaskName 'OSKAR Supervisor'          # 1) иначе он перезапустит демон через 10 с
# 2) сам демон и шлюз (жесткая остановка; биржа — источник правды, монитор досверит на старте):
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '\\oskard(\.exe)?"?\s*$|uv(\.exe)?"?\s+run\s+oskard\s*$' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'hermes(\.exe)?"?\s+gateway' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Снять автозапуск совсем: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall`.
Запустить одну итерацию руками (поднять то, что лежит, и выйти): `scripts\supervise.ps1 -Once`.

## Вторая машина (ноутбук): install НЕ запускать

Демон работает **только на одной машине**. Port-lock действует только на своей машине, а два демона на одном аккаунте BingX дают двойные позиции (урок v1, ADA). Hermes на двух машинах дал бы 409 Conflict у бота.

- На ноутбуке `install_autostart.ps1` **не запускать**. Он и сам откажет: торговый ПК записан в `scripts\trading_host.txt` (сейчас `DESKTOP-RGBK4KM`).
- Если запустить `supervise.ps1` на другом ПК, он ничего не стартует: пришлет алерт и выйдет.
- Ноутбук нужен для разработки и чтения vault: `git pull` / `git push`, не больше.
- Если второй oskard все же запущен (руками), он видит на бирже ордера OSKAR чужого запуска (run_id, которого нет в его журнале). Такие ордера он **не снимает**, а сам уходит в SAFE MODE с алертом «ордера/позиции OSKAR чужого запуска (вторая машина?)». Основной демон так же реагирует на ордера второго. Останови лишний демон, проверь биржу, затем на основном `oskar panic-reset`, если он тоже ушел в SAFE MODE. Межмашинного замка нет: если на бирже в момент старта нет ордеров OSKAR, второй демон этого не заметит.
- Перенос торговли на другой ПК: на старом выполнить `install_autostart.ps1 -Uninstall` и остановить oskard и hermes (см. выше). Затем вписать в `scripts\trading_host.txt` имя нового ПК (`$env:COMPUTERNAME`), закоммитить, на новом сделать `git pull` и установить.

## Ограничения

- **Крышка ноутбука.** Keep-awake не мешает сну по закрытию крышки и по кнопке «Сон». Для 24/7 нужен стационарный ПК: на ноутбуке с закрытой крышкой демон спит.
- **Без входа в систему не работает.** Задача стартует при входе Ивана. После перезагрузки (например, ночного обновления Windows) все поднимется только после входа. При выходе из учетной записи Windows завершает и демон.
- **Telegram через прокси.** Алерты идут через `HTTPS_PROXY` из окружения пользователя, как у демона. Если не вышло, через системный прокси, затем напрямую. Если прокси лежит, алерт остается только в `data\supervisor.log`.
