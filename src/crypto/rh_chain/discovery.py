"""Watchlist + DexScreener pool-scan discovery for Robinhood Chain (PR3).

``discovery.mode``:
  - ``watchlist`` (default) — PR1 path, offline when entries carry price/liq
  - ``pool_scan`` — DexScreener pools around seed tokens (WETH / USDG)
  - ``both`` — watchlist ∪ pool_scan, deduped by address (watchlist wins)

Does **not** depend on PR2 live executor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable

from .client import RhChainClient
from .dexscreener import DexScreenerClient
from .models import RhAsset

log = logging.getLogger(__name__)

Enricher = Callable[[RhAsset], Awaitable[RhAsset] | RhAsset]

# Official RH Chain seeds (docs / PR2 config). Do not invent stock-token addresses.
DEFAULT_USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
DEFAULT_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
# Known impostor USDG — always exclude from candidates.
DEFAULT_EXCLUDE = ["0x8218d73C00567A01481495Ad6c5143e00D5BB5b4"]

KNOWN_MODES = frozenset({"watchlist", "pool_scan", "both", ""})


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


def _norm_addr(value: str) -> str:
    return (value or "").strip().lower()


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
    """Batch discovery — watchlist, DexScreener pool_scan, or both."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: RhChainClient | None = None,
        enricher: Enricher | None = None,
        dex_client: DexScreenerClient | None = None,
    ):
        self.config = config or {}
        rh = self.config.get("robinhood_chain") or {}
        discovery = rh.get("discovery") or {}
        self.mode = str(discovery.get("mode") or "watchlist").lower()
        self.watchlist: list[dict[str, Any]] = list(discovery.get("watchlist") or [])
        self.rpc_url = str(rh.get("rpc_url") or "")
        self.chain_id = int(rh.get("chain_id") or 4663)
        self._rh = rh
        self._discovery_cfg = discovery
        self._filter_cfg = dict(rh.get("filter") or {})
        self._pool_scan_cfg = dict(discovery.get("pool_scan") or {})
        self._client = client
        self._enricher = enricher
        self._dex = dex_client

        # Official quote / WETH from parent rh config (seeds for pool_scan).
        self.quote_token = str(rh.get("quote_token") or DEFAULT_USDG).strip()
        self.weth = str(rh.get("weth") or DEFAULT_WETH).strip()

    @property
    def client(self) -> RhChainClient | None:
        return self._client

    @property
    def dex_client(self) -> DexScreenerClient:
        if self._dex is None:
            self._dex = DexScreenerClient()
        return self._dex

    def watchlist_addresses(self) -> set[str]:
        return {
            _norm_addr(str(e.get("address") or ""))
            for e in self.watchlist
            if e.get("address")
        }

    def scoring_allowlist(self) -> set[str] | None:
        """Allowlist for ``score_rh_asset(..., allowlist=...)``.

        - ``watchlist`` mode → watchlist addresses (enforce allowlist)
        - ``pool_scan`` / ``both`` → ``None`` (skip allowlist veto; discoveries
          are not on the curated list). Desk may also set
          ``filter.require_allowlist: false``.
        """
        if self.mode in {"pool_scan", "both"}:
            return None
        return self.watchlist_addresses()

    def _seed_addresses(self) -> list[str]:
        cfg_seeds = self._pool_scan_cfg.get("seeds")
        if isinstance(cfg_seeds, list) and cfg_seeds:
            out = [_norm_addr(str(s)) for s in cfg_seeds if str(s).strip()]
            return out
        return [_norm_addr(self.quote_token), _norm_addr(self.weth)]

    def _exclude_addresses(self) -> set[str]:
        raw = self._pool_scan_cfg.get("exclude_addresses")
        if raw is None:
            raw = list(DEFAULT_EXCLUDE)
        return {_norm_addr(str(a)) for a in (raw or []) if str(a).strip()}

    def _min_liquidity_usd(self) -> float | None:
        val = self._pool_scan_cfg.get("min_liquidity_usd", None)
        if val is None:
            # Fall back to filter.min_liquidity_usd when pool_scan leaves it null.
            if "min_liquidity_usd" in self._filter_cfg:
                try:
                    return float(self._filter_cfg["min_liquidity_usd"])
                except (TypeError, ValueError):
                    return None
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _max_results(self) -> int:
        try:
            return max(0, int(self._pool_scan_cfg.get("max_results", 50)))
        except (TypeError, ValueError):
            return 50

    def _search_queries(self) -> list[str]:
        qs = self._pool_scan_cfg.get("search_queries") or []
        if not isinstance(qs, list):
            return []
        return [str(q).strip() for q in qs if str(q).strip()]

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

    async def _run_watchlist(self) -> list[RhAsset]:
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

    def _candidate_from_normalized(
        self,
        norm: dict[str, Any],
        seeds: set[str],
        exclude: set[str],
    ) -> RhAsset | None:
        """Pick non-seed side (or baseToken if neither is seed) → RhAsset."""
        base = norm.get("base_token") or {}
        quote = norm.get("quote_token") or {}
        base_addr = _norm_addr(str(base.get("address") or ""))
        quote_addr = _norm_addr(str(quote.get("address") or ""))
        if not base_addr and not quote_addr:
            return None

        base_is_seed = base_addr in seeds
        quote_is_seed = quote_addr in seeds

        if base_is_seed and not quote_is_seed:
            cand_addr, cand_meta, is_base = quote_addr, quote, False
        elif quote_is_seed and not base_is_seed:
            cand_addr, cand_meta, is_base = base_addr, base, True
        elif not base_is_seed and not quote_is_seed:
            # Neither side is a known seed — take baseToken as candidate.
            cand_addr, cand_meta, is_base = base_addr, base, True
        else:
            # Both seeds (e.g. WETH/USDG pool) — not a discovery target.
            return None

        if not cand_addr or cand_addr in exclude or cand_addr in seeds:
            return None

        liq = float(norm.get("liquidity_usd") or 0.0)
        min_liq = self._min_liquidity_usd()
        if min_liq is not None and liq < min_liq:
            return None

        # priceUsd on DexScreener is the base token's USD price.
        price = float(norm.get("price_usd") or 0.0) if is_base else 0.0

        raw = {
            "pair_address": norm.get("pair_address") or "",
            "dex_id": norm.get("dex_id") or "",
            "pool_scan": True,
            "chain_id_slug": norm.get("chain_id") or "",
            "url": norm.get("url") or "",
            "candidate_is_base": is_base,
        }
        return RhAsset(
            address=cand_addr,
            symbol=str(cand_meta.get("symbol") or ""),
            name=str(cand_meta.get("name") or cand_meta.get("symbol") or ""),
            decimals=18,
            price_usd=price,
            liquidity_usd=liq,
            volume_24h_usd=float(norm.get("volume_24h_usd") or 0.0),
            holders=None,
            underlying="",
            is_stock_token=False,
            raw=raw,
        )

    async def _fetch_pairs_for_seed(self, seed: str) -> list[dict[str, Any]]:
        dex = self.dex_client
        try:
            return await asyncio.to_thread(dex.pools_for_token, seed)
        except Exception as exc:  # noqa: BLE001
            log.warning("pool_scan pools_for_token(%s) error: %s", seed, exc)
            return []

    async def _fetch_search(self, query: str) -> list[dict[str, Any]]:
        dex = self.dex_client
        try:
            return await asyncio.to_thread(dex.search, query)
        except Exception as exc:  # noqa: BLE001
            log.warning("pool_scan search(%r) error: %s", query, exc)
            return []

    async def _run_pool_scan(self) -> list[RhAsset]:
        """DexScreener pool scan around configured seeds (+ optional search).

        Soft-fail: network/client errors log a warning and yield [].
        """
        try:
            seeds_list = self._seed_addresses()
            seeds = set(seeds_list)
            exclude = self._exclude_addresses() | seeds
            dex = self.dex_client

            raw_pairs: list[dict[str, Any]] = []
            for seed in seeds_list:
                raw_pairs.extend(await self._fetch_pairs_for_seed(seed))
            for query in self._search_queries():
                raw_pairs.extend(await self._fetch_search(query))

            by_addr: dict[str, RhAsset] = {}
            for pair in raw_pairs:
                try:
                    norm = dex.normalize_pair(pair)
                    if not norm:
                        continue
                    # Drop non-robinhood pairs that slipped through search.
                    cid = str(norm.get("chain_id") or "").lower()
                    if cid and cid != "robinhood":
                        continue
                    asset = self._candidate_from_normalized(norm, seeds, exclude)
                    if asset is None:
                        continue
                    key = asset.address_norm
                    prev = by_addr.get(key)
                    if prev is None or asset.liquidity_usd > prev.liquidity_usd:
                        by_addr[key] = asset
                except Exception as exc:  # noqa: BLE001
                    log.warning("pool_scan pair normalize skipped: %s", exc)
                    continue

            ranked = sorted(
                by_addr.values(),
                key=lambda a: float(a.liquidity_usd or 0.0),
                reverse=True,
            )
            max_n = self._max_results()
            assets = ranked[:max_n] if max_n else []
            out: list[RhAsset] = []
            for asset in assets:
                out.append(await self._maybe_enrich(asset))
            log.info("rh discovery: %d pool_scan asset(s)", len(out))
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("pool_scan soft-fail: %s", exc)
            return []

    @staticmethod
    def _dedupe_by_address(assets: list[RhAsset]) -> list[RhAsset]:
        """First occurrence wins (call with watchlist before pool_scan)."""
        seen: set[str] = set()
        out: list[RhAsset] = []
        for asset in assets:
            key = asset.address_norm
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(asset)
        return out

    async def run(self) -> list[RhAsset]:
        """Return RhAsset candidates per ``discovery.mode``."""
        if self.mode not in KNOWN_MODES:
            log.warning("discovery mode %r unknown — using watchlist", self.mode)
            return await self._run_watchlist()

        if self.mode in {"watchlist", ""}:
            return await self._run_watchlist()

        if self.mode == "pool_scan":
            return await self._run_pool_scan()

        # both: watchlist ∪ pool_scan, dedupe (watchlist preferred)
        watchlist_assets = await self._run_watchlist()
        try:
            scan_assets = await self._run_pool_scan()
        except Exception as exc:  # noqa: BLE001
            log.warning("pool_scan soft-fail in both mode — watchlist only: %s", exc)
            scan_assets = []
        merged = self._dedupe_by_address(watchlist_assets + scan_assets)
        log.info(
            "rh discovery both: %d watchlist + %d scan → %d unique",
            len(watchlist_assets),
            len(scan_assets),
            len(merged),
        )
        return merged

    def iter_watchlist(self) -> Iterable[dict[str, Any]]:
        return iter(self.watchlist)
