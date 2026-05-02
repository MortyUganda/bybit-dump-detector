@echo off
REM === Удобный запуск ML-скриптов под Windows ===
REM Использование:
REM   scripts\run_ml.bat              -> прогнать обе модели на последних CSV
REM   scripts\run_ml.bat outcome      -> только outcome model
REM   scripts\run_ml.bat decision     -> только decision model
REM   scripts\run_ml.bat export       -> только выгрузить свежие CSV из БД
REM   scripts\run_ml.bat all          -> выгрузить + прогнать обе модели
REM
REM Скрипт ищет последние auto_shorts_*.csv и canceled_signals_*.csv
REM в текущей директории (там где ты его запустил).

setlocal enabledelayedexpansion

set MODE=%1
if "%MODE%"=="" set MODE=run

if /i "%MODE%"=="export" goto :export
if /i "%MODE%"=="all" goto :export_then_run
if /i "%MODE%"=="outcome" goto :outcome
if /i "%MODE%"=="decision" goto :decision
if /i "%MODE%"=="run" goto :run

echo Unknown mode: %MODE%
echo Use: export ^| outcome ^| decision ^| run ^| all
exit /b 1

:export
echo === Выгружаю свежие CSV из dev2 ===
docker compose -p dev2 -f docker/docker-compose.dev2.yml exec bot python -m scripts.export_trades_csv
if errorlevel 1 exit /b 1
docker cp dd_bot_dev2:/app/exports/. .
echo.
echo Файлы выгружены в текущую директорию.
if /i "%MODE%"=="export" exit /b 0
goto :run

:export_then_run
goto :export

:outcome
echo === Outcome model (auto_shorts → AUC, importance) ===
python -m scripts.train_outcome_model %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

:decision
echo === Decision model (auto_shorts + canceled → unified prof model) ===
python -m scripts.train_decision_model %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

:run
echo === Outcome model ===
python -m scripts.train_outcome_model
echo.
echo === Decision model ===
python -m scripts.train_decision_model
exit /b 0
