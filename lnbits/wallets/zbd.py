import asyncio
from typing import AsyncGenerator, Dict, Optional

import httpx
from loguru import logger

from lnbits import bolt11
from lnbits.settings import settings

from .base import (
    InvoiceResponse,
    InvoiceResponseFailed,
    InvoiceResponseSuccess,
    PaymentResponse,
    PaymentResponseFailed,
    PaymentResponseSuccess,
    PaymentStatus,
    PaymentStatusMap,
    PaymentStatusPending,
    StatusResponse,
    Unsupported,
    Wallet,
)


class ZBDWallet(Wallet):
    """https://zbd.dev/api-reference/"""

    def __init__(self):
        if not settings.zbd_api_endpoint:
            raise ValueError("cannot initialize ZBDWallet: missing zbd_api_endpoint")
        if not settings.zbd_api_key:
            raise ValueError("cannot initialize ZBDWallet: missing zbd_api_key")

        self.endpoint = self.normalize_endpoint(settings.zbd_api_endpoint)
        headers = {
            "apikey": settings.zbd_api_key,
            "User-Agent": settings.user_agent,
        }
        self.client = httpx.AsyncClient(base_url=self.endpoint, headers=headers)

    @property
    def payment_status_map(self) -> PaymentStatusMap:
        return PaymentStatusMap(
            success=["completed", "paid"],
            failed=["failed", "expired"],
            pending=["initial", "pending", "error", "unpaid"],
        )

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")

    async def status(self) -> StatusResponse:
        try:
            r = await self.client.get("wallet", timeout=10)
        except (httpx.ConnectError, httpx.RequestError):
            return StatusResponse(f"Unable to connect to '{self.endpoint}'", 0)

        if r.is_error:
            error_message = r.json()["message"]
            return StatusResponse(error_message, 0)

        data = int(r.json()["data"]["balance"])
        # ZBD returns everything as a str not int
        # balance is returned in msats already in ZBD
        return StatusResponse(None, data)

    async def create_invoice(
        self,
        amount: int,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        **kwargs,
    ) -> InvoiceResponse:
        # https://api.zebedee.io/v0/charges
        if description_hash or unhashed_description:
            raise Unsupported("description_hash")

        msats_amount = amount * 1000
        data: Dict = {
            "amount": f"{msats_amount}",
            "description": memo,
            "expiresIn": 3600,
            "callbackUrl": "",
            "internalId": "",
        }

        r = await self.client.post(
            "charges",
            json=data,
            timeout=40,
        )

        if r.is_error:
            return InvoiceResponseFailed(error_message=r.json()["message"])

        data = r.json()["data"]
        return InvoiceResponseSuccess(
            checking_id=data["id"], payment_request=data["invoice"]["request"]
        )

    async def pay_invoice(
        self, bolt11_invoice: str, fee_limit_msat: int
    ) -> PaymentResponse:
        # https://api.zebedee.io/v0/payments
        r = await self.client.post(
            "payments",
            json={
                "invoice": bolt11_invoice,
                "description": "",
                "amount": "",
                "internalId": "",
                "callbackUrl": "",
            },
            timeout=40,
        )

        if r.is_error:
            error_message = r.json()["message"]
            return PaymentResponseFailed(error_message=error_message)

        data = r.json()

        checking_id = bolt11.decode(bolt11_invoice).payment_hash
        fee_msat = -int(data["data"]["fee"])
        preimage = data["data"]["preimage"]

        return PaymentResponseSuccess(
            checking_id=checking_id, fee_msat=fee_msat, preimage=preimage
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        r = await self.client.get(f"charges/{checking_id}")
        if r.is_error:
            return PaymentStatusPending()
        data = r.json()["data"]

        return self.payment_status(data.get("status"))

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        r = await self.client.get(f"payments/{checking_id}")
        if r.is_error:
            return PaymentStatusPending()

        data = r.json()["data"]

        return self.payment_status(data.get("status"))

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        self.queue: asyncio.Queue = asyncio.Queue(0)
        while True:
            value = await self.queue.get()
            yield value
