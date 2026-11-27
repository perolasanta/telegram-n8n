#!/usr/bin/env python3
"""TEST only: never target production; each synthetic Hi can trigger outbound Meta work."""
import argparse
import asyncio
import statistics
import time
from collections import Counter
import httpx


def args():
    p = argparse.ArgumentParser(description="Load-test a TEST WhatsApp webhook")
    p.add_argument("--url", required=True)
    p.add_argument("--confirm-test-data", action="store_true")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--requests", type=int, default=50)
    p.add_argument("--phone-number-id", required=True)
    return p.parse_args()


def webhook_payload(number, phone_number_id):
    sender = f"234900000{number % 10000:04d}"
    return {"object": "whatsapp_business_account", "entry": [{"id": "test-waba", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "2349000000000", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Load Test"}, "wa_id": sender}],
        "messages": [{"from": sender, "id": f"test-load-{number}", "timestamp": "1700000000", "type": "text", "text": {"body": "Hi"}}],
    }}]}]}


def percentile(samples, q):
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))] if ordered else 0.0


async def main():
    options = args()
    if not options.confirm_test_data:
        raise SystemExit("Refusing to run: pass --confirm-test-data only for a TEST tenant with a dummy WhatsApp token.")
    if options.concurrency < 1 or options.requests < 1:
        raise SystemExit("--concurrency and --requests must be positive")
    limit, latencies, statuses = asyncio.Semaphore(options.concurrency), [], Counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        async def send(number):
            async with limit:
                start = time.perf_counter()
                try:
                    response = await client.post(options.url, json=webhook_payload(number, options.phone_number_id))
                    statuses[response.status_code] += 1
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1
                finally:
                    latencies.append((time.perf_counter() - start) * 1000)
        await asyncio.gather(*(send(i) for i in range(options.requests)))
    succeeded = sum(count for status, count in statuses.items() if isinstance(status, int) and 200 <= status < 300)
    print(f"requests={options.requests} concurrency={options.concurrency}")
    print(f"success_rate={succeeded / options.requests:.1%} status_counts={dict(statuses)}")
    print(f"ack_latency_ms p50={percentile(latencies, .50):.1f} p95={percentile(latencies, .95):.1f} p99={percentile(latencies, .99):.1f} mean={statistics.fmean(latencies):.1f}")


if __name__ == "__main__":
    asyncio.run(main())
