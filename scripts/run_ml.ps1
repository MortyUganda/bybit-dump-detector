# === Удобный запуск ML-скриптов из PowerShell ===
# Использование (из корня проекта):
#   .\scripts\run_ml.ps1                  -> обе модели на последних CSV
#   .\scripts\run_ml.ps1 -Mode outcome    -> только outcome
#   .\scripts\run_ml.ps1 -Mode decision   -> только decision
#   .\scripts\run_ml.ps1 -Mode export     -> только выгрузить CSV
#   .\scripts\run_ml.ps1 -Mode all        -> выгрузить + обе модели
#   .\scripts\run_ml.ps1 -Mode decision_v2 -> эксперименты decision v2
#   .\scripts\run_ml.ps1 -Mode decision_nodead -> эксперимент: decision без мёртвых фичей
#   .\scripts\run_ml.ps1 -Mode clean       -> фильтрация CSV для ML
#   .\scripts\run_ml.ps1 -Mode diagnose   -> диагностика фолдов (drift)
#   .\scripts\run_ml.ps1 -Mode decision -Txt -> сохранить лог в models/model_txt_<stamp>.txt
#   .\scripts\run_ml.ps1 -MinId 700       -> outcome с другим min_id
#   .\scripts\run_ml.ps1 -Splits 8        -> кастомное число фолдов
#
# Ищет последние auto_shorts_*.csv и canceled_signals_*.csv
# в текущей директории.

[CmdletBinding()]
param(
    [ValidateSet("run", "outcome", "decision", "decision_v2", "decision_nodead", "export", "all", "clean", "diagnose")]
    [string]$Mode = "run",
    [int]$MinId = 1,
    [int]$Splits = 5,
    [string]$AutoCsv = "",
    [string]$CanceledCsv = "",
    [switch]$IncludeAllOpened,
    [switch]$Txt
)

$ErrorActionPreference = "Stop"

# Заставляем Python выводить в UTF-8 — иначе print('📊') падает на cp1251
# (именно из-за этого _feature_engineering.py:293 ронял train_decision_model[_v2])
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Export-TradesCsv {
    Write-Host "=== Выгружаю свежие CSV из dev2 ===" -ForegroundColor Cyan
    docker compose -p dev2 -f docker/docker-compose.dev2.yml `
        exec bot python -m scripts.export_trades_csv
    if ($LASTEXITCODE -ne 0) { throw "export_trades_csv упал" }

    docker cp dd_bot_dev2:/app/exports/. .
    if ($LASTEXITCODE -ne 0) { throw "docker cp упал" }

    Write-Host "Файлы выгружены в $(Get-Location)" -ForegroundColor Green
}

function Invoke-Outcome {
    Write-Host "=== Outcome model ===" -ForegroundColor Cyan
    $args = @("--min-id", $MinId, "--splits", $Splits)
    if ($AutoCsv) { $args += @("--csv", $AutoCsv) }
    python -m scripts.train_outcome_model @args
}

function Save-DecisionLog {
    param(
        [Parameter(Mandatory)] [string]$LogText,
        [Parameter(Mandatory)] [string]$ModelGlob,
        [string]$Suffix = ""
    )
    $modelsDir = Join-Path (Get-Location) "models"
    if (-not (Test-Path $modelsDir)) {
        New-Item -ItemType Directory -Path $modelsDir | Out-Null
    }
    # Ищем свежий .pkl, чтобы взять его таймстамп (имя формируется внутри Python)
    $latest = Get-ChildItem -Path $modelsDir -Filter $ModelGlob -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest -and $latest.BaseName -match '_(\d{4}-\d{2}-\d{2})_(\d{6})$') {
        $stamp = "$($matches[1])_$($matches[2])"
    } else {
        # Fallback — текущее время
        $stamp = (Get-Date).ToString("yyyy-MM-dd_HHmmss")
    }
    $fname = if ($Suffix) { "model_txt_${stamp}_${Suffix}.txt" } else { "model_txt_${stamp}.txt" }
    $logPath = Join-Path $modelsDir $fname
    $LogText | Out-File -FilePath $logPath -Encoding utf8
    Write-Host "📝 Отчёт сохранён: $logPath" -ForegroundColor Green
}

function Invoke-Decision {
    $aoLabel = if ($IncludeAllOpened) { "ВКЛЮЧЁН" } else { "ОТКЛЮЧЕН" }
    Write-Host "`n=== Decision model [all_opened: $aoLabel] ===" -ForegroundColor Cyan
    $args = @("--splits", $Splits)
    if ($AutoCsv) { $args += @("--auto-csv", $AutoCsv) }
    if ($CanceledCsv) { $args += @("--canceled-csv", $CanceledCsv) }
    if ($IncludeAllOpened) { $args += "--include-all-opened" }
    # Перехватываем stdout+stderr; временно ОТКЛЮЧАЕМ Stop, иначе любой
    # stderr от Python (даже warnings) становится NativeCommandError и traceback обрезается.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -m scripts.train_decision_model @args 2>&1 | Tee-Object -Variable teed | Out-Host
        $exit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    $logText = ($teed | Out-String)
    if ($exit -ne 0) {
        $logText = "!!! python exit code: $exit !!!`n`n" + $logText
    }
    if ($Txt) {
        Save-DecisionLog -LogText $logText -ModelGlob "decision_model_*.pkl"
    }
    if ($exit -ne 0) {
        $msg = if ($Txt) { "❌ train_decision_model упал (exit $exit) — см. model_txt_*.txt" } else { "❌ train_decision_model упал (exit $exit). Для сохранения лога запусти с -Txt" }
        Write-Host $msg -ForegroundColor Red
    }
}

function Invoke-DecisionV2 {
    $aoLabel = if ($IncludeAllOpened) { "ВКЛЮЧЁН" } else { "ОТКЛЮЧЕН" }
    Write-Host "`n=== Decision model v2 (experiments) [all_opened: $aoLabel] ===" -ForegroundColor Cyan
    $args = @("--splits", $Splits)
    if ($AutoCsv) { $args += @("--auto-csv", $AutoCsv) }
    if ($CanceledCsv) { $args += @("--canceled-csv", $CanceledCsv) }
    if ($IncludeAllOpened) { $args += "--include-all-opened" }
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -m scripts.train_decision_model_v2 @args 2>&1 | Tee-Object -Variable teed | Out-Host
        $exit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    $logText = ($teed | Out-String)
    if ($exit -ne 0) {
        $logText = "!!! python exit code: $exit !!!`n`n" + $logText
    }
    if ($Txt) {
        Save-DecisionLog -LogText $logText -ModelGlob "decision_model_v2_*.pkl" -Suffix "v2"
    }
    if ($exit -ne 0) {
        $msg = if ($Txt) { "❌ train_decision_model_v2 упал (exit $exit) — см. model_txt_*.txt" } else { "❌ train_decision_model_v2 упал (exit $exit). Для сохранения лога запусти с -Txt" }
        Write-Host $msg -ForegroundColor Red
    }
}

function Invoke-DecisionNoDead {
    $aoLabel = if ($IncludeAllOpened) { "ВКЛЮЧЁН" } else { "ОТКЛЮЧЕН" }
    Write-Host "`n=== Decision model BEZ DEAD features [all_opened: $aoLabel] ===" -ForegroundColor Cyan
    $args = @("--splits", $Splits)
    if ($AutoCsv) { $args += @("--auto-csv", $AutoCsv) }
    if ($CanceledCsv) { $args += @("--canceled-csv", $CanceledCsv) }
    if ($IncludeAllOpened) { $args += "--include-all-opened" }
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -m scripts.train_decision_model_nodead @args 2>&1 | Tee-Object -Variable teed | Out-Host
        $exit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    $logText = ($teed | Out-String)
    if ($exit -ne 0) {
        $logText = "!!! python exit code: $exit !!!`n`n" + $logText
    }
    if ($Txt) {
        Save-DecisionLog -LogText $logText -ModelGlob "decision_model_nodead_*.pkl" -Suffix "nodead"
    }
    if ($exit -ne 0) {
        $msg = if ($Txt) { "❌ train_decision_model_nodead упал (exit $exit) — см. model_txt_*_nodead.txt" } else { "❌ train_decision_model_nodead упал (exit $exit). Для сохранения лога запусти с -Txt" }
        Write-Host $msg -ForegroundColor Red
    }
}

function Invoke-Clean {
    Write-Host "`n=== Фильтрация CSV для ML ===" -ForegroundColor Cyan
    python -m scripts.export_clean_for_ml
}

function Invoke-Diagnose {
    Write-Host "`n=== Диагностика фолдов ===" -ForegroundColor Cyan
    $args = @("--splits", $Splits)
    if ($AutoCsv) { $args += @("--auto-csv", $AutoCsv) }
    if ($CanceledCsv) { $args += @("--canceled-csv", $CanceledCsv) }
    python -m scripts.diagnose_folds @args
}

switch ($Mode) {
    "export" {
        Export-TradesCsv
    }
    "outcome" {
        Invoke-Outcome
    }
    "decision" {
        Invoke-Decision
    }
    "all" {
        Export-TradesCsv
        Invoke-Outcome
        Invoke-Decision
    }
    "decision_v2" {
        Invoke-DecisionV2
    }
    "decision_nodead" {
        Invoke-DecisionNoDead
    }
    "clean" {
        Invoke-Clean
    }
    "diagnose" {
        Invoke-Diagnose
    }
    "run" {
        Invoke-Outcome
        Invoke-Decision
    }
}
