"""Robinhood Chain scoring matrix. Pure code — hard vetoes live here.

PR1 agents feed pulse only (no Solana auditor/narrative). Components are
liquidity quality, 24h volume, pulse, and a small Stock-Token prior.
"""

from __future__ import annotations

from typing import Any, Iterable

from .models import RhAsset

DEFAULT_WEIGHTS = {
    "liquidity": 0.35,
    "volume": 0.25,
    "pulse": 0.25,
    "stock_token": 0.15,
    "min_score_to_buy": 0.62,
}


def _norm_addr(value: str) -> str:
    return (value or "").strip().lower()


def _allowlist_set(
    allowlist: Iterable[str] | None,
    asset: RhAsset,
) -> set[str]:
    if allowlist is not None:
        return {_norm_addr(a) for a in allowlist if a}
    raw = asset.raw or {}
    for key in ("allowlist_addresses", "watchlist_addresses"):
        found = raw.get(key)
        if found:
            return {_norm_addr(str(a)) for a in found if a}
    return set()


def hard_veto_rh(
    asset: RhAsset,
    pulse: dict[str, Any],
    filter_cfg: dict[str, Any] | None = None,
    min_go_signal: float = 0.3,
    *,
    allowlist: Iterable[str] | None = None,
) -> str | None:
    """Return a stable veto slug, or None if the asset may be scored."""
    cfg = filter_cfg or {}
    require_allowlist = bool(cfg.get("require_allowlist", True))
    min_price = float(cfg.get("min_price_usd", 0.01))
    min_liq = float(cfg.get("min_liquidity_usd", 50_000.0))

    if require_allowlist:
        allowed = _allowlist_set(allowlist, asset)
        if not allowed or _norm_addr(asset.address) not in allowed:
            return "not_on_allowlist"

    price = float(asset.price_usd or 0.0)
    if price <= 0.0 or price < min_price:
        return "oracle_or_price_missing"

    liquidity = float(asset.liquidity_usd or 0.0)
    if liquidity < min_liq:
        return "liquidity_too_low"

    if float(pulse.get("go_signal", 0.0)) < min_go_signal:
        return "veto_market_paused"

    return None


def _liquidity_component(asset: RhAsset, min_liq: float) -> float:
    # Saturate around 4x the minimum floor.
    floor = max(min_liq, 1.0)
    return round(min(float(asset.liquidity_usd or 0.0) / (floor * 4.0), 1.0), 4)


def _volume_component(asset: RhAsset) -> float:
    return round(min(float(asset.volume_24h_usd or 0.0) / 250_000.0, 1.0), 4)


def _stock_token_component(asset: RhAsset) -> float:
    if asset.is_stock_token or asset.underlying:
        return 1.0
    return 0.4


def score_rh_asset(
    asset: RhAsset,
    pulse: dict[str, Any],
    filter_cfg: dict[str, Any] | None = None,
    weights: dict[str, Any] | None = None,
    min_go_signal: float = 0.3,
    *,
    allowlist: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Weighted score plus buy/skip verdict — same shape as ``score_token``."""
    cfg = filter_cfg or {}
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    min_liq = float(cfg.get("min_liquidity_usd", 50_000.0))

    components = {
        "liquidity": _liquidity_component(asset, min_liq),
        "volume": _volume_component(asset),
        "pulse": float(pulse.get("go_signal", 0.0)),
        "stock_token": _stock_token_component(asset),
    }

    veto = hard_veto_rh(
        asset,
        pulse,
        filter_cfg=cfg,
        min_go_signal=min_go_signal,
        allowlist=allowlist,
    )
    if veto is not None:
        return {
            "score": 0.0,
            "buy": False,
            "reason": veto,
            "vetoed": True,
            "components": components,
        }

    denominator = sum(float(w[k]) for k in components) or 1.0
    score = round(sum(components[k] * float(w[k]) for k in components) / denominator, 4)
    threshold = float(w["min_score_to_buy"])
    return {
        "score": score,
        "buy": score >= threshold,
        "reason": "above_threshold" if score >= threshold else "below_threshold",
        "vetoed": False,
        "components": components,
        "threshold": threshold,
    }
