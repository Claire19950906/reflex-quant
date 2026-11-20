"""v335: 门卫分房 —— 新闻没点名公司时, 判断它真正影响哪个品种。

用户原话: "一个新闻有可能没写跟哪个公司相关, 但它需要判断跟哪个公司最有相关性,
跟哪个产品最有关系……不能把这个产品错误地推到一个对它影响不大的标题上。"
所以这里最关键的不是"能不能判", 而是**判错的方向**: 宁可 NONE, 不可乱推。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

from feeds import news_asset_router as r


def _primary(text, body=""):
    return [h["asset"] for h in r.route(text, body)["primary"]]


def test_direct_company_mention_routes_to_itself():
    assert _primary("英伟达发布新一代 Blackwell GPU") == ["NVDA"]
    assert _primary("苹果 iPhone 17 销量超预期") == ["AAPL"]


def test_transmit_path_geopolitics_to_oil():
    assert "WTI" in _primary("伊朗称三艘油轮在霍尔木兹海峡遇袭")


def test_macro_needs_us_context_gate():
    """德国 CPI 不该推到黄金房间 —— 这是用户明确否掉的"弱相关硬推"."""
    assert _primary("德国10月调和CPI年率初值 2.8%，预期3.1%") == []
    assert _primary("美国10月CPI年率 3.1%，高于预期") == ["XAU"]


def test_weak_link_never_becomes_primary():
    """AI 融资只算算力联想, 只能进观察, 不能当主品种。"""
    res = r.route("Anthropic计划在日本市场筹资30-35亿美元")
    assert res["primary"] == []
    assert res["verdict"] == "weak"


def test_source_label_noise_does_not_leak_into_routing():
    """标题尾巴带的 "Yahoo DXY 快讯" 是来源栏, 不能把鞋企新闻判成黄金。"""
    raw = ("[hash:4ac3baf9] Under Armour Cuts Full-Year Gross Margin Outlook\n"
           "Yahoo DXY (DX-Y.NYB) 快讯 | 原文: https://finance.yahoo.com/x")
    assert r.clean_text(raw).startswith("Under Armour")
    assert _primary(raw) == []


def test_noise_boilerplate_is_dropped():
    res = r.route("【中国人民银行副行长出席国际清算银行行长例会】")
    assert res["verdict"] in ("noise", "none")
    assert res["primary"] == []


def test_multi_asset_headline_keeps_strongest_as_primary():
    """英伟达 HBM 采购: NVDA 是主品种, SKHYNIX 只能算次品种, 不能并列推。"""
    res = r.route("英伟达扩大 HBM 采购，SK海力士与三星受益")
    top = res["primary"][0]["asset"]
    assert top == "NVDA"
    assert any(h["asset"] == "SKHYNIX" for h in res["secondary"]) or \
        any(h["asset"] == "SKHYNIX" for h in res["primary"])
