"""DexScreener HTTP client for Robinhood Chain pool discovery (PR3).

Preferred source — no API key. Chain slug is the string ``robinhood``
(not numeric chain id 4663).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.dexscreener.com"
DEFAULT_CHAIN_SLUG = "robinhood"

HttpGet = Callable[[str], Any]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _token_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"address": "", "symbol": "", "name": ""}
    return {
        "address": str(raw.get("address") or "").strip(),
        "symbol": str(raw.get("symbol") or ""),
        "name": str(raw.get("name") or raw.get("symbol") or ""),
    }


class DexScreenerClient:
    """Thin DexScreener client with injectable ``http_get`` for offline tests."""

    def __init__(
        self,
        http_get: HttpGet | None = None,
        base_url: str = DEFAULT_BASE_URL,
        chain_slug: str = DEFAULT_CHAIN_SLUG,
    ):
        self.http_get = http_get
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.chain_slug = chain_slug or DEFAULT_CHAIN_SLUG

    def _urllib_get(self, url: str) -> Any:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "grok-trading-desk-rh-chain/pr3", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — public API
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else []

    def _request(self, url: str) -> Any:
        getter = self.http_get if self.http_get is not None else self._urllib_get
        return getter(url)

    @staticmethod
    def _as_pair_list(payload: Any) -> list[dict[str, Any]]:
        """Accept bare arrays or ``{pairs: [...]}`` / ``{pair: {...}}`` wrappers."""
        if payload is None:
            return []
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            if isinstance(payload.get("pairs"), list):
                return [p for p in payload["pairs"] if isinstance(p, dict)]
            if isinstance(payload.get("pair"), dict):
                return [payload["pair"]]
            # Single pair-shaped object
            if "pairAddress" in payload or "baseToken" in payload:
                return [payload]
        return []

    def pools_for_token(self, address: str) -> list[dict[str, Any]]:
        """GET /token-pairs/v1/{chain}/{tokenAddress} — never raises on empty/errors."""
        addr = (address or "").strip()
        if not addr:
            return []
        url = f"{self.base_url}/token-pairs/v1/{self.chain_slug}/{addr}"
        try:
            payload = self._request(url)
            return self._as_pair_list(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("dexscreener pools_for_token(%s) failed: %s", addr, exc)
            return []

    def search(self, query: str) -> list[dict[str, Any]]:
        """GET /latest/dex/search?q=… then keep pairs with chainId == robinhood."""
        q = (query or "").strip()
        if not q:
            return []
        url = f"{self.base_url}/latest/dex/search?q={urllib.parse.quote(q)}"
        try:
            payload = self._request(url)
            pairs = self._as_pair_list(payload)
            slug = self.chain_slug.lower()
            return [
                p
                for p in pairs
                if str(p.get("chainId") or "").lower() == slug
            ]
        except Exception as exc:  # noqa: BLE001
            log.warning("dexscreener search(%r) failed: %s", q, exc)
            return []

    def normalize_pair(self, pair: dict[str, Any] | None) -> dict[str, Any]:
        """Flatten a DexScreener pair into typed numeric fields + token sides."""
        if not isinstance(pair, dict):
            return {}
        base = _token_dict(pair.get("baseToken"))
        quote = _token_dict(pair.get("quoteToken"))
        liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
        return {
            "chain_id": str(pair.get("chainId") or ""),
            "dex_id": str(pair.get("dexId") or ""),
            "pair_address": str(pair.get("pairAddress") or "").strip(),
            "url": str(pair.get("url") or ""),
            "base_token": base,
            "quote_token": quote,
            "price_usd": _as_float(pair.get("priceUsd")),
            "liquidity_usd": _as_float(liquidity.get("usd")),
            "volume_24h_usd": _as_float(volume.get("h24")),
            "raw_pair": pair,
        }
