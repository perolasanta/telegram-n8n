#!/usr/bin/env python3
"""Send a signed Paystack webhook fixture to a local/test Chowlin service only."""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def kobo(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_payload(reference, amount, event):
    return {
        "event": event,
        "data": {
            "reference": reference,
            "amount": kobo(amount),
            "currency": "NGN",
            "status": "success" if event == "charge.success" else "failed",
            "metadata": {"reference": reference},
        },
    }


def lookup_amount(reference):
    """Best-effort local Supabase lookup; credentials and bodies are never printed."""
    base = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not base or not key:
        return None
    url = f"{base.rstrip('/')}/rest/v1/payments?select=amount&paystack_reference=eq.{reference}&limit=1"
    try:
        request = Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
        with urlopen(request, timeout=10) as response:
            rows = json.loads(response.read().decode("utf-8"))
        return rows[0].get("amount") if rows else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Send a signed test Paystack webhook.")
    parser.add_argument("--reference", required=True)
    parser.add_argument("--amount", help="Naira amount; required because this script does not access production data")
    parser.add_argument("--event", choices=("charge.success", "bank.transfer.rejected"), default="charge.success")
    parser.add_argument("--url", default=os.getenv("PAYSTACK_WEBHOOK_URL", "http://localhost:8001/webhook/paystack"))
    args = parser.parse_args()
    secret = os.getenv("PAYSTACK_SECRET_KEY")
    if not secret:
        parser.error("PAYSTACK_SECRET_KEY must be set")
    amount = args.amount if args.amount is not None else lookup_amount(args.reference)
    if amount is None:
        parser.error("--amount is required when the reference cannot be found in the configured database")
    raw = json.dumps(build_payload(args.reference, amount, args.event), separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha512).hexdigest()
    request = Request(args.url, data=raw, method="POST", headers={"Content-Type": "application/json", "x-paystack-signature": signature})
    print(f"Sending event={args.event} reference={args.reference} amount_kobo={kobo(amount)}")
    try:
        with urlopen(request, timeout=20) as response:
            print(response.status, response.read().decode("utf-8", "replace"))
    except HTTPError as error:
        print(error.code, error.read().decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
