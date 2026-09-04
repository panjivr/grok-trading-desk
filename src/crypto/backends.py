"""Crypto backend selector for Solana vs Robinhood Chain.

Default remains ``solana`` so existing desks see zero behavior change.
"""

from __future__ import annotations

from typing import Any, Literal

CryptoBackend = Literal["solana", "robinhood_chain"]

BACKEND_SOLANA: CryptoBackend = "solana"
BACKEND_ROBINHOOD_CHAIN: CryptoBackend = "robinhood_chain"

_KNOWN = frozenset({BACKEND_SOLANA, BACKEND_ROBINHOOD_CHAIN})


def get_crypto_backend(config: dict[str, Any] | None) -> CryptoBackend:
    """Return the configured crypto backend name (lowercased).

    Falls back to ``solana`` when ``crypto.backend`` is absent or empty.
    Unknown values also fall back to ``solana`` so a typo never silently
    disables the existing pump.fun path.
    """
    raw = ((config or {}).get("crypto") or {}).get("backend", BACKEND_SOLANA)
    name = str(raw or BACKEND_SOLANA).strip().lower()
    if name not in _KNOWN:
        return BACKEND_SOLANA
    return name  # type: ignore[return-value]


def is_robinhood_chain(config: dict[str, Any] | None) -> bool:
    return get_crypto_backend(config) == BACKEND_ROBINHOOD_CHAIN


def is_solana(config: dict[str, Any] | None) -> bool:
    return get_crypto_backend(config) == BACKEND_SOLANA


def rh_chain_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ``robinhood_chain`` section (possibly empty)."""
    return dict(((config or {}).get("robinhood_chain") or {}))


def build_rh_discovery(config: dict[str, Any]):
    """Construct watchlist discovery for the RH backend."""
    from .rh_chain.discovery import RhDiscovery

    return RhDiscovery(config)


def build_rh_executor(config: dict[str, Any], live_ack: bool = False):
    """Construct the RH paper / live Uniswap executor."""
    from .rh_chain.rh_executor import RhExecutor

    return RhExecutor(config, live_ack=live_ack)


def build_crypto_stack(
    config: dict[str, Any], live_ack: bool = False
) -> dict[str, Any]:
    """Factory used by desk wiring.

    Returns a dict with ``backend`` plus either Solana-oriented placeholders
    (``scout`` / ``executor`` left to desk.py existing construction) or RH
    objects (``discovery`` + ``executor``).
    """
    backend = get_crypto_backend(config)
    if backend == BACKEND_ROBINHOOD_CHAIN:
        return {
            "backend": backend,
            "discovery": build_rh_discovery(config),
            "executor": build_rh_executor(config, live_ack=live_ack),
            "scout": None,
        }
    return {
        "backend": backend,
        "discovery": None,
        "executor": None,  # desk keeps constructing CryptoExecutor
        "scout": None,  # desk keeps constructing Scout
    }
