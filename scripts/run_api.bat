@echo off
chcp 65001 >nul
REM ============================================================
REM  Trident - API   terminal 2 of 3
REM
REM  Run only this:  scripts\run_api.bat
REM  Everything else is automatic:
REM    create/patch DB - import prompt-library config - start API
REM
REM  No manual init_db.py needed: this script runs it first, and the API
REM  lifespan also migrates + auto-imports. Idempotent, never overwrites
REM  existing rows.
REM
REM  Keep this file ASCII-only: cmd.exe parses .bat with the system ANSI
REM  code page, so non-ASCII text would be mangled and break commands.
REM ============================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"

set "PY="
if exist "%ROOT%\venv\Scripts\python.exe" set "PY=%ROOT%\venv\Scripts\python.exe"
if not defined PY if exist "%ROOT%\backend\venv\Scripts\python.exe" set "PY=%ROOT%\backend\venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 goto :nopython
  set "PY=python"
)

REM -- overridable via environment; defaults match deployment --
if not defined API_PORT set "API_PORT=8000"
if not defined API_HOST set "API_HOST=127.0.0.1"

echo ============================================================
echo  Trident API starting
echo  url    : http://%API_HOST%:%API_PORT%
echo  python : %PY%
echo ============================================================
echo.

echo [PREP] init database and import config ...
"%PY%" -X utf8 "%ROOT%\backend\init_db.py"
if errorlevel 1 echo [WARN] init returned non-zero - continuing, API will migrate anyway
echo.

REM -- guard: do not start a second API on the same port --
netstat -ano | findstr ":%API_PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :portbusy
echo.

set "UVICORN=%ROOT%\venv\Scripts\uvicorn.exe"
if not exist "%UVICORN%" set "UVICORN=%ROOT%\backend\venv\Scripts\uvicorn.exe"

echo [RUN] API - Ctrl+C to stop
echo.
REM -- Service processes restart often. First-deploy auto actions belong to the
REM    [PREP] step above only; otherwise deleted items would return on restart.
set "TRIDENT_NO_AUTO_SETUP=1"
cd /d "%ROOT%\backend\src_python"
if exist "%UVICORN%" goto :use_uvicorn
"%PY%" -X utf8 -m uvicorn api_server:app --host %API_HOST% --port %API_PORT%
goto :done

:use_uvicorn
"%UVICORN%" api_server:app --host %API_HOST% --port %API_PORT%

:done
echo.
echo [EXIT] API stopped.
pause
exit /b 0

:portbusy
echo [ERROR] Port %API_PORT% is already in use - the API looks like it is
echo         ALREADY RUNNING in another window. Close that one first, or use
echo         a different port:  set API_PORT=8010 ^&^& scripts\run_api.bat
echo.
pause
exit /b 1

:nopython
echo [ERROR] Python not found. Install Python 3.11+ and add it to PATH,
echo         or create a venv in the project root: python -m venv venv
pause
exit /b 1
