"""Robinhood Chain asset model (ERC-20 / Stock Token)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RhAsset(BaseModel):
    """An ERC-20 (often a Stock Token) on Robinhood Chain.

    Distinct from Solana ``Token``: no bonding-curve fields. Price and
    liquidity are expected from the watchlist entry or an injected enricher.
    """

    address: str
    symbol: str = ""
    name: str = ""
    decimals: int = 18
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    holders: int | None = None
    underlying: str = ""  # e.g. AAPL if Stock Token
    is_stock_token: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)
    seen_at: datetime = Field(default_factory=_utcnow)

    @property
    def address_norm(self) -> str:
        return (self.address or "").strip().lower()
