"""C-4b-2A：上下文收集器——出网范围显式化。

只接受调用方显式提交的上下文块（素材摘录 / 手动输入），验证归属与长度，
产出「发送清单」（EgressManifest 的 blocks——只记来源与字符数，不复制完整正文）。
不让 Gateway 默认读取整个项目目录或整份文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Material
from .ai_gateway import ContextBlock, GatewayContextError

MAX_TOTAL_CHARS = 16000   # v1 出网总字符预算
MAX_BLOCK_CHARS = 8000    # 单块截断上限


@dataclass
class ContextSpec:
    """调用方显式声明的上下文块。"""

    kind: str          # material_excerpt | manual_input
    ref: str           # 素材 id 或 "manual"
    label: str = ""
    text: str = ""     # manual_input 由用户手动输入；material_excerpt 由收集器读取并截断


class ContextCollector:
    """验证归属/范围 → 读取与截断 → 产出 ContextBlock 列表。"""

    def __init__(self, session: Any, project_id: str) -> None:
        self._session = session
        self._project_id = project_id

    def collect(self, specs: list[dict[str, Any]]) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        total = 0
        for spec in specs or []:
            kind = spec.get("kind", "")
            ref = str(spec.get("ref", ""))
            label = str(spec.get("label", "") or kind)
            if kind == "manual_input":
                text = str(spec.get("text", ""))
                if not text.strip():
                    raise GatewayContextError("手动输入上下文为空")
                if len(text) > MAX_BLOCK_CHARS:
                    raise GatewayContextError(
                        f"单块上下文超出上限（>{MAX_BLOCK_CHARS} 字符），请缩小范围")
            elif kind == "material_excerpt":
                text = self._material_excerpt(ref)
            else:
                raise GatewayContextError(f"不支持的上下文类型：{kind}")
            total += len(text)
            if total > MAX_TOTAL_CHARS:
                raise GatewayContextError(f"上下文超出出网预算（>{MAX_TOTAL_CHARS} 字符），请缩小范围")
            blocks.append(ContextBlock(kind=kind, ref=ref, label=label, text=text))
        if not blocks:
            raise GatewayContextError("至少提交一个上下文块")
        return blocks

    def _material_excerpt(self, material_id: str) -> str:
        m = self._session.get(Material, material_id)
        if m is None:
            raise GatewayContextError(f"素材不存在：{material_id[:8]}")
        # 归属校验：素材必须绑定当前项目（出网内容仅限本项目资源）
        if m.project_id and m.project_id != self._project_id:
            raise GatewayContextError("素材不属于当前项目，拒绝出网")
        return (m.text or "")[:MAX_BLOCK_CHARS]
