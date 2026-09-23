"""S-0：make_dist 敏感信息扫描回归——哨兵/真实模式命中即阻断，干净包放行。

测试中的"密钥"均为动态拼接的假值（非真实凭据），用于验证构建阻断机制本身。
"""

from __future__ import annotations

import pytest

from scripts.make_dist import SecretScanError, scan_package_for_secrets

SENTINEL = "BIDVAULT-SECRET-SENTINEL-2024"
_FAKE_OPENAI_KEY = "sk-" + "a1b2c3d4" * 4          # 假值：仅用于触发扫描模式
_FAKE_ZHIPU_VALUE = "zp" + "9f8e7d6c" * 3          # 假值


def _write(pkg, rel: str, data: bytes | str) -> None:
    f = pkg / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        f.write_text(data, encoding="utf-8")
    else:
        f.write_bytes(data)


def test_blocks_env_file(tmp_path):
    """非 backend/.env 位置的 .env 文件一律禁止（含 runtime、根目录等）。"""
    _write(tmp_path, "runtime/.env", "ANY_KEY=whatever\n")
    with pytest.raises(SecretScanError, match="禁止携带"):
        scan_package_for_secrets(tmp_path)


def test_blocks_sentinel(tmp_path):
    _write(tmp_path, "docs/说明.txt", f"测试哨兵 {SENTINEL} 出现在文本中")
    with pytest.raises(SecretScanError, match="哨兵密钥"):
        scan_package_for_secrets(tmp_path)


def test_blocks_openai_style_key(tmp_path):
    _write(tmp_path, "backend/config.json",
           json_dumps_api_key(_FAKE_OPENAI_KEY))
    with pytest.raises(SecretScanError):
        scan_package_for_secrets(tmp_path)


def test_blocks_zhipu_assignment(tmp_path):
    _write(tmp_path, "backend/notes.txt", f"ZHIPU_API_KEY = {_FAKE_ZHIPU_VALUE}\n")
    with pytest.raises(SecretScanError, match="ZHIPU_API_KEY"):
        scan_package_for_secrets(tmp_path)


def json_dumps_api_key(key: str) -> str:
    import json

    return json.dumps({"api_key": key})


def test_blocks_packaged_jwt_secret(tmp_path):
    """JWT 实例密钥按安装实例首启生成——交付包（含配置文件）携带 JWT_SECRET 即阻断。"""
    _write(tmp_path, "backend/.env", "ONLYOFFICE_JWT_SECRET=xY3abcDEFghiJKLmnoPQRstuVWXyz0123456789\n")
    with pytest.raises(SecretScanError) as ei:
        scan_package_for_secrets(tmp_path)
    # 命中顺序可能是泛 secret 模式或 JWT 专项模式，两者均阻断
    assert "JWT" in str(ei.value) or "secret" in str(ei.value).lower()


def test_allows_clean_package(tmp_path):
    """无密钥配置包放行：backend/.env 仅含本机 ONLYOFFICE 地址。"""
    _write(tmp_path, "backend/app/main.py", "app = 1\n")
    _write(tmp_path, "backend/.env", "ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL=http://127.0.0.1:8080\n")
    _write(tmp_path, "交付说明.txt", "双击启动标书工作台.bat")
    scan_package_for_secrets(tmp_path)
