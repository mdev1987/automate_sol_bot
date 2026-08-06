"""Jupiter Swap API V2 client — /order + /execute managed swaps.

Two API calls, no RPC needed:

1. ``GET /swap/v2/order``  — Jupiter quotes and assembles a transaction
   (``transaction`` base64 + ``requestId``) with all routing engines
   competing for the best price.
2. ``POST /swap/v2/execute`` — we sign the transaction locally and Jupiter
   lands it with confirmation and retry.

We trade **USDC** only. Amounts are passed in raw base units (USDC has 6
decimals). Buy proceeds are captured from ``/execute``'s raw
``totalOutputAmount`` so we never need the token's decimals to sell later.

Selling is slippage-resilient: on failure we retry with escalating
slippage (config, then 300/500/1000 bps) before giving up.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from .config import USDC_DECIMALS, USDC_MINT, Settings

log = logging.getLogger(__name__)

# Slippage escalation ladder when a sell keeps failing (basis points).
SELL_SLIPPAGE_ESCALATION = (200, 300, 500, 1000)


class JupiterError(RuntimeError):
    """Raised when a swap order/execute fails and cannot be retried."""


@dataclass(frozen=True)
class SwapResult:
    """Outcome of an executed swap."""

    success: bool
    signature: str
    input_amount: int   # raw: what went in
    output_amount: int  # raw: what came out
    error: str = ""


class JupiterSwap:
    """Async wrapper around the managed /order + /execute swap path."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.jupiter_api
        self._headers = {"accept": "application/json"}
        if settings.jupiter_api_key:
            self._headers["x-api-key"] = settings.jupiter_api_key
        self._slippage_bps = settings.slippage_bps
        self._keypair: Optional[Keypair] = settings.keypair
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()

    @property
    def ready(self) -> bool:
        """False when we have no wallet key to sign with (dry-run only)."""
        return self._keypair is not None

    # ------------------------------------------------------------------- order
    async def _order(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int,
    ) -> dict:
        """Request an assembled swap transaction from Jupiter."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "taker": str(self._keypair.pubkey()),
            "slippageBps": slippage_bps,
        }
        resp = await self._client.get(
            f"{self._base}/swap/v2/order", params=params, headers=self._headers
        )
        if resp.status_code != 200:
            raise JupiterError(f"order HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        transaction = data.get("transaction")
        if not transaction:
            raise JupiterError(
                f"order failed: {data.get('errorMessage') or data.get('error') or data}"
            )
        return data

    # ---------------------------------------------------------------- signing
    def _sign(self, b64_transaction: str) -> str:
        """Sign a base64 transaction with the wallet keypair; return base64."""
        if self._keypair is None:
            raise JupiterError("no wallet key configured; cannot sign (dry-run?)")
        raw = base64.b64decode(b64_transaction)
        tx = VersionedTransaction.from_bytes(raw)
        # A Jupiter order tx arrives with an empty signature slot; sign its
        # message bytes and repopulate the single (wallet) signature.
        if any(sig != b"\x00" * 64 for sig in tx.signatures):
            log.warning("transaction already partially signed")
        signature = self._keypair.sign_message(tx.message.serialize())
        signed = VersionedTransaction.populate(tx.message, [signature])
        return base64.b64encode(bytes(signed)).decode()

    # ----------------------------------------------------------------- execute
    async def execute(self, order: dict) -> SwapResult:
        """POST the signed transaction to /execute managed landing."""
        signed = self._sign(order["transaction"])
        body = {
            "signedTransaction": signed,
            "requestId": order.get("requestId", ""),
        }
        resp = await self._client.post(
            f"{self._base}/swap/v2/execute", json=body, headers=self._headers
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or data.get("status") != "Success":
            return SwapResult(
                success=False,
                signature=data.get("signature", ""),
                input_amount=int(data.get("totalInputAmount") or 0),
                output_amount=int(data.get("totalOutputAmount") or 0),
                error=data.get("error") or f"execute HTTP {resp.status_code}",
            )
        return SwapResult(
            success=True,
            signature=data.get("signature", ""),
            input_amount=int(data.get("totalInputAmount") or 0),
            output_amount=int(data.get("totalOutputAmount") or 0),
        )

    # ------------------------------------------------------------- high level
    async def buy(self, mint: str, amount_usdc: float) -> SwapResult:
        """Buy ``amount_usdc`` worth of ``mint`` using USDC."""
        amount_raw = int(amount_usdc * (10 ** USDC_DECIMALS))
        order = await self._order(USDC_MINT, mint, amount_raw, self._slippage_bps)
        return await self.execute(order)

    async def sell(self, mint: str, amount_raw: int) -> SwapResult:
        """Sell ``amount_raw`` of ``mint`` for USDC, escalating slippage."""
        last: Optional[SwapResult] = None
        for slippage in (self._slippage_bps,) + SELL_SLIPPAGE_ESCALATION:
            try:
                order = await self._order(mint, USDC_MINT, amount_raw, slippage)
            except JupiterError as exc:
                log.warning("sell order @%dbps failed: %s", slippage, exc)
                last = SwapResult(False, "", amount_raw, 0, str(exc))
                continue
            result = await self.execute(order)
            if result.success:
                return result
            last = result
            log.warning("sell execute @%dbps failed: %s", slippage, result.error)
            # Keep climbing the ladder on any failure — a wider slippage bound
            # can still land on a thin book, and a failed exit is worse than
            # a slightly worse fill. Only the final rung stops.
            if slippage >= 1000:
                break
        return last or SwapResult(False, "", amount_raw, 0, "sell failed")