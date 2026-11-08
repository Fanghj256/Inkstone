@echo off
rem 双击即可启动收件箱；配合「任务计划程序 → 登录时」可开机自动跑
cd /d "%~dp0"
python inbox.py >> inbox.log 2>&1
