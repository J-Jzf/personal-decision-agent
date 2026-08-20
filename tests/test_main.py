"""验证命令行自检不会跨事件循环关闭 MCP 资源。"""

from __future__ import annotations

import asyncio

import main


class _Gateway:
    """记录发现和关闭所处事件循环的测试替身。"""

    def __init__(self) -> None:
        self.discover_loop = None
        self.close_loop = None

    async def discover(self):
        """记录发现阶段的事件循环。"""
        self.discover_loop = asyncio.get_running_loop()
        return []

    async def close(self) -> None:
        """记录关闭阶段的事件循环。"""
        self.close_loop = asyncio.get_running_loop()


def test_check_discovers_and_closes_gateway_in_one_event_loop(monkeypatch) -> None:
    """自检必须在同一个事件循环中完成 MCP 发现和关闭。"""
    gateway = _Gateway()
    services = type("Services", (), {
        "gateway": gateway,
        "database": type("Database", (), {"path": __import__("pathlib").Path("decision.db")})(),
        "skills": type("Skills", (), {"list": lambda self: []})(),
    })()
    monkeypatch.setattr(main, "build_services", lambda _: services)

    assert main.check() == 0
    assert gateway.discover_loop is gateway.close_loop
