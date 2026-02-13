import base64
import qrcode
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

restaurant_id = input("Enter restaurant ID: ")

tables = supabase.table("restaurant_tables")\
    .select("id, table_number")\
    .eq("restaurant_id", restaurant_id)\
    .execute()

for table in tables.data:

    payload = f"{restaurant_id}|{table['id']}"

    encoded = base64.urlsafe_b64encode(
        payload.encode()
    ).decode().rstrip("=")

    url = f"https://t.me/Chaolin_bot?start={encoded}"

    print(url)

    img = qrcode.make(url)

    img.save(f"qr_table_{table['table_number']}.png")

print("Done")

