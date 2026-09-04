"""Offline discovery tests — watchlist path still intact (PR1 + PR3)."""

from __future__ import annotations

import pytest

from src.crypto.rh_chain import RhAsset, RhDiscovery


def _cfg(watchlist: list[dict] | None = None, mode: str = "watchlist") -> dict:
    return {
        "crypto": {"backend": "robinhood_chain"},
        "robinhood_chain": {
            "chain_id": 4663,
            "rpc_url": "http://127.0.0.1:9",  # must never be contacted
            "quote_token": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
            "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
            "discovery": {
                "mode": mode,
                "watchlist": watchlist or [],
            },
        },
    }


@pytest.mark.asyncio
async def test_empty_watchlist_returns_empty():
    disc = RhDiscovery(_cfg([]))
    assert await disc.run() == []


@pytest.mark.asyncio
async def test_watchlist_parses_assets_offline():
    disc = RhDiscovery(
        _cfg(
            [
                {
                    "address": "0xAbc0000000000000000000000000000000000001",
                    "symbol": "AAPL",
                    "underlying": "AAPL",
                    "is_stock_token": True,
                    "price_usd": 190.0,
                    "liquidity_usd": 250_000,
                    "volume_24h_usd": 80_000,
                },
                {
                    "address": "0xDef0000000000000000000000000000000000002",
                    "symbol": "TSLA",
                    "price_usd": 250.0,
                    "liquidity_usd": 100_000,
                },
            ]
        )
    )
    assets = await disc.run()
    assert len(assets) == 2
    assert all(isinstance(a, RhAsset) for a in assets)
    assert assets[0].symbol == "AAPL"
    assert assets[0].is_stock_token is True
    assert assets[0].price_usd == 190.0
    assert assets[0].address.lower().startswith("0xabc")
    # allowlist addresses are stamped onto raw for scoring
    assert "0xabc0000000000000000000000000000000000001" in {
        a.lower() for a in assets[0].raw["allowlist_addresses"]
    }


@pytest.mark.asyncio
async def test_skips_entries_without_address():
    disc = RhDiscovery(_cfg([{"symbol": "NOPE"}, {"address": "0x1", "symbol": "OK"}]))
    assets = await disc.run()
    assert len(assets) == 1
    assert assets[0].symbol == "OK"


@pytest.mark.asyncio
async def test_injectable_enricher_runs_without_client():
    async def enrich(asset: RhAsset) -> RhAsset:
        return asset.model_copy(update={"liquidity_usd": 999_999.0})

    disc = RhDiscovery(
        _cfg([{"address": "0x1", "symbol": "X", "price_usd": 1.0}]),
        enricher=enrich,
    )
    assets = await disc.run()
    assert assets[0].liquidity_usd == 999_999.0


def test_watchlist_addresses_normalized():
    disc = RhDiscovery(
        _cfg(
            [
                {"address": "0xAA", "symbol": "A"},
                {"address": "0xbb", "symbol": "B"},
            ]
        )
    )
    addrs = disc.watchlist_addresses()
    assert addrs == {"0xaa", "0xbb"}


def test_scoring_allowlist_watchlist_mode():
    disc = RhDiscovery(
        _cfg([{"address": "0xAA", "symbol": "A"}], mode="watchlist")
    )
    assert disc.scoring_allowlist() == {"0xaa"}


def test_scoring_allowlist_pool_scan_is_none():
    disc = RhDiscovery(_cfg([], mode="pool_scan"))
    assert disc.scoring_allowlist() is None


def test_scoring_allowlist_both_is_none():
    disc = RhDiscovery(_cfg([{"address": "0xAA"}], mode="both"))
    assert disc.scoring_allowlist() is None
