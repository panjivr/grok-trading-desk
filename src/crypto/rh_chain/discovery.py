"""Watchlist discovery for Robinhood Chain (PR1).

No PumpPortal / pool scan. Candidates come from
``robinhood_chain.discovery.watchlist``. When price/liquidity are already on
the entry, ``run()`` never touches the network.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from .client import RhChainClient
from .models import RhAsset

log = logging.getLogger(__name__)

Enricher = Callable[[RhAsset], Awaitable[RhAsset] | RhAsset]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entry_to_asset(entry: dict[str, Any], allowlist_addrs: set[str]) -> RhAsset | None:
    address = str(entry.get("address") or "").strip()
    if not address:
        log.warning("watchlist entry missing address — skipped")
        return None
    raw = dict(entry.get("raw") or {})
    # Stash allowlist so scoring can validate without a separate arg.
    raw.setdefault("allowlist_addresses", sorted(allowlist_addrs))
    raw.setdefault("watchlist_entry", True)
    holders = _as_int(entry.get("holders"))
    return RhAsset(
        address=address,
        symbol=str(entry.get("symbol") or ""),
        name=str(entry.get("name") or entry.get("symbol") or ""),
        decimals=int(entry.get("decimals") or 18),
        price_usd=_as_float(entry.get("price_usd")),
        liquidity_usd=_as_float(entry.get("liquidity_usd")),
        volume_24h_usd=_as_float(entry.get("volume_24h_usd")),
        holders=holders,
        underlying=str(entry.get("underlying") or ""),
        is_stock_token=bool(entry.get("is_stock_token", bool(entry.get("underlying")))),
        raw=raw,
    )


class RhDiscovery:
    """Batch discovery — watchlist mode only in PR1."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: RhChainClient | None = None,
        enricher: Enricher | None = None,
    ):
        self.config = config or {}
        rh = self.config.get("robinhood_chain") or {}
        discovery = rh.get("discovery") or {}
        self.mode = str(discovery.get("mode") or "watchlist").lower()
        self.watchlist: list[dict[str, Any]] = list(discovery.get("watchlist") or [])
        self.rpc_url = str(rh.get("rpc_url") or "")
        self.chain_id = int(rh.get("chain_id") or 4663)
        self._client = client
        self._enricher = enricher

    @property
    def client(self) -> RhChainClient | None:
        return self._client

    def watchlist_addresses(self) -> set[str]:
        return {
            str(e.get("address") or "").strip().lower()
            for e in self.watchlist
            if e.get("address")
        }

    def _get_or_build_client(self) -> RhChainClient:
        if self._client is None:
            self._client = RhChainClient(self.rpc_url, self.chain_id)
        return self._client

    async def _maybe_enrich(self, asset: RhAsset) -> RhAsset:
        if self._enricher is None:
            return asset
        result = self._enricher(asset)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[misc]
        return result  # type: ignore[return-value]

    async def run(self) -> list[RhAsset]:
        """Return RhAsset candidates from the configured watchlist.

        Default path is offline when entries already carry price/liquidity.
        An optional ``enricher`` / ``client`` can fill gaps; PR1 never calls
        RPC unless the caller injects that behavior.
        """
        if self.mode not in {"watchlist", ""}:
            log.warning("discovery mode %r not supported in PR1 — using watchlist", self.mode)

        allowlist = self.watchlist_addresses()
        assets: list[RhAsset] = []
        for entry in self.watchlist:
            if not isinstance(entry, dict):
                continue
            asset = _entry_to_asset(entry, allowlist)
            if asset is None:
                continue
            asset = await self._maybe_enrich(asset)
            assets.append(asset)

        log.info("rh discovery: %d watchlist asset(s)", len(assets))
        return assets

    def iter_watchlist(self) -> Iterable[dict[str, Any]]:
        return iter(self.watchlist)
