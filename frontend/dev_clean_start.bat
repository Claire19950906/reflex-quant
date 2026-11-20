@echo off
REM ============================================================
REM  Trident frontend 一键启动（自动清理 .next 缓存）
REM  用途：改前端代码后页面不生效时，用这个脚本启动，
REM        它会先删除 .next 编译缓存（强制全量重编译），再启动 dev server。
REM  用法：在 frontend 目录双击本脚本，或命令行执行 dev_clean_start.bat
REM ============================================================
cd /d "%~dp0"

echo.
echo [1/2] Deleting .next cache (force full recompile) ...
if exist ".next" (
    rmdir /s /q ".next"
    echo       .next removed.
) else (
    echo       no .next cache found.
)

echo [2/2] Starting dev server on http://localhost:3030 ...
echo       (press Ctrl+C to stop)
echo.
npm run dev
