"""Minimal Uniswap V3 SwapRouter02 backend for Robinhood Chain.

Lazy-imports ``web3`` / ``eth_account`` so offline pytest never needs them.
Never logs or stores the private key in ``__repr__``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Official RH Chain Uniswap v3 deployments (docs.robinhood.com/chain/contracts/).
DEFAULT_SWAP_ROUTER02 = "0xcaf681a66d020601342297493863e78c959e5cb2"
DEFAULT_UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
DEFAULT_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
DEFAULT_QUOTER_V2 = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
DEFAULT_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
DEFAULT_USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"  # Global Dollar, 6 decimals

ERC20_ABI: list[dict[str, Any]] = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

# SwapRouter02 exactInputSingle — no deadline field (unlike V3 SwapRouter).
SWAP_ROUTER02_ABI: list[dict[str, Any]] = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]


class SwapError(Exception):
    """Classified swap / RPC failure with a stable ``reason`` slug."""

    def __init__(self, reason: str, detail: str = ""):
        # Never put secrets in detail — callers must scrub first.
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def classify_swap_error(exc: BaseException) -> str:
    """Map a web3 / RPC exception onto a stable reason slug."""
    text = str(exc).lower()
    if "insufficient funds" in text or "gas required exceeds" in text:
        return "insufficient_gas"
    if "execution reverted" in text or "revert" in text:
        return "swap_reverted"
    if any(
        needle in text
        for needle in (
            "connection",
            "timeout",
            "503",
            "502",
            "429",
            "rpc",
            "http",
            "provider",
        )
    ):
        return "rpc_error"
    return "swap_failed"


def _scrub(text: str, *secrets: str) -> str:
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "<redacted>")
    return out


class UniswapV3SwapBackend:
    """Signs and broadcasts SwapRouter02 ``exactInputSingle`` swaps.

    The private key is held only as a private attribute and is never included
    in ``__repr__`` / logs. Prefer injecting a mock backend in unit tests.
    """

    def __init__(
        self,
        client: Any,
        private_key: str,
        router: str = DEFAULT_SWAP_ROUTER02,
        chain_id: int = 4663,
        receipt_timeout: float = 120.0,
    ):
        self._client = client
        self._private_key = private_key  # never log
        self.router = router
        self.chain_id = int(chain_id)
        self.receipt_timeout = float(receipt_timeout)
        self._account: Any | None = None
        self._address: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - safety
        return (
            f"UniswapV3SwapBackend(router={self.router!r}, "
            f"chain_id={self.chain_id}, address={self.address!r})"
        )

    def _load_account(self) -> Any:
        if self._account is not None:
            return self._account
        try:
            from eth_account import Account  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "eth_account is not installed; pip install eth-account for live swaps"
            ) from exc
        self._account = Account.from_key(self._private_key)
        self._address = self._account.address
        return self._account

    @property
    def address(self) -> str:
        if self._address is None:
            self._load_account()
        assert self._address is not None
        return self._address

    def _w3(self) -> Any:
        # RhChainClient exposes .web3; plain Web3 instances work too.
        if hasattr(self._client, "web3"):
            return self._client.web3
        return self._client

    def _checksum(self, addr: str) -> str:
        from web3 import Web3  # type: ignore[import-untyped]

        return Web3.to_checksum_address(addr)

    def erc20(self, token: str) -> Any:
        w3 = self._w3()
        return w3.eth.contract(
            address=self._checksum(token),
            abi=ERC20_ABI,
        )

    def router_contract(self) -> Any:
        w3 = self._w3()
        return w3.eth.contract(
            address=self._checksum(self.router),
            abi=SWAP_ROUTER02_ABI,
        )

    def token_decimals(self, token: str) -> int:
        return int(self.erc20(token).functions.decimals().call())

    def balance_of(self, token: str, owner: str | None = None) -> int:
        who = owner or self.address
        return int(self.erc20(token).functions.balanceOf(self._checksum(who)).call())

    def _ensure_allowance(self, token: str, amount: int) -> str | None:
        """Approve router if allowance is insufficient. Returns approve tx hash or None."""
        token_c = self.erc20(token)
        router = self._checksum(self.router)
        owner = self._checksum(self.address)
        current = int(token_c.functions.allowance(owner, router).call())
        if current >= amount:
            return None
        # Approve max uint256 to avoid repeated approvals.
        max_uint = (1 << 256) - 1
        return self._send_contract_tx(
            token_c.functions.approve(router, max_uint),
            value=0,
        )

    def _send_contract_tx(self, fn: Any, value: int = 0) -> str:
        w3 = self._w3()
        account = self._load_account()
        try:
            nonce = w3.eth.get_transaction_count(self.address)
            tx = fn.build_transaction(
                {
                    "from": self.address,
                    "nonce": nonce,
                    "chainId": self.chain_id,
                    "value": int(value),
                }
            )
            # Fill gas if the node did not.
            if "gas" not in tx or not tx["gas"]:
                tx["gas"] = int(fn.estimate_gas({"from": self.address, "value": int(value)}))
            signed = account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(
                signed, "rawTransaction", None
            )
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self.receipt_timeout
            )
            if int(getattr(receipt, "status", 0) or 0) != 1:
                raise SwapError("swap_reverted", "transaction status != 1")
            return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        except SwapError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = classify_swap_error(exc)
            detail = _scrub(str(exc), self._private_key)
            raise SwapError(reason, detail) from None

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
        """Approve (if needed) then ``exactInputSingle``.

        ``deadline`` is enforced locally (SwapRouter02 has no deadline field).
        Returns ``{tx_hash, amount_out}`` — ``amount_out`` may be ``None`` if
        the receipt cannot be decoded.
        """
        now = int(time.time())
        if int(deadline) and now > int(deadline):
            raise SwapError("swap_reverted", "deadline expired before broadcast")

        amount_in = int(amount_in)
        amount_out_min = int(amount_out_min)
        if amount_in <= 0:
            raise SwapError("swap_failed", "amount_in must be positive")

        self._ensure_allowance(token_in, amount_in)

        router = self.router_contract()
        params = (
            self._checksum(token_in),
            self._checksum(token_out),
            int(fee),
            self._checksum(recipient),
            amount_in,
            amount_out_min,
            0,  # sqrtPriceLimitX96
        )
        bal_before = 0
        try:
            bal_before = self.balance_of(token_out, recipient)
        except Exception:  # noqa: BLE001 - best-effort fill parsing
            bal_before = 0

        tx_hash = self._send_contract_tx(
            router.functions.exactInputSingle(params),
            value=0,
        )

        amount_out: int | None = None
        try:
            bal_after = self.balance_of(token_out, recipient)
            delta = bal_after - bal_before
            if delta > 0:
                amount_out = int(delta)
        except Exception:  # noqa: BLE001
            amount_out = None

        log.info(
            "RH Uniswap exactInputSingle tx_hash=%s fee=%s amount_in=%s amount_out=%s",
            tx_hash,
            fee,
            amount_in,
            amount_out,
        )
        return {"tx_hash": tx_hash, "amount_out": amount_out}
