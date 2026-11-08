@echo off
rem ===========================================================================
rem  WMS demo launcher  (ASCII only on purpose!)
rem
rem  Why ASCII-only: cmd.exe parses a .cmd line by line using the CURRENT code
rem  page. A UTF-8 (no BOM) script that also runs "chcp 65001" gets its Chinese
rem  text and even line breaks mis-parsed -- you then see garbage commands such
rem  as "'dmin' is not recognized" and the server never starts. Keeping this
rem  file pure ASCII makes it immune to any code page.
rem  The Chinese project path is never typed here: %~dp0 resolves it at runtime.
rem
rem  Usage:  start-demo.cmd [port] [--no-browser]
rem ===========================================================================
setlocal EnableExtensions
pushd "%~dp0"

set "PORT=%~1"
set "OPEN_BROWSER=1"
if /i "%~1"=="--no-browser" set "PORT="
if /i "%~1"=="--no-browser" set "OPEN_BROWSER=0"
if /i "%~2"=="--no-browser" set "OPEN_BROWSER=0"
if not defined PORT set "PORT=8000"

rem ---------- 1. locate a WORKING python (the Store stub exists but errors out)
set "PY="
call :try_py py -3
if not defined PY call :try_py python
if not defined PY call :try_py python3
if not defined PY call :try_py "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try_py "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try_py "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY call :try_py "D:\ProgramData\anaconda3\python.exe"
if not defined PY call :try_py "C:\ProgramData\anaconda3\python.exe"
if not defined PY call :try_py "%ProgramFiles%\Python312\python.exe"
if not defined PY call :try_py "%ProgramFiles%\Python311\python.exe"

if not defined PY goto :no_python

rem ---------- 2. pick a free port (8000, 8001 ... 8010)
set "TRY=%PORT%"
:check_port
netstat -ano | findstr /C:":%TRY%" | findstr /C:"LISTENING" >nul 2>nul
if errorlevel 1 goto :port_ok
set /a TRY=%TRY%+1
if %TRY% GTR 8010 goto :no_port
goto :check_port
:port_ok
set "PORT=%TRY%"

if not exist "%~dp0app.py" goto :no_app

rem ---------- 3. start
echo ===========================================================================
echo   WMS demo  (e-commerce edition)
echo ---------------------------------------------------------------------------
echo   Python : %PY%
echo   Folder : %CD%
echo   URL    : http://127.0.0.1:%PORT%
echo   Login  : admin / 123456
echo.
echo   Keep this window open; closing it stops the server.
echo ===========================================================================
echo.
if "%OPEN_BROWSER%"=="1" start "" "http://127.0.0.1:%PORT%/"
"%PY%" -u app.py --port %PORT%
set "RC=%ERRORLEVEL%"
echo.
echo Server stopped (exit code %RC%).
pause
popd
exit /b %RC%

rem ---------------------------------------------------------------------------
:try_py
rem %~1 = command or full path; verify it really runs (not a broken Store stub)
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0

:no_python
echo.
echo   [x] Python 3.8+ not found.
echo       Install from https://www.python.org/downloads/windows/
echo       and tick "Add python.exe to PATH", then double-click this file again.
echo.
pause
popd
exit /b 1

:no_port
echo.
echo   [x] Ports 8000-8010 are all in use.
echo       Close whatever is using them, or run:  start-demo.cmd 8123
echo.
pause
popd
exit /b 1

:no_app
echo.
echo   [x] app.py not found next to this script.
echo       Keep the whole demo folder together (app.py + web/ + db/ + data/).
echo.
pause
popd
exit /b 1
