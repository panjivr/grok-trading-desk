"""Lazy web3 JSON-RPC client for Robinhood Chain.

Importing this module must never require ``web3`` — only the first call that
actually talks to the chain pulls the SDK in. Offline pytest stays green.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Official public endpoints (rate-limited). Prefer Alchemy in production.
DEFAULT_MAINNET_RPC = "https://rpc.mainnet.chain.robinhood.com"
DEFAULT_TESTNET_RPC = "https://rpc.testnet.chain.robinhood.com"
MAINNET_CHAIN_ID = 4663
TESTNET_CHAIN_ID = 46630


class RhChainClient:
    """Thin wrapper around an HTTP JSON-RPC endpoint."""

    def __init__(self, rpc_url: str, chain_id: int = MAINNET_CHAIN_ID):
        self.rpc_url = (rpc_url or DEFAULT_MAINNET_RPC).rstrip("/")
        self.chain_id = int(chain_id)
        self._web3: Any | None = None

    def _ensure_web3(self) -> Any:
        if self._web3 is not None:
            return self._web3
        try:
            from web3 import Web3  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised when web3 absent
            raise RuntimeError(
                "web3 is not installed; pip install web3 to use RhChainClient "
                "network methods (discovery/tests that stay offline never need it)"
            ) from exc
        self._web3 = Web3(Web3.HTTPProvider(self.rpc_url))
        return self._web3

    @property
    def web3(self) -> Any:
        """Lazy Web3 instance. Raises RuntimeError if the SDK is missing."""
        return self._ensure_web3()

    def get_block_number(self) -> int:
        """Return the latest block height. Requires web3 when called."""
        w3 = self._ensure_web3()
        return int(w3.eth.block_number)

    def is_connected(self) -> bool:
        """Best-effort connectivity probe (needs web3)."""
        try:
            return bool(self._ensure_web3().is_connected())
        except Exception as exc:  # noqa: BLE001
            log.debug("RH Chain RPC not connected: %s", exc)
            return False
