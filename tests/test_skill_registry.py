from pathlib import Path

import pytest


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


def valid_front_matter(**overrides: object) -> str:
    """Build a complete, independently controlled Skill front matter fixture."""
    fields: dict[str, object] = {
        "name": "valid-skill",
        "description": "A complete skill definition.",
        "recommended_agents": ["planner"],
        "recommended_tools": ["source_lookup"],
        "analysis_dimensions": ["scope"],
        "workflow": ["inspect"],
        "risk_checks": ["validate"],
        "completion_conditions": ["complete"],
        "output_schema": ["result"],
    }
    fields.update(overrides)
    lines = []
    for name, value in fields.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(repr(item) for item in value) + "]"
        else:
            rendered = repr(value)
        lines.append(f"{name}: {rendered}")
    return "\n".join(lines)


def write_skill(directory: Path, front_matter: str, body: str = "# Procedure\n\nExecute the procedure.", newline: str = "\n") -> None:
    """Write a real Skill artifact with a deliberately chosen line ending."""
    directory.mkdir(parents=True, exist_ok=True)
    content = f"---{newline}{front_matter}{newline}---{newline}{body}"
    (directory / "SKILL.md").write_text(content, encoding="utf-8", newline="")


def test_registry_discovers_all_eight_skill_definitions():
    """Catches a registry that skips a bundled skill directory."""
    from skills.registry import SkillRegistry

    registry = SkillRegistry(SKILLS_ROOT)
    definitions = registry.load_all()

    assert [definition.name for definition in definitions] == [
        "course-subscription-evaluator",
        "decision-retrospective",
        "evidence-verification",
        "job-offer-evaluator",
        "portfolio-review",
        "product-comparison",
        "risk-debate-moderator",
        "travel-destination-compare",
    ]


def test_loaded_definitions_expose_every_required_field():
    """Catches front matter that is accepted despite an incomplete contract."""
    from skills.registry import REQUIRED_SKILL_FIELDS, SkillRegistry

    definitions = SkillRegistry(SKILLS_ROOT).load_all()

    for definition in definitions:
        data = definition.to_dict()
        assert REQUIRED_SKILL_FIELDS <= data.keys()
        assert all(data[field] not in (None, "", [], {}) for field in REQUIRED_SKILL_FIELDS)


@pytest.mark.parametrize(
    ("front_matter", "message"),
    [
        ("name: broken\ndescription: incomplete", "missing required fields"),
        ("name: ''", "name must be a non-empty string"),
    ],
)
def test_registry_rejects_missing_or_invalid_definition(tmp_path: Path, front_matter: str, message: str):
    """Catches malformed skill definitions being silently accepted."""
    from skills.registry import SkillRegistry

    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"---\n{front_matter}\n---\nBody", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        SkillRegistry(tmp_path).load_all()


def test_registry_rejects_duplicate_skill_names(tmp_path: Path):
    """Catches duplicate names that would make lookup depend on scan order."""
    from skills.registry import SkillRegistry

    front_matter = """name: duplicate
description: A valid duplicate skill.
recommended_agents: [planner]
recommended_tools: [none]
analysis_dimensions: [scope]
workflow: [inspect]
risk_checks: [validate]
completion_conditions: [complete]
output_schema: [result]
"""
    for directory in (tmp_path / "first", tmp_path / "second"):
        directory.mkdir()
        (directory / "SKILL.md").write_text(f"---\n{front_matter}---\nBody", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate skill name: duplicate"):
        SkillRegistry(tmp_path).load_all()


def test_registry_uses_supplied_root_instead_of_current_working_directory(tmp_path: Path, monkeypatch):
    """Catches path resolution that only works when launched from the repository root."""
    from skills.registry import SkillRegistry

    monkeypatch.chdir(tmp_path)
    registry = SkillRegistry(SKILLS_ROOT)
    registry.load_all()

    assert registry.get("product-comparison").name == "product-comparison"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_registry_accepts_exclusive_front_matter_delimiters_with_common_line_endings(tmp_path: Path, newline: str):
    """Catches parsers that reject valid LF or CRLF Markdown Skill files."""
    from skills.registry import SkillRegistry

    write_skill(tmp_path / "valid", valid_front_matter(), newline=newline)

    assert SkillRegistry(tmp_path).load_all()[0].name == "valid-skill"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("--\nname: invalid\n---\nBody", "front matter must start"),
        ("--- metadata\nname: invalid\n---\nBody", "front matter must start"),
        ("---\nname: invalid\n--- extra\nBody", "front matter must end"),
        ("---\nname: invalid\nBody", "front matter must end"),
    ],
)
def test_registry_rejects_nonexclusive_or_missing_front_matter_delimiters(tmp_path: Path, content: str, message: str):
    """Catches delimiter parsing that accepts embedded or suffixed fence text."""
    from skills.registry import SkillRegistry

    directory = tmp_path / "invalid"
    directory.mkdir()
    (directory / "SKILL.md").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        SkillRegistry(tmp_path).load_all()


@pytest.mark.parametrize("separator", ["\r", "\r\r\n"])
def test_registry_rejects_nonstandard_delimiter_line_endings(tmp_path: Path, separator: str):
    """Catches delimiter recognition that accepts CR-only or malformed CR/LF lines."""
    from skills.registry import SkillRegistry

    content = f"---{separator}{valid_front_matter()}{separator}---{separator}Body"
    directory = tmp_path / "invalid_line_ending"
    directory.mkdir()
    (directory / "SKILL.md").write_text(content, encoding="utf-8", newline="")

    with pytest.raises(ValueError, match="front matter must start"):
        SkillRegistry(tmp_path).load_all()


@pytest.mark.parametrize(
    ("front_matter", "message"),
    [
        ("name: [unterminated", "invalid YAML"),
        (valid_front_matter(description=""), "description must be a non-empty string"),
        (valid_front_matter(description=7), "description must be a non-empty string"),
    ],
)
def test_registry_rejects_invalid_yaml_or_invalid_description(tmp_path: Path, front_matter: str, message: str):
    """Catches unsafe YAML acceptance and descriptions that cannot explain a Skill."""
    from skills.registry import SkillRegistry

    write_skill(tmp_path / "invalid", front_matter)

    with pytest.raises(ValueError, match=message):
        SkillRegistry(tmp_path).load_all()


@pytest.mark.parametrize(
    "field",
    [
        "recommended_agents", "recommended_tools", "analysis_dimensions", "workflow", "risk_checks",
        "completion_conditions", "output_schema",
    ],
)
@pytest.mark.parametrize("invalid_value", ["not-a-list", [""], [1]])
def test_registry_rejects_invalid_list_field_values(tmp_path: Path, field: str, invalid_value: object):
    """Catches each list contract accepting a scalar, blank entry, or non-string entry."""
    from skills.registry import SkillRegistry

    write_skill(tmp_path / "invalid", valid_front_matter(**{field: invalid_value}))

    with pytest.raises(ValueError, match=rf"{field} must be a list of non-empty strings"):
        SkillRegistry(tmp_path).load_all()


def test_registry_requires_nonempty_markdown_body(tmp_path: Path):
    """Catches a metadata-only definition that contains no executable SOP."""
    from skills.registry import SkillRegistry

    write_skill(tmp_path / "empty_body", valid_front_matter(), body=" \r\n\t ")

    with pytest.raises(ValueError, match="Markdown body must be non-empty"):
        SkillRegistry(tmp_path).load_all()


def test_registry_get_rejects_unknown_skill_name(tmp_path: Path):
    """Catches lookup that turns a caller typo into an unhelpful raw KeyError."""
    from skills.registry import SkillRegistry

    write_skill(tmp_path / "valid", valid_front_matter())
    registry = SkillRegistry(tmp_path)
    registry.load_all()

    with pytest.raises(ValueError, match="unknown skill: absent"):
        registry.get("absent")


def test_registry_does_not_scan_nested_skill_directories(tmp_path: Path):
    """Catches recursive discovery that bypasses the immediate-child registry boundary."""
    from skills.registry import SkillRegistry

    write_skill(tmp_path / "container" / "nested", valid_front_matter(name="nested_skill"))

    assert SkillRegistry(tmp_path).load_all() == []
