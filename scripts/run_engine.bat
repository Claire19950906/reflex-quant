@echo off
chcp 65001 >nul
REM ============================================================
REM  Trident - ENGINE   terminal 1 of 3
REM
REM  Run only this:  scripts\run_engine.bat
REM  Everything else is automatic:
REM    create/patch DB - import prompt-library config
REM    - rescue wrongly deactivated insights - start engine
REM
REM  No manual init_db.py needed: this script runs it first, and the
REM  engine also migrates + auto-imports on boot. Idempotent, never
REM  overwrites existing rows.
REM
REM  Keep this file ASCII-only: cmd.exe parses .bat with the system ANSI
REM  code page, so non-ASCII text would be mangled and break commands.
REM ============================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"

REM -- locate Python: prefer project venv, else system python --
set "PY="
if exist "%ROOT%\venv\Scripts\python.exe" set "PY=%ROOT%\venv\Scripts\python.exe"
if not defined PY if exist "%ROOT%\backend\venv\Scripts\python.exe" set "PY=%ROOT%\backend\venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 goto :nopython
  set "PY=python"
)

echo ============================================================
echo  Trident ENGINE starting
echo  root   : %ROOT%
echo  python : %PY%
echo ============================================================
echo.

echo [PREP] init database and import config ...
"%PY%" -X utf8 "%ROOT%\backend\init_db.py"
if errorlevel 1 echo [WARN] init returned non-zero - continuing, engine will migrate anyway
echo.

REM -- guard: do not start a second engine on the same webhook port --
REM    Engine webhook port comes from TREE_NEWS_PORT, default 9000.
if not defined TREE_NEWS_PORT set "TREE_NEWS_PORT=9000"
netstat -ano | findstr ":%TREE_NEWS_PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto :portbusy
echo.

echo [RUN] engine - Ctrl+C to stop
echo.
REM -- Service processes restart often. First-deploy auto actions (seed import,
REM    insight rescue) belong to the [PREP] step above only; otherwise things you
REM    deleted in the UI would come back on every restart.
set "TRIDENT_NO_AUTO_SETUP=1"
cd /d "%ROOT%\backend\src_python"
"%PY%" -X utf8 engine.py

echo.
echo [EXIT] engine stopped.
pause
exit /b 0

:portbusy
echo [ERROR] Port %TREE_NEWS_PORT% is already in use - the engine looks like it is
echo         ALREADY RUNNING in another window. Close that one first, or stop it
echo         with:  netstat -ano ^| findstr :%TREE_NEWS_PORT%
echo         To run on another port:  set TREE_NEWS_PORT=9100 ^&^& scripts\run_engine.bat
echo.
pause
exit /b 1

:nopython
echo [ERROR] Python not found. Install Python 3.11+ and add it to PATH,
echo         or create a venv in the project root: python -m venv venv
pause
exit /b 1
