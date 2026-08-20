from datetime import datetime
from zoneinfo import ZoneInfo


def test_relative_weekend_is_converted_to_a_shanghai_date_range():
    """相对周末必须以可注入的上海时刻转换成供模型使用的具体日期。"""
    from app.temporal_context import build_temporal_context

    context = build_temporal_context(
        "这周末，上海和贵州选择一个城市旅游。",
        now=datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert context["timezone"] == "Asia/Shanghai"
    assert context["reference_date"] == "2026-08-19"
    assert context["expressions"] == [{
        "raw": "这周末", "kind": "date_range",
        "start_date": "2026-08-22", "end_date": "2026-08-23",
    }]


def test_relative_tomorrow_keeps_original_words_and_a_concrete_date():
    """规范化上下文不能覆盖用户原句，并且明天必须具有唯一的日期。"""
    from app.temporal_context import build_temporal_context

    context = build_temporal_context(
        "明天去看房是否合适？",
        now=datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert context["expressions"] == [{
        "raw": "明天", "kind": "date",
        "date": "2026-08-20",
    }]
