"""Robinhood Chain crypto backend (watchlist + paper/live Uniswap executor)."""

from .discovery import RhDiscovery
from .models import RhAsset
from .rh_executor import RhExecutor
from .rh_scoring import score_rh_asset
from .uniswap_v3 import (
    DEFAULT_SWAP_ROUTER02,
    DEFAULT_USDG,
    DEFAULT_WETH,
    SwapError,
    UniswapV3SwapBackend,
)

__all__ = [
    "RhAsset",
    "RhDiscovery",
    "RhExecutor",
    "SwapError",
    "UniswapV3SwapBackend",
    "DEFAULT_SWAP_ROUTER02",
    "DEFAULT_USDG",
    "DEFAULT_WETH",
    "score_rh_asset",
]
