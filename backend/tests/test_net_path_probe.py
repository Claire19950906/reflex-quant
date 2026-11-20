# -*- coding: utf-8 -*-
"""tests/test_net_path_probe.py — 出网通路体检 (v324)

全部离线: 只测纯函数与文件写入逻辑, 不真的发网络请求。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import net_path_probe as npp


# ── 端口探测 ──────────────────────────────────────────────────────────────
def test_port_open_returns_bool():
    assert isinstance(npp._port_open(1), bool)
    assert isinstance(npp._port_open(65000), bool)


def test_port_open_refused_port_is_false():
    # 1 端口几乎不可能被监听
    assert npp._port_open(1) is False


# ── 本地代理探测 ──────────────────────────────────────────────────────────
def test_detect_local_proxy_shape():
    out = npp.detect_local_proxy()
    assert "ports" in out and "processes" in out
    assert isinstance(out["ports"], list)
    assert isinstance(out["processes"], list)


# ── 建议文本 ──────────────────────────────────────────────────────────────
def test_suggest_proxy_when_port_found():
    txt = npp.suggest_proxy_line({"ports": [7890], "processes": []})
    assert "7890" in txt
    assert "HTTP_PROXY=http://127.0.0.1:7890" in txt
    assert str(npp.ENV_PATH) in txt
    assert "kickstart" in txt


def test_suggest_proxy_when_no_port():
    txt = npp.suggest_proxy_line({"ports": [], "processes": []})
    assert "未检测到本地代理端口" in txt
    assert "kickstart" in txt


# ── 写 ~/.trident/env ────────────────────────────────────────────────────
def test_write_env_proxy_updates_only_proxy_keys(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text(
        "# 注释保留\n"
        "HTTP_PROXY=\n"
        "HTTPS_PROXY=\n"
        "ALL_PROXY=\n"
        "OTHER_KEY=keepme\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(npp, "ENV_PATH", env)
    out = npp.write_env_proxy("http://127.0.0.1:7890")
    assert out["ok"] is True
    text = env.read_text(encoding="utf-8")
    assert "HTTP_PROXY=http://127.0.0.1:7890" in text
    assert "HTTPS_PROXY=http://127.0.0.1:7890" in text
    assert "ALL_PROXY=" in text
    assert "OTHER_KEY=keepme" in text, "不能动无关配置"
    assert "# 注释保留" in text, "注释必须保留"
    assert Path(out["backup"]).exists(), "写前必须备份"


def test_write_env_proxy_without_url_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(npp, "ENV_PATH", tmp_path / "env")
    out = npp.write_env_proxy("")
    assert out["ok"] is False


def test_write_env_proxy_creates_file_when_absent(tmp_path, monkeypatch):
    env = tmp_path / "sub" / "env"
    monkeypatch.setattr(npp, "ENV_PATH", env)
    out = npp.write_env_proxy("http://127.0.0.1:1080")
    assert out["ok"] is True
    assert env.exists()
    assert "HTTP_PROXY=http://127.0.0.1:1080" in env.read_text(encoding="utf-8")


def test_write_env_proxy_does_not_duplicate_keys(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text("HTTP_PROXY=\nHTTP_PROXY=\n", encoding="utf-8")
    monkeypatch.setattr(npp, "ENV_PATH", env)
    npp.write_env_proxy("http://127.0.0.1:7890")
    lines = [ln for ln in env.read_text(encoding="utf-8").splitlines()
             if ln.startswith("HTTP_PROXY=")]
    assert len(lines) == 1


# ── 报告渲染 ──────────────────────────────────────────────────────────────
def test_render_report_contains_sections():
    diag = {
        "generated_at": "2026-11-10 00:00:00",
        "env_proxy": {"HTTP_PROXY": ""},
        "local_proxy": {"ports": [], "processes": []},
        "results": [
            {"name": "Binance", "ok": True, "code": 200, "elapsed_ms": 100, "error": ""},
            {"name": "Yahoo", "ok": False, "code": None, "elapsed_ms": 5000,
             "error": "URLError: SSL", "overseas": True},
        ],
    }
    text = npp.render_report(diag)
    assert "出网通路体检" in text
    assert "Binance" in text
    assert "Yahoo" in text
    assert "修复建议" in text


# ── 探测失败不抛异常 ──────────────────────────────────────────────────────
def test_probe_url_handles_failure_gracefully():
    # 保留端口 1 (几乎必然连不上) → 必须返回结构化错误而不是抛异常
    out = npp.probe_url("http://127.0.0.1:1/none", timeout=1.0)
    assert out["ok"] is False
    assert out["error"]
    assert "elapsed_ms" in out


# ── v324: 硬时限 (DNS 解析不受 socket timeout 约束) ───────────────────────
def test_probe_url_hard_cap_returns_in_time(monkeypatch):
    """被墙源 (DNS 慢) 必须在硬时限内返回, 不能拖住体检。

    v452：原来真的去打 news.google.com —— 本机网络/代理一慢就红，
    全量跑时还和 engine 的并发请求抢带宽（实测 1/3 的全量跑会红）。
    这里改成注入一个「永远不返回」的探测函数，硬时限机制本身才是被测对象。
    """
    import time

    def _never_returns(url, timeout):
        time.sleep(30)

    monkeypatch.setattr(npp, "_probe_once", _never_returns)
    t0 = time.time()
    out = npp.probe_url("https://example.invalid/hard-cap", timeout=1.0, hard_cap=2.0)
    elapsed = time.time() - t0
    assert elapsed < 5.0, "硬时限失效: 探测拖了 %.1fs" % elapsed
    assert out["ok"] is False
    assert "硬超时" in out["error"]


def test_probe_url_hard_cap_returns_result_when_fast():
    """够快时正常返回探测结果, 不被硬时限误伤。"""
    out = npp.probe_url("http://127.0.0.1:1/none", timeout=1.0, hard_cap=5.0)
    assert out["ok"] is False
    assert out["error"]
    assert "硬超时" not in out["error"]


# ── v324: 国内直连源分组 ──────────────────────────────────────────────────
def test_cn_targets_present():
    assert len(npp.CN_TARGETS) >= 4
    names = [n for n, _u in npp.CN_TARGETS]
    assert "腾讯行情" in names
    assert "新浪行情" in names


def test_run_probe_includes_cn_group(monkeypatch):
    """run_probe 必须把国内源单独分组返回 (并发探测)。"""
    def _fake_probe(url, timeout=5.0, hard_cap=None):
        return {"ok": True, "code": 200, "elapsed_ms": 10, "error": ""}
    monkeypatch.setattr(npp, "probe_url", _fake_probe)
    out = npp.run_probe(timeout=1.0)
    assert "cn_results" in out
    assert out["cn_total"] == len(npp.CN_TARGETS)
    assert out["cn_ok"] == out["cn_total"]
    assert "probe_seconds" in out
    assert len(out["results"]) == len(npp.TARGETS)


def test_render_report_shows_cn_section():
    diag = {
        "generated_at": "2026-11-10 00:00:00",
        "probe_seconds": 24.0,
        "env_proxy": {"HTTP_PROXY": ""},
        "local_proxy": {"ports": [], "processes": []},
        "results": [{"name": "Yahoo", "ok": False, "code": None,
                     "elapsed_ms": 100, "error": "403", "overseas": True}],
        "cn_results": [{"name": "腾讯行情", "ok": True, "code": 200,
                        "elapsed_ms": 265, "error": ""}],
        "cn_ok": 1, "cn_total": 1,
    }
    text = npp.render_report(diag)
    assert "国内直连源" in text
    assert "腾讯行情" in text
    assert "海外/通用源" in text
    assert "24.0s" in text

