"""把用户自然语言中的常见相对日期补充为供模型使用的上海具体日期。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def build_temporal_context(text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """解析一段用户文本中的相对日期，返回不修改原文的结构化时间上下文。"""
    current = _as_shanghai_time(now)
    expressions: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def append(item: dict[str, str]) -> None:
        """按原词和日期去重，避免“这周末”被多个规则重复记录。"""
        key = (item["raw"], item["kind"], item.get("date") or item.get("start_date", ""))
        if key not in seen:
            expressions.append(item)
            seen.add(key)

    for raw, offset in (("今天", 0), ("明天", 1), ("后天", 2)):
        if raw in text:
            append({"raw": raw, "kind": "date", "date": (current.date() + timedelta(days=offset)).isoformat()})

    weekend_patterns = (
        (r"(?:这周末|本周末)", 0),
        (r"下周末", 1),
        (r"(?<![本这下])周末", 0),
    )
    for pattern, week_offset in weekend_patterns:
        for match in re.finditer(pattern, text):
            monday = current.date() - timedelta(days=current.weekday()) + timedelta(days=7 * week_offset)
            append({
                "raw": match.group(0), "kind": "date_range",
                "start_date": (monday + timedelta(days=5)).isoformat(),
                "end_date": (monday + timedelta(days=6)).isoformat(),
            })

    weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    for match in re.finditer(r"(?P<prefix>本周|这周|下周)(?P<weekday>[一二三四五六日天])", text):
        monday = current.date() - timedelta(days=current.weekday())
        if match.group("prefix") == "下周":
            monday += timedelta(days=7)
        append({
            "raw": match.group(0), "kind": "date",
            "date": (monday + timedelta(days=weekdays[match.group("weekday")])).isoformat(),
        })

    return {
        "timezone": "Asia/Shanghai",
        "reference_date": current.date().isoformat(),
        "expressions": expressions,
    }


def merge_temporal_context(existing: object, text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """将新用户文本的日期解析并入既有上下文，以便 HITL 多次补充也能被模型理解。"""
    generated = build_temporal_context(text, now=now)
    prior = existing if isinstance(existing, dict) else {}
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in [*(prior.get("expressions", []) if isinstance(prior.get("expressions"), list) else []), *generated["expressions"]]:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("raw", "kind")):
            continue
        key = (item["raw"], item["kind"], str(item.get("date") or item.get("start_date") or ""))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return {**generated, "expressions": merged}


def birth_year_range_from_age(age: int, reference_date: str) -> str:
    """把没有生日信息的周岁转成保守出生年份范围，避免编造精确出生日期。"""
    year = date.fromisoformat(reference_date).year
    return f"{year - age - 1}-{year - age}"


def _as_shanghai_time(now: datetime | None) -> datetime:
    """将测试传入或系统当前时刻统一转换为上海时区。"""
    value = now or datetime.now(SHANGHAI_TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TIMEZONE)
    return value.astimezone(SHANGHAI_TIMEZONE)
