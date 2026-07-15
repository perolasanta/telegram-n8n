import csv
import io

async def generate_catalog_feed_csv(restaurant_id: str, supabase) -> str:
    items = supabase.table("menu_items")\
        .select("id, name, description, price, is_available, image_url")\
        .eq("restaurant_id", restaurant_id)\
        .execute()

    restaurant = supabase.table("restaurants")\
        .select("name, slug").eq("id", restaurant_id).execute().data[0]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "title", "description", "availability", "condition",
        "price", "link", "image_link", "brand"
    ])
    writer.writeheader()

    for item in items.data or []:
        retailer_id = f"menuitem_{item['id'][:12]}"
        supabase.table("menu_item_catalog_map").upsert({
            "menu_item_id": item["id"],
            "restaurant_id": restaurant_id,
            "catalog_retailer_id": retailer_id
        }).execute()

        writer.writerow({
            "id": retailer_id,
            "title": item["name"],
            "description": item.get("description") or item["name"],
            "availability": "in stock" if item["is_available"] else "out of stock",
            "condition": "new",
            "price": f"{float(item['price']):.2f} NGN",
            "link": f"https://chowlin.com.ng/menu/{restaurant['slug']}",
            "image_link": item.get("image_url") or "https://chowlin.com.ng/default-food.jpg",
            "brand": restaurant["name"],
        })

    return output.getvalue()