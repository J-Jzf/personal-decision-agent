from mcp.adapters import normalize_tool_result


def test_all_mcp_tool_results_keep_the_first_sixteen_thousand_characters():
    """统一归一化边界应为 16,000 字符，而非工具类型特例。"""
    result = {"content": [{"text": "a" * 20000}]}

    normalized = normalize_tool_result(result)

    assert len(normalized) == 16000
    assert normalized == "a" * 16000
