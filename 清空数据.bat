@echo off
chcp 65001 >nul
title 标书工作台 · 清空数据

echo ================================================
echo   清空标书工作台全部业务数据
echo   （文档 / 素材 / 项目 / 审计记录 / 上传文件）
echo   本地模型缓存与程序不受影响
echo ================================================
echo.

powershell -Command "$p=(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess; if($p){Stop-Process -Id $p -Force; Write-Host '服务已停止'} else {Write-Host '服务未在运行'}"

cd /d %~dp0data 2>nul
if exist db rmdir /s /q db
if exist files rmdir /s /q files
if exist cache rmdir /s /q cache
cd /d %~dp0

echo 数据已清空。下次启动服务将自动重建空数据库。
echo.
pause
