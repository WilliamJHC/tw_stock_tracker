@echo off
REM Biweekly local re-run of run_backtest.py, registered via
REM scripts\register_backtest_task.ps1 as a Windows Scheduled Task (1st and
REM 15th of each month). Requires the machine to be on and network-connected
REM at run time -- a missed run just means the "last backtest" timestamp
REM stays stale until the next scheduled slot or a manual re-run.
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" src\run_backtest.py --refresh-data >> "logs\run_backtest_biweekly.log" 2>&1
endlocal
