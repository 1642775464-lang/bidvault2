@echo off
cd /d %~dp0

if not exist "runtime\pythonw.exe" (
    echo runtime\pythonw.exe not found. Please reinstall.
    pause
    exit /b 1
)

set "DATA_DIR=%LOCALAPPDATA%\BidVault\data"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%DATA_DIR%\logs" mkdir "%DATA_DIR%\logs"

rem Already running -> just open the UI
netstat -ano | find ":8000" | find "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    start "" "http://127.0.0.1:8000/"
    exit /b 0
)

rem Silent start: pythonw = no console window; logs go to DATA_DIR\logs\backend.log
start "" "%~dp0runtime\pythonw.exe" -c "import sys,os;sys.path.insert(0,r'%~dp0backend');os.chdir(r'%~dp0backend');os.environ['DATA_DIR']=r'%DATA_DIR%';f=open(r'%DATA_DIR%\logs\backend.log','a',encoding='utf-8',errors='replace');sys.stdout=sys.stderr=f;import uvicorn;from app.main import app;uvicorn.run(app,host='127.0.0.1',port=8000,log_level='warning')"

ping -n 7 127.0.0.1 >nul
start "" "http://127.0.0.1:8000/"
exit /b 0
