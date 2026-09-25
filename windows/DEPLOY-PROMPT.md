# Готовый промпт для opencode на Windows-машине

Скопируй этот блок целиком в чат opencode на Windows-машине (заменив
`<СЮДА>...` на реальные значения при необходимости). Секреты через чат НЕ
передаются — агент попросит ввести их в терминале напрямую (win_secrets.py
подтянет опытный промит), и проверит результатом `check`.

Перед запуском (делается вручную, не через чат):
1. Скопируй `servers.json` и `servers.key` из
   `~/Library/Application Support/Parallels SQL Admin/`
   в `%APPDATA%\Parallels SQL Admin\` (репозитории агент склонирует сам).

```
Ты — агент на Windows-машине в корп. сети. Разверни два инструмента мониторинга
Tradesoft: (1) сторож веб-проценки (pricing-alert) и (2) автоочистку БД aisql
(db-clean). Всё уже написано и лежит в локальных каталогах — твоя задача:
проверить наличие, поставить зависимости, зарегистрировать задачи Планировщика
и запустить контрольные тесты. НЕ отправляй реальные уведомления в B24/Telegram
(это шум для боевого чата) — тестовые прогоны должны это учитывать.

Каталоги (создай при отсутствии):
  %USERPROFILE%\Work\scripts\monitoring
  %USERPROFILE%\Work\scripts\parallels-sql-admins
  %USERPROFILE%\Work\scripts\db-clean
  %USERPROFILE%\Work\ts-b24
  %USERPROFILE%\Work\logs

Шаги:

1. ПРОВЕРЬ НАЛИЧИЕ репозиториев. Для каждого каталога из %USERPROFILE%\Work\:
   если отсутствует — склонируй:
     git clone https://github.com/alekosExclusiveVersion/parallels-sql-admins.git
     git clone https://github.com/alekosExclusiveVersion/ts-b24.git
     git clone https://github.com/alekosExclusiveVersion/monitoring.git
     git clone https://github.com/alekosExclusiveVersion/db-clean.git
   если присутствует — сделай git pull (ветка main).

2. ПРОВЕРЬ python: (py -3 --version или python --version) >= 3.11. Если нет —
   остановись и сообщи.

3. ЗАПУСТИ установку и создание задач:
     powershell -ExecutionPolicy Bypass -File %USERPROFILE%\Work\scripts\monitoring\windows\setup-windows.ps1
   Это создаст venv, поставит зависимости (windows/requirements.txt) и вызовет
   tasks-windows.ps1, который    зарегистрирует две задачи Планировщика:
     tradesoft-pricing-alert   (каждые 15 мин)
     tradesoft-db-clean        (ежедневно 21:00; уведомления только при удалении БД)
   Задачи регистрируются от текущего пользователя (интерактивная сессия).

4. СЕКРЕТЫ. Это НЕ вводить через чат. Найди пусть секретов в
   %USERPROFILE%\Work\scripts\monitoring\windows\win_secrets.py и для КАЖДОГО
   сервиса из списка ниже попроси пользователя в терминале на этой машине
   выполнить:
     py %USERPROFILE%\Work\scripts\monitoring\windows\win_secrets.py set <service>
   Значение система запросит интерактивно (getpass), в чат не попадает.
   Список сервисов:
     opencode.grafana.login
     opencode.grafana.password
     opencode.ts-b24.base-url
     opencode.ts-b24.webhook-token
     opencode.tg-alert-token
     opencode.tg-alert-chat2
     opencode.tg-alert-thread
   После каждого ввода проверь: win_secrets.py check <service>. Если хоть один
   check вернул ошибку — сообщи и остановись.

5. ПРОВЕРЬ приложение PSA-данных: в %APPDATA%\Parallels SQL Admin\ должны лежать
   config.ini, servers.json и servers.key (скопированы вручную). Если нет
   servers.json/servers.key — сообщи, что их нужно скопировать с macOS.

6. КОНТРОЛЬНЫЙ ПРОГОН (не слать уведомления):
   a) db-clean dry-run (без --notify, чтобы не слать в чаты):
        %USERPROFILE%\Work\scripts\monitoring\.venv\Scripts\python.exe %USERPROFILE%\Work\scripts\db-clean\db-clean-aisql.py --dry-run
      Ожидание: строка SUMMARY candidates=... to_drop=... skipped=...
   b) pricing-alert в «нормальном» режиме — запусти один раз и убедись, что
      нет traceback и окно запроса посчитанo (первый запуск может занять
      2–5 минут из-за Grafana):
        %USERPROFILE%\Work\scripts\monitoring\.venv\Scripts\python.exe %USERPROFILE%\Work\scripts\monitoring\pricing_alert.py
      НЕ указывай --notify и не запускай второй раз подряд — это фоновая задача
      по расписанию, повторный ручной запуск создаст лишние алерты.

7. ОТЧЁТ. Дай пользователю краткое резюме:
    - какие задачи Планировщика созданы (2 шт), их состояние через
      Get-ScheduledTaskInfo;
   - результат dry-run db-clean;
   - результат контроля pricing-alert;
   - список сервисов секретов, которые «check» подтвердил;
   - любые ошибки/предупреждения.
   НЕ выводи значения секретов.
```