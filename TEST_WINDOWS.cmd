@echo off
chcp 65001 >nul
call "%~dp0START_WINDOWS.cmd" --self-test
pause
