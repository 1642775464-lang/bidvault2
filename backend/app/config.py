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


def _resolve_jwt_secret(data_dir: Path) -> str:
    """ONLYOFFICE JWT 实例密钥解析（C-4c 收正：按安装实例生成，禁止公开默认回退）。

    环境变量优先（开发/联调注入）；未配置时在数据/配置目录首启生成并持久化
    （data/config/jwt_secret，受数据目录访问权限保护，随备份/迁移走），
    后续启动复用同一密钥，不重复生成。缺失时不回退任何公开默认值。
    """
    env_val = _env("ONLYOFFICE_JWT_SECRET")
    if env_val:
        return env_val
    import secrets as _secrets

    cfg_dir = data_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    f = cfg_dir / "jwt_secret"
    if f.is_file():
        val = f.read_text(encoding="utf-8").strip()
        if val:
            return val
    val = _secrets.token_urlsafe(32)
    f.write_text(val, encoding="utf-8")
    return val


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
