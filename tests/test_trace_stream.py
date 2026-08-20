"""验证实时轨迹脱敏不会破坏前端重建状态所需的公开标识。"""

from app.trace_stream import sanitize_trace_value


def test_trace_sanitizer_keeps_information_target_key_but_masks_real_secret():
    """target_key 是公开状态关联标识，不能因包含 key 子串而被遮蔽。"""
    result = sanitize_trace_value({
        "target_key": "suzhou-weather",
        "api_key": "private-value",
    })

    assert result["target_key"] == "suzhou-weather"
    assert result["api_key"] == "***"
