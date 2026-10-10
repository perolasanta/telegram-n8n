"""Unit tests for the pure cart-building helpers in whatsapp.py.

No network calls, no live Supabase, no Telegram bot required.
Heavy top-level initialisations in bot.py are neutralised by setting
dummy environment variables before any import from that module occurs.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

# Neutralise bot.py's module-level Bot / Supabase construction
os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

# Import only the pure functions and constants we need — no I/O triggered
from whatsapp import (
    available_payment_methods,
    MAX_LINE_QTY,
    MAX_COMPOSITE_UNITS,
    WHATSAPP_COMPOSITES_ENABLED,
    build_modifier_rows,
    build_reorder_catalog_rows,
    build_stock_conflict_caption,
    can_self_cancel,
    classify_cart_lines,
    extract_table_ref,
    format_order_status_line,
    format_partial_cart_body,
    is_whatsapp_addon_active,
    is_binding_fresh,
    expand_composite_queue,
    filter_same_composite_units,
    parse_requested_items,
    parse_modifier_quantity,
    parse_customer_intent,
    price_composite_line,
    recalculate_total,
    can_skip_group,
    validate_group_selection,
)
from bot import format_order_headline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestParseCustomerIntent(unittest.TestCase):

    def test_supported_keywords(self):
        expected = {
            "status": "status", "track": "status", "my order": "status",
            "orders": "history", "history": "history", "my orders": "history",
            "cancel": "cancel", "restart": "restart", "reset": "restart",
            "start over": "restart", "help": "help", "menu": "help",
            "hi": "help", "hello": "help", "hey": "help", "start": "help",
        }
        for text, intent in expected.items():
            with self.subTest(text=text):
                self.assertEqual(parse_customer_intent(text), intent)

    def test_case_punctuation_and_whitespace_are_normalized(self):
        self.assertEqual(parse_customer_intent("  MY   ORDERS!!!  "), "history")
        self.assertEqual(parse_customer_intent("\tStart over...\n"), "restart")

    def test_near_misses_and_addresses_do_not_match(self):
        for text in (
            "cancel my order please",
            "status update please",
            "12 High Street, Lagos",
            "14 My Order Road",
            "hello there",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_customer_intent(text))


class TestTableBindingHelpers(unittest.TestCase):

    def test_extract_table_ref_case_spacing_and_junk(self):
        self.assertEqual(extract_table_ref("Hello REF:   Table_7-QR"), "Table_7-QR")
        self.assertIsNone(extract_table_ref("reference: table-1"))
        self.assertIsNone(extract_table_ref("ref: !!!"))

    def test_extract_table_ref_rejects_over_length_code(self):
        self.assertIsNone(extract_table_ref("ref:" + "a" * 65))

    def test_binding_freshness_boundary_and_invalid_value(self):
        bound = datetime(2026, 1, 1, tzinfo=timezone.utc)
        boundary = bound + timedelta(minutes=180)
        self.assertTrue(is_binding_fresh(bound.isoformat(), boundary))
        self.assertFalse(is_binding_fresh(bound.isoformat(), boundary + timedelta(microseconds=1)))
        self.assertTrue(is_binding_fresh("2026-01-01T00:00:00", bound))
        self.assertFalse(is_binding_fresh("not-a-timestamp", bound))


class TestCartMenuFilter(unittest.TestCase):

    def setUp(self):
        self.requested = {"retailer-1": 1}
        self.rows = {"retailer-1": {
            "menu_item_id": "item-1",
            "menu_items": {
                "name": "Jollof Rice", "price": "1200", "is_available": True,
                "item_type": "simple",
                "menu_categories": {"name": "Lunch — Rice", "is_active": True},
            },
        }}

    def test_matching_category_prefix_is_kept_case_insensitively(self):
        cart, _, problems = classify_cart_lines(self.requested, self.rows, "lunch")
        self.assertIn("item-1", cart)
        self.assertEqual(problems, [])

    def test_mismatching_category_prefix_is_rejected(self):
        cart, _, problems = classify_cart_lines(self.requested, self.rows, "Dinner")
        self.assertEqual(cart, {})
        self.assertEqual(problems, ["Jollof Rice isn't on this table's menu"])

    def test_no_filter_keeps_category(self):
        cart, _, problems = classify_cart_lines(self.requested, self.rows)
        self.assertIn("item-1", cart)
        self.assertEqual(problems, [])


class TestOrderStatusFormatting(unittest.TestCase):

    def test_order_status_wording_and_totals(self):
        for status, wording in {
            "pending": "Received",
            "preparing": "Being prepared",
            "ready": "Ready",
            "delivered": "Delivered",
            "cancelled": "Cancelled",
        }.items():
            with self.subTest(status=status):
                self.assertEqual(
                    format_order_status_line({
                        "id": "12345678-1234", "order_status": status, "total_amount": "1250.25",
                    }),
                    f"#12345678 — {wording} — ₦1,250",
                )

    def test_pending_payment_overrides(self):
        self.assertIn("Awaiting payment", format_order_status_line({
            "id": "order-123", "order_status": "pending", "payment_method": "Paystack",
            "payment_status": "pending", "total_amount": 2500,
        }))
        self.assertIn("Payment being verified", format_order_status_line({
            "id": "order-123", "order_status": "pending", "payment_method": "Bank Transfer",
            "payment_status": "pending", "total_amount": 2500,
        }))


class TestKitchenOrderHeadline(unittest.TestCase):

    def test_location_lines_for_each_order_type(self):
        expected = {
            "dine_in": "<b>🪑 TABLE 5 · DINE-IN</b>",
            "pickup": "<b>🏃 PICKUP</b>",
            "delivery": "<b>🛵 DELIVERY</b>",
        }
        for order_type, location in expected.items():
            with self.subTest(order_type=order_type):
                self.assertEqual(
                    format_order_headline(order_type, "5", "Cash Payment", 1250)[0],
                    location,
                )

    def test_payment_lines_for_each_payment_method(self):
        expected = {
            "Cash Payment": "<b>💰 CASH · COLLECT ₦1,250</b>",
            "Pay on Delivery": "<b>💵 PAY ON DELIVERY · COLLECT ₦1,250</b>",
            "Bank Transfer": "<b>🏦 BANK TRANSFER · VERIFY PROOF</b>",
            "Paystack": "<b>💳 PAID ONLINE (PAYSTACK)</b>",
        }
        for payment_method, payment_line in expected.items():
            with self.subTest(payment_method=payment_method):
                self.assertEqual(
                    format_order_headline("pickup", None, payment_method, 1250)[1],
                    payment_line,
                )

    def test_table_number_is_html_escaped(self):
        location_line, _ = format_order_headline("dine_in", "A&B", "Cash Payment", 0)
        self.assertEqual(location_line, "<b>🪑 TABLE A&amp;B · DINE-IN</b>")


class TestAvailablePaymentMethods(unittest.TestCase):

    def test_order_type_and_enabled_method_matrix(self):
        for order_type in ("dine_in", "pickup", "delivery"):
            for pod_enabled in (False, True):
                for paystack_enabled in (False, True):
                    for has_bank in (False, True):
                        with self.subTest(
                            order_type=order_type, pod_enabled=pod_enabled,
                            paystack_enabled=paystack_enabled, has_bank=has_bank,
                        ):
                            expected = []
                            if order_type in {"dine_in", "pickup"}:
                                expected.append("cash")
                            if has_bank:
                                expected.append("bank")
                            if paystack_enabled:
                                expected.append("paystack")
                            if order_type == "delivery" and pod_enabled:
                                expected.append("pod")
                            self.assertEqual(
                                available_payment_methods(
                                    order_type, pod_enabled, paystack_enabled, has_bank,
                                ),
                                expected,
                            )

    def test_delivery_can_have_no_available_methods(self):
        self.assertEqual(available_payment_methods("delivery", False, False, False), [])


class TestSelfCancellationPolicy(unittest.TestCase):

    def test_only_pending_cash_and_pay_on_delivery_orders_are_cancellable(self):
        for payment_method in ("Cash Payment", "Pay on Delivery"):
            with self.subTest(payment_method=payment_method):
                self.assertTrue(can_self_cancel({
                    "order_status": "pending", "payment_method": payment_method,
                }))
        for payment_method in ("Bank Transfer", "Paystack", "Other"):
            with self.subTest(payment_method=payment_method):
                self.assertFalse(can_self_cancel({
                    "order_status": "pending", "payment_method": payment_method,
                }))
        for status in ("preparing", "ready", "delivered", "cancelled"):
            with self.subTest(status=status):
                self.assertFalse(can_self_cancel({
                    "order_status": status, "payment_method": "Cash Payment",
                }))


class TestReorderCatalogAdapter(unittest.TestCase):

    def test_pseudo_rows_classify_like_catalog_rows(self):
        menu_item = {
            "id": "menu-item-1", "name": "Jollof Rice", "price": "2500.00",
            "is_available": True, "item_type": "simple", "category_id": "cat-1",
            "menu_categories": {"is_active": True},
        }
        requested, pseudo_rows = build_reorder_catalog_rows(
            [{"menu_item_id": "menu-item-1", "quantity": 2}],
            {"menu-item-1": menu_item},
        )
        catalog_rows = {
            "retailer-1": {
                "menu_item_id": "menu-item-1",
                "menu_items": menu_item,
            },
        }
        catalog_cart, catalog_composites, catalog_problems = classify_cart_lines(
            {"retailer-1": 2}, catalog_rows,
        )
        reorder_cart, reorder_composites, reorder_problems = classify_cart_lines(
            requested, pseudo_rows,
        )
        self.assertEqual(reorder_cart, catalog_cart)
        self.assertEqual(reorder_composites, catalog_composites)
        self.assertEqual(reorder_problems, catalog_problems)

class TestWhatsAppAddonActive(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def test_disabled_addon_is_inactive(self):
        self.assertFalse(is_whatsapp_addon_active({"whatsapp_addon_enabled": False}, self.now))

    def test_enabled_addon_without_expiry_is_active(self):
        self.assertTrue(is_whatsapp_addon_active({"whatsapp_addon_enabled": True}, self.now))

    def test_future_expiry_is_active(self):
        self.assertTrue(is_whatsapp_addon_active({
            "whatsapp_addon_enabled": True,
            "whatsapp_addon_expires_at": "2030-01-02T00:00:00+00:00",
        }, self.now))

    def test_past_expiry_is_inactive(self):
        self.assertFalse(is_whatsapp_addon_active({
            "whatsapp_addon_enabled": True,
            "whatsapp_addon_expires_at": "2029-12-31T23:59:59+00:00",
        }, self.now))

    def test_z_suffixed_expiry_is_supported(self):
        self.assertTrue(is_whatsapp_addon_active({
            "whatsapp_addon_enabled": True,
            "whatsapp_addon_expires_at": "2030-01-02T00:00:00Z",
        }, self.now))

    def test_naive_expiry_is_assumed_utc(self):
        self.assertTrue(is_whatsapp_addon_active({
            "whatsapp_addon_enabled": True,
            "whatsapp_addon_expires_at": "2030-01-02T00:00:00",
        }, self.now))

class TestFormatPartialCartBody(unittest.TestCase):

    def test_formats_problems_as_bullets(self):
        body = format_partial_cart_body(["Rice unavailable", "Soup sold out"])
        self.assertEqual(
            body,
            "Some items couldn't be added:\n• Rice unavailable\n• Soup sold out\n\nContinue with the rest?",
        )

    def test_truncates_body_at_limit(self):
        body = format_partial_cart_body(["problem " + ("x" * 1200)])
        self.assertLessEqual(len(body), 1000)
        self.assertTrue(body.startswith("Some items couldn't be added:\n• "))
        self.assertTrue(body.endswith("\n\nContinue with the rest?"))


class TestBuildStockConflictCaption(unittest.TestCase):

    def test_includes_shortages_and_escapes_dynamic_values(self):
        caption = build_stock_conflict_caption(
            {
                "customer_name": "A&B <Customer>",
                "total_price": 1250,
                "cart": {"item-1": {"name": "Zobo <Large>", "qty": 5}},
            },
            "+234&123",
            [{"name": "Zobo <Large>", "requested": 5, "available": 2}],
        )

        self.assertIn("STOCK CONFLICT, needs manual resolution", caption)
        self.assertIn("Customer: A&amp;B &lt;Customer&gt;", caption)
        self.assertIn("WhatsApp: +234&amp;123", caption)
        self.assertIn("Zobo &lt;Large&gt; × 5", caption)
        self.assertIn("Zobo &lt;Large&gt;: asked 5, has 2", caption)
        self.assertNotIn("Zobo <Large>", caption)


class TestPriceCompositeLine(unittest.TestCase):

    def test_refreshes_price_deltas_and_applies_quantity(self):
        unit_price, modifiers = price_composite_line(
            "100.00",
            {"protein": [{"option_id": "o1", "name": "Chicken", "price_delta": 5, "quantity": 3}]},
            {"o1": {"id": "o1", "name": "Chicken", "price_delta": "12.50", "is_available": True}},
        )

        self.assertEqual(str(unit_price), "137.50")
        self.assertEqual(str(modifiers["protein"][0]["price_delta"]), "12.50")
        self.assertEqual(modifiers["protein"][0]["quantity"], 3)

    def test_float_price_delta_from_database_is_supported(self):
        unit_price, modifiers = price_composite_line(
            100,
            {"protein": [{"option_id": "o1", "name": "Chicken", "quantity": 2}]},
            {"o1": {"name": "Chicken", "price_delta": 12.5, "is_available": True}},
        )

        self.assertEqual(str(unit_price), "125.0")
        self.assertEqual(str(modifiers["protein"][0]["price_delta"]), "12.5")

    def test_unavailable_option_raises(self):
        with self.assertRaisesRegex(ValueError, "Chicken is no longer available"):
            price_composite_line(
                100,
                {"protein": [{"option_id": "o1", "name": "Chicken", "quantity": 1}]},
                {"o1": {"name": "Chicken", "price_delta": 10, "is_available": False}},
            )

    def test_missing_option_raises(self):
        with self.assertRaisesRegex(ValueError, "Chicken is no longer available"):
            price_composite_line(
                100,
                {"protein": [{"option_id": "missing", "name": "Chicken", "quantity": 1}]},
                {},
            )


class _SimpleCartQuery:
    def __init__(self, data):
        self.data = data
        self.filters = {}

    def select(self, _columns):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def execute(self):
        return type("Result", (), {"data": self.data})()


class _SimpleCartSupabase:
    def table(self, table_name):
        assert table_name == "menu_items"
        return _SimpleCartQuery([{"id": "item1", "name": "Rice", "price": "10.25", "is_available": True}])


class _SimpleCartState:
    def __init__(self):
        self.supabase = _SimpleCartSupabase()
        self.data = {"cart": {"item1": {"name": "Old name", "price": 8, "qty": 2}}, "order_type": "pickup"}

    async def get_data(self):
        return self.data

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


class TestRecalculateSimpleLine(unittest.IsolatedAsyncioTestCase):

    async def test_simple_line_keeps_existing_pricing_shape(self):
        state = _SimpleCartState()
        total = await recalculate_total(state, {"id": "restaurant1"})

        self.assertEqual(str(total), "20.50")
        self.assertEqual(state.data["cart"]["item1"], {
            "name": "Rice",
            "price": 10.25,
            "qty": 2,
            "menu_item_id": "item1",
        })


class TestCompositeFlowHelpers(unittest.TestCase):

    def test_modifier_rows_respect_row_cap_and_paging(self):
        group = {"selection_mode": "multi", "min_select": 0}
        options = [
            {
                "id": f"option-{index}",
                "name": "An exceedingly long option title " + str(index),
                "price_delta": 123456,
                "unit_label": "a very long unit label that still fits only after truncation",
                "is_available": True,
            }
            for index in range(15)
        ]

        rows, page, page_count = build_modifier_rows("group-1", group, options, 0)
        self.assertEqual(page, 0)
        self.assertGreater(page_count, 1)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[-3:], [
            {"id": "mod_done", "title": "Done"},
            {"id": "mod_skip", "title": "Skip"},
            {"id": "mod_more", "title": "More options"},
        ])
        for row in rows:
            self.assertLessEqual(len(row["title"]), 24)
            self.assertLessEqual(len(row.get("description", "")), 72)
        self.assertEqual(rows[0]["id"], "mod:group-1:option-0")

    def test_group_selection_min_max_and_skip_rules(self):
        group = {"min_select": 1, "max_select": 2}
        self.assertIsNotNone(validate_group_selection(group, []))
        self.assertIsNone(validate_group_selection(group, [{}, {}]))
        self.assertIsNotNone(validate_group_selection(group, [{}, {}, {}]))
        self.assertFalse(can_skip_group(group))
        self.assertTrue(can_skip_group({"min_select": 0}))

    def test_modifier_quantity_parser(self):
        self.assertEqual(parse_modifier_quantity("20"), 20)
        self.assertEqual(parse_modifier_quantity("1"), 1)
        for value in ("0", "21", "1.5", " 2", "+2", "٢", "abc", ""):
            self.assertIsNone(parse_modifier_quantity(value))

    def test_composite_queue_expansion_and_unit_cap(self):
        queue, exceeded = expand_composite_queue([
            {"menu_item_id": "combo", "name": "Combo", "qty": 2},
            {"menu_item_id": "meal", "name": "Meal", "qty": 1},
        ])
        self.assertFalse(exceeded)
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0], {"menu_item_id": "combo", "name": "Combo"})
        self.assertEqual(set(queue[1]), {"menu_item_id", "name"})

        too_many, exceeded = expand_composite_queue([
            {"menu_item_id": "combo", "name": "Combo", "qty": MAX_COMPOSITE_UNITS + 1},
        ])
        self.assertTrue(exceeded)
        self.assertEqual(too_many, [])

    def test_same_all_queue_filter_preserves_other_item_order(self):
        queue = [
            {"menu_item_id": "combo-a", "unit_no": 2},
            {"menu_item_id": "combo-b", "unit_no": 1},
            {"menu_item_id": "combo-a", "unit_no": 3},
        ]
        matching, remaining = filter_same_composite_units(queue, "combo-a")

        self.assertEqual(matching, [queue[0], queue[2]])
        self.assertEqual(remaining, [queue[1]])

def _make_row(
    rid: str,
    menu_item_id: str,
    name: str,
    price: float = 1000.0,
    is_available: bool = True,
    item_type: str = "simple",
    category_is_active: bool | None = True,
) -> dict:
    """Build a synthetic menu_item_catalog_map row as Supabase would return it."""
    menu_categories = None if category_is_active is None else {"is_active": category_is_active}
    return {
        "catalog_retailer_id": rid,
        "menu_item_id": menu_item_id,
        "menu_items": {
            "name": name,
            "price": price,
            "is_available": is_available,
            "item_type": item_type,
            "menu_categories": menu_categories,
        },
    }


def _rows_by_rid(*rows) -> dict:
    return {r["catalog_retailer_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# parse_requested_items
# ---------------------------------------------------------------------------

class TestParseRequestedItems(unittest.TestCase):

    def _call(self, product_items):
        return parse_requested_items({"product_items": product_items})

    # --- valid paths --------------------------------------------------------

    def test_single_valid_item(self):
        requested, problems = self._call([{"product_retailer_id": "rid1", "quantity": 2}])
        self.assertEqual(requested, {"rid1": 2})
        self.assertEqual(problems, [])

    def test_duplicate_retailer_ids_aggregate(self):
        items = [
            {"product_retailer_id": "rid1", "quantity": 1},
            {"product_retailer_id": "rid1", "quantity": 3},
        ]
        requested, problems = self._call(items)
        self.assertEqual(requested["rid1"], 4)
        self.assertEqual(problems, [])

    def test_multiple_distinct_items(self):
        items = [
            {"product_retailer_id": "rid1", "quantity": 1},
            {"product_retailer_id": "rid2", "quantity": 5},
        ]
        requested, problems = self._call(items)
        self.assertEqual(requested, {"rid1": 1, "rid2": 5})
        self.assertEqual(problems, [])

    # --- invalid quantity ---------------------------------------------------

    def test_zero_quantity_rejected(self):
        _, problems = self._call([{"product_retailer_id": "rid1", "quantity": 0}])
        self.assertIn("an item has an invalid quantity", problems)

    def test_negative_quantity_rejected(self):
        _, problems = self._call([{"product_retailer_id": "rid1", "quantity": -1}])
        self.assertIn("an item has an invalid quantity", problems)

    def test_non_numeric_quantity_rejected(self):
        _, problems = self._call([{"product_retailer_id": "rid1", "quantity": "lots"}])
        self.assertIn("an item has an invalid quantity", problems)

    def test_none_quantity_rejected(self):
        _, problems = self._call([{"product_retailer_id": "rid1", "quantity": None}])
        self.assertIn("an item has an invalid quantity", problems)

    def test_float_string_quantity_is_invalid(self):
        # "2.5" cannot be converted with int() directly — should be rejected
        _, problems = self._call([{"product_retailer_id": "rid1", "quantity": "2.5"}])
        self.assertIn("an item has an invalid quantity", problems)

    def test_duplicate_invalid_problem_deduped(self):
        """Two invalid items should produce only one copy of the problem message."""
        items = [
            {"product_retailer_id": "rid1", "quantity": 0},
            {"product_retailer_id": "rid2", "quantity": -3},
        ]
        _, problems = self._call(items)
        self.assertEqual(problems.count("an item has an invalid quantity"), 1)

    def test_missing_retailer_id_rejected(self):
        _, problems = self._call([{"product_retailer_id": None, "quantity": 1}])
        self.assertIn("an item has an invalid quantity", problems)

    def test_empty_retailer_id_rejected(self):
        _, problems = self._call([{"product_retailer_id": "  ", "quantity": 1}])
        self.assertIn("an item has an invalid quantity", problems)

    # --- MAX_LINE_QTY -------------------------------------------------------

    def test_qty_at_max_is_accepted(self):
        requested, problems = self._call([{"product_retailer_id": "rid1", "quantity": MAX_LINE_QTY}])
        self.assertIn("rid1", requested)
        self.assertEqual(problems, [])

    def test_qty_over_max_dropped_with_problem(self):
        requested, problems = self._call([{"product_retailer_id": "rid1", "quantity": MAX_LINE_QTY + 1}])
        self.assertNotIn("rid1", requested)
        self.assertTrue(any("rid1" in p and "too large" in p for p in problems))

    def test_aggregated_qty_over_max_dropped(self):
        items = [
            {"product_retailer_id": "rid1", "quantity": MAX_LINE_QTY},
            {"product_retailer_id": "rid1", "quantity": 1},
        ]
        requested, problems = self._call(items)
        self.assertNotIn("rid1", requested)
        self.assertTrue(any("rid1" in p and "too large" in p for p in problems))

    # --- edge cases ---------------------------------------------------------

    def test_empty_product_items(self):
        requested, problems = parse_requested_items({})
        self.assertEqual(requested, {})
        self.assertEqual(problems, [])

    def test_none_product_items_key(self):
        requested, problems = parse_requested_items({"product_items": None})
        self.assertEqual(requested, {})
        self.assertEqual(problems, [])


# ---------------------------------------------------------------------------
# classify_cart_lines
# ---------------------------------------------------------------------------

class TestClassifyCartLines(unittest.TestCase):

    def test_simple_item_goes_into_cart(self):
        row = _make_row("rid1", "mid1", "Jollof Rice", price=1500.0)
        cart, composites, problems = classify_cart_lines({"rid1": 2}, _rows_by_rid(row))
        self.assertIn("mid1", cart)
        self.assertEqual(cart["mid1"]["qty"], 2)
        self.assertEqual(cart["mid1"]["price"], 1500.0)
        self.assertEqual(cart["mid1"]["name"], "Jollof Rice")
        self.assertEqual(cart["mid1"]["menu_item_id"], "mid1")
        self.assertEqual(composites, [])
        self.assertEqual(problems, [])

    def test_composite_goes_to_composites_list_not_cart(self):
        row = _make_row("rid1", "mid1", "Swallow Combo", price=800.0, item_type="composite")
        cart, composites, problems = classify_cart_lines({"rid1": 1}, _rows_by_rid(row))
        self.assertEqual(cart, {})
        self.assertEqual(len(composites), 1)
        self.assertEqual(composites[0]["menu_item_id"], "mid1")
        self.assertEqual(composites[0]["name"], "Swallow Combo")
        self.assertEqual(composites[0]["base_price"], 800.0)
        self.assertEqual(composites[0]["qty"], 1)
        self.assertEqual(problems, [])

    def test_unavailable_item_produces_problem_not_in_cart(self):
        row = _make_row("rid1", "mid1", "Egusi Soup", is_available=False)
        cart, composites, problems = classify_cart_lines({"rid1": 1}, _rows_by_rid(row))
        self.assertEqual(cart, {})
        self.assertIn("Egusi Soup is unavailable", problems)

    def test_inactive_category_produces_problem_not_in_cart(self):
        row = _make_row("rid1", "mid1", "Puff Puff", category_is_active=False)
        cart, composites, problems = classify_cart_lines({"rid1": 1}, _rows_by_rid(row))
        self.assertEqual(cart, {})
        self.assertIn("Puff Puff is unavailable", problems)

    def test_missing_menu_categories_embed_treated_as_active(self):
        """A None menu_categories embed must not block the item."""
        row = _make_row("rid1", "mid1", "Boli", category_is_active=None)
        cart, composites, problems = classify_cart_lines({"rid1": 3}, _rows_by_rid(row))
        self.assertIn("mid1", cart)
        self.assertEqual(cart["mid1"]["qty"], 3)
        self.assertEqual(problems, [])

    def test_unknown_retailer_id_produces_catalog_problem(self):
        cart, composites, problems = classify_cart_lines({"ghost_rid": 1}, {})
        self.assertEqual(cart, {})
        self.assertIn("an item is no longer in this restaurant's catalog", problems)

    def test_two_retailer_ids_mapping_to_same_menu_item_merge_quantities(self):
        row_a = _make_row("rid1", "mid1", "Fried Rice", price=1200.0)
        row_b = _make_row("rid2", "mid1", "Fried Rice", price=1200.0)
        cart, _, problems = classify_cart_lines(
            {"rid1": 2, "rid2": 3}, _rows_by_rid(row_a, row_b)
        )
        self.assertIn("mid1", cart)
        self.assertEqual(cart["mid1"]["qty"], 5)
        self.assertEqual(problems, [])

    def test_price_stored_as_float_via_money_helper(self):
        """Price must go through money() so Decimal arithmetic is consistent."""
        row = _make_row("rid1", "mid1", "Suya", price="750.50")
        cart, _, _ = classify_cart_lines({"rid1": 1}, _rows_by_rid(row))
        self.assertIsInstance(cart["mid1"]["price"], float)
        self.assertAlmostEqual(cart["mid1"]["price"], 750.50)

    def test_mixed_valid_and_invalid_items(self):
        valid = _make_row("rid1", "mid1", "Moi Moi")
        invalid = _make_row("rid2", "mid2", "Stale Item", is_available=False)
        cart, _, problems = classify_cart_lines(
            {"rid1": 1, "rid2": 1}, _rows_by_rid(valid, invalid)
        )
        self.assertIn("mid1", cart)
        self.assertNotIn("mid2", cart)
        self.assertIn("Stale Item is unavailable", problems)

    def test_empty_requested_returns_empty_results(self):
        cart, composites, problems = classify_cart_lines({}, {})
        self.assertEqual(cart, {})
        self.assertEqual(composites, [])
        self.assertEqual(problems, [])

    def test_composite_base_price_is_float(self):
        row = _make_row("rid1", "mid1", "Bowl", price="999", item_type="composite")
        _, composites, _ = classify_cart_lines({"rid1": 2}, _rows_by_rid(row))
        self.assertIsInstance(composites[0]["base_price"], float)
        self.assertAlmostEqual(composites[0]["base_price"], 999.0)

    def test_active_category_does_not_block_item(self):
        row = _make_row("rid1", "mid1", "Pepper Soup", category_is_active=True)
        cart, _, problems = classify_cart_lines({"rid1": 1}, _rows_by_rid(row))
        self.assertIn("mid1", cart)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
