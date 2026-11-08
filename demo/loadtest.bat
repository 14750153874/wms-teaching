@echo off
rem ===========================================================================
rem  WMS one-click LOAD TEST   (ASCII only: cmd mis-parses Chinese inside .bat)
rem
rem  1. reuse an isolated test server on 8011 if it is running
rem  2. else use your demo server on 8000
rem  3. else start one on 8011 and wait
rem  4. READ phase + ORDER phase, then QPS / P50 / P95 / P99
rem  5. every run appended to logs\loadtest.csv
rem
rem  Usage:  loadtest.bat [seconds] [threads] [pay|keep]
rem          loadtest.bat 300 30
rem          loadtest.bat 60 10 pay
rem ===========================================================================
setlocal EnableExtensions
pushd "%~dp0"

set "SECONDS=%~1"
set "THREADS=%~2"
set "OPTION=%~3"
if not defined SECONDS set "SECONDS=120"
if not defined THREADS set "THREADS=20"

set "PYEXE="
call :try_py py -3
if not defined PYEXE call :try_py python
if not defined PYEXE call :try_py python3
if not defined PYEXE call :try_py "D:\ProgramData\anaconda3\python.exe"
if not defined PYEXE call :try_py "C:\ProgramData\anaconda3\python.exe"
if not defined PYEXE call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE call :try_py "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE goto :no_python
if not exist app.py goto :no_app
if not exist logs mkdir logs

set "PORTNUM=8011"
set "WMS_DB_PATH=data/_loadtest.db"
set "WMS_RATE_PER_MINUTE=600000"
set "WMS_RATE_BURST=200000"
set "WMS_MAX_CONCURRENCY=256"

echo ===========================================================================
echo   WMS load test    seconds=%SECONDS%   threads=%THREADS%   option=%OPTION%
echo ===========================================================================

call :probe 8011
if "%UP%"=="1" goto :server_ready
call :probe 8000
if "%UP%"=="1" (
  set "PORTNUM=8000"
  echo   [warn] using your DEMO server on 8000 - rate limit may return 429 errors
  goto :server_ready
)

echo [1/3] starting an isolated server on %PORTNUM% ...
start "" /min "%PYEXE%" -u app.py --reset --port %PORTNUM%
set /a WAITED=0
:wait_server
call :probe %PORTNUM%
if "%UP%"=="1" goto :server_ready
set /a WAITED+=3
if %WAITED% GEQ 45 goto :server_timeout
echo       waiting ... %WAITED%s
ping -n 4 127.0.0.1 >nul 2>nul
goto :wait_server

:server_ready
echo [2/3] server %PORTNUM% ready, warm up ...
"%PYEXE%" scripts\loadtest.py --port %PORTNUM% --seconds 2 --threads 2 --mode read >nul 2>nul

set /a HALF=%SECONDS%/2
if %HALF% LSS 3 set /a HALF=3
set /a HALF2=%SECONDS%-%HALF%
set /a W_THREADS=%THREADS%/2
if %W_THREADS% LSS 2 set /a W_THREADS=2

echo.
echo [3/3] READ phase  (%HALF%s, %THREADS% threads)
"%PYEXE%" scripts\loadtest.py --port %PORTNUM% --seconds %HALF% --threads %THREADS% --mode read

echo.
echo [3/3] ORDER phase (%HALF2%s, %W_THREADS% threads)
"%PYEXE%" scripts\loadtest.py --port %PORTNUM% --seconds %HALF2% --threads %W_THREADS% --mode order

if /i "%OPTION%"=="pay" (
  echo.
  echo [3/3] PAY phase   (%HALF2%s, %W_THREADS% threads, real stock deduction)
  "%PYEXE%" scripts\loadtest.py --port %PORTNUM% --seconds %HALF2% --threads %W_THREADS% --mode pay
)

echo.
echo ===========================================================================
echo   finished.  results: logs\loadtest.csv
echo   the test server (if started here) runs in its own window - close it.
echo ===========================================================================
echo.
pause
popd
exit /b 0

rem ---------------------------------------------------------------------------
:probe
set "UP=0"
powershell -NoProfile -Command "try { $null = Invoke-RestMethod 'http://127.0.0.1:%~1/api/v1/health' -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 set "UP=1"
exit /b 0

:try_py
"%~1" %~2 -c "import sys" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYEXE=%~1"
exit /b 0

:no_python
echo.
echo   [x] Python 3.8+ not found. Install it and tick "Add python.exe to PATH".
echo.
pause
popd
exit /b 1

:no_app
echo.
echo   [x] app.py not found next to this script - keep the demo folder together.
echo.
pause
popd
exit /b 1

:server_timeout
echo.
echo   [x] could not reach a server (45s). Either:
echo       A. double-click the demo launcher (server on 8000), or
echo       B. run:  "%PYEXE%" app.py --port 8011
echo.
pause
popd
exit /b 1