"""Offline pool_scan discovery tests — fake http_get, no network."""

from __future__ import annotations

import pytest

from src.crypto.rh_chain import DexScreenerClient, RhDiscovery, score_rh_asset
from src.crypto.rh_chain.models import RhAsset

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
IMPOSTOR = "0x8218d73C00567A01481495Ad6c5143e00D5BB5b4"
TOKEN_A = "0xAaa0000000000000000000000000000000000001"
TOKEN_B = "0xBbb0000000000000000000000000000000000002"
TOKEN_LOW = "0xCcc0000000000000000000000000000000000003"


def _pair(
    *,
    base: str,
    quote: str,
    base_sym: str = "BASE",
    quote_sym: str = "QUOTE",
    pair: str = "0xPair000000000000000000000000000000000001",
    price: str = "10.5",
    liq: float = 200_000.0,
    vol: float = 40_000.0,
    dex_id: str = "uniswap",
    chain: str = "robinhood",
) -> dict:
    return {
        "chainId": chain,
        "dexId": dex_id,
        "pairAddress": pair,
        "url": f"https://dexscreener.com/{chain}/{pair}",
        "baseToken": {"address": base, "symbol": base_sym, "name": base_sym},
        "quoteToken": {"address": quote, "symbol": quote_sym, "name": quote_sym},
        "priceUsd": price,
        "liquidity": {"usd": liq, "base": 1.0, "quote": 1.0},
        "volume": {"h24": vol, "h6": 0, "h1": 0, "m5": 0},
    }


FIXTURE_PAIRS = [
    # TOKEN_A / USDG — candidate is TOKEN_A (base), non-seed
    _pair(
        base=TOKEN_A,
        quote=USDG,
        base_sym="TOKA",
        quote_sym="USDG",
        pair="0xPairA",
        price="12.0",
        liq=300_000,
        vol=90_000,
    ),
    # WETH / TOKEN_B — candidate is TOKEN_B (quote), non-seed; priceUsd is WETH
    _pair(
        base=WETH,
        quote=TOKEN_B,
        base_sym="WETH",
        quote_sym="TOKB",
        pair="0xPairB",
        price="3500.0",
        liq=150_000,
        vol=20_000,
    ),
    # Impostor USDG as base with TOKEN_A — impostor excluded as candidate;
    # TOKEN_A still ok as non-seed side when impostor is treated as non-seed...
    # Actually impostor is in exclude list; if impostor is base and TOKEN_A quote,
    # candidate is TOKEN_A. Separate pair: impostor as the *candidate* side:
    _pair(
        base=IMPOSTOR,
        quote=WETH,
        base_sym="FAKE",
        quote_sym="WETH",
        pair="0xPairFake",
        price="1.0",
        liq=999_999,
        vol=1,
    ),
    # Below min liquidity
    _pair(
        base=TOKEN_LOW,
        quote=USDG,
        base_sym="LOW",
        quote_sym="USDG",
        pair="0xPairLow",
        price="1.0",
        liq=1_000,
        vol=10,
    ),
    # Non-robinhood (should be ignored if returned by search)
    _pair(
        base=TOKEN_A,
        quote=USDG,
        base_sym="TOKA",
        quote_sym="USDG",
        pair="0xPairEth",
        chain="ethereum",
        liq=9_000_000,
    ),
]


def _fake_http_get(url: str):
    u = url.lower()
    if "/token-pairs/v1/robinhood/" in u:
        # Return full fixture for any seed request
        return list(FIXTURE_PAIRS)
    if "/latest/dex/search" in u:
        return {"pairs": list(FIXTURE_PAIRS)}
    raise AssertionError(f"unexpected url in offline test: {url}")


def _cfg(mode: str = "pool_scan", **pool_scan_overrides) -> dict:
    pool_scan = {
        "min_liquidity_usd": 50_000,
        "max_results": 50,
        "exclude_addresses": [IMPOSTOR],
        "search_queries": [],
    }
    pool_scan.update(pool_scan_overrides)
    return {
        "crypto": {"backend": "robinhood_chain"},
        "robinhood_chain": {
            "chain_id": 4663,
            "rpc_url": "http://127.0.0.1:9",
            "quote_token": USDG,
            "weth": WETH,
            "discovery": {
                "mode": mode,
                "watchlist": [],
                "pool_scan": pool_scan,
            },
            "filter": {
                "min_liquidity_usd": 50_000,
                "require_allowlist": True,
                "min_price_usd": 0.01,
            },
        },
    }


def _disc(mode: str = "pool_scan", **pool_scan_overrides) -> RhDiscovery:
    dex = DexScreenerClient(http_get=_fake_http_get, chain_slug="robinhood")
    return RhDiscovery(_cfg(mode, **pool_scan_overrides), dex_client=dex)


@pytest.mark.asyncio
async def test_pool_scan_finds_non_seed_sides():
    assets = await _disc().run()
    addrs = {a.address_norm for a in assets}
    assert TOKEN_A.lower() in addrs
    assert TOKEN_B.lower() in addrs
    # Impostor never appears as a candidate
    assert IMPOSTOR.lower() not in addrs
    # Low-liq filtered
    assert TOKEN_LOW.lower() not in addrs
    # Seeds themselves never appear
    assert USDG.lower() not in addrs
    assert WETH.lower() not in addrs


@pytest.mark.asyncio
async def test_pool_scan_raw_flags():
    assets = await _disc().run()
    a = next(x for x in assets if x.address_norm == TOKEN_A.lower())
    assert a.raw.get("pool_scan") is True
    assert a.raw.get("pair_address")
    assert a.raw.get("dex_id") == "uniswap"
    assert a.symbol == "TOKA"
    assert a.price_usd == 12.0
    assert a.liquidity_usd == 300_000
    assert a.volume_24h_usd == 90_000


@pytest.mark.asyncio
async def test_pool_scan_quote_side_candidate_price_zero():
    """When candidate is quoteToken, priceUsd is for base — leave price 0."""
    assets = await _disc().run()
    b = next(x for x in assets if x.address_norm == TOKEN_B.lower())
    assert b.price_usd == 0.0
    assert b.symbol == "TOKB"
    assert b.liquidity_usd == 150_000


@pytest.mark.asyncio
async def test_max_results_truncates():
    assets = await _disc(max_results=1).run()
    assert len(assets) == 1
    # Highest liquidity among kept candidates is TOKEN_A at 300k
    assert assets[0].address_norm == TOKEN_A.lower()


@pytest.mark.asyncio
async def test_search_queries_filtered_to_robinhood():
    assets = await _disc(search_queries=["USDG"]).run()
    # ethereum pair must not appear as a duplicate with higher liq winning wrongly
    # (ethereum pair also uses TOKEN_A — but chain filter drops it before merge)
    a = next(x for x in assets if x.address_norm == TOKEN_A.lower())
    assert a.liquidity_usd == 300_000  # not 9_000_000 from ethereum


@pytest.mark.asyncio
async def test_both_dedupes_watchlist_preferred():
    cfg = _cfg("both")
    cfg["robinhood_chain"]["discovery"]["watchlist"] = [
        {
            "address": TOKEN_A,
            "symbol": "WATCH",
            "price_usd": 99.0,
            "liquidity_usd": 10.0,  # lower than scan — watchlist still wins
        }
    ]
    dex = DexScreenerClient(http_get=_fake_http_get)
    disc = RhDiscovery(cfg, dex_client=dex)
    assets = await disc.run()
    by = {a.address_norm: a for a in assets}
    assert by[TOKEN_A.lower()].symbol == "WATCH"
    assert by[TOKEN_A.lower()].raw.get("watchlist_entry") is True
    assert TOKEN_B.lower() in by


@pytest.mark.asyncio
async def test_soft_fail_returns_empty():
    def boom(_url: str):
        raise RuntimeError("network down")

    dex = DexScreenerClient(http_get=boom)
    disc = RhDiscovery(_cfg("pool_scan"), dex_client=dex)
    assert await disc.run() == []


@pytest.mark.asyncio
async def test_both_soft_fail_keeps_watchlist():
    def boom(_url: str):
        raise RuntimeError("network down")

    cfg = _cfg("both")
    cfg["robinhood_chain"]["discovery"]["watchlist"] = [
        {"address": "0x1", "symbol": "OK", "price_usd": 1.0, "liquidity_usd": 100_000}
    ]
    disc = RhDiscovery(cfg, dex_client=DexScreenerClient(http_get=boom))
    assets = await disc.run()
    assert len(assets) == 1
    assert assets[0].symbol == "OK"


def test_normalize_pair_fields():
    dex = DexScreenerClient(http_get=lambda u: [])
    norm = dex.normalize_pair(FIXTURE_PAIRS[0])
    assert norm["pair_address"] == "0xPairA"
    assert norm["price_usd"] == 12.0
    assert norm["liquidity_usd"] == 300_000
    assert norm["volume_24h_usd"] == 90_000
    assert norm["base_token"]["address"].lower() == TOKEN_A.lower()


def test_min_liquidity_falls_back_to_filter():
    cfg = _cfg("pool_scan")
    cfg["robinhood_chain"]["discovery"]["pool_scan"]["min_liquidity_usd"] = None
    cfg["robinhood_chain"]["filter"]["min_liquidity_usd"] = 50_000
    disc = RhDiscovery(cfg, dex_client=DexScreenerClient(http_get=_fake_http_get))
    assert disc._min_liquidity_usd() == 50_000.0


@pytest.mark.asyncio
async def test_scoring_allowlist_none_with_pool_scan_asset():
    assets = await _disc().run()
    a = next(x for x in assets if x.address_norm == TOKEN_A.lower())
    disc = _disc()
    allowlist = disc.scoring_allowlist()
    assert allowlist is None
    out = score_rh_asset(
        a,
        {"go_signal": 0.9},
        filter_cfg={
            "min_liquidity_usd": 50_000,
            "require_allowlist": True,
            "min_price_usd": 0.01,
        },
        allowlist=allowlist,
    )
    assert out["vetoed"] is False
    assert out["buy"] is True
