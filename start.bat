@echo off
rem ============================================================
rem  Aliyundrive Subscription Transfer - one-click local launcher
rem
rem  Usage:
rem    start.bat          Start service (Web UI on http://127.0.0.1:8000)
rem    start.bat stop     Stop running service (reads data/start.pid)
rem
rem  Behavior:
rem    1. Locate project root from this script location.
rem    2. Pick a Python that can "import flask".
rem    3. If flask missing, pip install -r requirements.txt (idempotent).
rem    4. Ensure data/ dir exists (SQLite + logs + pid).
rem    5. Start app.py in background, poll /healthz, then attach foreground.
rem
rem  Notes:
rem    - Refresh token is read from env ALIYUNDRIVE_REFRESH_TOKEN or the
rem      token table in DB. Web UI still loads without a token; transfer
rem      only works once a valid token is configured in Settings.
rem    - Pure ASCII on purpose (avoid GBK codepage issues with CJK paths).
rem ============================================================

setlocal ENABLEDELAYEDEXPANSION

rem Force UTF-8 so emoji/Chinese log output does not crash on GBK consoles.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ROOT="

rem Resolve project root to absolute canonical path.
for %%i in ("%~dp0.") do set "ROOT=%%~fi"

cd /d "%ROOT%" || ( echo [FAIL] Cannot change to project root: %ROOT% & pause & exit /b 1 )

rem ---------- stop mode ----------
if /i "%~1"=="stop" goto :stop

echo [OK] Project root: %ROOT%

rem ---------- 1. resolve python interpreter ----------
set "PY="
call :find_python "%ROOT%\.venv\Scripts\python.exe"
if defined PY goto :py_ready
call :find_python "C:\Users\chen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if defined PY goto :py_ready
call :find_python_bare py
if defined PY goto :py_ready
call :find_python_bare python3
if defined PY goto :py_ready
call :find_python_bare python
if defined PY goto :py_ready
goto :py_missing

:py_ready
echo [OK] Python interpreter: %PY%

rem ---------- 2. ensure dependencies importable (idempotent) ----------
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo [WARN] flask not importable; running pip install -r requirements.txt
  "%PY%" -m pip install -r "%ROOT%\requirements.txt" >nul 2>&1
  if errorlevel 1 (
    echo [FAIL] pip install failed. Fix:
    echo   "%PY%" -m pip install -r "%ROOT%\requirements.txt"
    pause & exit /b 1
  )
  "%PY%" -c "import flask" >nul 2>&1
  if errorlevel 1 (
    echo [FAIL] flask still not importable after install. Fix dependencies manually.
    pause & exit /b 1
  )
)

rem ---------- 3. ensure data dir ----------
if not exist "%ROOT%\data" mkdir "%ROOT%\data"

rem ---------- 4. read WEB_HOST / WEB_PORT (default 127.0.0.1:8000) ----------
set "WEB_HOST=127.0.0.1"
set "WEB_PORT=8000"
if exist "%ROOT%\.env" (
  for /f "usebackq tokens=1,* delims==" %%a in (`findstr /i /b "WEB_HOST= WEB_PORT=" "%ROOT%\.env"`) do (
    if /i "%%a"=="WEB_HOST" set "WEB_HOST=%%b"
    if /i "%%a"=="WEB_PORT" set "WEB_PORT=%%b"
  )
)

rem ---------- 5. start app.py in background ----------
echo [OK] Starting app.py (log: %ROOT%\data\app.log)...
start "" /b "%PY%" "%ROOT%\app.py" > "%ROOT%\data\app.log" 2>&1

rem ---------- 6. poll /healthz (max 30 tries x 1s) ----------
echo [OK] Waiting for service at http://%WEB_HOST%:%WEB_PORT%/healthz ...
powershell -NoProfile -Command ^
 "$ok=$false; for($i=1;$i-le 30;$i++){ try { $r=Invoke-WebRequest -Uri 'http://%WEB_HOST%:%WEB_PORT%/healthz' -UseBasicParsing -TimeoutSec 1; if($r.StatusCode -eq 200){$ok=$true; break} } catch {} ; if($ok){break}; Start-Sleep -Seconds 1 }; if($ok){exit 0}else{exit 1}"
if errorlevel 1 (
  echo [FAIL] Service did not become healthy within 30s. Check log:
  echo   %ROOT%\data\app.log
  if exist "%ROOT%\data\start.pid" del /f "%ROOT%\data\start.pid" >nul
  pause & exit /b 1
)

rem ---------- capture PID ----------
call :capture_pid
if defined APP_PID (
  echo %APP_PID% > "%ROOT%\data\start.pid"
  echo [OK] Service process PID=!APP_PID!
) else (
  echo [WARN] Could not capture PID; 'stop' will fall back to killing python.exe by image name.
)

echo [OK] Service is UP: http://%WEB_HOST%:%WEB_PORT%
echo [OK] Web UI:        http://%WEB_HOST%:%WEB_PORT%/
echo [OK] Logs:          %ROOT%\data\app.log
echo [OK] Press Ctrl+C to detach (service keeps running), or run: start.bat stop

rem ---------- foreground attach ----------
powershell -NoProfile -Command "Wait-Process -Id !APP_PID!" 2>nul
if exist "%ROOT%\data\start.pid" del /f "%ROOT%\data\start.pid" >nul
echo [OK] Service stopped.
exit /b 0

rem ============================================================
rem  helpers
rem ============================================================

:find_python
if not exist "%~1" exit /b 1
"%~1" -c "import flask" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%~1"
exit /b 0

:find_python_bare
"%~1" -c "import flask" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%~1"
exit /b 0

:capture_pid
set "APP_PID="
set "TMPPS=%TEMP%\aliyun_start_pid.ps1"
> "%TMPPS%" (
  echo $root = $env:ROOT
  echo $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" ^| Where-Object { $_.CommandLine -like '*app.py*' } ^| Sort-Object CreationDate -Descending ^| Select-Object -First 1
  echo if ^($p^) { $p.ProcessId ^| Out-File -FilePath ^(Join-Path $root data\start.pid^) -Encoding ascii }
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMPPS%" 2>nul
if exist "%ROOT%\data\start.pid" (
  set /p APP_PID=<"%ROOT%\data\start.pid"
)
exit /b 0

:stop
if not exist "%ROOT%\data\start.pid" (
  echo [WARN] PID file not found; service may not be running.
  echo [WARN] Fallback: killing python.exe by image name.
  taskkill /FI "IMAGENAME eq python.exe" /F >nul 2>&1
  if errorlevel 1 ( echo [WARN] No python.exe process found. ) else ( echo [OK] Killed python.exe processes. )
  exit /b 0
)
set /p APP_PID=<"%ROOT%\data\start.pid"
if defined APP_PID (
  taskkill /PID %APP_PID% /F >nul 2>&1
  if errorlevel 1 (
    echo [WARN] Process PID=%APP_PID% not found or already exited.
  ) else (
    echo [OK] Stopped service PID=%APP_PID%.
  )
)
del /f "%ROOT%\data\start.pid" >nul 2>&1
exit /b 0

:py_missing
echo [FAIL] No usable Python 3.10+ with flask found.
echo [FAIL] Fix: install Python 3.10+ and ensure it is on PATH, then re-run.
pause
exit /b 1
