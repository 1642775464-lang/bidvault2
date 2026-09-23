"""生成交付包：内嵌 Python 运行时 + 后端源码 + 前端成品 + 启动脚本。

用法：runtime python 或 venv python 均可——
    backend/.venv/Scripts/python.exe scripts/make_dist.py

产物：<repo>/交付包/BidVault标书工作台/，整目录拷贝到客户机器即可运行
（客户机器无需安装 Python / Node；数据落在包内 data/）。
注意：首次图片 OCR 会联网下载 PaddleOCR 模型缓存。

安全（S-0）：禁止交付包携带任何凭据——
    不拷贝开发 .env；生成仅含非敏感默认值的 .env（JWT 密钥按包随机生成）；
    构建末尾全量敏感信息扫描，命中即阻断（含哨兵密钥回归，见 test_make_dist_secret_scan.py）。
"""

from __future__ import annotations

import re
import secrets
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

大模型联网说明：智能抽取/识别的在线调用需要云 AI 密钥。
本包默认不携带任何密钥——需要 AI 联网能力时，请在本机以环境变量
ZHIPU_API_KEY 配置你自己的云 AI 密钥后重启服务（密钥不落包内文件）。
未配置时系统仍可用（规则与本地 OCR 通道）。
首次使用图片 OCR 时会联网下载识别模型缓存。
"""

# 敏感信息模式（S-0）：命中即阻断构建。哨兵值用于回归验证阻断机制本身。
_SECRET_PATTERNS_GLOBAL = (
    re.compile(rb"ZHIPU_API_KEY\s*[=:]\s*\S"),               # 云 AI 密钥赋值（高置信）
    re.compile(rb"sk-[A-Za-z0-9]{16,}"),                     # OpenAI 风格密钥
    re.compile(rb"BIDVAULT-SECRET-SENTINEL-[0-9]+"),         # 哨兵密钥（回归用）
)
# 泛模式仅用于配置类文件（.py 源码中合法的取钥代码会误报，故排除）
_SECRET_PATTERNS_CONFIG = (
    re.compile(rb"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9._-]{16,}"),
    re.compile(rb"(?i)secret\s*[=:]\s*['\"]?[A-Za-z0-9._-]{24,}"),
)
# 文件名规则：禁止 .env 变体；生成的 backend/.env（机器本地配置，不含云凭据）豁免文件名规则、仍受内容扫描
_SECRET_FILENAMES = {".env", ".env.local", ".env.production"}
_SCAN_TEXT_SUFFIXES = {
    ".txt", ".md", ".bat", ".cmd", ".ps1", ".py", ".json", ".toml",
    ".ini", ".cfg", ".yaml", ".yml", ".xml", ".html", ".js", ".ts",
}


class SecretScanError(RuntimeError):
    """交付包检出敏感信息——阻断构建。"""


def scan_package_for_secrets(pkg: Path) -> None:
    """全量扫描交付包：.env 变体文件名禁止（生成的 backend/.env 除外）；
    内容命中密钥模式/哨兵即阻断。机器本地 JWT 行豁免（防自身误报）。"""
    offenders: list[str] = []
    jwt_allow = re.compile(rb"(?m)^ONLYOFFICE_JWT_SECRET=.*$")  # 机器本地随机值，允许交付
    for f in sorted(pkg.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(pkg).as_posix()  # 统一 POSIX 分隔符（Windows 下豁免规则按 / 匹配）
        if f.name in _SECRET_FILENAMES and rel != "backend/.env":
            offenders.append(f"{rel}（禁止携带 .env 类文件）")
            continue
        if f.suffix.lower() not in _SCAN_TEXT_SUFFIXES and rel != "backend/.env":
            continue  # 二进制/运行时文件不做文本语义扫描
        try:
            data = f.read_bytes()
        except OSError:
            continue
        scanned = jwt_allow.sub(b"ONLYOFFICE_JWT_SECRET=<local>", data)
        if b"BIDVAULT-SECRET-SENTINEL" in scanned:
            offenders.append(f"{rel}：命中哨兵密钥")
            continue
        # 全局高置信模式（任何文本文件）
        hit = next((p for p in _SECRET_PATTERNS_GLOBAL if p.search(scanned)), None)
        # 泛模式仅扫一方与配置类文件（.py/库源码中合法的取钥与赋值代码会误报，故排除）
        if hit is None and f.suffix.lower() != ".py" and not rel.startswith("runtime/"):
            hit = next((p for p in _SECRET_PATTERNS_CONFIG if p.search(scanned)), None)
        if hit is not None:
            offenders.append(f"{rel}：命中敏感模式 {hit.pattern.decode(errors='replace')[:40]}")
    if offenders:
        raise SecretScanError(
            "交付包含敏感信息，构建已阻断：\n  " + "\n  ".join(offenders[:20]))


def write_env_template(pkg: Path) -> None:
    """生成仅含非敏感默认值的 .env（JWT 密钥按包随机生成，不出网、不入库）。"""
    onlyoffice_internal = "http://127.0.0.1:8000"
    onlyoffice_public = "http://127.0.0.1:8080"
    jwt_secret = secrets.token_urlsafe(32)
    (pkg / "backend" / ".env").write_text(
        "## BidVault 单机交付配置（本文件不含任何云 AI 密钥）\n"
        "## AI 联网能力：请在本机以环境变量 ZHIPU_API_KEY 配置你自己的密钥后重启服务。\n"
        f"ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL={onlyoffice_public}\n"
        f"ONLYOFFICE_BACKEND_INTERNAL_URL={onlyoffice_internal}\n"
        f"ONLYOFFICE_JWT_SECRET={jwt_secret}\n"
        "ONLYOFFICE_JWT_HEADER=Authorization\n",
        encoding="utf-8",
    )


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
    # S-0：禁止拷贝开发 .env（含真实云 AI 密钥）；改为生成不含凭据的机器本地配置
    write_env_template(pkg)

    print("5/5 写入启动脚本与说明…")
    (pkg / "启动标书工作台.bat").write_text(BAT_RUN, encoding="utf-8")
    (pkg / "清空数据.bat").write_text(BAT_CLEAN, encoding="utf-8")
    (pkg / "交付说明.txt").write_text(README, encoding="utf-8")

    print("6/6 敏感信息扫描（命中即阻断）…")
    scan_package_for_secrets(pkg)

    total = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file())
    print(f"完成：{pkg}")
    print(f"总大小 {total / 1048576:.0f} MB · 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
