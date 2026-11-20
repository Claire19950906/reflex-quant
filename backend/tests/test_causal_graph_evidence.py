"""causal_graph 的归一化与证据体积用例。

这个模块把 LLM 的因果链写进 `asset_driver_nodes`，它的两种错法都是"事后才炸"：
- **evidence 体积失控**：每次 upsert 把旧 evidence 重新序列化 → 列表元素本身是 JSON 字符串
  → 每轮多一层转义、体积约 ×2。实测 13 个节点各 536,519,911 字节，整库膨胀到 3.66GB，
  `/api/analysis/kg-stats` 稳定 60s+ 超时。
- **核验元数据被合并抹掉**：旧 `_merge_evidence` 只返回 chain 列表，_status/_fail/_fact_ids
  全丢 → 被事实否证过的机制悄悄变回「未确认」（954 个活跃节点里 861 个 UNCONFIRMED）。

所以这里重点钉：链元素必须拆平、体积必须有硬上限、合并必须保留旧元数据。
"""

import json

import pytest

import causal_graph as cg


# ── 数值与枚举归一 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (0.3, 0.3), ("0.8", 0.8), (1.9, 1.0), (-0.5, 0.0), (None, 0.5), ("abc", 0.5),
])
def test_clamp_stays_in_unit_interval(raw, expect):
    assert cg._clamp(raw) == pytest.approx(expect)


def test_clamp_default_is_configurable():
    assert cg._clamp("垃圾", default=0.2) == 0.2


@pytest.mark.parametrize("raw,expect", [
    ("涨", "up"), ("上涨", "up"), ("看涨", "up"), ("+", "up"), ("1", "up"),
    ("跌", "down"), ("利空", "down"), ("-1", "down"), ("-", "down"),
    ("不定", None), ("", None), (None, None), ("说不清", None),
])
def test_norm_expect(raw, expect):
    # 预期端方向是核验的基准：反向机制（实际利率↑→金价↓）靠它才不会系统性判反
    assert cg._norm_expect(raw) == expect


def test_norm_label_folds_fullwidth_and_case():
    assert cg._norm_label("  美联储 降息 ") == "美联储降息"
    # 已知口径：只做全角标点→半角 + 小写，全角字母/数字不转换（要的就是"同形同键"，不是转义）
    assert cg._norm_label("ＡＢＣ（测试）") == "ａｂｃ(测试)"
    assert cg._norm_label("") == ""


@pytest.mark.parametrize("raw,expect", [
    ("现象：金价下跌", "金价下跌"),
    ("因为: 利率上行", "利率上行"),          # 半角冒号也要剥
    ("Root cause: 需求萎缩", "需求萎缩"),     # 大小写不敏感
    ("最底层", ""),                           # 裸前缀词也剥：这种层本来就没内容，剥完为空是对的
    ("结果:", ""),                            # 只剩标签的空层同样清空，不留"结果:"这种空节点
    ("地缘冲突升级", "地缘冲突升级"),
])
def test_clean_chain_item_strips_layer_prefixes(raw, expect):
    assert cg._clean_chain_item(raw) == expect


@pytest.mark.parametrize("name,expect", [
    ("", True), ("N/A", True), ("none", True), ("服务端兜底因素", True), ("兜底因素", True),
    ("美联储降息", False),
])
def test_is_placeholder(name, expect):
    assert cg._is_placeholder(name) is expect


# ── 因素名提取 ───────────────────────────────────────────────────────────

def test_parse_factors_from_all_supported_shapes():
    assert cg._parse_factors(["降息", "通胀"]) == ["降息", "通胀"]
    assert cg._parse_factors([{"factor": "降息"}, {"name": "通胀"}]) == ["降息", "通胀"]
    assert cg._parse_factors({"降息": 0.8, "通胀": 0.6}) == ["降息", "通胀"]
    assert cg._parse_factors({"宏观": ["降息", {"key": "通胀"}]}) == ["降息", "通胀"]
    assert cg._parse_factors('["降息", "通胀"]') == ["降息", "通胀"]


@pytest.mark.parametrize("raw", [None, "", "不是 JSON", 42, []])
def test_parse_factors_empty_cases(raw):
    assert cg._parse_factors(raw) == []


def test_parse_factors_drops_placeholders():
    assert cg._parse_factors(["降息", "N/A", "服务端兜底因素"]) == ["降息"]


def test_norm_asset_upper_strips():
    assert cg._norm_asset(" btc ") == "BTC"
    assert cg._norm_asset(None) == ""


# ── evidence 体积：拆平 + 硬上限 ─────────────────────────────────────────

def test_flatten_chain_items_unwraps_nested_serialized_evidence():
    # 套娃是体积翻倍的根源：外层每序列化一次，内层引号/反斜杠就多一层。
    # 会被拆的只有 {"chain": [...]} 这一种形状（dict 里带 chain 列表）。
    nested = json.dumps({"chain": ["地缘冲突", json.dumps({"chain": ["油价上行"]}, ensure_ascii=False)]},
                        ensure_ascii=False)
    out = cg._flatten_chain_items([nested])
    assert out == ["地缘冲突", "油价上行"]


def test_flatten_leaves_list_of_strings_alone():
    # 已知口径：列表套列表不会被拆（只认 dict.chain），实际写入路径也只产生 dict 形状
    inner = json.dumps([json.dumps({"chain": ["A"]}, ensure_ascii=False)], ensure_ascii=False)
    assert cg._flatten_chain_items([inner]) == [inner]


def test_flatten_keeps_plain_text_and_broken_json_untouched():
    assert cg._flatten_chain_items(["正常句子", "{\"坏 json\":"]) == ["正常句子", "{\"坏 json\":"]


def test_flatten_recursion_is_depth_limited():
    # 注意别真的套 32 层：每层 json.dumps 会给内层加一次转义，体积约 ×2，
    # 套到 30+ 层在测试里等于申请几个 GB 内存（写这行注释就是因为踩过）。
    # 这里直接把 depth 顶到上限，验证"到顶就不再往里拆"。
    payload = json.dumps({"chain": ["最内层"]}, ensure_ascii=False)
    out = cg._flatten_chain_items([payload], _depth=cg._MAX_EVIDENCE_NEST_DEPTH)
    assert out == [payload]


def test_truncate_bytes_does_not_split_multibyte_chars():
    s = "金" * 100
    out = cg._truncate_bytes(s, 40)
    assert out.endswith("...[TRUNCATED]")
    assert "金" in out
    out.encode("utf-8")                       # 能编码即证明没切碎多字节字符


def test_cap_evidence_leaves_small_payload_alone():
    text = json.dumps(["短证据"], ensure_ascii=False)
    assert cg._cap_evidence(text) == text
    assert cg._cap_evidence("") == ""


def test_cap_evidence_drops_oldest_and_keeps_newest():
    items = [("旧" + str(i)) * 300 for i in range(50)]        # 远超 64KB
    text = json.dumps(items, ensure_ascii=False)
    out = cg._cap_evidence(text)
    assert len(out.encode("utf-8")) <= cg._MAX_EVIDENCE_BYTES
    kept = json.loads(out)
    assert kept[-1] == items[-1]                              # 最新那条必须留下


def test_cap_evidence_truncates_a_single_oversized_item():
    # v301-fix：单条就超限时旧实现因 `out` 为空而短路放行，cap 形同虚设
    huge = "巨" * (cg._MAX_EVIDENCE_BYTES // 2)
    out = cg._cap_evidence(json.dumps([huge], ensure_ascii=False))
    assert len(out.encode("utf-8")) <= cg._MAX_EVIDENCE_BYTES
    assert "TRUNCATED" in out


# ── evidence 解析 / 序列化 / 合并 ────────────────────────────────────────

def test_parse_evidence_empty_and_plain_string():
    empty = cg._parse_evidence("")
    assert empty["chain"] == [] and empty["_status"] == cg._STATUS_UNCONFIRMED
    plain = cg._parse_evidence("不是 JSON 的一句话")
    assert plain["chain"] == ["不是 JSON 的一句话"]


def test_parse_evidence_legacy_list_is_wrapped():
    ev = cg._parse_evidence(json.dumps(["第一层", "第二层"], ensure_ascii=False))
    assert ev["chain"] == ["第一层", "第二层"]
    assert ev["_status"] == cg._STATUS_UNCONFIRMED


def test_parse_evidence_keeps_verification_metadata():
    ev = cg._parse_evidence(json.dumps({
        "chain": ["利率上行", "金价承压"], "_status": "CONFIRMED", "_fail": None,
        "_fact_ids": [11, 12], "_position": "cause", "_expect": "down",
        "_tier": "core", "_source": "chain_lib:7",
        "_cond_n": 33, "_cond_hits": 21, "_cond_ratio": 0.636,
    }, ensure_ascii=False))
    assert ev["_status"] == "CONFIRMED"
    assert ev["_fact_ids"] == [11, 12]
    assert ev["_expect"] == "down" and ev["_expect_src"] == "inferred"
    assert ev["_tier"] == "core" and ev["_source"] == "chain_lib:7"
    assert ev["_cond_n"] == 33 and ev["_cond_hits"] == 21


def test_serialize_refuses_dict_passed_as_chain():
    # v430 事故：把 evidence 字典误当 chain 传进去，静默写出一串键名
    with pytest.raises(TypeError):
        cg._serialize_evidence({"chain": ["x"], "_status": "CONFIRMED"})


def test_serialize_filters_empty_items_and_records_expect_source():
    out = json.loads(cg._serialize_evidence(["  ", "有效", ""], status="CONFIRMED", expect="up"))
    assert out["chain"] == ["有效"]
    assert out["_status"] == "CONFIRMED"
    assert out["_expect"] == "up" and out["_expect_src"] == "inferred"


def test_serialize_writes_cond_block_only_when_samples_exist():
    no_samples = json.loads(cg._serialize_evidence(["x"], cond={"n": 0, "hits": 0}))
    assert "_cond_n" not in no_samples
    with_samples = json.loads(cg._serialize_evidence(
        ["x"], cond={"n": 33, "hits": 21, "ratio": 0.636, "chain_ids": [7],
                     "base_rate": 0.64, "edge": -0.004,
                     "oos_n": 16, "oos_hits": 11, "oos_ratio": 0.688, "is_ratio": 0.6}))
    assert with_samples["_cond_n"] == 33 and with_samples["_cond_chains"] == [7]
    assert with_samples["_cond_base_rate"] == pytest.approx(0.64)   # 净增益要能算出来
    assert with_samples["_cond_oos_n"] == 16


def test_merge_keeps_chain_union_and_old_verification_status():
    old = cg._serialize_evidence(["A", "B"], status="CONFIRMED", fact_ids=[1])
    new = cg._serialize_evidence(["B", "C"], status="UNCONFIRMED", fact_ids=[2])
    merged = cg._parse_evidence(cg._merge_evidence(old, new))
    assert merged["chain"] == ["A", "B", "C"]
    # v424 事故：合并把 _status/_fact_ids 抹掉 → 已被否证的机制变回"未确认"
    assert merged["_status"] == "CONFIRMED"
    assert merged["_fact_ids"] == [1]


def test_merge_does_not_inflate_size_across_rounds():
    ev = cg._serialize_evidence(["地缘冲突升级", "油价上行"], status="CONFIRMED")
    sizes = []
    for _ in range(5):
        ev = cg._merge_evidence(ev, cg._serialize_evidence(["地缘冲突升级"]))
        sizes.append(len(ev.encode("utf-8")))
    # 不拆平的话每轮多一层转义、体积约 ×2
    assert max(sizes) <= sizes[0] * 1.2


def test_merge_falls_back_to_new_text_when_nothing_parses():
    merged = cg._merge_evidence("", "无结构的新证据")
    assert "无结构的新证据" in merged


def test_chain_text_joins_items_for_keyword_matching():
    assert cg._chain_text({"chain": ["利率上行", "金价承压"]}) == "利率上行 金价承压"
    assert cg._chain_text({}) == ""


# ── 链匹配（二元组包含度）────────────────────────────────────────────────

def test_bigrams_ignore_punctuation_and_spaces():
    assert cg._bigrams("地缘冲突，升级！") == cg._bigrams("地缘冲突升级")
    assert cg._bigrams("") == set()


def test_contain_ratio_bounds():
    assert cg._contain_ratio("地缘冲突", "地缘冲突升级引发油价上行") == pytest.approx(1.0)
    assert cg._contain_ratio("", "任意") == 0.0
    assert cg._contain_ratio("完全无关的句子", "地缘冲突") < 0.2


# ── 节点位置分类 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,core,out_cnt,in_cnt,expect", [
    ("asset", False, 0, 3, cg._POSITION_EFFECT),      # 资产节点永远是果端
    ("driver", True, 1, 0, cg._POSITION_CAUSE),       # core 有出边即因端
    ("driver", True, 0, 0, cg._POSITION_MIDDLE),
    ("driver", False, 2, 1, cg._POSITION_MIDDLE),
    ("driver", False, 2, 0, cg._POSITION_CAUSE),
    ("driver", False, 0, 2, cg._POSITION_EFFECT),
    ("driver", False, 0, 0, cg._POSITION_MIDDLE),     # 孤立节点
])
def test_classify_position(kind, core, out_cnt, in_cnt, expect):
    assert cg._classify_position(kind, core, out_cnt, in_cnt) == expect


# ── 指标关键词 ───────────────────────────────────────────────────────────

def test_indicator_keywords_come_from_alias_table():
    kws = cg._extract_indicator_keywords("dxy")
    assert "美元" in kws and "美元指数" in kws and "dxy" in kws
    assert cg._extract_indicator_keywords("") == []


def test_chain_contains_indicator_uses_aliases_case_insensitively():
    assert cg._chain_contains_indicator("美元指数走弱，利好金价", "DXY") is True
    assert cg._chain_contains_indicator("美元指数走弱", "fedrate") is False


# ── 方向词 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ("金价上涨", "up"), ("油价下跌", "down"),
    ("涨跌互现", None),                    # 双向词都有 → 不给方向
    ("横盘", None), ("", None),
])
def test_direction_of(text, expect):
    assert cg._direction_of(text) == expect
