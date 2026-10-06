import base64
import argparse
import qrcode
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

parser = argparse.ArgumentParser()
parser.add_argument("--landing", action="store_true", help="Encode Chowlin table landing-page URLs")
args = parser.parse_args()

landing_base = None
if args.landing:
    landing_base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("FASTAPI_WEBHOOK_URL") or "").rstrip("/")
    if not landing_base:
        parser.error("--landing requires PUBLIC_BASE_URL or FASTAPI_WEBHOOK_URL to be set")

if args.landing:
    print(f"URL pattern: {landing_base}/t/<public_code>")
else:
    print("URL pattern: https://t.me/Chaolin_bot?start=<encoded restaurant_id|table_id>")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

restaurant_id = input("Enter restaurant ID: ")

tables = supabase.table("restaurant_tables")\
    .select("id, table_number, public_code")\
    .eq("restaurant_id", restaurant_id)\
    .eq("is_active", True)\
    .execute()

for table in tables.data:

    payload = f"{restaurant_id}|{table['id']}"

    encoded = base64.urlsafe_b64encode(
        payload.encode()
    ).decode().rstrip("=")

    if args.landing:
        url = f"{landing_base}/t/{table.get('public_code')}"
    else:
        url = f"https://t.me/Chaolin_bot?start={encoded}"

    print(url)

    img = qrcode.make(url)

    img.save(f"qr_table_{table['table_number']}.png")

print("Done")
