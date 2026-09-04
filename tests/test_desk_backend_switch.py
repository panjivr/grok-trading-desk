"""Backend factory / selector tests (no full TradingDesk boot)."""

from __future__ import annotations

import pytest

from src.crypto.backends import (
    BACKEND_ROBINHOOD_CHAIN,
    BACKEND_SOLANA,
    build_crypto_stack,
    build_rh_discovery,
    build_rh_executor,
    get_crypto_backend,
    is_robinhood_chain,
    is_solana,
    rh_chain_config,
)
from src.crypto.rh_chain import RhDiscovery, RhExecutor


def test_default_backend_is_solana():
    assert get_crypto_backend({}) == BACKEND_SOLANA
    assert get_crypto_backend({"crypto": {}}) == BACKEND_SOLANA
    assert get_crypto_backend(None) == BACKEND_SOLANA


def test_explicit_robinhood_chain():
    cfg = {"crypto": {"backend": "robinhood_chain"}}
    assert get_crypto_backend(cfg) == BACKEND_ROBINHOOD_CHAIN
    assert is_robinhood_chain(cfg) is True
    assert is_solana(cfg) is False


def test_case_insensitive_and_unknown_fallback():
    assert get_crypto_backend({"crypto": {"backend": "Robinhood_Chain"}}) == (
        BACKEND_ROBINHOOD_CHAIN
    )
    assert get_crypto_backend({"crypto": {"backend": "not-a-real-backend"}}) == (
        BACKEND_SOLANA
    )


def test_rh_chain_config_section():
    cfg = {"robinhood_chain": {"chain_id": 4663, "paper": True}}
    assert rh_chain_config(cfg)["chain_id"] == 4663
    assert rh_chain_config({}) == {}


def test_build_stack_solana_leaves_executor_none():
    stack = build_crypto_stack({"crypto": {"backend": "solana"}})
    assert stack["backend"] == BACKEND_SOLANA
    assert stack["discovery"] is None
    assert stack["executor"] is None


def test_build_stack_rh_wires_discovery_and_executor():
    cfg = {
        "crypto": {"backend": "robinhood_chain"},
        "mode": "paper",
        "robinhood_chain": {
            "paper": True,
            "discovery": {"mode": "watchlist", "watchlist": []},
        },
    }
    stack = build_crypto_stack(cfg)
    assert stack["backend"] == BACKEND_ROBINHOOD_CHAIN
    assert isinstance(stack["discovery"], RhDiscovery)
    assert isinstance(stack["executor"], RhExecutor)


def test_build_helpers_direct():
    cfg = {
        "robinhood_chain": {
            "paper": True,
            "discovery": {"watchlist": [{"address": "0x1", "symbol": "Z"}]},
        }
    }
    assert isinstance(build_rh_discovery(cfg), RhDiscovery)
    assert isinstance(build_rh_executor(cfg), RhExecutor)


@pytest.mark.asyncio
async def test_rh_stack_empty_watchlist_no_buys_path():
    """Mirrors desk: empty watchlist → discovery returns [] → no evaluate."""
    cfg = {
        "crypto": {"backend": "robinhood_chain"},
        "robinhood_chain": {
            "paper": True,
            "discovery": {"mode": "watchlist", "watchlist": []},
            "filter": {"require_allowlist": True, "min_liquidity_usd": 50_000},
        },
    }
    stack = build_crypto_stack(cfg)
    assets = await stack["discovery"].run()
    assert assets == []
