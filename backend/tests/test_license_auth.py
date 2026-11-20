"""
Trident v300 — license_auth 模块测试
=====================================

覆盖：HMAC 签名 / License 过期 / 设备指纹 / API Key 校验。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src_python"))


def _write_license(tmp_path, secret, *, tier="trial", days=30, fp=None, **extra):
    """写一个合法签名的 license 文件，返回路径。"""
    import license_auth
    license_auth.LICENSE_SECRET = secret
    license_auth.LICENSE_FILE = str(tmp_path / "license.lic")
    license_auth._cached_license = None

    now = datetime.now(timezone.utc)
    payload = {
        "license_key": "tr-TEST-TEST-TEST",
        "tier": tier,
        "issued_to": "test@example.com",
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fp": fp or "",
        "features": ["realtime"],
        **extra,
    }
    sig = license_auth._compute_expected_sig(payload, secret)
    payload["sig"] = sig
    (tmp_path / "license.lic").write_text(json.dumps(payload))
    return license_auth.LICENSE_FILE


def test_load_license_success(tmp_path, monkeypatch):
    """合法 license 应成功加载。"""
    secret = "test-secret-1"
    monkeypatch.setenv("TRIDENT_LICENSE_SECRET", secret)
    _write_license(tmp_path, secret)
    import license_auth
    license_auth.LICENSE_SECRET = secret
    license_auth.LICENSE_FILE = str(tmp_path / "license.lic")
    license_auth._cached_license = None
    lic = license_auth.load_license()
    assert lic is not None
    assert lic.tier == "trial"
    assert lic.issued_to == "test@example.com"
    assert not lic.is_expired()


def test_load_license_missing_secret(tmp_path, monkeypatch):
    """TRIDENT_LICENSE_SECRET 未设 → fail-closed（返回 None）。"""
    secret = "test-secret-2"
    _write_license(tmp_path, secret)
    import license_auth
    license_auth.LICENSE_SECRET = ""
    license_auth.LICENSE_FILE = str(tmp_path / "license.lic")
    license_auth._cached_license = None
    lic = license_auth.load_license()
    assert lic is None


def test_load_license_tampered_sig(tmp_path, monkeypatch):
    """签名错误 → 拒绝。"""
    secret = "test-secret-3"
    monkeypatch.setenv("TRIDENT_LICENSE_SECRET", secret)
    _write_license(tmp_path, secret, days=30)
    import license_auth
    license_auth.LICENSE_SECRET = secret
    license_auth.LICENSE_FILE = str(tmp_path / "license.lic")
    license_auth._cached_license = None
    raw = json.loads((tmp_path / "license.lic").read_text())
    raw["expires_at"] = "2099-01-01T00:00:00Z"
    (tmp_path / "license.lic").write_text(json.dumps(raw))
    assert license_auth.load_license() is None


def test_load_license_expired(tmp_path, monkeypatch):
    """已过期 license 仍能加载但 is_expired()=True。"""
    secret = "test-secret-4"
    monkeypatch.setenv("TRIDENT_LICENSE_SECRET", secret)
    _write_license(tmp_path, secret, days=-1)
    import license_auth
    license_auth.LICENSE_SECRET = secret
    license_auth.LICENSE_FILE = str(tmp_path / "license.lic")
    license_auth._cached_license = None
    lic = license_auth.load_license()
    assert lic is not None
    assert lic.is_expired()


def test_load_license_missing_sig(tmp_path, monkeypatch):
    """缺 sig 字段 → 拒绝。"""
    secret = "test-secret-5"
    monkeypatch.setenv("TRIDENT_LICENSE_SECRET", secret)
    _write_license(tmp_path, secret)
    raw = json.loads((tmp_path / "license.lic").read_text())
    raw.pop("sig")
    (tmp_path / "license.lic").write_text(json.dumps(raw))
    import license_auth
    license_auth.LICENSE_SECRET = secret
    license_auth.LICENSE_FILE = str(tmp_path / "license.lic")
    license_auth._cached_license = None
    assert license_auth.load_license() is None


def test_load_license_file_not_found(tmp_path, monkeypatch):
    """license 文件不存在 → 返回 None。"""
    import license_auth
    license_auth.LICENSE_SECRET = "any"
    license_auth.LICENSE_FILE = str(tmp_path / "nonexistent.lic")
    license_auth._cached_license = None
    assert license_auth.load_license() is None


def test_get_machine_fingerprint_stable():
    """设备指纹：同一进程两次调用应相同。"""
    import license_auth
    fp1 = license_auth.get_machine_fingerprint()
    fp2 = license_auth.get_machine_fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 32
    assert all(c in "0123456789abcdef" for c in fp1)


def test_license_has_feature():
    """通配符 `*` 应放行所有。"""
    import license_auth
    lic = license_auth.License(
        key="x", tier="trial", issued_to="", issued_at="",
        expires_at=None, fingerprint=None, features=["*"],
    )
    assert lic.has_feature("anything")


def test_license_has_feature_specific():
    """未在列表中应 False。"""
    import license_auth
    lic = license_auth.License(
        key="x", tier="trial", issued_to="", issued_at="",
        expires_at=None, fingerprint=None, features=["realtime"],
    )
    assert lic.has_feature("realtime")
    assert not lic.has_feature("historical_30d")


def test_verify_api_key_missing(tmp_path, monkeypatch):
    """无 token → 返回 None。"""
    import license_auth
    monkeypatch.setattr(license_auth, "API_KEY_STORE_FILE",
                        str(tmp_path / "no_keys.json"))
    assert license_auth.verify_api_key("") is None
    assert license_auth.verify_api_key(None) is None  # type: ignore


def test_verify_api_key_invalid(tmp_path, monkeypatch):
    """错误 token → 返回 None。"""
    import license_auth
    monkeypatch.setattr(license_auth, "API_KEY_STORE_FILE",
                        str(tmp_path / "no_keys.json"))
    assert license_auth.verify_api_key("trk_invalid") is None


def test_verify_api_key_revoked(tmp_path, monkeypatch):
    """已撤销的 key → 返回 None。"""
    import license_auth
    store = {"trk_abc": {"name": "x", "revoked": True}}
    p = tmp_path / "keys.json"
    p.write_text(json.dumps(store))
    monkeypatch.setattr(license_auth, "API_KEY_STORE_FILE", str(p))
    assert license_auth.verify_api_key("trk_abc") is None


def test_verify_api_key_valid(tmp_path, monkeypatch):
    """正常 key → 返回元数据。"""
    import license_auth
    store = {"trk_abc": {"name": "li", "tier": "trial", "revoked": False}}
    p = tmp_path / "keys.json"
    p.write_text(json.dumps(store))
    monkeypatch.setattr(license_auth, "API_KEY_STORE_FILE", str(p))
    meta = license_auth.verify_api_key("trk_abc")
    assert meta is not None
    assert meta["name"] == "li"


def test_compute_expected_sig_deterministic():
    """同 payload + 同 secret → 同 sig。"""
    import license_auth
    payload = {"a": 1, "b": [2, 3], "c": {"d": 4}}
    sig1 = license_auth._compute_expected_sig(payload, "s")
    sig2 = license_auth._compute_expected_sig(payload, "s")
    assert sig1 == sig2
    payload3 = {"c": {"d": 4}, "a": 1, "b": [2, 3]}
    sig3 = license_auth._compute_expected_sig(payload3, "s")
    assert sig1 == sig3


def test_compute_expected_sig_different_secret():
    """不同 secret → 不同 sig。"""
    import license_auth
    payload = {"a": 1}
    assert (license_auth._compute_expected_sig(payload, "s1")
            != license_auth._compute_expected_sig(payload, "s2"))