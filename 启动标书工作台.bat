@echo off
chcp 65001 >nul
title 标书工作台 BidVault v2
cd /d %~dp0backend

if not exist .venv\Scripts\python.exe (
    echo ================================================
    echo   尚未安装运行环境
    echo   请先安装 Python 3.12，并在 backend 目录执行：
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -e .
    echo   然后再运行本脚本
    echo ================================================
    pause
    exit /b
)

echo ================================================
echo   标书工作台 BidVault v2
echo   数据目录：bidvault2\data （备份即拷贝此目录）
echo   启动后浏览器将自动打开 http://127.0.0.1:8000/
echo   关闭本窗口即停止服务
echo ================================================

start "" cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8000/"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
