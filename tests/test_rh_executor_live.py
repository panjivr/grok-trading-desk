"""Live-gate + mock tx_backend tests — offline, no web3, no real keys."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from src.crypto.rh_chain import RhExecutor
from src.crypto.rh_chain.uniswap_v3 import DEFAULT_USDG, SwapError


USDG = DEFAULT_USDG
# Shape-only test key — never a real secret. Prefer mocks so eth_account is unused.
_ZERO_KEY = "0x" + ("00" * 32)


class MockTxBackend:
    """Records swap_exact_in calls; never talks to a chain."""

    def __init__(self, address: str = "0x" + ("11" * 20)):
        self.address = address
        self.calls: list[dict[str, Any]] = []
        self._balances: dict[str, int] = {}
        self._decimals: dict[str, int] = {USDG.lower(): 6}

    def set_balance(self, token: str, amount: int) -> None:
        self._balances[token.lower()] = int(amount)

    def balance_of(self, token: str, owner: str | None = None) -> int:
        return int(self._balances.get(token.lower(), 0))

    def token_decimals(self, token: str) -> int:
        return int(self._decimals.get(token.lower(), 18))

    def swap_exact_in(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        recipient: str,
        fee: int,
        deadline: int,
    ) -> dict[str, Any]:
        call = {
            "token_in": token_in,
            "token_out": token_out,
            "amount_in": int(amount_in),
            "amount_out_min": int(amount_out_min),
            "recipient": recipient,
            "fee": int(fee),
            "deadline": int(deadline),
        }
        self.calls.append(call)
        # Synthetic out: 1e18 wei of token_out
        out = 10**18
        self._balances[token_out.lower()] = self._balances.get(token_out.lower(), 0) + out
        return {"tx_hash": "0xdeadbeef", "amount_out": out}


def _live_cfg(**rh_overrides: Any) -> dict:
    rh = {
        "paper": False,
        "chain_id": 4663,
        "rpc_url": "http://127.0.0.1:9",
        "wallet_key": "REPLACE_ME",
        "router": "0xcaf681a66d020601342297493863e78c959e5cb2",
        "quote_token": USDG,
        "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "pool_fee": 3000,
        "max_slippage_bps": 50,
        "deadline_seconds": 120,
    }
    rh.update(rh_overrides)
    return {"mode": "live", "robinhood_chain": rh}


@pytest.mark.asyncio
async def test_mode_paper_stays_paper_even_with_live_ack(caplog):
    cfg = {
        "mode": "paper",
        "robinhood_chain": {
            "paper": False,
            "rpc_url": "http://127.0.0.1:9",
            "wallet_key": "REPLACE_ME",
            "quote_token": USDG,
        },
    }
    backend = MockTxBackend()
    with caplog.at_level(logging.WARNING):
        ex = RhExecutor(cfg, live_ack=True, tx_backend=backend)
    assert ex.paper is True
    fill = await ex.buy("0xToken", 100.0, price=10.0)
    assert fill["paper"] is True
    assert fill["tx_id"].startswith("rh-paper-")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_mode_live_without_live_ack_stays_paper_and_warns(caplog):
    cfg = _live_cfg(paper=False)
    backend = MockTxBackend()
    with caplog.at_level(logging.WARNING):
        ex = RhExecutor(cfg, live_ack=False, tx_backend=backend)
    assert ex.paper is True
    joined = " ".join(r.message for r in caplog.records)
    assert "i-understand-the-risk" in joined
    fill = await ex.buy("0xToken", 50.0, price=5.0)
    assert fill["paper"] is True
    assert backend.calls == []
    # Never log the placeholder key value as a secret leak pattern for real keys
    assert _ZERO_KEY not in joined
    assert "wallet_key" not in joined.lower()


@pytest.mark.asyncio
async def test_live_ack_but_rh_paper_true_stays_paper(caplog):
    cfg = _live_cfg(paper=True)
    with caplog.at_level(logging.WARNING):
        ex = RhExecutor(cfg, live_ack=True)
    assert ex.paper is True
    joined = " ".join(r.message for r in caplog.records)
    assert "robinhood_chain.paper" in joined


@pytest.mark.asyncio
async def test_live_buy_with_mock_backend(caplog):
    backend = MockTxBackend()
    cfg = _live_cfg(paper=False, wallet_key=_ZERO_KEY)
    with caplog.at_level(logging.INFO):
        ex = RhExecutor(cfg, live_ack=True, tx_backend=backend)
    assert ex.paper is False

    fill = await ex.buy(
        "0xStockToken",
        100.0,
        price=10.0,
        amount_in_wei=100_000_000,  # 100 USDG (6 decimals)
        expected_out=10**18,
        token_decimals=18,
    )
    assert fill["paper"] is False
    assert fill["tx_id"] == "0xdeadbeef"
    assert fill["quantity"] == pytest.approx(1.0)
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["token_in"].lower() == USDG.lower()
    assert call["token_out"] == "0xStockToken"
    assert call["amount_in"] == 100_000_000
    # 50 bps slippage on expected_out
    assert call["amount_out_min"] == 10**18 * (10_000 - 50) // 10_000
    assert call["fee"] == 3000

    joined = " ".join(r.message for r in caplog.records)
    assert _ZERO_KEY not in joined
    assert "wallet_key" not in joined.lower()
    # repr must not embed the key
    assert _ZERO_KEY not in repr(ex)


@pytest.mark.asyncio
async def test_live_sell_swaps_fraction_of_balance():
    backend = MockTxBackend()
    token = "0xStockToken"
    backend.set_balance(token, 1_000_000)
    cfg = _live_cfg(paper=False, wallet_key=_ZERO_KEY)
    ex = RhExecutor(cfg, live_ack=True, tx_backend=backend)
    sold = await ex.sell(token, 0.5, token_decimals=6, price=2.0)
    assert sold["paper"] is False
    assert sold["tx_id"] == "0xdeadbeef"
    assert sold["quantity"] == pytest.approx(0.5)  # 500000 / 1e6
    assert backend.calls[0]["amount_in"] == 500_000
    assert backend.calls[0]["token_in"] == token
    assert backend.calls[0]["token_out"].lower() == USDG.lower()


@pytest.mark.asyncio
async def test_live_get_positions_returns_empty():
    cfg = _live_cfg(paper=False, wallet_key=_ZERO_KEY)
    ex = RhExecutor(cfg, live_ack=True, tx_backend=MockTxBackend())
    assert await ex.get_positions() == []


@pytest.mark.asyncio
async def test_live_tighten_stop_desk_side_only():
    cfg = _live_cfg(paper=False, wallet_key=_ZERO_KEY)
    ex = RhExecutor(cfg, live_ack=True, tx_backend=MockTxBackend())
    result = await ex.tighten_stop("0x1", 1.25)
    assert result["ok"] is True
    assert result["stop_price"] == pytest.approx(1.25)
    assert result["paper"] is False


@pytest.mark.asyncio
async def test_live_buy_missing_quote_raises_without_leaking_key(caplog):
    cfg = _live_cfg(paper=False, wallet_key=_ZERO_KEY, quote_token="")
    # Force empty after defaults — RhExecutor applies DEFAULT_USDG when empty.
    # Override post-init to simulate misconfig for the require path.
    ex = RhExecutor(cfg, live_ack=True, tx_backend=MockTxBackend())
    ex.quote_token = ""
    with pytest.raises(SwapError) as ei:
        await ex.buy("0x1", 10.0, amount_in_wei=1)
    msg = str(ei.value).lower()
    assert "quote_token" in msg
    assert _ZERO_KEY not in msg
    assert "wallet_key" not in msg


@pytest.mark.asyncio
async def test_never_asserts_raw_private_key_in_logs(caplog):
    """Guard: even with a key-shaped value present, logs must not contain it."""
    backend = MockTxBackend()
    cfg = _live_cfg(paper=False, wallet_key=_ZERO_KEY)
    with caplog.at_level(logging.DEBUG):
        ex = RhExecutor(cfg, live_ack=True, tx_backend=backend)
        await ex.buy("0xT", 1.0, amount_in_wei=1_000_000, expected_out=10**18)
    blob = "\n".join(r.message for r in caplog.records)
    assert _ZERO_KEY not in blob
    assert "00" * 32 not in blob
