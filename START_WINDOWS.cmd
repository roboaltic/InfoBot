@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 main.py %*
) else (
    python main.py %*
)
if errorlevel 1 (
    echo.
    echo Виникла помилка запуску. Перегляньте повідомлення вище.
    echo Потрібен Python 3.10 або новіший із Tcl/Tk.
    echo Інструкція: README_UA.md
    pause
)
