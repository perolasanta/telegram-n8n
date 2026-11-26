from whatsapp import PAYMENT_OPTION_IDS, enabled_payment_option_ids, order_status_words
from bot import available_payment_methods


def test_status_wording_for_delivery_and_pickup():
    cases = {
        "pending": "We've received your order and the restaurant will start soon.",
        "preparing": "Your order is being prepared.",
        "delivered": "Your order was delivered.",
        "cancelled": "This order was cancelled.",
    }
    for order_type in ("delivery", "pickup"):
        for status, expected in cases.items():
            assert order_status_words({"order_status": status, "order_type": order_type}) == expected
    assert order_status_words({"order_status": "ready", "order_type": "delivery"}) == "Your order is ready and will be sent out shortly."
    assert order_status_words({"order_status": "ready", "order_type": "delivery", "dispatch_sent_at": "now"}) == "Your order is on its way."
    assert order_status_words({"order_status": "ready", "order_type": "pickup"}) == "Your order is ready for pickup."


def test_shared_payment_option_order_and_unchanged_reply_ids():
    methods = available_payment_methods("delivery", True, True, True, True)
    assert methods == ["transfer", "paystack", "pod", "bank"]
    restaurant = {"pay_on_delivery_enabled": True, "paystack_enabled": True,
                  "paystack_subaccount_code": "ACCT_x", "bank_name": "Bank", "account_number": "1"}
    assert enabled_payment_option_ids(restaurant, "delivery") == [
        "pay_transfer_instant", "pay_paystack", "pay_pod", "pay_bank"
    ]
    assert PAYMENT_OPTION_IDS["bank"] == "pay_bank"
    assert len("Transfer + screenshot") <= 24
    assert len("Needs screenshot proof; restaurant confirms manually") <= 72
