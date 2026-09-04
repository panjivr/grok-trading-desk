"""Paper executor tests — no network, no keys logged."""

from __future__ import annotations

import logging

import pytest

from src.crypto.rh_chain import RhExecutor
from src.models import Market


def _cfg(paper: bool = True) -> dict:
    return {
        "mode": "paper" if paper else "live",
        "robinhood_chain": {
            "paper": paper,
            "chain_id": 4663,
            "rpc_url": "http://127.0.0.1:9",
            "wallet_key": "REPLACE_ME",
            "router": "",
            "max_slippage_bps": 50,
        },
    }


@pytest.mark.asyncio
async def test_paper_buy_returns_synthetic_fill(caplog):
    ex = RhExecutor(_cfg(paper=True))
    with caplog.at_level(logging.INFO):
        fill = await ex.buy("0xAbc1", 100.0, price=10.0, symbol="AAPL")
    assert fill["paper"] is True
    assert fill["tx_id"].startswith("rh-paper-")
    assert fill["quantity"] == pytest.approx(10.0)
    assert fill["price"] == pytest.approx(10.0)
    # Never leak wallet_key into logs
    joined = " ".join(r.message for r in caplog.records)
    assert "REPLACE_ME" not in joined
    assert "wallet_key" not in joined.lower()

    positions = await ex.get_positions()
    assert len(positions) == 1
    assert positions[0].market == Market.CRYPTO
    assert positions[0].meta["backend"] == "robinhood_chain"
    assert positions[0].meta["address"] == "0xAbc1"
    assert positions[0].meta["tx_hash"] == fill["tx_id"]


@pytest.mark.asyncio
async def test_paper_sell_and_close():
    ex = RhExecutor(_cfg(True))
    await ex.buy("0x1", 50.0, price=5.0, symbol="X")
    sold = await ex.sell("0x1", 0.5)
    assert sold["quantity"] == pytest.approx(5.0)  # 50/5 * 0.5
    positions = await ex.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == pytest.approx(5.0)

    closed = await ex.close_position("0x1")
    assert closed["paper"] is True
    assert await ex.get_positions() == []


@pytest.mark.asyncio
async def test_tighten_stop_updates_paper_book():
    ex = RhExecutor(_cfg(True))
    await ex.buy("0x1", 10.0, price=2.0, symbol="Y")
    await ex.tighten_stop("0x1", 1.5)
    pos = (await ex.get_positions())[0]
    assert pos.stop_price == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_live_raises_not_implemented():
    ex = RhExecutor(_cfg(paper=False))
    # Force live even if mode says paper
    ex.paper = False
    with pytest.raises(NotImplementedError) as ei:
        await ex.buy("0x1", 10.0)
    msg = str(ei.value).lower()
    assert "uniswap" in msg or "not implemented" in msg
    assert "wallet_key" not in msg  # message must not embed secrets


@pytest.mark.asyncio
async def test_live_sell_close_tighten_also_stubbed():
    ex = RhExecutor(_cfg(False))
    ex.paper = False
    for coro in (
        ex.sell("0x1", 1.0),
        ex.close_position("0x1"),
        ex.tighten_stop("0x1", 1.0),
        ex.get_positions(),
    ):
        with pytest.raises(NotImplementedError):
            await coro
