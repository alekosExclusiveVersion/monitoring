# Invoke-DbCleanRetry.ps1 — retry-обёртка для tradesoft-db-clean-retry.
#
# Логика как macOS db-clean-retry.sh: пока стоит маркер DB_CLEAN_RETRY_MARKER
# (его ставит db_clean.py при недоступности aisql / ошибках удаления и снимает
# при успехе), пробуем очистку снова. Без маркера — тихий выход.
#
# Retry идёт в тихом режиме (DB_CLEAN_RETRY=1): повторные недоступности не
# дублируют сигнал db-clean.fail (он уже отправлен основным прогоном) —
# только строка в логе. Успех/ошибки удаления уведомляются как обычно.
#
# Вызывается планировщиком каждые 30 мин (см. tasks-windows.ps1).

[CmdletBinding()]
param(
    [string]$MonitoringRepo = $env:MONITORING_REPO,
    [string]$DbCleanRepo    = "$env:USERPROFILE\Work\scripts\db-clean",
    [string]$LogDir         = "$env:USERPROFILE\Work\logs"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($MonitoringRepo)) {
    $MonitoringRepo = "$env:USERPROFILE\Work\scripts\monitoring"
}
$marker = $env:DB_CLEAN_RETRY_MARKER
if ([string]::IsNullOrWhiteSpace($marker)) {
    $marker = Join-Path $LogDir "db-clean.retry"
}

if (-not (Test-Path -LiteralPath $marker)) { exit 0 }

$py = Join-Path $MonitoringRepo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $py)) {
    $py = Join-Path $MonitoringRepo ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $py)) { throw "venv python не найден: $py" }

$env:DB_CLEAN_RETRY = "1"
$p = Start-Process -FilePath $py `
    -ArgumentList "`"$DbCleanRepo\db_clean.py`" --commit --notify" `
    -WorkingDirectory $MonitoringRepo -Wait -PassThru
exit ($p.ExitCode)
