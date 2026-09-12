@echo off
rem ============================================
rem  AS Trading Workbench Launcher
rem ============================================
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py"
pause
exit
