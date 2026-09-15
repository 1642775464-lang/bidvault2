"""测试环境：paddle Windows CPU 兼容（mkldnn PIR 指令未实现）+ HF 镜像，需在 paddle/hub 导入前生效。"""

from __future__ import annotations

import os

os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
