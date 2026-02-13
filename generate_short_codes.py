import random
import string
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def generate_short_code(length=8):
    """Generate random alphanumeric code"""
    # Use uppercase letters and digits only (easier to read)
    chars = string.ascii_uppercase + string.digits
    # Exclude confusing characters: 0, O, 1, I, L
    chars = chars.replace('0', '').replace('O', '').replace('1', '').replace('I', '').replace('L', '')
    return ''.join(random.choice(chars) for _ in range(length))

# Get restaurant ID
restaurant_id = input("Enter your restaurant ID: ")

# Get tables
tables = supabase.table("restaurant_tables")\
    .select("id, table_number, qr_code")\
    .eq("restaurant_id", restaurant_id)\
    .order("table_number")\
    .execute()

if not tables.data:
    print("❌ No tables found!")
    exit()

print(f"\n📋 Found {len(tables.data)} tables\n")

for table in tables.data:
    table_id = table["id"]
    table_number = table["table_number"]
    existing_code = table.get("qr_code")
    
    if existing_code:
        print(f"Table {table_number}: Already has code {existing_code} - skipping")
        continue
    
    # Generate unique short code
    while True:
        short_code = generate_short_code(8)
        
        # Check if code already exists
        existing = supabase.table("restaurant_tables")\
            .select("id")\
            .eq("qr_code", short_code)\
            .execute()
        
        if not existing.data:
            break
    
    # Update table with short code
    result = supabase.table("restaurant_tables")\
        .update({"qr_code": short_code})\
        .eq("id", table_id)\
        .execute()
    
    print(f"Table {table_number}: Generated code {short_code}")

print("\n✅ All short codes generated!")
print("Now run generate_qr_final.py to create QR images")
