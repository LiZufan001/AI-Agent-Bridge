@echo off
cd /d "%~dp0"
python worker\dashboard_service.py open
if errorlevel 1 pause
