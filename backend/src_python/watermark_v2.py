"""
Trident v300 — 动态水印（v2）
=============================

相比 v1 的差异：
  - 每次访问都生成新水印（含 IP + 时间戳 + 投资人 ID）
  - 可注入到 HTML 响应正文（不是只在 header）
  - 截图泄漏时，肉眼可见地定位到具体访问者

用法：
    from watermark_v2 import inject_watermark_into_html, make_watermark_token

    token = make_watermark_token(investor_id="INV-7f3a", ip="...", ts=...)
    html = inject_watermark_into_html(html_body, token)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

ENABLED = os.getenv("TRIDENT_WATERMARK_V2") == "1"
WATERMARK_SECRET = os.getenv("TRIDENT_WATERMARK_SECRET", "trident-wm-secret-change-me")


def make_watermark_token(*, investor_id: str, ip: str, ts: Optional[float] = None) -> str:
    """生成水印 token。格式 base64url(JSON({investor_id, ip_hash, ts_minute, sig}))。"""
    if ts is None:
        ts = time.time()
    ts_minute = int(ts // 60)  # 1 分钟粒度，1 分钟内同一 IP 同一水印（截图定位够用）
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:12]
    payload = {
        "iid": investor_id,
        "iph": ip_hash,
        "tsm": ts_minute,
    }
    sig = hmac.new(
        WATERMARK_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    payload["sig"] = sig
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_watermark_token(token: str) -> dict:
    """反向解码（用于审计时核对）。"""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        return json.loads(raw)
    except Exception:
        return {}


def inject_watermark_into_html(html: str, token: str, *, style: str = "corner") -> str:
    """把水印 token 注入到 HTML body。style: corner | repeat | hidden。"""
    if not ENABLED or not token:
        return html

    decoded = decode_watermark_token(token)
    if not decoded:
        return html

    label = f"{decoded.get('iid', '?')} · {decoded.get('iph', '?')[:6]}"

    if style == "hidden":
        # 肉眼不可见，但 Ctrl+A 选中文字会看到
        style_tag = (
            "<style>.trident-wm{color:transparent;font-size:1px;"
            "position:absolute;left:-9999px}</style>"
        )
        tag = f'<span class="trident-wm">{label}</span>'
    elif style == "repeat":
        # 满屏半透明水印（截图时无处可藏）
        style_tag = (
            "<style>"
            ".trident-wm-bg{position:fixed;inset:0;pointer-events:none;z-index:9999;"
            "background:repeating-linear-gradient(-30deg,transparent 0 80px,"
            "rgba(0,0,0,.04) 80px 160px);"
            "color:rgba(0,0,0,.15);font:12px monospace;padding:8px;"
            "display:flex;flex-wrap:wrap;gap:24px;align-content:flex-start;}"
            ".trident-wm-bg span{margin:2px 6px;}"
            "</style>"
        )
        tag = (f'<div class="trident-wm-bg">'
               + "".join(f"<span>{label}</span>" for _ in range(80))
               + "</div>")
    else:  # corner（默认）
        style_tag = (
            "<style>"
            ".trident-wm{position:fixed;right:8px;bottom:8px;z-index:9999;"
            "font:11px/1.3 monospace;color:rgba(0,0,0,.45);"
            "background:rgba(255,255,255,.7);padding:2px 6px;border-radius:3px;"
            "user-select:none;pointer-events:none;}"
            "</style>"
        )
        tag = f'<div class="trident-wm">{label}</div>'

    inject = style_tag + tag
    # 插在 </body> 之前；没有就插到末尾
    if "</body>" in html.lower():
        idx = html.lower().rfind("</body>")
        return html[:idx] + inject + html[idx:]
    return html + inject


if __name__ == "__main__":
    os.environ["TRIDENT_WATERMARK_V2"] = "1"
    import importlib
    import watermark_v2
    importlib.reload(watermark_v2)

    tok = watermark_v2.make_watermark_token(investor_id="INV-7f3a", ip="1.2.3.4")
    print("token:", tok)
    print("decoded:", watermark_v2.decode_watermark_token(tok))
    sample = "<html><body><h1>Test</h1></body></html>"
    print("with wm:", watermark_v2.inject_watermark_into_html(sample, tok, style="corner")[:200])
