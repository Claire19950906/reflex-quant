"""v170 _parse_result 端到端验证：闭包 introspection + 5 个真实场景。"""
import inspect
import json
import re
import textwrap

from engine import ai_worker


def _exec_parse_result():
    """提取闭包内 _parse_result 的定义，exec 到独立命名空间后返回函数。"""
    src = inspect.getsource(ai_worker._call_llm_sync)
    m = re.search(
        r"( {4}def _parse_result\(raw_text: str\) -> Dict\[str, Any\]:.*?)(?=\n {4}# =|\n {4}# -|\Z)",
        src, re.DOTALL,
    )
    if not m:
        raise RuntimeError("找不到 _parse_result 闭包定义")
    body = textwrap.dedent(m.group(1))
    ns = {
        "re": re,
        "json": json,
        "Dict": dict,
        "Any": object,
        "List": list,
        "Optional": type(None),
        "_sanitize_llm_result": ai_worker._sanitize_llm_result,
        "_extract_cross_net_from_text": ai_worker._extract_cross_net_from_text,
    }
    exec(body, ns)
    return ns["_parse_result"]


_parse_result = _exec_parse_result()


def test_e2e_m3_long_chinese():
    """MiniMax-M3 长中文推演（无 JSON）→ Fallback 2.5 应抽到时间和来源"""
    text = (
        "【事件溯源】\n"
        "根据路透社报道，2026年9月19日 22:00 美联储紧急宣布将联邦基金利率目标区间下调50个基点。\n"
        "这是2026年以来首次降息，标志着美联储货币政策的重大转向。\n"
        "美联储主席鲍威尔在新闻发布会上表示，通胀已显著缓和至2.1%。"
    )
    r = _parse_result(text)
    assert r["cross_net_first_seen_ts"] == "2026-09-19 22:00:00", r
    assert "路透社" in r["cross_net_source"], r
    print(f"  ✓ M3 长推演 → ts={r['cross_net_first_seen_ts']!r} src={r['cross_net_source']!r}")


def test_e2e_legit_json_passes():
    """合法 JSON 应原样透传 cross_net_*"""
    text = json.dumps({
        "reasoning_path": "正常LLM输出",
        "sentiment_score": 0.5,
        "suggested_action": "BUY",
        "reasoning": "好",
        "cross_net_first_seen_ts": "2026-09-19 22:00:00",
        "cross_net_source": "美联储官网新闻稿",
        "net_origin_info": "测试",
    }, ensure_ascii=False)
    r = _parse_result(text)
    assert r["cross_net_first_seen_ts"] == "2026-09-19 22:00:00"
    assert r["cross_net_source"] == "美联储官网新闻稿"
    assert r["net_origin_info"] == "测试"
    print(f"  ✓ JSON 直传 → ts={r['cross_net_first_seen_ts']!r} src={r['cross_net_source']!r}")


def test_e2e_fallback3_has_cross_net_placeholders():
    """fallback3 关键词推断 → 必须带 cross_net 三字段占位（v170 全网溯源补丁）"""
    text = "美联储降息"  # 短文本，json.loads 失败，regex 也匹配不到 → 落 fallback3
    r = _parse_result(text)
    assert r.get("reasoning_path", "").startswith("关键词推断"), r
    assert "cross_net_first_seen_ts" in r
    assert "cross_net_source" in r
    assert "net_origin_info" in r
    assert r["cross_net_first_seen_ts"] == ""
    assert r["cross_net_source"] == ""
    assert r["net_origin_info"] == ""
    print(f"  ✓ Fallback3 占位 → ts={r['cross_net_first_seen_ts']!r} src={r['cross_net_source']!r} noi={r['net_origin_info']!r}")


def test_e2e_m3_only_source_no_ts():
    """MiniMax-M3 文本：只有来源没有时间 → 应填 cn_src, noi，不填 cn_ts"""
    text = "据新华社消息，美联储今晚紧急降息50个基点。"
    r = _parse_result(text)
    assert r["cross_net_first_seen_ts"] == "", r
    assert "新华社" in r["cross_net_source"], r
    print(f"  ✓ M3 仅来源 → ts={r['cross_net_first_seen_ts']!r} src={r['cross_net_source']!r}")


def test_e2e_garbage_no_signal():
    """完全无信号的文本 → 三字段全空"""
    text = "今天天气晴朗"
    r = _parse_result(text)
    assert r["cross_net_first_seen_ts"] == ""
    assert r["cross_net_source"] == ""
    assert r["net_origin_info"] == ""
    print(f"  ✓ 无信号 → 三个字段全空")


if __name__ == "__main__":
    test_e2e_m3_long_chinese()
    test_e2e_legit_json_passes()
    test_e2e_fallback3_has_cross_net_placeholders()
    test_e2e_m3_only_source_no_ts()
    test_e2e_garbage_no_signal()
    print("\n🎉 5/5 _parse_result 端到端场景全部通过")