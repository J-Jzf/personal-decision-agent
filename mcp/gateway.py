"""具备 stdio JSON-RPC 自动发现、故障切换和审计能力的安全 MCP 网关。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from app.trace_stream import TraceSink, emit_to_sink
from models.contracts import AgentName, ToolCallStatus, ToolObservation
from .adapters import extract_tools, normalize_tool_result, tool_result_error
from .policy import ToolPolicy
from .registry import RegisteredTool, ToolRegistry


class StdioMCPConnection:
    """最小化 MCP stdio 客户端，避免安全层依赖某个 SDK 的内部实现。"""

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.command, self.args, self.env = command, args or [], env or {}
        self.process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        environment = os.environ.copy()
        # MCP 服务可能由 uvx 创建独立 Python 环境，不能继承宿主 Python 的包搜索路径。
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.update(self.env)
        self.process = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # 某些 MCP 服务的工具 Schema 会超过 asyncio 默认 64 KiB 单行读取限制，因此提高上限。
            limit=1024 * 1024, env=environment,
        )
        await self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "personal-decision-agent", "version": "1.0.0"},
        })
        await self._notify("notifications/initialized", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ConnectionError("MCP process is not running")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.process.stdin.write(body + b"\n")
        await self.process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        async with self._lock:
            if self.process is None:
                await self.start()
            self._request_id += 1
            request_id = self._request_id
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if self.process is None or self.process.stdout is None:
                raise ConnectionError("MCP process has no stdout")
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    diagnostic = ""
                    if self.process.stderr is not None:
                        diagnostic = (await self.process.stderr.read()).decode("utf-8", errors="replace").strip()
                    detail = f": {diagnostic[-1000:]}" if diagnostic else ""
                    raise ConnectionError(f"MCP process closed stdout{detail}")
                message = json.loads(line.decode("utf-8"))
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result")

    async def list_tools(self) -> Any:
        await self.start()
        return await self._request("tools/list", {})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self.start()
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        if self.process is None:
            return
        process = self.process
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
        self.process = None


class MCPGateway:
    def __init__(self, *, registry: ToolRegistry | None = None, policy: ToolPolicy | None = None,
                 sessions: dict[str, Any] | None = None, audit_sink: Any = None,
                 timeout_seconds: float = 20.0, max_calls_per_task: int = 3) -> None:
        self.registry = registry or ToolRegistry()
        self.policy = policy or ToolPolicy()
        self.sessions = sessions or {}
        self.audit_sink = audit_sink
        self.timeout_seconds = timeout_seconds
        self.max_calls_per_task = max_calls_per_task
        self._task_call_counts: dict[tuple[str, str, str], int] = {}
        self.discovery_errors: dict[str, str] = {}

    @classmethod
    def from_commands(cls, commands: list[dict[str, Any]], **kwargs: Any) -> "MCPGateway":
        sessions = {}
        for index, config in enumerate(commands):
            command = str(config.get("command", "")).strip()
            if not command:
                continue
            name = str(config.get("name") or f"stdio-{index + 1}")
            sessions[name] = StdioMCPConnection(command, list(config.get("args", [])), dict(config.get("env", {})))
        return cls(sessions=sessions, **kwargs)

    async def discover(self) -> list[Any]:
        self.discovery_errors = {}
        for provider, session in self.sessions.items():
            self.registry.clear_provider(provider)
            try:
                result = await asyncio.wait_for(session.list_tools(), self.timeout_seconds)
            except Exception as error:
                self.discovery_errors[provider] = f"{type(error).__name__}: {error}"
                continue
            for remote in extract_tools(result):
                data = remote if isinstance(remote, dict) else {
                    "name": getattr(remote, "name", ""),
                    "description": getattr(remote, "description", ""),
                }
                try:
                    self.policy.validate_tool(str(data.get("name", "")), str(data.get("description", "")), True)
                except PermissionError:
                    continue
                self.registry.register(provider, remote, session)
        return self.registry.list_capabilities()

    async def call_tool(self, agent: str | AgentName, tool_name: str, arguments: dict[str, Any], *,
                        decision_id: str = "unknown", task_id: str = "unknown", target_id: str | None = None,
                        trace_sink: TraceSink | None = None) -> ToolObservation:
        """只调用专家明确选择的一个远程 MCP 工具，并把完整失败原因返回给下一轮 ReAct。"""
        agent_enum = agent if isinstance(agent, AgentName) else AgentName(agent)
        call_id, started = str(uuid4()), perf_counter()
        provider = self.registry.get(tool_name)
        if provider is None:
            observation = self._observation(
                call_id, decision_id, task_id, agent_enum, tool_name, arguments,
                ToolCallStatus.UNAVAILABLE, started, error="explicit tool_name is unavailable or ambiguous",
            )
            self._audit(observation)
            return observation
        capability = provider.descriptor.capability
        try:
            self.policy.authorize(agent_enum, capability, arguments)
        except (PermissionError, ValueError) as error:
            observation = self._observation(call_id, decision_id, task_id, agent_enum, tool_name, arguments, ToolCallStatus.DENIED, started, error=str(error))
            self._audit(observation)
            raise PermissionError("MCP operation is not permitted") from error

        task_key = (decision_id, task_id, target_id or task_id)
        if self._task_call_counts.get(task_key, 0) >= self.max_calls_per_task:
            observation = self._observation(
                call_id, decision_id, task_id, agent_enum, tool_name, arguments,
                ToolCallStatus.DENIED, started,
                error=f"信息目标 MCP 调用额度耗尽（上限 {self.max_calls_per_task} 次）；请基于已有证据重规划、核验或结束判断。",
            )
            self._audit(observation)
            await emit_to_sink(
                trace_sink, "tool_budget_exhausted", "MCP 调用额度耗尽",
                "当前信息目标已达到 MCP 调用上限；将保留已有证据并交由总控决定重规划、核验或最终判断。",
                {"task_id": task_id, "agent": agent_enum.value, "tool": tool_name,
                 "target_id": target_id or task_id, "limit": self.max_calls_per_task, "status": observation.status.value},
            )
            return observation
        self._task_call_counts[task_key] = self._task_call_counts.get(task_key, 0) + 1

        validation_error = self._validate_arguments(provider.descriptor.input_schema, arguments)
        if validation_error is not None:
            await emit_to_sink(
                trace_sink, "tool_validation_failed", "工具参数不符合 Schema",
                "调用前校验发现参数不符合该 MCP 工具要求，专家将收到完整错误并调整下一步。",
                {"task_id": task_id, "agent": agent_enum.value, "capability": capability,
                 "tool": provider.remote_name, "provider": provider.provider, "error": validation_error},
            )
            observation = self._observation(call_id, decision_id, task_id, agent_enum, tool_name, arguments, ToolCallStatus.FAILED, started, error=validation_error)
            self._audit(observation)
            return observation

        last_error = "tool invocation failed"
        last_status = ToolCallStatus.FAILED
        for attempt in range(2):
            try:
                await emit_to_sink(
                    trace_sink, "tool_attempt", "调用 MCP 工具",
                    f"尝试通过 {provider.provider} 提供方调用 {provider.remote_name}。",
                    {"task_id": task_id, "agent": agent_enum.value, "capability": capability,
                     "tool": provider.remote_name, "provider": provider.provider,
                     "attempt": attempt + 1, "arguments": arguments},
                )
                result = await asyncio.wait_for(provider.session.call_tool(provider.remote_name, arguments), self.timeout_seconds)
                summary = normalize_tool_result(result)
                if not summary:
                    last_error, last_status = "tool returned an empty result", ToolCallStatus.UNAVAILABLE
                    break
                result_error = tool_result_error(result, summary)
                if result_error is not None:
                    last_error = f"MCP 工具 {provider.remote_name}（提供方 {provider.provider}）返回错误：{result_error}"
                    last_status = ToolCallStatus.FAILED
                    break
                observation = self._observation(call_id, decision_id, task_id, agent_enum, tool_name, arguments, ToolCallStatus.SUCCEEDED, started, summary=summary)
                self._audit(observation)
                return observation
            except (asyncio.TimeoutError, TimeoutError) as error:
                detail = str(error) or "tool timed out"
                last_error = f"MCP 工具 {provider.remote_name}（提供方 {provider.provider}）调用超时：{detail}"
                last_status = ToolCallStatus.TIMED_OUT
            except Exception as error:
                last_error = f"MCP 工具 {provider.remote_name}（提供方 {provider.provider}）调用异常：{error}"
                last_status = ToolCallStatus.FAILED
            if attempt == 0:
                await emit_to_sink(
                    trace_sink, "tool_retry", "工具调用失败，正在重试",
                    "同一个已选择工具首次调用失败，将重试一次；失败原因会反馈给专家。",
                    {"task_id": task_id, "agent": agent_enum.value, "capability": capability,
                     "tool": provider.remote_name, "provider": provider.provider,
                     "attempt": attempt + 2, "reason": last_error},
                )
        observation = self._observation(call_id, decision_id, task_id, agent_enum, tool_name, arguments, last_status, started, error=last_error)
        self._audit(observation)
        return observation

    def select_tool_name(self, agent: str | AgentName, capability: str,
                         arguments: dict[str, Any]) -> str | None:
        """为非 ReAct 调用方从一个能力中挑出参数合同匹配的明确工具名。"""
        agent_enum = agent if isinstance(agent, AgentName) else AgentName(agent)
        try:
            self.policy.authorize(agent_enum, capability, arguments)
        except (PermissionError, ValueError):
            return None
        candidates = sorted(self.registry.providers(capability), key=lambda item: (item.provider, item.remote_name))
        for candidate in candidates:
            if self._validate_arguments(candidate.descriptor.input_schema, arguments) is None:
                return candidate.remote_name
        return None

    @staticmethod
    def _observation(call_id: str, decision_id: str, task_id: str, agent: AgentName,
                     tool: str, arguments: dict[str, Any], status: ToolCallStatus,
                     started: float, summary: str | None = None, error: str | None = None) -> ToolObservation:
        return ToolObservation(call_id=call_id, decision_id=decision_id, task_id=task_id,
            agent=agent, tool_name=tool, arguments=arguments, status=status,
            latency_ms=max(0, round((perf_counter() - started) * 1000)), result_summary=summary, error=error)

    def _audit(self, observation: ToolObservation) -> None:
        if self.audit_sink is None:
            return
        saver: Callable[[ToolObservation], Any] = getattr(self.audit_sink, "save", self.audit_sink)
        saver(observation)

    async def close(self) -> None:
        for session in self.sessions.values():
            closer = getattr(session, "close", None)
            if closer is not None:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result

    def reset_decision_budget(self, decision_id: str) -> None:
        """为一次新的执行或续跑生命周期创建独立的单任务调用额度。"""
        self._task_call_counts = {
            key: count for key, count in self._task_call_counts.items() if key[0] != decision_id
        }

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
        """执行足够严格的本地 JSON Schema 子集校验，先拦截缺失字段和明显类型错误。"""
        if not isinstance(schema, dict) or not schema:
            return None
        if schema.get("type") not in (None, "object"):
            return "tool input schema must describe an object"
        for required in schema.get("required", []):
            if required not in arguments or arguments[required] in (None, ""):
                return f"tool argument validation failed: '{required}' is a required property"
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return None
        expected_types = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}
        for key, value in arguments.items():
            descriptor = properties.get(key)
            if not isinstance(descriptor, dict) or "type" not in descriptor:
                continue
            expected = expected_types.get(descriptor["type"])
            if expected is not None and (not isinstance(value, expected) or (descriptor["type"] in {"number", "integer"} and isinstance(value, bool))):
                return f"tool argument validation failed: '{key}' must be {descriptor['type']}"
        return None
