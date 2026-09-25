# tasks-windows.ps1 — создание задач Планировщика Windows для мониторинга и db-clean.
#
# Задачи:
#   tradesoft-pricing-alert     каждые 15 минут  — pricing_alert.py (мониторинг проценки)
#   tradesoft-tg-support-reader ежечасно          — tg_support_alert.py (оповещения ts-support)
#   tradesoft-db-clean          ежедневно 21:00  — db-clean-aisql.py --commit --notify
#
# Все задачи выполняются через pythonw.exe (GUI-подсистема) — окна консоли не
# появляются и не мешают работе за рабочим столом.
#
# На Windows db-clean отправляет уведомление только при фактическом удалении БД;
# ошибки и пустые результаты остаются в db-clean.log.
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
$venvPyw = "$MonitoringRepo\.venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPyw)) { throw "venv pythonw не найден: $venvPyw" }
if (-not (Test-Path $PsaRepo)) { throw "каталог parallels-sql-admins не найден: $PsaRepo" }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --- env уровня пользователя (виден планировщику и скриптам) -----------------
$env_map = @{
    PSA_REPO                  = $PsaRepo
    B24_REPO                  = $B24Repo
    MONITORING_REPO           = $MonitoringRepo
    TS_B24                    = "$B24Repo\scripts"
    AISQL_HOST                = "aisql.tradesoft.corp\supportsql"
    DB_CLEAN_LOG              = "$LogDir\db-clean.log"
    DB_CLEAN_SIGNALS          = "$LogDir\corp-vpn-signals"
    DB_CLEAN_RETRY_MARKER     = "$LogDir\db-clean.retry"
    DB_CLEAN_NOTIFY_ON_DELETE = "1"
    ALERTS_LOG                = "$LogDir\alerts.log"
}
foreach ($k in $env_map.Keys) {
    [Environment]::SetEnvironmentVariable($k, $env_map[$k], "User")
    Write-Host "  env $k = $($env_map[$k])"
}

# --- задачи -----------------------------------------------------------------
function New-Task([string]$Name, [string]$Arguments, $Trigger) {
    $action = New-ScheduledTaskAction -Execute $venvPyw -Argument $Arguments -WorkingDirectory $MonitoringRepo
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Force | Out-Null
    Write-Host "task created: $Name"
}

$trigAlert = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$trigHourly = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$trigDaily = New-ScheduledTaskTrigger -Daily -At "21:00"

New-Task "tradesoft-pricing-alert" "`"$MonitoringRepo\pricing_alert.py`"" $trigAlert
New-Task "tradesoft-tg-support-reader" "`"$MonitoringRepo\tg_support_alert.py`"" $trigHourly
New-Task "tradesoft-db-clean" "`"$DbCleanRepo\db-clean-aisql.py`" --commit --notify" $trigDaily

$retryTaskName = "tradesoft-db-clean-retry"
$retryTask = Get-ScheduledTask -TaskName $retryTaskName -ErrorAction SilentlyContinue
if ($null -ne $retryTask) {
    Stop-ScheduledTask -TaskName $retryTaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $retryTaskName -Confirm:$false
    Write-Host "task removed: $retryTaskName"
}
foreach ($path in @("$PSScriptRoot\run-db-clean-retry.pyw", "$LogDir\run-db-clean-retry.cmd")) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

Write-Host "Готово. Проверка: Get-ScheduledTask -TaskName 'tradesoft-*'"
Write-Host "Логи: $LogDir"