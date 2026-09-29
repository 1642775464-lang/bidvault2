"""发布前清理约定（项目负责人 2026-09-29 立规）：新版本/上线默认清除测试数据与文档。

用法：
    python tools/release_clean.py            # 干跑：只报告，不删除
    python tools/release_clean.py --apply    # 停 8000 实例 + 清空真实数据目录（%LOCALAPPDATA%\\BidVault\\data）
    python tools/release_clean.py --apply --purge-installs   # 追加：删除登记的旧测试安装目录

纪律（写入标库-开发规范.md 第七节）：
  1. 自动化回归/冒烟一律使用隔离 DATA_DIR（临时或专用目录），跑完即删；禁止写 %LOCALAPPDATA% 真实数据目录。
  2. 打新安装包前、交付验收前，默认执行本脚本 --apply，并核对 /api/health counts 全 0。
  3. 安装包不得携带任何测试数据（iss 的 main-pack 内禁止出现 data/ 目录）。
  4. 清理结果登记进标库-STATUS.md 的发布条目。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "BidVault" / "data"

# 历轮验证留下的测试安装目录（登记制：新增验证目录时在此追加）
OLD_INSTALLS = [
    r"D:\BidVault",                 # 安装器 /DIR 默认路径变体
    r"D:\bidvault-d1r3-clean",
    r"D:\bidvault-d1r4",
    r"D:\bidvault-d2b",
    r"D:\bidvault-d2b-final",
    r"D:\BidVault-Installed",
    r"D:\BidVault-Manual",
    r"D:\BidVault-Delivery",
    r"D:\bidvault-e0v3-clean",      # 当前实机验收实例（发布时先停 8000 再随包替换）
]


def listener_pid() -> str | None:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ":8000" in line and "LISTENING" in line:
            return line.split()[-1]
    return None


def report() -> None:
    pid = listener_pid()
    print(f"[i] 8000 端口实例 PID: {pid or '无'}")
    if DATA_DIR.is_dir():
        n = sum(1 for _ in DATA_DIR.rglob("*") if _.is_file())
        size = sum(p.stat().st_size for p in DATA_DIR.rglob("*") if p.is_file())
        print(f"[i] 真实数据目录 {DATA_DIR}: {n} 文件, {size / 1024 / 1024:.1f} MiB")
    else:
        print(f"[i] 真实数据目录不存在（首启将新建）: {DATA_DIR}")
    for d in OLD_INSTALLS:
        p = Path(d)
        print(f"[i] 旧测试安装目录 {'存在' if p.is_dir() else '—'}: {d}")


def apply(purge_installs: bool) -> None:
    pid = listener_pid()
    if pid:
        print(f"[x] 停止 8000 实例 PID {pid}")
        subprocess.run(["taskkill", "/PID", pid, "/F"], check=False)
    if DATA_DIR.is_dir():
        shutil.rmtree(DATA_DIR)
        print(f"[x] 已清空 {DATA_DIR}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if purge_installs:
        for d in OLD_INSTALLS:
            p = Path(d)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                print(f"[x] 已删除旧测试安装目录 {d}")
    print("[ok] 清理完成；重启启动器后 /api/health counts 应全 0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--purge-installs", action="store_true")
    a = ap.parse_args()
    if not a.apply:
        report()
        print("[i] 干跑模式：加 --apply 才执行删除")
        return 0
    apply(a.purge_installs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
