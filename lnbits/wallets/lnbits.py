import asyncio
import json
from typing import AsyncGenerator, Dict, Optional

import httpx
from loguru import logger

from lnbits.settings import settings

from .base import (
    InvoiceResponse,
    InvoiceResponseFailed,
    InvoiceResponseSuccess,
    PaymentResponse,
    PaymentResponseFailed,
    PaymentResponseSuccess,
    PaymentStatus,
    PaymentStatusFailed,
    PaymentStatusMap,
    PaymentStatusPending,
    PaymentStatusSuccess,
    StatusResponse,
    Wallet,
)


class LNbitsWallet(Wallet):
    """https://github.com/lnbits/lnbits"""

    def __init__(self):
        if not settings.lnbits_endpoint:
            raise ValueError("cannot initialize LNbitsWallet: missing lnbits_endpoint")
        key = (
            settings.lnbits_key
            or settings.lnbits_admin_key
            or settings.lnbits_invoice_key
        )
        if not key:
            raise ValueError(
                "cannot initialize LNbitsWallet: "
                "missing lnbits_key or lnbits_admin_key or lnbits_invoice_key"
            )
        self.endpoint = self.normalize_endpoint(settings.lnbits_endpoint)
        self.headers = {"X-Api-Key": key, "User-Agent": settings.user_agent}
        self.client = httpx.AsyncClient(base_url=self.endpoint, headers=self.headers)

    @property
    def payment_status_map(self) -> PaymentStatusMap:
        return PaymentStatusMap(
            success=[True],
            failed=[False],
            pending=[None],
        )

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")

    async def status(self) -> StatusResponse:
        try:
            r = await self.client.get(url="/api/v1/wallet", timeout=15)
        except Exception as exc:
            return StatusResponse(
                f"Failed to connect to {self.endpoint} due to: {exc}", 0
            )

        try:
            data = r.json()
        except Exception:
            return StatusResponse(
                f"Failed to connect to {self.endpoint}, got: '{r.text[:200]}...'", 0
            )

        if r.is_error:
            return StatusResponse(data["detail"], 0)

        return StatusResponse(None, data["balance"])

    async def create_invoice(
        self,
        amount: int,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        **kwargs,
    ) -> InvoiceResponse:
        data: Dict = {"out": False, "amount": amount, "memo": memo or ""}
        if kwargs.get("expiry"):
            data["expiry"] = kwargs["expiry"]
        if description_hash:
            data["description_hash"] = description_hash.hex()
        if unhashed_description:
            data["unhashed_description"] = unhashed_description.hex()

        r = await self.client.post(url="/api/v1/payments", json=data)

        data = r.json()
        if r.is_error:
            return InvoiceResponseFailed(error_message=data["detail"])

        return InvoiceResponseSuccess(
            checking_id=data["checking_id"], payment_request=data["payment_request"]
        )

    async def pay_invoice(self, bolt11: str, fee_limit_msat: int) -> PaymentResponse:
        r = await self.client.post(
            url="/api/v1/payments",
            json={"out": True, "bolt11": bolt11},
            timeout=None,
        )

        if r.is_error:
            return PaymentResponseFailed(error_message=r.json()["detail"])

        data = r.json()
        checking_id = data["payment_hash"]

        # we do this to get the fee and preimage
        payment: PaymentStatus = await self.get_payment_status(checking_id)

        return PaymentResponseSuccess(
            checking_id=checking_id,
            fee_msat=payment.fee_msat,
            preimage=payment.preimage,
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        try:
            r = await self.client.get(
                url=f"/api/v1/payments/{checking_id}",
            )
            r.raise_for_status()

            data = r.json()
            details = data.get("details", None)

            if details and details.get("pending", False) is True:
                return PaymentStatusPending()
            if data.get("paid", False) is True:
                return PaymentStatusSuccess()
            return PaymentStatusFailed()
        except Exception:
            return PaymentStatusPending()

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        r = await self.client.get(url=f"/api/v1/payments/{checking_id}")

        if r.is_error:
            return PaymentStatusPending()
        data = r.json()

        if "paid" not in data or not data["paid"]:
            return PaymentStatusPending()

        if "details" not in data:
            return PaymentStatusPending()

        return PaymentStatusSuccess(
            fee_msat=data["details"]["fee"], preimage=data["preimage"]
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        url = f"{self.endpoint}/api/v1/payments/sse"

        while True:
            try:
                async with httpx.AsyncClient(
                    timeout=None, headers=self.headers
                ) as client:
                    del client.headers[
                        "accept-encoding"
                    ]  # we have to disable compression for SSEs
                    async with client.stream(
                        "GET", url, content="text/event-stream"
                    ) as r:
                        sse_trigger = False
                        async for line in r.aiter_lines():
                            # The data we want to listen to is of this shape:
                            # event: payment-received
                            # data: {.., "payment_hash" : "asd"}
                            if line.startswith("event: payment-received"):
                                sse_trigger = True
                                continue
                            elif sse_trigger and line.startswith("data:"):
                                data = json.loads(line[len("data:") :])
                                sse_trigger = False
                                yield data["payment_hash"]
                            else:
                                sse_trigger = False

            except (OSError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout):
                pass

            logger.error(
                "lost connection to lnbits /payments/sse, retrying in 5 seconds"
            )
            await asyncio.sleep(5)
