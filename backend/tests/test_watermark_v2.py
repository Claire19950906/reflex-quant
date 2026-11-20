"""watermark_v2（投资人水印）单测。

2026-11-20: 覆盖率 25%。这模块是**泄漏溯源**的唯一手段：截图流出去以后要靠它定位
到"是哪次访问、哪个 IP"。它错一点不会报错，只会让水印失去定位能力——最典型的是
"时间粒度写成秒"，一分钟内几十个 token，审计时对不上。

测的承诺：
  · token 是 base64url(JSON)，可解、带签名、1 分钟粒度；
  · IP 只存前 12 位哈希（原文不入库）；
  · 水印插在 </body> 之前；没有 body 就追加到末尾；
  · 开关关闭（TRIDENT_WATERMARK_V2 != 1）时原文原样返回。
"""

import base64
import json

import pytest

import watermark_v2 as wm


def test_token_roundtrip_and_fields():
    tok = wm.make_watermark_token(investor_id="INV-7f3a", ip="1.2.3.4", ts=1700000000)
    dec = wm.decode_watermark_token(tok)
    assert dec["iid"] == "INV-7f3a"
    assert dec["tsm"] == 1700000000 // 60        # 分钟粒度，不是秒
    assert len(dec["iph"]) == 12                 # 哈希截断，不存原 IP
    assert "1.2.3.4" not in json.dumps(dec)
    assert len(dec["sig"]) == 16


def test_same_minute_same_token_different_minute_differs():
    a = wm.decode_watermark_token(wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000000))
    b = wm.decode_watermark_token(wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000030))
    c = wm.decode_watermark_token(wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000061))
    assert a["tsm"] == b["tsm"]                  # 同一分钟内不换水印
    assert c["tsm"] == a["tsm"] + 1


def test_token_is_urlsafe_base64_json():
    tok = wm.make_watermark_token(investor_id="I", ip="9.9.9.9", ts=1700000000)
    assert "=" not in tok                        # 去掉 padding，能直接塞进 URL/HTML
    padded = tok + "=" * (-len(tok) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded.encode()))["iid"] == "I"


def test_signature_changes_when_secret_changes(monkeypatch):
    tok1 = wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000000)
    monkeypatch.setattr(wm, "WATERMARK_SECRET", "another-secret")
    tok2 = wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000000)
    assert wm.decode_watermark_token(tok1)["sig"] != wm.decode_watermark_token(tok2)["sig"]


@pytest.mark.parametrize("bad", ["", "not-base64!!", base64.urlsafe_b64encode(b"[1,2]").decode(),
                                 base64.urlsafe_b64encode(b"\"just a string\"").decode()])
def test_decode_returns_empty_dict_for_anything_not_an_object(bad):
    # 审计脚本会拿任意字符串来解，绝不能抛异常；也不能返回 list/str 让下游 .get() 崩
    assert wm.decode_watermark_token(bad) == {}


def test_inject_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", False)
    html = "<html><body>x</body></html>"
    assert wm.inject_watermark_into_html(html, "tok") == html


def test_inject_noop_without_token(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", True)
    html = "<html><body>x</body></html>"
    assert wm.inject_watermark_into_html(html, "") == html


def test_inject_corner_before_body_close(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", True)
    tok = wm.make_watermark_token(investor_id="INV-7f3a", ip="1.2.3.4", ts=1700000000)
    html = "<html><body><h1>Hi</h1></body></html>"
    out = wm.inject_watermark_into_html(html, tok)
    assert "trident-wm" in out
    assert out.index("trident-wm") < out.index("</body>")
    assert "INV-7f3a" in out and "1.2.3." not in out   # 只显示哈希前 6 位


def test_inject_appends_when_no_body(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", True)
    tok = wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000000)
    out = wm.inject_watermark_into_html("<div>fragment</div>", tok)
    assert out.startswith("<div>fragment</div>") and "trident-wm" in out


def test_inject_repeat_and_hidden_styles(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", True)
    tok = wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000000)
    rep = wm.inject_watermark_into_html("<html><body>x</body></html>", tok, style="repeat")
    assert "trident-wm-bg" in rep and rep.count("<span>") >= 80   # 满屏，截图无处可藏
    hid = wm.inject_watermark_into_html("<html><body>x</body></html>", tok, style="hidden")
    assert "color:transparent" in hid                            # 肉眼不见，Ctrl+A 可见


def test_inject_uses_last_body_tag(monkeypatch):
    # 多段 HTML 只应插在最后一个 </body> 前
    monkeypatch.setattr(wm, "ENABLED", True)
    tok = wm.make_watermark_token(investor_id="I", ip="1.1.1.1", ts=1700000000)
    html = "<body>a</body><div>after</div><body>b</body>"
    out = wm.inject_watermark_into_html(html, tok)
    assert out.rindex("trident-wm") < out.rindex("</body>")
    assert out.startswith("<body>a</body>")


def test_inject_bad_token_returns_original(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", True)
    html = "<html><body>x</body></html>"
    assert wm.inject_watermark_into_html(html, "garbage!!") == html
