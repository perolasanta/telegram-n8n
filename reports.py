# reports.py
from datetime import datetime, timedelta
from supabase import Client

async def generate_daily_report(supabase: Client, restaurant_id: str, date: datetime = None) -> str:
    """Generate daily sales report"""
    
    if date is None:
        date = datetime.now()
    
    # Get date range (start and end of day)
    start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    # Get restaurant info
    restaurant = supabase.table("restaurants")\
        .select("name")\
        .eq("id", restaurant_id)\
        .execute()
    
    restaurant_name = restaurant.data[0]["name"] if restaurant.data else "Restaurant"
    
    # Get orders for the day
    orders = supabase.table("orders")\
        .select("*, order_items(*, menu_items(name))")\
        .eq("restaurant_id", restaurant_id)\
        .gte("created_at", start_of_day.isoformat())\
        .lte("created_at", end_of_day.isoformat())\
        .execute()
    
    if not orders.data:
        return f"📊 *Daily Report - {date.strftime('%Y-%m-%d')}*\n\n" \
               f"🏪 {restaurant_name}\n\n" \
               f"No orders today."
    
    # Calculate statistics
    total_orders = len(orders.data)
    total_revenue = sum(float(order["total_amount"]) for order in orders.data)
    
    # Payment method breakdown
    payment_methods = {}
    for order in orders.data:
        method = order.get("payment_method", "Unknown")
        payment_methods[method] = payment_methods.get(method, 0) + float(order["total_amount"])
    
    # Order status breakdown
    order_statuses = {}
    for order in orders.data:
        status = order.get("order_status", "unknown")
        order_statuses[status] = order_statuses.get(status, 0) + 1
    
    # Top selling items
    items_sold = {}
    for order in orders.data:
        for item in order.get("order_items", []):
            item_name = item["menu_items"]["name"]
            qty = item["quantity"]
            items_sold[item_name] = items_sold.get(item_name, 0) + qty
    
    # Sort by quantity
    top_items = sorted(items_sold.items(), key=lambda x: x[1], reverse=True)[:5]
    
    # Build report
    report = f"📊 *Daily Report - {date.strftime('%Y-%m-%d')}*\n\n"
    report += f"🏪 *{restaurant_name}*\n\n"
    
    report += f"📈 *Summary*\n"
    report += f"• Total Orders: {total_orders}\n"
    report += f"• Total Revenue: ₦{total_revenue:,.0f}\n"
    report += f"• Average Order: ₦{total_revenue/total_orders:,.0f}\n\n"
    
    report += f"💳 *Payment Methods*\n"
    for method, amount in payment_methods.items():
        report += f"• {method}: ₦{amount:,.0f}\n"
    report += "\n"
    
    report += f"📦 *Order Status*\n"
    for status, count in order_statuses.items():
        report += f"• {status.title()}: {count}\n"
    report += "\n"
    
    if top_items:
        report += f"🔥 *Top Selling Items*\n"
        for i, (item, qty) in enumerate(top_items, 1):
            report += f"{i}. {item} - {qty} sold\n"
    
    return report


async def generate_weekly_report(supabase: Client, restaurant_id: str, end_date: datetime = None) -> str:
    """Generate weekly sales report"""
    
    if end_date is None:
        end_date = datetime.now()
    
    start_date = end_date - timedelta(days=7)
    
    # Get restaurant info
    restaurant = supabase.table("restaurants")\
        .select("name")\
        .eq("id", restaurant_id)\
        .execute()
    
    restaurant_name = restaurant.data[0]["name"] if restaurant.data else "Restaurant"
    
    # Get orders for the week
    orders = supabase.table("orders")\
        .select("*, order_items(*, menu_items(name))")\
        .eq("restaurant_id", restaurant_id)\
        .gte("created_at", start_date.isoformat())\
        .lte("created_at", end_date.isoformat())\
        .execute()
    
    if not orders.data:
        return f"📊 *Weekly Report*\n" \
               f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n\n" \
               f"🏪 {restaurant_name}\n\n" \
               f"No orders this week."
    
    # Calculate statistics
    total_orders = len(orders.data)
    total_revenue = sum(float(order["total_amount"]) for order in orders.data)
    
    # Daily breakdown
    daily_sales = {}
    for order in orders.data:
        order_date = datetime.fromisoformat(order["created_at"].replace('Z', '+00:00')).date()
        daily_sales[order_date] = daily_sales.get(order_date, {
            'orders': 0,
            'revenue': 0
        })
        daily_sales[order_date]['orders'] += 1
        daily_sales[order_date]['revenue'] += float(order["total_amount"])
    
    # Payment method breakdown
    payment_methods = {}
    for order in orders.data:
        method = order.get("payment_method", "Unknown")
        payment_methods[method] = payment_methods.get(method, 0) + float(order["total_amount"])
    
    # Top selling items
    items_sold = {}
    for order in orders.data:
        for item in order.get("order_items", []):
            item_name = item["menu_items"]["name"]
            qty = item["quantity"]
            revenue = float(item["subtotal"])
            
            if item_name not in items_sold:
                items_sold[item_name] = {'qty': 0, 'revenue': 0}
            
            items_sold[item_name]['qty'] += qty
            items_sold[item_name]['revenue'] += revenue
    
    # Sort by revenue
    top_items = sorted(items_sold.items(), key=lambda x: x[1]['revenue'], reverse=True)[:10]
    
    # Build report
    report = f"📊 *Weekly Report*\n"
    report += f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n\n"
    report += f"🏪 *{restaurant_name}*\n\n"
    
    report += f"📈 *Summary*\n"
    report += f"• Total Orders: {total_orders}\n"
    report += f"• Total Revenue: ₦{total_revenue:,.0f}\n"
    report += f"• Average Order: ₦{total_revenue/total_orders:,.0f}\n"
    report += f"• Daily Average: {total_orders/7:.1f} orders, ₦{total_revenue/7:,.0f}\n\n"
    
    report += f"📅 *Daily Breakdown*\n"
    for date in sorted(daily_sales.keys()):
        data = daily_sales[date]
        report += f"• {date.strftime('%a %m/%d')}: {data['orders']} orders, ₦{data['revenue']:,.0f}\n"
    report += "\n"
    
    report += f"💳 *Payment Methods*\n"
    for method, amount in payment_methods.items():
        percentage = (amount / total_revenue) * 100
        report += f"• {method}: ₦{amount:,.0f} ({percentage:.1f}%)\n"
    report += "\n"
    
    if top_items:
        report += f"🔥 *Top Selling Items*\n"
        for i, (item, data) in enumerate(top_items, 1):
            report += f"{i}. {item}\n"
            report += f"   Sold: {data['qty']} | Revenue: ₦{data['revenue']:,.0f}\n"
    
    return report