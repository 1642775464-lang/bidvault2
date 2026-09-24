"""标库 BidVault v2 · 应用配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _env(key: str, *, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _data_dir() -> Path:
    return Path(_env("DATA_DIR", default=str(_BASE_DIR / "data")))


def _wincred_read(target: str) -> str | None:
    """Windows 凭据管理器读取（D-1R4 安全收口）；非 Windows/失败返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes

        adv = ctypes.windll.advapi32
        cred = ctypes.c_void_p()
        ok = adv.CredReadW(target, 1, 0, ctypes.byref(cred))
        if not ok:
            return None
        try:
            blob = ctypes.cast(cred, ctypes.POINTER(wintypes.FILETIME))  # 仅占位，按结构偏移取
            # CREDENTIALW: 按字段偏移读取 CredentialBlobSize 与 CredentialBlob
            class _CRED(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ]
            p = ctypes.cast(cred, ctypes.POINTER(_CRED)).contents
            raw = ctypes.string_at(p.CredentialBlob, p.CredentialBlobSize)
            return raw.decode("utf-8")
        finally:
            adv.CredFree(cred)
    except Exception:  # noqa: BLE001  凭据不可用回退文件
        return None


def _wincred_write(target: str, value: str) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        class _CRED(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        data = value.encode("utf-8")
        buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
        c = _CRED(Type=1, TargetName=target, CredentialBlobSize=len(data),
                  CredentialBlob=buf, Persist=2)  # CRED_PERSIST_LOCAL_MACHINE=2
        return bool(ctypes.windll.advapi32.CredWriteW(ctypes.byref(c), 0))
    except Exception:  # noqa: BLE001
        return False


def _resolve_instance_secret(data_dir: Path, env_key: str, cred_target: str, fname: str,
                             *, mirror_file: bool = False) -> str:
    """实例密钥统一解析：env → Windows 凭据管理器 → 数据目录文件 → 首启生成（写凭据+文件）。

    mirror_file=True 时（本机访问令牌）保证数据目录存在文件镜像，供编辑器壳/回归脚本读取；
    JWT 不镜像（仅凭据管理器/文件首启路径）。
    """
    env_val = _env(env_key)
    if env_val:
        return env_val
    got = _wincred_read(cred_target)
    if got:
        if mirror_file:
            cfg = data_dir / "config"
            cfg.mkdir(parents=True, exist_ok=True)
            (cfg / fname).write_text(got, encoding="utf-8")
        return got
    cfg_dir = data_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    f = cfg_dir / fname
    if f.is_file():
        val = f.read_text(encoding="utf-8").strip()
        if val:
            return val
    import secrets as _secrets

    val = _secrets.token_urlsafe(32)
    _wincred_write(cred_target, val)
    f.write_text(val, encoding="utf-8")
    return val


def _resolve_jwt_secret(data_dir: Path) -> str:
    """ONLYOFFICE JWT 实例密钥（C-4c 收正 + D-1R4 凭据管理器接入）。

    环境变量优先（开发/联调注入）；否则 Windows 凭据管理器；再否则数据目录文件；
    首启生成并同时写入凭据管理器与文件。禁止公开默认回退。
    """
    return _resolve_instance_secret(data_dir, "ONLYOFFICE_JWT_SECRET", "BidVault/onlyoffice-jwt", "jwt_secret")


def _resolve_local_token(data_dir: Path) -> str:
    """D-1R4 本机访问令牌：业务写接口必须携带（头或 Cookie），防本机其他进程裸写。"""
    return _resolve_instance_secret(data_dir, "BIDVAULT_LOCAL_TOKEN", "BidVault/local-token",
                                    "local_token", mirror_file=True)


@dataclass(frozen=True)
class Settings:
    zhipu_api_key: str = field(default_factory=lambda: _env("ZHIPU_API_KEY"))
    zhipu_base_url: str = field(
        default_factory=lambda: _env("ZHIPU_BASE_URL", default="https://open.bigmodel.cn/api/paas/v4")
    )
    data_dir: Path = field(default_factory=_data_dir)
    scan_interval_seconds: int = field(default_factory=lambda: int(_env("SCAN_INTERVAL_SECONDS", default="600")))
    # ONLYOFFICE Document Server（阶段二：查看/编辑原始 DOCX）
    # PUBLIC_URL：浏览器加载编辑器与 api.js 的地址；INTERNAL_URL：DS 容器回调/拉取文件用的本服务地址
    onlyoffice_public_url: str = field(default_factory=lambda: _env("ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL").rstrip("/"))
    onlyoffice_backend_internal_url: str = field(default_factory=lambda: _env("ONLYOFFICE_BACKEND_INTERNAL_URL", default="http://host.docker.internal:8000").rstrip("/"))
    # JWT 实例密钥（C-4c 收正）：env 优先（开发/联调）；正式交付=首启生成到 data/config/jwt_secret
    # 并持久复用；禁止公开开发默认回退
    onlyoffice_jwt_secret: str = field(default_factory=lambda: _resolve_jwt_secret(_data_dir()))
    # D-1R4 本机鉴权：业务写接口需携带实例令牌；BIDVAULT_LOCAL_AUTH=0 仅用于测试/开发关闭
    local_auth_enabled: bool = field(default_factory=lambda: _env("BIDVAULT_LOCAL_AUTH", default="1") != "0")
    local_access_token: str = field(default_factory=lambda: _resolve_local_token(_data_dir()))
    # 阶段三 callback 落地前默认只读；置 1 仅限开发模式（页面会明确提示“修改不会保存”）
    onlyoffice_edit_enabled: bool = field(default_factory=lambda: _env("ONLYOFFICE_EDIT_ENABLED", default="0") == "1")
    # B-1c-3 迁移维护模式：禁止 Source 编辑/保存/恢复（读取链切换窗口）
    source_maintenance_mode: bool = field(default_factory=lambda: _env("SOURCE_MAINTENANCE_MODE", default="0") == "1")

    @property
    def onlyoffice_enabled(self) -> bool:
        return bool(self.onlyoffice_public_url)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "bidvault.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def cache_pages_dir(self) -> Path:
        return self.data_dir / "cache" / "pages"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for p in (self.db_path.parent, self.files_dir, self.cache_pages_dir, self.backup_dir, self.models_dir):
            p.mkdir(parents=True, exist_ok=True)


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
        _SETTINGS.ensure_dirs()
    return _SETTINGS
