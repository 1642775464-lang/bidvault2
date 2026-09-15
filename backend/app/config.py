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


@dataclass(frozen=True)
class Settings:
    zhipu_api_key: str = field(default_factory=lambda: _env("ZHIPU_API_KEY"))
    zhipu_base_url: str = field(
        default_factory=lambda: _env("ZHIPU_BASE_URL", default="https://open.bigmodel.cn/api/paas/v4")
    )
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", default=str(_BASE_DIR / "data"))))
    scan_interval_seconds: int = field(default_factory=lambda: int(_env("SCAN_INTERVAL_SECONDS", default="600")))

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

    def ensure_dirs(self) -> None:
        for p in (self.db_path.parent, self.files_dir, self.cache_pages_dir, self.backup_dir):
            p.mkdir(parents=True, exist_ok=True)


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
        _SETTINGS.ensure_dirs()
    return _SETTINGS
