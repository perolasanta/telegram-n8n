"""Paystack Charge API support for in-chat bank transfers.

This module deliberately contains no database globals: callers own order creation
and cancellation while both channels share the money, split and HTTP behaviour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
import os
from typing import Any


PAYSTACK_API_BASE = "https://api.paystack.co"


def transfer_customer_wording(channel: str, purpose: str = "check") -> str:
    """Keep transfer prompts channel-correct and easy to test."""
    if purpose == "restart":
        return "Reply MENU to place your order again." if channel == "whatsapp" else "Scan the restaurant's QR code again or send /start to order again."
    return "Reply STATUS to check, or MENU for options." if channel == "whatsapp" else "Tap Check payment below if you have paid and not yet received confirmation. You can also use /status or /cancel."


def build_order_status_text(order: dict, payment: dict | None, channel: str) -> str:
    """Return one channel-appropriate customer status from current order/payment rows."""
    order = order or {}
    payment = payment or {}
    payment_status = str(payment.get("status") or order.get("payment_status") or "").lower()
    payment_method = str(order.get("payment_method") or "").lower()
    paid_offline = payment_method in {"cash payment", "cash", "pay on delivery"}

    if payment_status != "confirmed" and not paid_offline:
        if payment_status == "pending":
            expires = payment.get("expires_at")
            remaining = ""
            if expires:
                try:
                    expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    seconds = max(0, int((expiry - datetime.now(timezone.utc)).total_seconds()))
                    remaining = f" Time left: {seconds // 60} minutes."
                except (TypeError, ValueError):
                    pass
            if payment:
                if channel == "whatsapp":
                    return (
                        f"Payment is still pending. Bank: {payment.get('bank_name') or ''}\n"
                        f"Account name: {payment.get('account_name') or ''}\n"
                        f"Account number: {payment.get('account_number') or ''}\n"
                        f"Amount: ₦{Decimal(str(payment.get('amount') or 0)):,.2f}.{remaining} "
                        f"{transfer_customer_wording(channel)}"
                    )
                return (
                    f"Payment is still pending. Bank: {payment.get('bank_name') or 'your transfer bank'}; "
                    f"account: {payment.get('account_name') or ''} {payment.get('account_number') or ''}; "
                    f"amount ₦{Decimal(str(payment.get('amount') or 0)):,.2f}.{remaining} "
                    f"{transfer_customer_wording(channel)}"
                )
            return "Your payment is still pending."
        if payment_status in {"needs_review", "amount_mismatch"}:
            return "The restaurant is reviewing your payment."
        if payment_status == "rejected":
            return f"This transfer was rejected. Please contact the restaurant. {transfer_customer_wording(channel, 'restart')}"
        if payment_status == "expired":
            return f"This transfer account has expired. {transfer_customer_wording(channel, 'restart')}"
        if payment_status == "cancelled":
            return f"This order was cancelled. {transfer_customer_wording(channel, 'restart')}"
        if payment_status in {"refunded", "refund_pending"}:
            return f"Your refund is being processed or has been initiated. Please allow a few business days for it to reach your account. {transfer_customer_wording(channel, 'restart')}"
        return "The restaurant is reviewing your payment."

    status = str(order.get("order_status") or "pending").lower()
    if status in {"pending", "received"}:
        stage = "We've received your order and the restaurant will start soon."
    elif status == "preparing":
        stage = "Your order is being prepared."
    elif status == "ready" and order.get("order_type") == "delivery" and order.get("dispatch_sent_at"):
        stage = "Your order is on its way."
    elif status == "ready" and order.get("order_type") == "pickup":
        stage = "Your order is ready for pickup."
    elif status == "ready":
        stage = "Your order is ready and will be sent out shortly."
    elif status == "delivered":
        stage = "Your order was delivered."
    elif status == "cancelled":
        stage = "This order was cancelled."
    else:
        stage = "We've received your order and the restaurant will start soon."
    return f"Payment confirmed. {stage}" if payment_status == "confirmed" else stage


def amount_to_kobo(amount: Any) -> int:
    """Convert naira to kobo without binary-float rounding."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def transfer_expiry(expiry_minutes: int = 30, now: datetime | None = None) -> str:
    if not 15 <= expiry_minutes <= 8 * 60:
        raise ValueError("Transfer expiry must be between 15 minutes and 8 hours")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) + timedelta(minutes=expiry_minutes)).isoformat().replace("+00:00", "Z")


def split_fields(amount: Any) -> dict[str, Any]:
    """The one source of truth for the hosted and Charge API split settings."""
    percentage = Decimal(os.getenv("PAYSTACK_COMMISSION_PERCENTAGE", "5"))
    charge = (Decimal(str(amount)) * percentage / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return {"transaction_charge": amount_to_kobo(charge), "bearer": "subaccount"}


def build_transfer_charge_payload(order: dict, restaurant: dict, customer_email: str,
                                  expiry_minutes: int = 30) -> dict[str, Any]:
    order_id = str(order.get("id") or order.get("order_id") or "")
    if not order_id:
        raise ValueError("Order id is required")
    amount = order.get("total_amount", order.get("amount"))
    subaccount = restaurant.get("paystack_subaccount_code")
    if not subaccount:
        raise ValueError("Paystack subaccount is not configured")
    payload = {
        "email": customer_email,
        "amount": amount_to_kobo(amount),
        "reference": order_id,
        "currency": "NGN",
        "subaccount": subaccount,
        "bank_transfer": {"account_expires_at": transfer_expiry(expiry_minutes)},
        "metadata": {
            "order_id": order_id,
            "restaurant_id": str(restaurant.get("id") or order.get("restaurant_id") or ""),
            "channel": str(order.get("order_channel") or order.get("channel") or ""),
        },
    }
    payload.update(split_fields(amount))
    return payload


def parse_transfer_response(result: dict) -> dict | None:
    data = result.get("data") if isinstance(result, dict) else None
    bank = data.get("bank") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(bank, dict):
        return None
    required = ("reference", "account_name", "account_number", "account_expires_at")
    if not all(data.get(key) for key in required) or not bank.get("name"):
        return None
    return {
        "reference": data.get("reference"), "status": data.get("status") or "pending",
        "account_name": data.get("account_name"), "account_number": data.get("account_number"),
        "bank_name": bank.get("name"), "expires_at": data.get("account_expires_at"),
    }


async def create_transfer_charge(order: dict, restaurant: dict, customer_email: str,
                                 expiry_minutes: int = 30) -> dict | None:
    """Create a temporary account. A timeout is reconciled once, never retried."""
    secret = os.getenv("PAYSTACK_SECRET_KEY")
    import aiohttp
    channel = str(order.get("order_channel") or order.get("channel") or "unknown")
    try:
        payload = build_transfer_charge_payload(order, restaurant, customer_email, expiry_minutes)
    except (ValueError, ArithmeticError, TypeError) as exc:
        stage = "missing_subaccount" if not restaurant.get("paystack_subaccount_code") else "invalid_amount"
        logging.error("Paystack transfer failed stage=%s channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", stage, channel, restaurant.get("id"), order.get("id"), None)
        return None
    reference = payload["reference"]
    if not secret:
        logging.error("Paystack transfer failed stage=paystack_secret_missing channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, restaurant.get("id"), order.get("id"), reference)
        return None
    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{PAYSTACK_API_BASE}/charge", headers=headers, json=payload) as response:
                result = await response.json(content_type=None)
                parsed = parse_transfer_response(result)
                if response.status < 300 and result.get("status") and parsed:
                    return parsed
                stage = "paystack_response_missing_account" if response.status < 300 else "paystack_http_error"
                logging.error("Paystack transfer failed stage=%s channel=%s restaurant_id=%s order_id=%s paystack_reference=%s http_status=%s paystack_code=%s paystack_message=%s fbtrace=%s", stage, channel, restaurant.get("id"), order.get("id"), reference, response.status, result.get("code"), result.get("message"), result.get("fbtrace_id"))
    except (aiohttp.ClientError, TimeoutError):
        # A request may have reached Paystack. Verify exactly once before cancelling.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(f"{PAYSTACK_API_BASE}/transaction/verify/{reference}", headers=headers) as response:
                    verified = await response.json(content_type=None)
                    parsed = parse_transfer_response(verified)
                    if response.status < 300 and parsed:
                        return parsed
        except (aiohttp.ClientError, TimeoutError):
            pass
        logging.error("Paystack transfer failed stage=paystack_timeout channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, restaurant.get("id"), order.get("id"), reference)
    return None
