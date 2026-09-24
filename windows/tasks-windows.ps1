# tasks-windows.ps1 — создание задач Планировщика Windows для мониторинга и db-clean.
#
# Задачи:
#   tradesoft-pricing-alert   каждые 15 минут  — pricing_alert.py (мониторинг проценки)
#   tradesoft-db-clean        ежедневно 03:00  — db-clean-aisql.py --commit --notify
#   tradesoft-db-clean-retry  каждые 15 минут  — повторная очистка (маркер ретрая,
#                                                 DB_CLEAN_RETRY=1 — тихий режим)
#
# Зависимости репозиториев на Windows (по умолчанию):
#   %USERPROFILE%\Work\scripts\monitoring   — этот пакет + notify/pricing
#   %USERPROFILE%\Work\scripts\db-clean     — db-clean-aisql.py
#   %USERPROFILE%\Work\ts-b24               — b24_client + .env (B24 webhook)
#
# Env-переменные (нужные скриптам) задаются УРОВНЕМ ПОЛЬЗОВАТЕЛЯ через
# [Environment]::SetEnvironmentVariable — планировщик их подхватит.
# Секреты берутся из Windows Credential Manager (keyring) под той же учёткой.

[CmdletBinding()]
param(
    [string]$MonitoringRepo = "$env:USERPROFILE\Work\scripts\monitoring",
    [string]$DbCleanRepo    = "$env:USERPROFILE\Work\scripts\db-clean",
    [string]$B24Repo        = "$env:USERPROFILE\Work\ts-b24",
    [string]$PsaRepo        = "$env:USERPROFILE\Work\scripts\parallels-sql-admins",
    [string]$LogDir         = "$env:USERPROFILE\Work\logs"
)

$ErrorActionPreference = "Stop"
$venvPy = "$MonitoringRepo\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv не найден: $venvPy" }
if (-not (Test-Path $PsaRepo)) { throw "каталог parallels-sql-admins не найден: $PsaRepo" }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --- env уровня пользователя (виден планировщику и скриптам) -----------------
$env_map = @{
    PSA_REPO               = $PsaRepo
    B24_REPO               = $B24Repo
    MONITORING_REPO        = $MonitoringRepo
    TS_B24                 = "$B24Repo\scripts"
    AISQL_HOST             = "aisql.tradesoft.corp\supportsql"
    DB_CLEAN_LOG           = "$LogDir\db-clean.log"
    DB_CLEAN_SIGNALS       = "$LogDir\corp-vpn-signals"
    DB_CLEAN_RETRY_MARKER  = "$LogDir\db-clean.retry"
    ALERTS_LOG             = "$LogDir\alerts.log"
}
foreach ($k in $env_map.Keys) {
    [Environment]::SetEnvironmentVariable($k, $env_map[$k], "User")
    Write-Host "  env $k = $($env_map[$k])"
}

# --- задачи -----------------------------------------------------------------
function New-Task([string]$Name, [string]$Args, $Trigger) {
    $action = New-ScheduledTaskAction -Execute $venvPy -Argument $Args -WorkingDirectory $MonitoringRepo
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Force | Out-Null
    Write-Host "task created: $Name"
}

$trigAlert   = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$trigDaily   = New-ScheduledTaskTrigger -Daily -At "03:00"
$trigRetry   = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

New-Task "tradesoft-pricing-alert" "`"$MonitoringRepo\pricing_alert.py`"" $trigAlert
New-Task "tradesoft-db-clean" "`"$DbCleanRepo\db-clean-aisql.py`" --commit --notify" $trigDaily

# retry-задача: отдельный env (DB_CLEAN_RETRY=1) через wrapper-cmd
$retryCmd = "$LogDir\run-db-clean-retry.cmd"
@"
@echo off
set DB_CLEAN_RETRY=1
"$venvPy" "$DbCleanRepo\db-clean-aisql.py" --commit --notify %*
"@ | Set-Content -Path $retryCmd -Encoding Ascii
$actionRetry = New-ScheduledTaskAction -Execute $retryCmd -WorkingDirectory $MonitoringRepo
$settingsRetry = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "tradesoft-db-clean-retry" -Action $actionRetry `
    -Trigger $trigRetry -Settings $settingsRetry -Force | Out-Null
Write-Host "task created: tradesoft-db-clean-retry (DB_CLEAN_RETRY=1 via wrapper)"

Write-Host "Готово. Проверка: Get-ScheduledTask -TaskName 'tradesoft-*'"
Write-Host "Логи: $LogDir"