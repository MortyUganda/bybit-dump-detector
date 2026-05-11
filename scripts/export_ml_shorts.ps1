# === Одной командой выгрузить ml_short_* в текущую папку ===
# Запуск из корня проекта:
#   .\scripts\export_ml_shorts.ps1            -> dev2 (по умолчанию)
#   .\scripts\export_ml_shorts.ps1 -Env prod  -> другой compose-проект

[CmdletBinding()]
param(
    [string]$Env = "dev2"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$compose = "docker/docker-compose.$Env.yml"
if (-not (Test-Path $compose)) { throw "Compose-файл не найден: $compose" }

Write-Host "=== Выгружаю ml_short_* из $Env ===" -ForegroundColor Cyan
docker compose -p $Env -f $compose exec bot python -m scripts.export_ml_shorts_csv
if ($LASTEXITCODE -ne 0) { throw "export_ml_shorts_csv упал" }

$cid = docker compose -p $Env -f $compose ps -q bot
if (-not $cid) { throw "Не нашёл контейнер bot в проекте $Env" }
docker cp "$cid`:/app/exports/." .
if ($LASTEXITCODE -ne 0) { throw "docker cp упал" }

Write-Host "Файлы выгружены в $(Get-Location)" -ForegroundColor Green
