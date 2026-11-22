# reports.py
import logging
from datetime import datetime, timedelta, timezone

import pytz
from supabase import Client

logger = logging.getLogger(__name__)

# A cursor older than this is clamped to now - 48h so a long outage never
# produces an unbounded (and unreadable) daily report. See get_daily_report_window.
MAX_DAILY_REPORT_LOOKBACK = timedelta(hours=48)


def build_inventory_summary(supabase: Client, restaurant_id: str) -> str:
    items = supabase.table("menu_items")\
        .select("name, inventory_count, restock_threshold, is_available")\
        .eq("restaurant_id", restaurant_id)\
        .eq("track_inventory", True)\
        .order("name")\
        .execute()

    if not items.data:
        return ""

    report = "📦 *Inventory Summary*\n"
    for item in items.data:
        count = int(item.get("inventory_count") or 0)
        threshold = int(item.get("restock_threshold") or 0)
        if count <= 0 or item.get("is_available") is False:
            report += f"• {item['name']} — {count} portions (unavailable)\n"
        elif count <= threshold:
            report += f"• {item['name']} — {count} portions ⚠️ Low\n"
        else:
            report += f"• {item['name']} — {count} portions remaining\n"

    return report + "\n"


def get_daily_report_window(last_cutoff, now, tz="Africa/Lagos"):
    """Pure: compute the half-open daily-report window ``[start, end)``.

    Args:
        last_cutoff: timezone-aware datetime, or None for a first run
            (NULL cursor). Comes from ``restaurants.last_daily_report_cutoff``.
        now: timezone-aware datetime captured once at the start of generation.
        tz: IANA timezone name used for the "start of today" first-run fallback.

    Returns:
        ``(start, end)`` timezone-aware datetimes, or ``(None, None)`` for an
        empty window (``start >= end``). ``end`` is always ``now``. When the gap
        from ``last_cutoff`` to ``now`` exceeds 48h, ``start`` is clamped to
        ``now - 48h`` and a warning is logged, so a long outage never yields an
        unbounded report.

    All returned datetimes are timezone-aware; callers query Supabase with UTC
    ISO strings. This helper performs no I/O.
    """
    if now is None:
        raise ValueError("now is required for get_daily_report_window")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware for get_daily_report_window")
    end = now

    if last_cutoff is not None:
        start = last_cutoff
        if now - last_cutoff > MAX_DAILY_REPORT_LOOKBACK:
            clamped = now - MAX_DAILY_REPORT_LOOKBACK
            logger.warning(
                "Daily report gap exceeds 48h (last_cutoff=%s, now=%s); clamping start to %s",
                last_cutoff.isoformat(), now.isoformat(), clamped.isoformat(),
            )
            start = clamped
    else:
        # First run (NULL cursor): fall back to the start of today in the report tz.
        local_tz = pytz.timezone(tz)
        start = now.astimezone(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)

    if start >= end:
        return None, None

    return start, end


async def generate_daily_report_for_window(
    supabase: Client, restaurant_id: str, start: datetime, end: datetime
) -> str:
    """Generate a daily report covering the half-open window ``[start, end)``.

    ``start`` and ``end`` are timezone-aware datetimes (typically returned by
    ``get_daily_report_window``). Orders are queried with
    ``created_at >= start AND created_at < end`` (half-open so an order placed
    exactly at the cutoff lands in exactly one report), scoped by
    ``restaurant_id``. Supabase is queried with UTC ISO strings.

    Revenue definition, payment-status handling and cancelled-order filtering
    (``order_status != 'cancelled'``) are unchanged from the existing daily
    report. The period is shown in the header in WAT.
    """
    start_iso = start.astimezone(timezone.utc).isoformat()
    end_iso = end.astimezone(timezone.utc).isoformat()

    restaurant = supabase.table("restaurants")\
        .select("name")\
        .eq("id", restaurant_id)\
        .execute()
    restaurant_name = restaurant.data[0]["name"] if restaurant.data else "Restaurant"

    # Half-open bounds: gte start, lt end. Same bounds style for every order
    # query in this report so the totals agree.
    orders = supabase.table("orders")\
        .select("*, order_items(*, menu_items(name))")\
        .eq("restaurant_id", restaurant_id)\
        .neq("order_status", "cancelled")\
        .gte("created_at", start_iso)\
        .lt("created_at", end_iso)\
        .execute()

    wat = pytz.timezone("Africa/Lagos")
    start_wat = start.astimezone(wat)
    end_wat = end.astimezone(wat)
    period_header = (
        "📊 *Daily Report*\n"
        f"Period: {start_wat.strftime('%a %d %b, %H:%M')} to "
        f"{end_wat.strftime('%a %d %b, %H:%M')}"
    )

    if not orders.data:
        inventory_summary = build_inventory_summary(supabase, restaurant_id)
        return f"{period_header}\n\n" \
               f"🏪 {restaurant_name}\n\n" \
               f"No orders in this period.\n\n" \
               f"{inventory_summary}"

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
    report = f"{period_header}\n\n"
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
        report += "\n"

    report += build_inventory_summary(supabase, restaurant_id)
    return report


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
        .neq("order_status", "cancelled")\
        .gte("created_at", start_of_day.isoformat())\
        .lte("created_at", end_of_day.isoformat())\
        .execute()
    
    if not orders.data:
        inventory_summary = build_inventory_summary(supabase, restaurant_id)
        return f"📊 *Daily Report - {date.strftime('%Y-%m-%d')}*\n\n" \
               f"🏪 {restaurant_name}\n\n" \
               f"No orders today.\n\n" \
               f"{inventory_summary}"
    
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
        report += "\n"

    report += build_inventory_summary(supabase, restaurant_id)
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
        .neq("order_status", "cancelled")\
        .gte("created_at", start_date.isoformat())\
        .lte("created_at", end_date.isoformat())\
        .execute()
    
    if not orders.data:
        inventory_summary = build_inventory_summary(supabase, restaurant_id)
        return f"📊 *Weekly Report*\n" \
               f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n\n" \
               f"🏪 {restaurant_name}\n\n" \
               f"No orders this week.\n\n" \
               f"{inventory_summary}"
    
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
        report += "\n"

    report += build_inventory_summary(supabase, restaurant_id)
    return report

