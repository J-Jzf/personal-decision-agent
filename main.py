"""面向 PowerShell 的服务入口，并提供不启动 HTTP 服务的初始化自检。"""

from __future__ import annotations

import argparse
import asyncio

from app.config import Settings
from app.container import build_services


def check() -> int:
    services = build_services(Settings())
    async def inspect_services():
        """在同一事件循环中发现并关闭 MCP 资源，避免 Windows 子进程跨循环异常。"""
        try:
            return await services.gateway.discover()
        finally:
            await services.gateway.close()

    tools = asyncio.run(inspect_services())
    capabilities = sorted({item.capability for item in tools})
    print(f"SQLite initialized: {services.database.path.resolve()}")
    print(f"Skills discovered ({len(services.skills.list())}): {', '.join(item.name for item in services.skills.list())}")
    print(f"MCP capabilities: {', '.join(capabilities) if capabilities else 'none configured (typed unavailable fallback active)'}")
    for provider, error in getattr(services.gateway, "discovery_errors", {}).items():
        print(f"MCP discovery failed [{provider}]: {error}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal Decision Agent")
    parser.add_argument("--check", action="store_true", help="initialize dependencies without starting HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    if arguments.check:
        return check()
    import uvicorn
    uvicorn.run("app.main:app", host=arguments.host, port=arguments.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
