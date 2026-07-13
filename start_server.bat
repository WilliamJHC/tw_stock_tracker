@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   台股追蹤器 v2  啟動中（區網模式）
echo ============================================
echo.
echo   電腦本機請開  http://localhost:8932
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  for /f "tokens=* delims= " %%b in ("%%a") do echo   手機（同一Wi-Fi）請開  http://%%b:8932
)
echo.
echo   （手機連不上時請看 README 的「手機存取」疑難排解）
echo   按 Ctrl+C 停止伺服器
echo ============================================
.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8932
pause
