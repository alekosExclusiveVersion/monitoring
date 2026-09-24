# Миграция monitoring + db-clean на Windows

> Быстрый путь: чтобы opencode на Windows-машине развернул всё сам, скопируй
> в его чат текст из **`DEPLOY-PROMPT.md`** (после предварительной ручной
> копировки двух папок, см. шаг 0).

Перенос сторожей мониторинга (pricing-alert, MySQL-скан) и автоочистки БД
(db-clean) с macOS на машину Windows в корп. сети. Цель — освободить Mac от
непрерывных фоновых задач: Windows-машина всегда онлайн в корп. сети.

## Архитектура после переноса

- **Секреты** — Windows Credential Manager (keyring, пространство `opencode`).
  Источник на macOS — Keychain (ещё живые демоны продолжают читать оттуда).
- **Каналы уведомлений** — B24 (chat `chat123028`) + Telegram (комната 4385).
  macOS-баннер остаётся только на darwin-машинах (в `notify.py` канал
  автоматически отключается на Windows).
- **Расписание** — Планировщик задач Windows (вместо launchd):

  | Задача | Расписание | Команда |
  |---|---|---|
  | `tradesoft-pricing-alert` | каждые 15 мин | `pricing_alert.py` |
  | `tradesoft-db-clean` | ежедневно 03:00 | `db-clean-aisql.py --commit --notify` |
  | `tradesoft-db-clean-retry` | каждые 15 мин | `db-clean-aisql.py --commit --notify` (DB_CLEAN_RETRY=1) |

- **БД-доступ** — данные приложения PSA лежат в
  `%APPDATA%\Parallels SQL Admin\` (servers.json + servers.key). Ключ — Fernet
  `file_key`, поэтому `servers.json` и `servers.key` переносятся **как есть**
  (в отличие от master_password, где нужен пароль).

## Что задействовано по коду

- `monitoring/notify.py` — `secret_get()`: macOS Keychain / Windows Credential
  Manager / env `SEC_<UPPER_SNAKE>`. Канал `macos` включается только на darwin.
- `monitoring/pricing_alert.py` — Grafana-секреты через `secret_get()`, CSV-фоллбэк
  в `%TEMP%`, ALERTS_LOG через env.
- `monitoring/detect_pricing_degradation.py` — данные PSA через
  `common.paths.app_data_dir()` (кроссплатформенный каталог).
- `db-clean/db-clean-aisql.py` — `--notify` шлёт события в B24/Telegram напрямую
  (без macOS-агента corp-notify.sh); RETRY-маркер под env; B24-секреты — из
  Credential Manager, если нет `.env`.
- `ts-b24/scripts/b24_client.py` — поддержал B24_BASE_URL/B24_WEBHOOK_TOKEN из env.

## Порядок установки (на Windows-машине)

0. **Склонировать репозитории** на Windows-машину (см. `DEPLOY-PROMPT.md`, шаг 1):
   ```
   %USERPROFILE%\Work\scripts\monitoring
   %USERPROFILE%\Work\scripts\db-clean
   %USERPROFILE%\Work\scripts\parallels-sql-admins
   %USERPROFILE%\Work\ts-b24
   ```
   Плюс бинарь/сборка Parallels SQL Admin для Windows (данные в `%APPDATA%`).

1. **Перенести PSA-данные** с macOS в `%APPDATA%\Parallels SQL Admin\`:
   - `~/Library/Application Support/Parallels SQL Admin/servers.json`
   - `~/Library/Application Support/Parallels SQL Admin/servers.key`
   Ключ копируется вместе с файлом — ничего расшифровывать не надо.

2. **Секреты в Credential Manager** (ключи такие же, как в macOS Keychain):
   ```bash
   echo "<secret>" | py windows/win_secrets.py set opencode.tg-alert-token
   py windows/win_secrets.py set opencode.tg-alert-chat2
   py windows/win_secrets.py set opencode.tg-alert-thread
   py windows/win_secrets.py set opencode.grafana.login
   py windows/win_secrets.py set opencode.grafana.password
   py windows/win_secrets.py set opencode.ts-b24.base-url
   py windows/win_secrets.py set opencode.ts-b24.webhook-token
   ```
   (Список полный — с источником в AGENTS.md macOS и `~/.config/opencode/AGENTS.md`.)

3. **Установить зависимости** (Python >= 3.11 обязателен, `python` в PATH):
   ```powershell
   powershell -ExecutionPolicy Bypass -File windows/setup-windows.ps1
   ```
   Создаст venv в `%USERPROFILE%\Work\scripts\monitoring\.venv`, поставит
   пакеты из `windows/requirements.txt`.

4. **Создать задачи Планировщика** (из install-скрипта, либо отдельно):
   ```powershell
   powershell -ExecutionPolicy Bypass -File windows/tasks-windows.ps1
   ```
   Проверка: `Get-ScheduledTask -TaskName 'tradesoft-*'`,
   состояние задач/последний запуск — в `eventvwr` / `Get-ScheduledTaskInfo`.

5. **Проверить сетевую доступность** с Windows-машины:
   - `aisql.tradesoft.corp\supportsql` (MSSQL, порт 1433)
   - MySQL :3306 (p5ru3 и пр.), Grafana `grafana.tradesoft.ru`
   - B24 `b24.tradesoft.ru`; Telegram IP `149.154.167.220:443`
   - Прямой DNS корп. сети обязателен (для `*.tradesoft.corp`).

6. **Тест-прогон** на Windows:
   ```powershell
   .venv\Scripts\python.exe monitoring\pricing_alert.py   # окно/норма
   .venv\Scripts\python.exe db-clean\db-clean-aisql.py --dry-run
   .venv\Scripts\python.exe db-clean\db-clean-aisql.py --dry-run --notify
   ```

7. **Переключение**: после стабильной работы на Windows остановить демоны
   macOS (launchd: `pricing-alert`, `corp-db-clean*`; `corp-notify` оставить —
   он обслуживает VPN-баннеры). Секреты в Keychain оставить — они не мешают.

## Наследие macOS (факт после миграции)

Состояние macOS-машины после переноса заданий на Windows (24.09.2026):

| Демон | Что было | Что сейчас |
|---|---|---|
| `com.tradesoft.pricing-alert` (user) | сторож проценки, каждые 15 мин | **остановлен + disabled** (bootout + `launchctl disable`) |
| `com.tradesoft.corp-db-clean` (root) | автоочистка aisql, 03:00 | **остановлен + disabled** (bootout + disable) |
| `com.tradesoft.corp-db-clean-retry` (root) | ретрай очистки, каждые 15 мин | **остановлен + disabled** |
| `com.tradesoft.corp-notify` (user) | баннеры + дублирование db-clean в B24/Telegram | **оставлен**, только VPN-ветки: из `corp-notify.sh` вырезаны `db-clean.*` (title и блок дублирования) — артефактная правка вне git, осталась в `/usr/local/sbin/` |

Следствия:
- pricing-alert и db-clean на macOS больше не шлют события → B24/Telegram не
  дублируются с Windows;
- до запуска задач на Windows существует временное окно без боевого мониторинга
  (кроме VPN-баннеров corp-notify);
- `pricing_alert.py`/`db-clean-aisql.py` остаются в репозитории как
  кроссплатформенный код — на Windows они нужны, на macOS их демоны отключены;
- VPN-маршруты/баннеры (`routes-dead.*`, `vpn.*`) по-прежнему на macOS.

## Ограничения / нюансы

- **Секреты не дублируются автоматически**: Keychain (macOS) и Credential Manager
  (Windows) — независимые хранилища. После переноса править нужно оба вручную,
  если демоны ещё живы на обеих машинах.
- **pricing-alert** посылает алерты только если в момент запуска машина онлайн;
  обратную засылку проспанных окон не делает (как и на macOS).
- **db-clean retry**: маркер живёт в `%USERPROFILE%\Work\logs` (env
  `DB_CLEAN_RETRY_MARKER`), а не `/var/run` как на macOS.