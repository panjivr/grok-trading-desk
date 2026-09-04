"""Robinhood Chain execution — paper fills + deliberate live stub.

Live signing is intentionally unimplemented (same safety stance as
``crypto_executor.py``). Wire Uniswap / router / Permit2 yourself; never log
``wallet_key``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ...models import Market, Position

log = logging.getLogger(__name__)

_LIVE_MESSAGE = (
    "RH Chain live execution is intentionally not implemented — "
    "wire Uniswap swapExactTokensForTokens (or Universal Router / Permit2) "
    "against robinhood_chain.router with max_slippage_bps, sign with an "
    "owner-held private key (never print or log secrets), and confirm on "
    "chain id 4663/46630 before marking a fill successful. "
    "See https://docs.robinhood.com/chain/"
)


def _synthetic_tx_id() -> str:
    return f"rh-paper-{uuid.uuid4().hex[:16]}"


class RhExecutor:
    """Paper / dry-run executor mirroring ``CryptoExecutor``'s interface."""

    market = Market.CRYPTO
    backend = "robinhood_chain"

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        rh = self.config.get("robinhood_chain") or {}
        # Prefer explicit paper flag; also honor global mode: paper.
        mode = str(self.config.get("mode") or "paper").lower()
        self.paper = bool(rh.get("paper", True)) or mode == "paper"
        self.rpc_url = str(rh.get("rpc_url") or "")
        self.chain_id = int(rh.get("chain_id") or 4663)
        self.router = str(rh.get("router") or "")
        self.max_slippage_bps = int(rh.get("max_slippage_bps", 50))
        # Read but never log the key — presence only for future live path.
        self._has_wallet_key = bool(rh.get("wallet_key")) and str(
            rh.get("wallet_key")
        ) not in {"", "REPLACE_ME"}
        self._positions: dict[str, Position] = {}

    def _addr_key(self, address: str) -> str:
        return (address or "").strip().lower()

    def _require_paper_or_raise(self) -> None:
        if not self.paper:
            raise NotImplementedError(_LIVE_MESSAGE)

    async def buy(self, address: str, amount_usd: float, **kwargs: Any) -> dict[str, Any]:
        """Swap USD notional for ``address``. Returns {tx_id, quantity, price}."""
        self._require_paper_or_raise()
        price = float(kwargs.get("price") or kwargs.get("price_usd") or 1.0)
        if price <= 0:
            price = 1.0
        quantity = float(amount_usd) / price if amount_usd else 0.0
        tx_id = _synthetic_tx_id()
        key = self._addr_key(address)
        symbol = str(kwargs.get("symbol") or address)
        pos = Position(
            market=Market.CRYPTO,
            symbol=symbol,
            quantity=quantity,
            entry_price=price,
            current_price=price,
            amount_usd=float(amount_usd),
            meta={
                "address": address,
                "tx_hash": tx_id,
                "backend": self.backend,
            },
        )
        self._positions[key] = pos
        log.info(
            "RH paper buy address=%s amount_usd=%.4f qty=%.8f tx_id=%s",
            address,
            amount_usd,
            quantity,
            tx_id,
        )
        return {"tx_id": tx_id, "quantity": quantity, "price": price, "paper": True}

    async def sell(
        self, address: str, fraction: float = 1.0, **kwargs: Any
    ) -> dict[str, Any]:
        """Sell ``fraction`` of the held balance (paper book)."""
        self._require_paper_or_raise()
        fraction = max(0.0, min(float(fraction), 1.0))
        key = self._addr_key(address)
        pos = self._positions.get(key)
        tx_id = _synthetic_tx_id()
        if pos is None:
            log.info("RH paper sell address=%s — no open paper position", address)
            return {"tx_id": tx_id, "quantity": 0.0, "price": 0.0, "paper": True}

        sold_qty = pos.quantity * fraction
        pos.quantity *= 1.0 - fraction
        pos.amount_usd *= 1.0 - fraction
        if pos.quantity <= 1e-12 or fraction >= 1.0:
            self._positions.pop(key, None)
        log.info(
            "RH paper sell address=%s fraction=%.4f sold_qty=%.8f tx_id=%s",
            address,
            fraction,
            sold_qty,
            tx_id,
        )
        return {
            "tx_id": tx_id,
            "quantity": sold_qty,
            "price": float(pos.current_price or pos.entry_price or 0.0),
            "paper": True,
        }

    async def close_position(self, address: str) -> dict[str, Any]:
        """Full exit. Equivalent to sell(address, 1.0)."""
        return await self.sell(address, 1.0)

    async def tighten_stop(self, address: str, new_stop_price: float) -> dict[str, Any]:
        """Desk-side stop watcher — no on-chain stop order in PR1."""
        self._require_paper_or_raise()
        key = self._addr_key(address)
        pos = self._positions.get(key)
        if pos is not None:
            pos.stop_price = float(new_stop_price)
        log.info(
            "RH paper tighten_stop address=%s new_stop=%.8f",
            address,
            new_stop_price,
        )
        return {"ok": True, "stop_price": float(new_stop_price), "paper": True}

    async def get_positions(self) -> list[Position]:
        """Return paper book holdings opened by this executor."""
        self._require_paper_or_raise()
        return list(self._positions.values())
