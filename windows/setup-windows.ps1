# setup-windows.ps1 — перенос monitoring + db-clean на Windows-машину корп. сети.
#
# Что делает:
#   1. проверяет python и создаёт venv в %USERPROFILE%\Work\scripts\monitoring\.venv;
#   2. ставит зависимости (windows/requirements.txt);
#   3. переносит PSA-данные (servers.json + servers.key) в %APPDATA%\Parallels SQL Admin;
#   4. создаёт задачи Планировщика (см. tasks-windows.ps1).
#
# Запуск от пользователя (не admin обязателен, но для Сервисов нужен admin):
#   powershell -ExecutionPolicy Bypass -File setup-windows.ps1
#
# Перед запуском: win_secrets.py set <service> для всех секретов (см. README.md).

[CmdletBinding()]
param(
    [string]$WorkRoot = "$env:USERPROFILE\Work\scripts",
    [string]$PsaRepo  = "$env:USERPROFILE\Work\scripts\parallels-sql-admins",
    [switch]$SkipTasks
)

$ErrorActionPreference = "Stop"
$monitoring = "$WorkRoot\monitoring"
$dbclean    = "$WorkRoot\db-clean"
$tsb24      = "$env:USERPROFILE\Work\ts-b24"

Write-Host "==> Python"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    throw "python не найден в PATH. Установите python.org >= 3.11 и добавьте в PATH."
}
Write-Host "python: $($py.Source)"

Write-Host "==> venv: $monitoring\.venv"
if (-not (Test-Path "$monitoring\.venv\Scripts\python.exe")) {
    & python -m venv "$monitoring\.venv"
}
$venvPy = "$monitoring\.venv\Scripts\python.exe"

Write-Host "==> Установка зависимостей (windows/requirements.txt, db-clean)"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r "$monitoring\windows\requirements.txt"

if (-not $SkipTasks) {
    Write-Host "==> Задачи Планировщика (нужен admin)"
    & powershell -ExecutionPolicy Bypass -File "$monitoring\windows\tasks-windows.ps1" `
        -MonitoringRepo $monitoring -DbCleanRepo $dbclean -B24Repo $tsb24 -PsaRepo $PsaRepo
}

Write-Host "Готово. Не забудьте:"
Write-Host "  1. win_secrets.py set opencode.tg-alert-token   (и остальные секреты)"
Write-Host "  2. servers.json/servers.key — скопировать в %APPDATA%\Parallels SQL Admin"
Write-Host "  3. Сверить доступность aisql/MySQL/Grafana/B24 с этой машины"