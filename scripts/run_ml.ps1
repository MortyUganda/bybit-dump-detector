# === Удобный запуск ML-скриптов из PowerShell ===
# Использование (из корня проекта):
#   .\scripts\run_ml.ps1                  -> обе модели на последних CSV
#   .\scripts\run_ml.ps1 -Mode outcome    -> только outcome
#   .\scripts\run_ml.ps1 -Mode decision   -> только decision
#   .\scripts\run_ml.ps1 -Mode export     -> только выгрузить CSV
#   .\scripts\run_ml.ps1 -Mode all        -> выгрузить + обе модели
#   .\scripts\run_ml.ps1 -Mode decision_v2 -> эксперименты decision v2
#   .\scripts\run_ml.ps1 -Mode clean       -> фильтрация CSV для ML
#   .\scripts\run_ml.ps1 -Mode diagnose   -> диагностика фолдов (drift)
#   .\scripts\run_ml.ps1 -MinId 700       -> outcome с другим min_id
#   .\scripts\run_ml.ps1 -Splits 8        -> кастомное число фолдов
#
# Ищет последние auto_shorts_*.csv и canceled_signals_*.csv
# в текущей директории.

[CmdletBinding()]
param(
    [ValidateSet("run", "outcome", "decision", "decision_v2", "export", "all", "clean", "diagnose")]
    [string]$Mode = "run",
    [int]$MinId = 1,
    [int]$Splits = 5,
    [string]$AutoCsv = "",
    [string]$CanceledCsv = "",
    [switch]$IncludeAllOpened
)

$ErrorActionPreference = "Stop"

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

function Invoke-Decision {
    $aoLabel = if ($IncludeAllOpened) { "ВКЛЮЧЁН" } else { "ОТКЛЮЧЕН" }
    Write-Host "`n=== Decision model [all_opened: $aoLabel] ===" -ForegroundColor Cyan
    $args = @("--splits", $Splits)
    if ($AutoCsv) { $args += @("--auto-csv", $AutoCsv) }
    if ($CanceledCsv) { $args += @("--canceled-csv", $CanceledCsv) }
    if ($IncludeAllOpened) { $args += "--include-all-opened" }
    python -m scripts.train_decision_model @args
}

function Invoke-DecisionV2 {
    $aoLabel = if ($IncludeAllOpened) { "ВКЛЮЧЁН" } else { "ОТКЛЮЧЕН" }
    Write-Host "`n=== Decision model v2 (experiments) [all_opened: $aoLabel] ===" -ForegroundColor Cyan
    $args = @("--splits", $Splits)
    if ($AutoCsv) { $args += @("--auto-csv", $AutoCsv) }
    if ($CanceledCsv) { $args += @("--canceled-csv", $CanceledCsv) }
    if ($IncludeAllOpened) { $args += "--include-all-opened" }
    python -m scripts.train_decision_model_v2 @args
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
