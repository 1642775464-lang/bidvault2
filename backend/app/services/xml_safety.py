"""OOXML XML 解析安全基础设施（C-3b-0）。

用户上传的 DOCX 是不可信输入：所有对包内 XML 的解析必须走本模块——
解析前拒绝 DOCTYPE/ENTITY，解析器显式禁用外部实体（XXE 防护），不依赖 lxml 默认行为。
禁止 recover=True：结构异常的文档宁可显式拒绝，不容错吞掉。
"""

from __future__ import annotations

from lxml import etree

_FORBIDDEN_MARKERS = (b"<!DOCTYPE", b"<!ENTITY", b"<!doctype", b"<!entity")


class UnsafeXmlError(Exception):
    """输入 XML 含 DOCTYPE/ENTITY 声明（拒绝解析，不尝试容错）。"""


def safe_xml_parser() -> etree.XMLParser:
    """显式安全解析器：不解析实体、不加载 DTD、不访问网络、禁超大树。"""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )


def safe_parse_xml(data: bytes | str) -> etree._Element:
    """解析不可信 XML：先拒绝 DOCTYPE/ENTITY，再用显式安全参数解析。

    合法 OOXML part（[Content_Types].xml / document.xml / rels 等）从不包含
    DOCTYPE/ENTITY 声明；出现即视为不可信构造输入，直接拒绝。
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    probe = raw[:4096]
    if any(marker in probe for marker in _FORBIDDEN_MARKERS):
        raise UnsafeXmlError("XML 含 DOCTYPE/ENTITY 声明，已拒绝解析")
    # 解析器参数必须内联在调用点（静态安全扫描可见），勿改为跨函数传递
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )
    return etree.fromstring(raw, parser)
