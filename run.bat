@echo off
REM AS 交易体系快捷入口
REM 用法: run.bat daily_flow --top 5
cd /d %~dp0
.venv\Scripts\python.exe utils\%1.py %2 %3 %4 %5 %6 %7 %8 %9
