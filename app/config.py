"""从环境变量和可选 .env 文件读取的运行时配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """个人决策服务的安全本地默认运行配置。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    llm_model_id: str | None = Field(default=None, validation_alias="LLM_MODEL_ID")
    llm_api_key: SecretStr | None = Field(default=None, validation_alias="LLM_API_KEY")
    llm_base_url: str | None = Field(default=None, validation_alias="LLM_BASE_URL")
    sqlite_path: Path = Field(default=Path("var/personal_decision.db"), validation_alias="SQLITE_PATH")
    qdrant_path: Path = Field(default=Path("var/qdrant"), validation_alias="QDRANT_PATH")
    mcp_commands: list[dict[str, Any]] = Field(default_factory=list, validation_alias="MCP_COMMANDS_JSON")
    request_timeout_seconds: float = Field(default=30.0, gt=0, validation_alias="REQUEST_TIMEOUT_SECONDS")
    tool_timeout_seconds: float = Field(default=20.0, gt=0, validation_alias="TOOL_TIMEOUT_SECONDS")
    react_call_limit: int = Field(default=3, ge=1, validation_alias="REACT_CALL_LIMIT")
    replan_limit: int = Field(default=3, ge=0, le=5, validation_alias="REPLAN_LIMIT")
    hitl_timeout_seconds: int = Field(default=30, ge=1, le=120, validation_alias="HITL_TIMEOUT_SECONDS")
    hitl_request_limit: int = Field(default=2, ge=1, le=5, validation_alias="HITL_REQUEST_LIMIT")
