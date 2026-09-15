@echo off
chcp 65001 >nul
title 标书工作台 BidVault v2
cd /d %~dp0backend

echo ================================================
echo   标书工作台 BidVault v2
echo   数据目录：bidvault2\data （备份即拷贝此目录）
echo   启动后浏览器将自动打开 http://127.0.0.1:8000/
echo   关闭本窗口即停止服务
echo ================================================

start "" cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8000/"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
