"""Robinhood Chain execution — paper fills + live Uniswap V3 swaps.

Live trading requires ALL of:
  1. config ``mode == "live"``
  2. ``live_ack=True`` (desk ``--i-understand-the-risk``)
  3. ``robinhood_chain.paper`` is False

Otherwise the executor stays on paper (synthetic fills), mirroring
``StockExecutor``. Never log ``wallet_key`` or private-key material.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Protocol

from ...models import Market, Position
from .uniswap_v3 import (
    DEFAULT_SWAP_ROUTER02,
    DEFAULT_USDG,
    DEFAULT_WETH,
    SwapError,
    UniswapV3SwapBackend,
)

log = logging.getLogger(__name__)


class TxBackend(Protocol):
    """Injectable swap backend — tests supply a mock; production uses Uniswap."""

    def swap_exact_in(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        recipient: str,
        fee: int,
        deadline: int,
    ) -> dict[str, Any]: ...

    @property
    def address(self) -> str: ...

    def balance_of(self, token: str, owner: str | None = None) -> int: ...

    def token_decimals(self, token: str) -> int: ...


def _synthetic_tx_id() -> str:
    return f"rh-paper-{uuid.uuid4().hex[:16]}"


def _is_placeholder_key(value: Any) -> bool:
    text = str(value or "").strip()
    return text in {"", "REPLACE_ME"} or text.lower() in {"replace_me", "none", "null"}


class RhExecutor:
    """Paper / live executor mirroring ``CryptoExecutor``'s interface."""

    market = Market.CRYPTO
    backend = "robinhood_chain"

    def __init__(
        self,
        config: dict[str, Any],
        live_ack: bool = False,
        client: Any = None,
        tx_backend: Any = None,
    ):
        self.config = config or {}
        rh = self.config.get("robinhood_chain") or {}
        wants_live = str(self.config.get("mode") or "paper").lower() == "live"
        rh_paper = bool(rh.get("paper", True))
        # Paper unless mode live AND live_ack AND robinhood_chain.paper is False.
        self.paper = not (wants_live and live_ack and not rh_paper)
        if wants_live and not live_ack:
            log.warning(
                "config asks for live trading but --i-understand-the-risk was not "
                "passed; staying on paper"
            )
        elif wants_live and live_ack and rh_paper:
            log.warning(
                "mode is live and live_ack set, but robinhood_chain.paper is true; "
                "staying on paper"
            )

        self.rpc_url = str(rh.get("rpc_url") or "")
        self.chain_id = int(rh.get("chain_id") or 4663)
        self.router = str(rh.get("router") or "").strip() or DEFAULT_SWAP_ROUTER02
        self.universal_router = str(rh.get("universal_router") or "")
        self.permit2 = str(rh.get("permit2") or "")
        self.quoter_v2 = str(rh.get("quoter_v2") or "")
        self.weth = str(rh.get("weth") or "").strip() or DEFAULT_WETH
        # Prefer explicit quote_token; default to USDG for stock-token buys.
        self.quote_token = str(rh.get("quote_token") or "").strip() or DEFAULT_USDG
        self.pool_fee = int(rh.get("pool_fee") or 3000)
        self.deadline_seconds = int(rh.get("deadline_seconds") or 120)
        self.max_slippage_bps = int(rh.get("max_slippage_bps", 50))

        # Read key presence only — never store the raw key on the executor when
        # a tx_backend is injected (tests). Live default backend receives the key.
        raw_key = rh.get("wallet_key")
        self._has_wallet_key = not _is_placeholder_key(raw_key)
        self._wallet_key: str | None = (
            str(raw_key).strip() if self._has_wallet_key else None
        )

        self._client = client
        self._tx_backend = tx_backend
        self._positions: dict[str, Position] = {}

    def __repr__(self) -> str:  # pragma: no cover - safety
        return (
            f"RhExecutor(paper={self.paper}, chain_id={self.chain_id}, "
            f"router={self.router!r}, has_wallet_key={self._has_wallet_key})"
        )

    def _addr_key(self, address: str) -> str:
        return (address or "").strip().lower()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from .client import RhChainClient

        if not self.rpc_url:
            raise SwapError("rpc_error", "robinhood_chain.rpc_url is required for live")
        self._client = RhChainClient(self.rpc_url, chain_id=self.chain_id)
        return self._client

    def _get_tx_backend(self) -> Any:
        if self._tx_backend is not None:
            return self._tx_backend
        if not self._has_wallet_key or not self._wallet_key:
            raise SwapError(
                "swap_failed",
                "robinhood_chain.wallet_key is required for live (set a real key, "
                "not REPLACE_ME)",
            )
        if not self.rpc_url:
            raise SwapError("rpc_error", "robinhood_chain.rpc_url is required for live")
        if not self.quote_token:
            raise SwapError(
                "swap_failed",
                "robinhood_chain.quote_token is required for live "
                "(USDG or WETH address)",
            )
        self._tx_backend = UniswapV3SwapBackend(
            client=self._get_client(),
            private_key=self._wallet_key,
            router=self.router,
            chain_id=self.chain_id,
            receipt_timeout=float(self.deadline_seconds) + 30.0,
        )
        return self._tx_backend

    def _require_live_config(self) -> None:
        if not self.rpc_url:
            raise SwapError("rpc_error", "robinhood_chain.rpc_url is required for live")
        if not self.quote_token:
            raise SwapError(
                "swap_failed",
                "robinhood_chain.quote_token is required for live "
                "(USDG or WETH address)",
            )
        if not self._has_wallet_key and self._tx_backend is None:
            raise SwapError(
                "swap_failed",
                "robinhood_chain.wallet_key is required for live (not REPLACE_ME)",
            )

    # -- paper path -----------------------------------------------------------------

    async def _paper_buy(
        self, address: str, amount_usd: float, **kwargs: Any
    ) -> dict[str, Any]:
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

    async def _paper_sell(
        self, address: str, fraction: float = 1.0, **kwargs: Any
    ) -> dict[str, Any]:
        fraction = max(0.0, min(float(fraction), 1.0))
        key = self._addr_key(address)
        pos = self._positions.get(key)
        tx_id = _synthetic_tx_id()
        if pos is None:
            log.info("RH paper sell address=%s — no open paper position", address)
            return {"tx_id": tx_id, "quantity": 0.0, "price": 0.0, "paper": True}

        sold_qty = pos.quantity * fraction
        price = float(pos.current_price or pos.entry_price or 0.0)
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
            "price": price,
            "paper": True,
        }

    # -- live helpers ---------------------------------------------------------------

    def _quote_decimals(self, backend: Any) -> int:
        # USDG is 6 decimals; WETH is 18. Prefer on-chain query when available.
        try:
            return int(backend.token_decimals(self.quote_token))
        except Exception:  # noqa: BLE001
            q = self.quote_token.lower()
            if q == DEFAULT_USDG.lower():
                return 6
            if q == DEFAULT_WETH.lower():
                return 18
            return 18

    def _amount_in_wei(
        self, amount_usd: float, backend: Any, **kwargs: Any
    ) -> int:
        if kwargs.get("amount_in_wei") is not None:
            return int(kwargs["amount_in_wei"])
        decimals = self._quote_decimals(backend)
        # USD price of one quote-token unit. USDG ≈ 1.0; WETH needs eth_price_usd.
        quote_price = float(
            kwargs.get("quote_price_usd")
            or kwargs.get("eth_price_usd")
            or (1.0 if self.quote_token.lower() == DEFAULT_USDG.lower() else 0.0)
        )
        if quote_price <= 0:
            raise SwapError(
                "swap_failed",
                "cannot size amount_in: pass amount_in_wei or quote_price_usd "
                "(required when quote_token is not USDG)",
            )
        units = float(amount_usd) / quote_price
        return int(units * (10**decimals))

    def _amount_out_min(
        self, amount_in: int, **kwargs: Any
    ) -> tuple[int, float]:
        """Return (amount_out_min_wei, expected_out_human_for_fill_price).

        Prefer kwargs ``expected_out`` (wei). Else estimate from token ``price``
        (USD per token) and quote notional.
        """
        slippage = self.max_slippage_bps
        if kwargs.get("slippage_bps") is not None:
            slippage = int(kwargs["slippage_bps"])

        if kwargs.get("amount_out_min") is not None:
            amin = int(kwargs["amount_out_min"])
            expected = float(kwargs.get("expected_out") or amin)
            return amin, expected

        if kwargs.get("expected_out") is not None:
            expected = int(kwargs["expected_out"])
            amin = expected * (10_000 - slippage) // 10_000
            return int(amin), float(expected)

        # Estimate from USD notional / token USD price → human qty, then scale
        # if token_decimals provided; else treat expected_out as human and
        # amount_out_min as 0-slippage floor in the same units when no decimals.
        token_price = float(kwargs.get("price") or kwargs.get("price_usd") or 0.0)
        amount_usd = float(kwargs.get("_amount_usd") or 0.0)
        if token_price > 0 and amount_usd > 0:
            expected_human = amount_usd / token_price
            token_decimals = kwargs.get("token_decimals")
            if token_decimals is not None:
                expected = int(expected_human * (10 ** int(token_decimals)))
                amin = expected * (10_000 - slippage) // 10_000
                return int(amin), float(expected)
            # No decimals — still enforce a min of 0 and report human qty later.
            return 0, expected_human

        # Conservative: no floor (rely on caller) — still apply 0 min.
        return 0, 0.0

    # -- public API -----------------------------------------------------------------

    async def buy(self, address: str, amount_usd: float, **kwargs: Any) -> dict[str, Any]:
        """Swap quote_token for ``address``. Returns {tx_id, quantity, price, paper}."""
        if self.paper:
            return await self._paper_buy(address, amount_usd, **kwargs)

        self._require_live_config()
        backend = self._get_tx_backend()
        try:
            amount_in = self._amount_in_wei(amount_usd, backend, **kwargs)
            kwargs = {**kwargs, "_amount_usd": float(amount_usd)}
            amount_out_min, expected_out = self._amount_out_min(amount_in, **kwargs)
            deadline = int(time.time()) + int(
                kwargs.get("deadline_seconds") or self.deadline_seconds
            )
            recipient = str(kwargs.get("recipient") or backend.address)
            fee = int(kwargs.get("fee") or kwargs.get("pool_fee") or self.pool_fee)

            result = backend.swap_exact_in(
                token_in=self.quote_token,
                token_out=address,
                amount_in=amount_in,
                amount_out_min=amount_out_min,
                recipient=recipient,
                fee=fee,
                deadline=deadline,
            )
        except SwapError:
            raise
        except Exception as exc:  # noqa: BLE001
            from .uniswap_v3 import classify_swap_error

            raise SwapError(classify_swap_error(exc), str(exc)) from None

        tx_hash = str(result.get("tx_hash") or result.get("tx_id") or "")
        raw_out = result.get("amount_out")
        token_price = float(kwargs.get("price") or kwargs.get("price_usd") or 0.0)

        if raw_out is not None and int(raw_out) > 0:
            token_decimals = kwargs.get("token_decimals")
            if token_decimals is not None:
                quantity = int(raw_out) / (10 ** int(token_decimals))
            elif expected_out and expected_out >= 1e6:
                # Treat raw_out as wei-scale matching expected_out.
                quantity = int(raw_out) / (10 ** 18) if int(raw_out) > 1e12 else float(raw_out)
            else:
                quantity = float(raw_out)
        elif token_price > 0:
            quantity = float(amount_usd) / token_price
        else:
            quantity = float(expected_out) if expected_out else 0.0

        fill_price = token_price if token_price > 0 else (
            float(amount_usd) / quantity if quantity else 0.0
        )
        log.info(
            "RH live buy address=%s amount_usd=%.4f qty=%.8f tx_id=%s",
            address,
            amount_usd,
            quantity,
            tx_hash,
        )
        return {
            "tx_id": tx_hash,
            "quantity": quantity,
            "price": fill_price,
            "paper": False,
        }

    async def sell(
        self, address: str, fraction: float = 1.0, **kwargs: Any
    ) -> dict[str, Any]:
        """Sell ``fraction`` of held balance back to quote_token."""
        if self.paper:
            return await self._paper_sell(address, fraction, **kwargs)

        self._require_live_config()
        fraction = max(0.0, min(float(fraction), 1.0))
        backend = self._get_tx_backend()
        try:
            bal = int(backend.balance_of(address))
            amount_in = int(bal * fraction)
            if amount_in <= 0:
                log.info(
                    "RH live sell address=%s — zero balance or fraction",
                    address,
                )
                return {"tx_id": "", "quantity": 0.0, "price": 0.0, "paper": False}

            amount_out_min = int(kwargs.get("amount_out_min") or 0)
            if kwargs.get("expected_out") is not None and amount_out_min == 0:
                expected = int(kwargs["expected_out"])
                amount_out_min = expected * (10_000 - self.max_slippage_bps) // 10_000

            deadline = int(time.time()) + int(
                kwargs.get("deadline_seconds") or self.deadline_seconds
            )
            recipient = str(kwargs.get("recipient") or backend.address)
            fee = int(kwargs.get("fee") or kwargs.get("pool_fee") or self.pool_fee)

            result = backend.swap_exact_in(
                token_in=address,
                token_out=self.quote_token,
                amount_in=amount_in,
                amount_out_min=amount_out_min,
                recipient=recipient,
                fee=fee,
                deadline=deadline,
            )
        except SwapError:
            raise
        except Exception as exc:  # noqa: BLE001
            from .uniswap_v3 import classify_swap_error

            raise SwapError(classify_swap_error(exc), str(exc)) from None

        tx_hash = str(result.get("tx_hash") or "")
        token_decimals = kwargs.get("token_decimals")
        if token_decimals is not None:
            sold_qty = amount_in / (10 ** int(token_decimals))
        else:
            sold_qty = float(amount_in)
        price = float(kwargs.get("price") or kwargs.get("price_usd") or 0.0)
        log.info(
            "RH live sell address=%s fraction=%.4f sold_qty=%.8f tx_id=%s",
            address,
            fraction,
            sold_qty,
            tx_hash,
        )
        return {
            "tx_id": tx_hash,
            "quantity": sold_qty,
            "price": price,
            "paper": False,
        }

    async def close_position(self, address: str) -> dict[str, Any]:
        """Full exit. Equivalent to sell(address, 1.0)."""
        return await self.sell(address, 1.0)

    async def tighten_stop(self, address: str, new_stop_price: float) -> dict[str, Any]:
        """Desk-side stop watcher — no on-chain stop order.

        Allowed in both paper and live; live updates the local paper book if
        present, otherwise just acknowledges the new stop for the desk.
        """
        key = self._addr_key(address)
        pos = self._positions.get(key)
        if pos is not None:
            pos.stop_price = float(new_stop_price)
        log.info(
            "RH tighten_stop address=%s new_stop=%.8f paper=%s",
            address,
            new_stop_price,
            self.paper,
        )
        return {
            "ok": True,
            "stop_price": float(new_stop_price),
            "paper": self.paper,
        }

    async def get_positions(self) -> list[Position]:
        """Paper: local book. Live: desk tracks positions — return []."""
        if self.paper:
            return list(self._positions.values())
        log.info(
            "RH live get_positions: desk-side book is source of truth; "
            "returning empty list from executor"
        )
        return []
