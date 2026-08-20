"""从 Markdown 文件加载、校验并选择可执行决策 Skill SOP。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml


REQUIRED_SKILL_FIELDS = frozenset(
    {
        "name",
        "description",
        "recommended_agents",
        "recommended_tools",
        "analysis_dimensions",
        "workflow",
        "risk_checks",
        "completion_conditions",
        "output_schema",
    }
)

_LIST_FIELDS = REQUIRED_SKILL_FIELDS - {"name", "description"}
_DELIMITER_PATTERN = re.compile(r"(?m)^---(?:\r?\n|\Z)")
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class SkillDefinition:
    """已校验、可执行的 Skill SOP 及其供模型参考的领域元数据。"""

    name: str
    description: str
    recommended_agents: list[str]
    recommended_tools: list[str]
    analysis_dimensions: list[str]
    workflow: list[str]
    risk_checks: list[str]
    completion_conditions: list[str]
    output_schema: list[str]
    body: str
    source_path: Path

    def to_dict(self) -> dict[str, Any]:
        """返回不包含解析器内部字段的公开 front matter 契约。"""
        return {
            field: getattr(self, field)
            for field in sorted(REQUIRED_SKILL_FIELDS)
        }


class SkillRegistry:
    """以文件系统目录为根、支持确定性加载与按名称查询的 Skill 注册表。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._definitions: dict[str, SkillDefinition] = {}

    def load_all(self) -> list[SkillDefinition]:
        """扫描一级子目录，并以扫描结果整体替换已加载注册表。"""
        if not self.root.is_dir():
            raise ValueError(f"skill registry root does not exist or is not a directory: {self.root}")

        loaded: dict[str, SkillDefinition] = {}
        for directory in sorted((path for path in self.root.iterdir() if path.is_dir()), key=lambda path: path.name):
            skill_file = directory / "SKILL.md"
            if not skill_file.is_file():
                continue
            definition = self._parse_file(skill_file)
            if definition.name in loaded:
                raise ValueError(f"duplicate skill name: {definition.name}")
            loaded[definition.name] = definition

        self._definitions = loaded
        return self.list()

    def list(self) -> list[SkillDefinition]:
        """按稳定的名称顺序返回全部 Skill 定义。"""
        return [self._definitions[name] for name in sorted(self._definitions)]

    def get(self, name: str) -> SkillDefinition:
        """按名称获取 Skill；找不到时提供便于调用者和作者定位的问题信息。"""
        try:
            return self._definitions[name]
        except KeyError as error:
            raise ValueError(f"unknown skill: {name}") from error

    @staticmethod
    def _parse_file(path: Path) -> SkillDefinition:
        with path.open("r", encoding="utf-8", newline="") as skill_file:
            text = skill_file.read()
        opening = _DELIMITER_PATTERN.match(text)
        if opening is None:
            raise ValueError(f"invalid skill definition in {path}: front matter must start with ---")
        closing = _DELIMITER_PATTERN.search(text, opening.end())
        if closing is None:
            raise ValueError(f"invalid skill definition in {path}: front matter must end with ---")
        raw_front_matter = text[opening.end():closing.start()]
        body = text[closing.end():].strip()
        if not body:
            raise ValueError(f"invalid skill definition in {path}: Markdown body must be non-empty")
        try:
            metadata = yaml.safe_load(raw_front_matter)
        except yaml.YAMLError as error:
            raise ValueError(f"invalid skill definition in {path}: invalid YAML: {error}") from error
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid skill definition in {path}: front matter must be a mapping")
        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"invalid skill definition in {path}: name must be a non-empty string")
        if not _SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid skill definition in {path}: name may only contain lowercase letters, numbers, and hyphens")
        missing = sorted(REQUIRED_SKILL_FIELDS - metadata.keys())
        if missing:
            raise ValueError(f"invalid skill definition in {path}: missing required fields: {', '.join(missing)}")
        description = metadata["description"]
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"invalid skill definition in {path}: description must be a non-empty string")
        for field in _LIST_FIELDS:
            value = metadata[field]
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"invalid skill definition in {path}: {field} must be a list of non-empty strings")
        return SkillDefinition(
            **{field: metadata[field] for field in REQUIRED_SKILL_FIELDS},
            body=body,
            source_path=path,
        )
