@echo off
chcp 65001 >nul
cd /d %~dp0
type "scripts\start_server_banner1.txt"
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  for /f "tokens=* delims= " %%b in ("%%a") do echo     http://%%b:8932
)
type "scripts\start_server_banner2.txt"
.venv\Scripts\python.exe -m uvicorn app:app --app-dir src --host 0.0.0.0 --port 8932
pause
