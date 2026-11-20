@echo off
chcp 65001 >nul
REM ============================================================
REM  Trident - FRONTEND   terminal 3 of 3
REM
REM  Run only this:  scripts\run_frontend.bat
REM  Everything else is automatic:
REM    check node/npm - npm install if deps missing - clear .next cache
REM    - npm run build - npm run start on port 3030
REM
REM  Optional arguments:
REM    run_frontend.bat dev      dev server only, hot reload, no build
REM    run_frontend.bat nobuild  skip build, just start an existing build
REM
REM  Keep this file ASCII-only: cmd.exe parses .bat with the system ANSI
REM  code page, so non-ASCII text would be mangled and break commands.
REM ============================================================
setlocal
cd /d "%~dp0..\frontend"
set "FE=%CD%"
set "MODE=%~1"

where node >nul 2>nul
if errorlevel 1 goto :nonode
where npm >nul 2>nul
if errorlevel 1 goto :nonpm

echo ============================================================
echo  Trident FRONTEND
echo  dir  : %FE%
for /f "delims=" %%v in ('node -v') do echo  node : %%v
echo ============================================================
echo.

if /i "%MODE%"=="dev" goto :dev
if /i "%MODE%"=="nobuild" goto :start_only

REM -- deps: install only when missing --
if not exist "%FE%\node_modules" goto :do_install
echo [PREP] dependencies present - skipping npm install
goto :after_install

:do_install
echo [PREP] node_modules missing - running npm install ...
call npm install
if errorlevel 1 goto :install_failed

:after_install
REM -- clear .next so stale build output cannot be served --
echo [PREP] clearing .next cache ...
if exist "%FE%\.next" rmdir /s /q "%FE%\.next"

echo.
echo [BUILD] npm run build ...
call npm run build
if errorlevel 1 goto :build_failed
echo.
echo [BUILD] done.

:start_only
echo.
echo [RUN] production server on http://localhost:3030 - Ctrl+C to stop
echo.
call npm run start
echo.
echo [EXIT] frontend stopped.
pause
exit /b 0

:dev
echo [RUN] dev server on http://localhost:3030 - hot reload - Ctrl+C to stop
echo.
call npm run dev
echo.
echo [EXIT] frontend stopped.
pause
exit /b 0

:nonode
echo [ERROR] Node.js not found. Install Node 18+ and add it to PATH.
pause
exit /b 1

:nonpm
echo [ERROR] npm not found. It is normally installed together with Node.js.
pause
exit /b 1

:install_failed
echo [ERROR] npm install failed.
pause
exit /b 1

:build_failed
echo [ERROR] frontend build failed - stopping, will NOT start with stale output.
pause
exit /b 1
