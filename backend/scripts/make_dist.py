"""生成交付包：内嵌 Python 运行时 + 后端源码 + 前端成品 + 启动脚本。

用法：runtime python 或 venv python 均可——
    backend/.venv/Scripts/python.exe scripts/make_dist.py

产物：<repo>/交付包/BidVault标书工作台/，整目录拷贝到客户机器即可运行
（客户机器无需安装 Python / Node；数据落在包内 data/）。
注意：首次图片 OCR 会联网下载 PaddleOCR 模型缓存。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
VENV = BACKEND / ".venv"
BASE_PY = Path(r"D:/tools/python/cpython-3.12.14-windows-x86_64-none")
DEFAULT_DEST = REPO / "交付包"

BAT_RUN = r"""@echo off
chcp 65001 >nul
title 标书工作台 BidVault v2
cd /d %~dp0

if not exist "runtime\python.exe" (
    echo 未找到内嵌运行时 runtime\python.exe，请完整拷贝交付包后再运行。
    pause
    exit /b 1
)
if not exist "frontend\dist\index.html" (
    echo 未找到前端成品 frontend\dist，请完整拷贝交付包后再运行。
    pause
    exit /b 1
)

echo 正在启动标书工作台（首次启动约需十几秒）…
start "BidVault 服务" /min runtime\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir backend
timeout /t 6 /nobreak >nul
start "" "http://127.0.0.1:8000/"
echo 已在浏览器打开 http://127.0.0.1:8000/
echo 关闭最小化的「BidVault 服务」窗口即退出系统。
timeout /t 4 >nul
"""

BAT_CLEAN = r"""@echo off
chcp 65001 >nul
title 标书工作台 · 清空数据

echo ================================================
echo   清空标书工作台全部业务数据
echo   （文档 / 素材 / 项目 / 审计记录 / 上传文件 / 备份）
echo   程序与内嵌运行时不受影响
echo ================================================
echo.

powershell -Command "$p=(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess; if($p){Stop-Process -Id $p -Force; Write-Host '服务已停止'} else {Write-Host '服务未在运行'}"

cd /d %~dp0data 2>nul
if exist db rmdir /s /q db
if exist files rmdir /s /q files
if exist cache rmdir /s /q cache
if exist backups rmdir /s /q backups
cd /d %~dp0

echo 数据已清空。下次启动服务将自动重建空数据库。
echo.
pause
"""

README = """BidVault 标书工作台 v2 · 单机交付包
====================================

运行：双击「启动标书工作台.bat」，浏览器自动打开 http://127.0.0.1:8000/
退出：关闭最小化的「BidVault 服务」命令行窗口。
数据：全部落在包内 data/ 目录（拷贝该目录即完成迁移/备份）。
清空：双击「清空数据.bat」（不可恢复，请先确认）。

大模型联网说明：智能抽取/识别的在线调用需要在本文件同目录的 .env 中
配置 ZHIPU_API_KEY；未配置时系统仍可用（规则与本地 OCR 通道）。
首次使用图片 OCR 时会联网下载识别模型缓存。
"""


def main() -> int:
    dist = REPO / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        print("缺少 frontend/dist，请先在前端目录执行 npm run build")
        return 1
    if not (BASE_PY / "python.exe").is_file():
        print(f"缺少基础 Python 运行时：{BASE_PY}")
        return 1
    if not (VENV / "Lib" / "site-packages").is_dir():
        print(f"缺少虚拟环境：{VENV}")
        return 1

    pkg = REPO / "交付包" / "BidVault标书工作台"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)

    t0 = time.time()
    print("1/5 拷贝基础 Python 运行时…")
    shutil.copytree(BASE_PY, pkg / "runtime")

    print("2/5 并入依赖库 site-packages（约 1.1GB，需要几分钟）…")
    shutil.copytree(
        VENV / "Lib" / "site-packages",
        pkg / "runtime" / "Lib" / "site-packages",
        dirs_exist_ok=True,
    )

    print("3/5 拷贝后端源码…")
    shutil.copytree(BACKEND / "app", pkg / "backend" / "app")
    for name in ("pyproject.toml",):
        if (BACKEND / name).is_file():
            shutil.copy2(BACKEND / name, pkg / "backend" / name)

    print("4/5 拷贝前端成品与数据目录…")
    shutil.copytree(dist, pkg / "frontend" / "dist")
    (pkg / "data").mkdir(exist_ok=True)
    env_src = BACKEND / ".env"
    if env_src.is_file():
        shutil.copy2(env_src, pkg / "backend" / ".env")

    print("5/5 写入启动脚本与说明…")
    (pkg / "启动标书工作台.bat").write_text(BAT_RUN, encoding="utf-8")
    (pkg / "清空数据.bat").write_text(BAT_CLEAN, encoding="utf-8")
    (pkg / "交付说明.txt").write_text(README, encoding="utf-8")

    total = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file())
    print(f"完成：{pkg}")
    print(f"总大小 {total / 1048576:.0f} MB · 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
