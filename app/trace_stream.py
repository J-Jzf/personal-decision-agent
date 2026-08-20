"""提供可持久化、可脱敏并可通过 SSE 转发的实时决策轨迹发布器。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from models.contracts import WorkflowEvent, WorkflowStatus


# 仅遮蔽真正承载凭据的字段；target_key 是前端恢复目标状态所需的公开标识。
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "access_token", "refresh_token", "id_token", "token",
    "secret", "client_secret", "authorization", "password",
})
_MAX_TEXT_LENGTH = 1500


def sanitize_trace_value(value: Any, key: str = "") -> Any:
    """递归删除敏感值，并限制文本长度以便安全地展示 Trace。"""
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(("_api_key", "_token", "_secret", "_password")):
        return "***"
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT_LENGTH else f"{value[:_MAX_TEXT_LENGTH]}…（已截断）"
    if isinstance(value, dict):
        return {str(item_key): sanitize_trace_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_trace_value(item, key) for item in value]
    return value


TraceSink = Callable[[str, str, str, dict[str, Any]], Awaitable[WorkflowEvent] | WorkflowEvent | None]


class TracePublisher:
    """将单次决策的轨迹先写 SQLite，再按顺序发布给对应的 SSE 请求。"""

    def __init__(self, decision_id: str, repository: Any) -> None:
        self.decision_id = decision_id
        self.repository = repository
        self._sequence = 0
        self._queue: asyncio.Queue[WorkflowEvent | None] = asyncio.Queue()
        self.result: Any = None
        self.error: str | None = None

    async def publish(self, event: WorkflowEvent) -> WorkflowEvent:
        """持久化一个已结构化的事件，并在同一顺序中将它放入流队列。"""
        self._sequence += 1
        safe_event = event.model_copy(update={
            "sequence": self._sequence,
            "payload": sanitize_trace_value(event.payload),
            "title": sanitize_trace_value(event.title),
            "summary": sanitize_trace_value(event.summary),
        })
        self.repository.save(safe_event)
        await self._queue.put(safe_event)
        return safe_event

    async def emit(self, *, state: WorkflowStatus, kind: str, title: str,
                   summary: str = "", payload: dict[str, Any] | None = None,
                   from_state: WorkflowStatus | None = None) -> WorkflowEvent:
        """根据调用方提供的展示信息创建并发布一条轨迹事件。"""
        return await self.publish(WorkflowEvent(
            event_id=str(uuid4()), decision_id=self.decision_id,
            from_state=from_state, to_state=state, kind=kind,
            title=title, summary=summary, payload=payload or {},
        ))

    async def next_event(self) -> WorkflowEvent | None:
        """等待下一条事件；返回 None 表示本次决策的流已关闭。"""
        return await self._queue.get()

    async def close(self) -> None:
        """标记不再有新的轨迹事件，同时不取消正在执行的业务任务。"""
        await self._queue.put(None)


async def emit_to_sink(sink: TraceSink | None, kind: str, title: str,
                       summary: str, payload: dict[str, Any]) -> WorkflowEvent | None:
    """兼容同步或异步回调地向请求局部 Trace 通道发送一条事件。"""
    if sink is None:
        return None
    result = sink(kind, title, summary, payload)
    return await result if inspect.isawaitable(result) else result


def encode_sse(name: str, data: dict[str, Any]) -> bytes:
    """将一个 JSON 数据包编码为浏览器可解析的标准 SSE 帧。"""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {body}\n\n".encode("utf-8")
