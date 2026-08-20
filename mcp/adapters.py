"""在不暴露 MCP SDK 对象的前提下归一化不同工具返回结果。"""

from __future__ import annotations

import json
import re
from typing import Any


def normalize_tool_result(result: Any, limit: int = 12000) -> str:
    if result is None:
        return ""
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if not isinstance(result, dict):
        content = getattr(result, "content", result)
    else:
        content = result.get("content", result)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(str(item.text))
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, ensure_ascii=False, default=str))
        text = "\n".join(parts)
    elif isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, ensure_ascii=False, default=str)
    return text[:limit]


_TOOL_ERROR_TEXT = re.compile(
    r"(?:input\s+validation\s+error|validation\s+error|invalid\s+(?:input|argument|parameter)|"
    r"missing\s+(?:required\s+)?(?:property|parameter|argument)|\brequired property\b)",
    re.IGNORECASE,
)


def tool_result_error(result: Any, summary: str) -> str | None:
    """识别 MCP 以正常 content 帧承载的工具失败，避免把错误文本写入证据池。"""
    raw = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    if isinstance(raw, dict) and raw.get("isError"):
        return summary or "MCP tool reported an error"
    return summary if _TOOL_ERROR_TEXT.search(summary) else None


def extract_tools(result: Any) -> list[Any]:
    if isinstance(result, dict):
        return list(result.get("tools", []))
    return list(getattr(result, "tools", []))
