"""Hard-veto / scoring tests for score_rh_asset (PR3 allowlist=None)."""

from __future__ import annotations

from src.crypto.rh_chain import RhAsset, score_rh_asset
from src.crypto.rh_chain.rh_scoring import hard_veto_rh

FILTER = {
    "min_liquidity_usd": 50_000,
    "require_allowlist": True,
    "min_price_usd": 0.01,
}

ALLOW = {"0xabc0000000000000000000000000000000000001"}


def _asset(**kwargs) -> RhAsset:
    base = dict(
        address="0xAbc0000000000000000000000000000000000001",
        symbol="AAPL",
        price_usd=190.0,
        liquidity_usd=250_000.0,
        volume_24h_usd=80_000.0,
        is_stock_token=True,
        underlying="AAPL",
        raw={},
    )
    base.update(kwargs)
    return RhAsset(**base)


def test_allowlist_miss_vetoes():
    asset = _asset(address="0xdead", raw={})
    out = score_rh_asset(
        asset, {"go_signal": 0.9}, filter_cfg=FILTER, allowlist=ALLOW
    )
    assert out["buy"] is False
    assert out["vetoed"] is True
    assert out["reason"] == "not_on_allowlist"


def test_allowlist_hit_passes():
    asset = _asset()
    veto = hard_veto_rh(asset, {"go_signal": 0.9}, FILTER, allowlist=ALLOW)
    assert veto is None


def test_allowlist_none_skips_veto():
    """PR3: allowlist=None skips require_allowlist even when filter says True."""
    asset = _asset(address="0xnotlisted", raw={})
    veto = hard_veto_rh(asset, {"go_signal": 0.9}, FILTER, allowlist=None)
    assert veto is None
    out = score_rh_asset(
        asset, {"go_signal": 0.9}, filter_cfg=FILTER, allowlist=None
    )
    assert out["vetoed"] is False
    assert out["buy"] is True


def test_zero_price_vetoes():
    out = score_rh_asset(
        _asset(price_usd=0.0),
        {"go_signal": 0.9},
        filter_cfg=FILTER,
        allowlist=ALLOW,
    )
    assert out["reason"] == "oracle_or_price_missing"
    assert out["buy"] is False


def test_below_min_price_vetoes():
    out = score_rh_asset(
        _asset(price_usd=0.001),
        {"go_signal": 0.9},
        filter_cfg=FILTER,
        allowlist=ALLOW,
    )
    assert out["reason"] == "oracle_or_price_missing"


def test_low_liquidity_vetoes():
    out = score_rh_asset(
        _asset(liquidity_usd=1_000.0),
        {"go_signal": 0.9},
        filter_cfg=FILTER,
        allowlist=ALLOW,
    )
    assert out["reason"] == "liquidity_too_low"


def test_pulse_veto():
    out = score_rh_asset(
        _asset(),
        {"go_signal": 0.1},
        filter_cfg=FILTER,
        allowlist=ALLOW,
        min_go_signal=0.3,
    )
    assert out["reason"] == "veto_market_paused"


def test_passing_asset_has_score_shape():
    out = score_rh_asset(
        _asset(),
        {"go_signal": 0.85},
        filter_cfg=FILTER,
        allowlist=ALLOW,
        min_go_signal=0.3,
    )
    assert "score" in out and "buy" in out and "reason" in out
    assert "components" in out
    assert out["vetoed"] is False
    assert set(out["components"]) >= {"liquidity", "volume", "pulse", "stock_token"}
    assert out["buy"] is True
    assert out["reason"] == "above_threshold"


def test_require_allowlist_false_skips_check():
    asset = _asset(address="0xnotlisted", raw={})
    cfg = {**FILTER, "require_allowlist": False}
    veto = hard_veto_rh(asset, {"go_signal": 0.9}, cfg, allowlist=ALLOW)
    assert veto is None
