# Chowlin Paystack Charge Transfer Bugs - Implementation Plan

## Investigation Summary

### Root Cause Analysis

**BUG 1: NameError in start_instant_transfer (Line 2418)**
- **Root Cause**: `start_instant_transfer()` in whatsapp.py uses bare `supabase.table(...)` calls but does not receive `supabase` as a parameter
- **Context**: Similar functions in whatsapp.py accept `supabase` as a parameter (e.g., `start_paystack_payment` has it, `handle_payment_selection` has it, `handle_payment_proof` has it)
- **Call Chain**: `handle_interactive` → `handle_payment_selection` (passes `supabase`) → `start_instant_transfer` (called but parameter not passed)
- **Solution Pattern**: WhatsApp functions use two patterns:
  1. When state is available: use `state.supabase` (e.g., line 2394 in `start_paystack_payment`)
  2. When state may not be initialized: accept `supabase` parameter and use it directly (e.g., line 2329 `handle_payment_selection`)
  3. `start_instant_transfer` has access to state, so should use `state.supabase` like `start_paystack_payment` does

**BUG 2: Undefined Names Found**
Comprehensive scan of all runtime modules for undefined names:
- **whatsapp.py line 2349**: Bare `supabase.table(...)` - this is the BUG 1 instance; also at line 2413
- **whatsapp.py line 1893**: Bare `supabase.table(...)` in function `handle_text` - same pattern, needs context
- **whatsapp.py line 2318**: Bare `supabase.table(...)` in `handle_interactive` - passed as parameter from caller
- **bot.py line 2861**: Bare `supabase` used in `check_instant_transfer` callback - this is a module-level global (defined line 61), so it's valid
- **All other files**: No undefined names found; all use proper imports or parameter passing

**BUG 3: Error Handling in start_instant_transfer**
Current implementation (lines 2402-2437):
- Monolithic try-except that catches ALL exceptions identically
- Lines 2413 & 2418 (bare supabase calls) are the undefined-name issue
- When charge fails (details is None), it cancels the order BUT says "not available" to customer (correct)
- When payment insert fails (line 2418), the NameError causes the catch-all to trigger, saying "not available" (wrong - should say "something went wrong")
- When send_text fails after success, exception is swallowed (wrong - should log and keep order pending)

**BUG 4: Check Payment Status-Aware Reply**
Current implementation: `check_instant_transfer()` at line 2860 in bot.py
- Always replies "Payment is no longer pending." regardless of actual status
- Code logic: if status != "pending", reply fixed message (line 2865)
- Missing: status-aware responses for pending (show details), confirmed (send confirmation), rejected/expired/etc.
- No removal of inline button on final statuses
- No Paystack re-verification when status is pending

---

## Implementation Plan

### TASK 1: Fix Undefined Name in start_instant_transfer

- [ ] **1.1 Modify function signature and usage of `start_instant_transfer` in whatsapp.py**
      - Change `async def start_instant_transfer(state, restaurant, from_number, customer_name):` to pass `supabase` parameter
      - Replace all bare `supabase.table(...)` calls with `state.supabase.table(...)` (since state is available throughout the function)
      - Files: `whatsapp.py` lines 2402, 2413, 2418
      - Verification: Run `python3 -m py_compile whatsapp.py` and confirm no syntax errors

### TASK 2: Create Static Undefined-Name Test

- [ ] **2.1 Create tests/test_undefined_names.py with AST-based undefined name checker**
      - Write a pure Python AST visitor that walks module and function scopes to find undefined names
      - Scan all runtime modules: bot.py, main.py, whatsapp.py, whatsapp_state.py, paystack_transfer.py, reports.py, receipt_generator.py
      - Identify names that are referenced but not:
        - Defined locally (assignments, function/class defs)
        - Imported (from X import Y, import X)
        - Defined in function parameters
        - Builtin (print, len, etc.)
      - Test must pass with zero undefined-name violations
      - Files: `tests/test_undefined_names.py` (create new)
      - Verification: Run `pytest tests/test_undefined_names.py -v` and confirm all checks pass on all 7 modules

### TASK 3: Refactor Error Handling in start_instant_transfer

- [ ] **3.1 Restructure start_instant_transfer with per-stage try-except blocks**
      - Stage 1 (stock re-check): stock re-check inside first try-except (validate_cart_inventory)
      - Stage 2 (charge creation): create_transfer_charge inside separate try-except block
      - Stage 3 (persist payment row): payment table insert inside separate try-except block
      - Stage 4 (send account details): send_text inside separate try-except block
      - Specific error handling per stage:
        - **Charge failure**: Log with stage="paystack_charge_failed", reply "Instant transfer isn't available right now, please choose another payment method"
        - **Persist failure**: Log ERROR with stage="persist_payment_failed" + channel + restaurant_id + order_id + paystack_ref, cancel order, reply "Something went wrong while setting up your transfer. Please try again or choose another payment method."
        - **Send details failure**: Log stage="send_details_failed", keep order pending, let STATUS re-send
      - Every log line must include: stage, channel ("whatsapp"), restaurant_id, order_id; never log secrets
      - Files: `whatsapp.py` lines 2402-2437
      - Verification: Run existing tests pass + new test cases confirm proper error messages per stage

- [ ] **3.2 Find and refactor Telegram equivalent in bot.py**
      - Search for Telegram instant transfer payment handling (should mirror whatsapp.py logic)
      - Apply same per-stage error handling pattern
      - Files: `bot.py` (search for Telegram instant transfer payment code)
      - Verification: Confirm same error handling as WhatsApp version

- [ ] **3.3 Verify Paystack kitchen gate hides unconfirmed orders**
      - Check kitchen board query in bot.py for how it filters orders
      - Confirm that orders with pending paystack status are hidden until confirmation
      - Look for the query that constructs `/pending` or kitchen board display
      - Files: `bot.py` (search for kitchen board / order status filtering)
      - Verification: Grep confirm that query filters by order_status or payment_status appropriately

### TASK 4: Fix Check Payment Status-Aware Reply

- [ ] **4.1 Identify ALL handlers that could fire for "Check payment" / STATUS**
      - Telegram inline button: `pcheck_` callback pattern (found at line 2840 in bot.py)
      - Telegram handler: `check_instant_transfer()` at line 2860 in bot.py
      - WhatsApp STATUS command: search for `cmd_status` or `STATUS` handling in whatsapp.py
      - Delivery-bot handlers: check if `delivery_bots` have separate handlers registered
      - Files: `bot.py`, `whatsapp.py`, search for status/check callbacks
      - Verification: Grep shows all handler locations and no duplicate/shadowed handlers

- [ ] **4.2 Create unified status-aware check payment handler in bot.py**
      - Replace `check_instant_transfer()` with status-aware logic:
        - If stored status is "pending": run Paystack verification first to get current status, then show account details again + time left
        - If stored status is "confirmed": reply "Payment confirmed. Your order is being prepared." + short order id
        - If stored status is "rejected": reply "Your payment was rejected. Please try again or choose another method."
        - If stored status is "expired": reply "Your payment window has expired. Please place a new order."
        - If stored status is "needs_review" / "amount_mismatch": reply "Your payment is under review. The restaurant will contact you."
        - If stored status is "cancelled": reply "Your order was cancelled."
        - If stored status is "refunded": reply "Your payment has been refunded."
        - On final status (confirmed/rejected/expired/cancelled), remove Check payment inline button with try-except tolerating "message is not modified" error
      - Channel-correct wording: Telegram must not mention STATUS/MENU; use "Check payment" or "tap the button"
      - Files: `bot.py` lines 2858-2869
      - Verification: Mock tests confirm each status value returns correct reply + button removed on final statuses

- [ ] **4.3 Fix WhatsApp STATUS command to use unified status check**
      - Find WhatsApp STATUS command handler in whatsapp.py
      - Reuse the status-aware logic from bot.py (import or extract as shared function)
      - Channel-correct wording: WhatsApp must not mention Telegram commands; use "Reply MENU" or plain text
      - Files: `whatsapp.py` (find STATUS handler, likely in handle_text)
      - Verification: Mock test confirms each status value returns correct WhatsApp-formatted reply

- [ ] **4.4 Check delivery-bot dispatcher doesn't shadow handlers**
      - Verify that delivery bots use same handler as shared bot
      - If separate handlers exist, unify or remove duplication
      - Files: `bot.py` search for `delivery_bots` callback registration, `get_bot_for_order()`
      - Verification: Grep confirms no duplicate status handlers; delivery-bot tests pass

### TASK 5: Create Comprehensive Test Suite

- [ ] **5.1 Create tests/test_instant_transfer.py with mock instant transfer scenarios**
      - Mock test 1: Persist payment row fails → order cancelled + "something went wrong" message, not "not available"
      - Mock test 2: Charge creation fails (Paystack error) → "not available" message, order cancelled
      - Mock test 3: Success path → payment row inserted + account details sent
      - Mock test 4: Send details fails after persist succeeds → order kept pending, send_details_failed logged
      - Use mocking for supabase operations, create_transfer_charge, send_text
      - Files: `tests/test_instant_transfer.py` (create new)
      - Verification: Run `pytest tests/test_instant_transfer.py -v` and all 4 test cases pass

- [ ] **5.2 Create tests/test_check_payment_status.py with status-aware check payment**
      - Mock test per status value: pending, confirmed, rejected, expired, needs_review, cancelled, refunded
      - Each test: check reply wording is status-aware and channel-correct (Telegram vs WhatsApp)
      - Final status tests: confirm inline button is removed (message edit tolerates "not modified" error)
      - Delivery-bot fixture: confirm payment checks work through dedicated delivery bot
      - Files: `tests/test_check_payment_status.py` (create new)
      - Verification: Run `pytest tests/test_check_payment_status.py -v` and all status scenarios pass

- [ ] **5.3 Run full test suite and compile checks**
      - Run `python3 -m py_compile bot.py main.py whatsapp.py whatsapp_state.py paystack_transfer.py reports.py receipt_generator.py` to verify no syntax errors
      - Run `pytest tests/test_undefined_names.py tests/test_instant_transfer.py tests/test_check_payment_status.py -v` to confirm all new tests pass
      - Run any existing tests to confirm no regressions
      - Files: all runtime modules + test files
      - Verification: All compile checks pass + all tests pass with no regressions

### TASK 6: Update Documentation

- [ ] **6.1 Update context.md with error handling and status check flow**
      - Document per-stage error handling in start_instant_transfer
      - Document status-aware Check payment flow
      - Document that persist_payment_failed now triggers order cancellation
      - Document send_details_failed keeps order pending for STATUS re-send
      - Files: `context.md`
      - Verification: context.md is internally consistent and matches code behavior

---

## Detailed Fix References

### BUG 1 Fix: whatsapp.py line 2402
**Current:**
```python
async def start_instant_transfer(state, restaurant, from_number, customer_name):
    """Create the pending Paystack order only after the normal stock re-check."""
    try:
        await state.update_data(payment_method="Paystack")
        order_id, order = await create_order_in_db(None, state, "Paystack", customer_name, "whatsapp", None, from_number)
        # ...
        supabase.table("orders").update({...}).execute()  # LINE 2413 - undefined supabase
        # ...
        supabase.table("payments").insert({...}).execute()  # LINE 2418 - undefined supabase
```

**Fix:**
```python
async def start_instant_transfer(state, restaurant, from_number, customer_name):
    """Create the pending Paystack order only after the normal stock re-check."""
    try:
        await state.update_data(payment_method="Paystack")
        order_id, order = await create_order_in_db(None, state, "Paystack", customer_name, "whatsapp", None, from_number)
        # ...
        state.supabase.table("orders").update({...}).execute()  # Use state.supabase
        # ...
        state.supabase.table("payments").insert({...}).execute()  # Use state.supabase
```

### BUG 3 Fix: whatsapp.py start_instant_transfer refactoring
**Current Structure:** Single try-except catching all exceptions with generic "not available" message

**New Structure:**
```python
async def start_instant_transfer(state, restaurant, from_number, customer_name):
    """Create the pending Paystack order with per-stage error handling."""
    
    # Stage 1: Stock re-check
    try:
        data = await state.get_data()
        total = await recalculate_total(state, restaurant)  # Validates stock
    except ValueError as exc:
        logging.error("WhatsApp instant transfer failed stage=stock_check channel=whatsapp restaurant_id=%s error=%s", 
                     restaurant.get("id"), str(exc)[:200])
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], 
                       from_number, f"⚠️ {truncate(exc, 1000)}")
        return
    
    # Stage 2: Create pending order & charge
    try:
        await state.update_data(payment_method="Paystack")
        order_id, order = await create_order_in_db(None, state, "Paystack", customer_name, "whatsapp", None, from_number)
        email = f"{''.join(ch for ch in from_number if ch.isdigit())}@chowlin.ng"
        order = {**(order or {}), "id": order_id, "total_amount": total, "restaurant_id": restaurant["id"], "order_channel": "whatsapp"}
        details = await create_transfer_charge(order, restaurant, email)
    except Exception as exc:
        logging.error("WhatsApp instant transfer failed stage=order_creation channel=whatsapp restaurant_id=%s order_id=%s error=%s",
                     restaurant.get("id"), order_id if order_id else "unknown", str(exc)[:200], exc_info=True)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"],
                       from_number, "Instant transfer isn't available right now, please choose another payment method")
        return
    
    if not details:
        logging.warning("Paystack transfer charge failed stage=paystack_charge_failed channel=whatsapp restaurant_id=%s order_id=%s",
                       restaurant.get("id"), order_id)
        try:
            state.supabase.table("orders").update({"order_status": "cancelled"}).eq("id", order_id).eq("restaurant_id", restaurant["id"]).execute()
        except Exception as e:
            logging.error("Failed to cancel order after charge failure order_id=%s error=%s", order_id, str(e)[:200])
        await state.set_state("waiting_for_payment_method")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"],
                       from_number, "Instant transfer isn't available right now, please choose another payment method")
        return
    
    # Stage 3: Persist payment row
    try:
        total_amount = money(order.get("total_amount"))
        state.supabase.table("payments").insert({
            "order_id": order_id,
            "restaurant_id": restaurant["id"],
            "amount": str(total_amount),
            "provider": "Paystack",
            "status": "pending",
            "provider_reference": details["reference"],
            "paystack_reference": details["reference"],
            "paystack_subaccount_code": restaurant["paystack_subaccount_code"],
            "account_number": details["account_number"],
            "account_name": details["account_name"],
            "bank_name": details["bank_name"],
            "expires_at": details["expires_at"],
            "channel": "transfer"
        }).execute()
    except Exception as exc:
        logging.error("WhatsApp instant transfer failed stage=persist_payment_failed channel=whatsapp restaurant_id=%s order_id=%s paystack_reference=%s error=%s",
                     restaurant.get("id"), order_id, details.get("reference"), str(exc)[:200], exc_info=True)
        try:
            await cancel_pending_order(order_id, restaurant["id"], state.supabase, "Setup failed", "whatsapp", from_number)
        except Exception as e:
            logging.error("Failed to cancel order after persist failure order_id=%s error=%s", order_id, str(e)[:200])
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"],
                       from_number, "Something went wrong while setting up your transfer. Please try again or choose another payment method.")
        return
    
    # Stage 4: Send account details
    expires = datetime.fromisoformat(details["expires_at"].replace("Z", "+00:00")).astimezone(__import__('pytz').timezone("Africa/Lagos")).strftime("%I:%M %p")
    try:
        await state.set_state("awaiting_transfer")
        await state.update_data(active_transfer_order_id=order_id)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"],
                       from_number, f"Bank: {details['bank_name']}\nAccount name: {details['account_name']}\nAccount number:\n{details['account_number']}\nAmount: ₦{total_amount:,.0f}\nExpires: {expires} WAT\n\nTransfer the exact amount. Your order is confirmed automatically once payment arrives. {transfer_customer_wording('whatsapp')}")
    except Exception as exc:
        logging.error("WhatsApp instant transfer failed stage=send_details_failed channel=whatsapp restaurant_id=%s order_id=%s paystack_reference=%s error=%s",
                     restaurant.get("id"), order_id, details.get("reference"), str(exc)[:200], exc_info=True)
        # Keep order and payment row pending; STATUS will re-send details
```

### BUG 4 Fix: bot.py line 2860 - check_instant_transfer refactoring
**Current:**
```python
@dp.callback_query(F.data.startswith("pcheck_"))
async def check_instant_transfer(callback_query: types.CallbackQuery):
    reference = (callback_query.data or "")[7:]
    payment = supabase.table("payments").select("order_id, restaurant_id, status").eq(
        "paystack_reference", reference
    ).execute()
    row = payment.data[0] if payment.data else None
    if not row or row.get("status") != "pending":
        await callback_query.answer("Payment is no longer pending.", show_alert=True)
        return
    # ...
    await callback_query.answer("Payment confirmed." if result.get("status") == "ok" else "Payment is still pending.", show_alert=True)
```

**New Status-Aware:**
```python
@dp.callback_query(F.data.startswith("pcheck_"))
async def check_instant_transfer(callback_query: types.CallbackQuery):
    """Status-aware payment check with Paystack re-verification when pending."""
    reference = (callback_query.data or "")[7:]
    payment = supabase.table("payments").select(
        "id, order_id, restaurant_id, status, paystack_reference, provider_reference"
    ).eq("paystack_reference", reference).limit(1).execute()
    
    if not payment.data:
        await callback_query.answer("Payment record not found.", show_alert=True)
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    
    payment_row = payment.data[0]
    stored_status = payment_row.get("status", "pending")
    order_id = payment_row.get("order_id")
    
    # Stage 1: If pending, verify current status with Paystack
    if stored_status == "pending":
        from main import confirm_paystack_payment
        result = await confirm_paystack_payment(reference, "telegram_check_payment")
        current_status = result.get("status_after_check", stored_status)
    else:
        current_status = stored_status
    
    # Stage 2: Build status-aware reply
    try:
        if current_status == "pending":
            # Still waiting - show details again + time left
            expires_result = supabase.table("payments").select("expires_at").eq("id", payment_row.get("id")).execute()
            expires_at = expires_result.data[0].get("expires_at") if expires_result.data else None
            if expires_at:
                expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                time_left = (expires_dt - datetime.now(timezone.utc)).total_seconds() / 60
                minutes_left = int(time_left)
                if minutes_left > 0:
                    await callback_query.answer(f"⏱️ Your payment window is still open. You have ~{minutes_left} minutes left.", show_alert=True)
                else:
                    await callback_query.answer("Your payment window has expired. Please place a new order.", show_alert=True)
                    try:
                        await callback_query.message.edit_reply_markup(reply_markup=None)
                    except TelegramBadRequest:
                        pass
            else:
                await callback_query.answer("Payment is still pending. Please complete the transfer.", show_alert=True)
        
        elif current_status == "confirmed":
            await callback_query.answer(f"✅ Payment confirmed! Your order is being prepared.", show_alert=True)
            try:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        
        elif current_status == "rejected":
            await callback_query.answer("❌ Your payment was rejected. Please try again or choose another payment method.", show_alert=True)
            try:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        
        elif current_status == "expired":
            await callback_query.answer("⏱️ Your payment window has expired. Please place a new order.", show_alert=True)
            try:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        
        elif current_status in ("needs_review", "amount_mismatch"):
            await callback_query.answer("⏳ Your payment is under review. The restaurant will contact you shortly.", show_alert=True)
        
        elif current_status == "cancelled":
            await callback_query.answer("Your order was cancelled.", show_alert=True)
            try:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        
        elif current_status == "refunded":
            await callback_query.answer("Your payment has been refunded.", show_alert=True)
            try:
                await callback_query.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        
        else:
            await callback_query.answer("Payment status unknown. Please contact support.", show_alert=True)
    
    except Exception as e:
        logging.exception("Error in check_instant_transfer reference=%s", reference)
        await callback_query.answer("Could not check payment status. Please try again.", show_alert=True)
```

---

## Files to Modify

1. **whatsapp.py** (3 locations):
   - Line 2402: Fix `start_instant_transfer` function signature
   - Lines 2413, 2418: Replace bare `supabase` with `state.supabase`
   - Refactor entire function with per-stage error handling

2. **bot.py** (2 locations):
   - Line 2860: Refactor `check_instant_transfer` callback with status-aware logic
   - Find and refactor Telegram instant transfer payment setup (mirror WhatsApp logic)

3. **tests/test_undefined_names.py** (create new):
   - AST-based undefined name checker

4. **tests/test_instant_transfer.py** (create new):
   - 4 mock test scenarios for instant transfer error handling

5. **tests/test_check_payment_status.py** (create new):
   - 7 status-aware check payment test scenarios + delivery-bot fixture

6. **context.md** (update):
   - Document per-stage error handling
   - Document status-aware check payment flow

7. **Dockerfile** (update if new test modules):
   - Include any new runtime modules in COPY directive

---

## Verification Checklist

- [ ] Python compilation: `python3 -m py_compile bot.py main.py whatsapp.py whatsapp_state.py paystack_transfer.py reports.py receipt_generator.py`
- [ ] Static undefined names test passes: `pytest tests/test_undefined_names.py -v`
- [ ] Instant transfer error handling tests pass: `pytest tests/test_instant_transfer.py -v`
- [ ] Check payment status tests pass: `pytest tests/test_check_payment_status.py -v`
- [ ] No regressions in existing tests: `pytest tests/ -v`
- [ ] context.md reflects all changes accurately

---

## Known Constraints Respected

✅ No new environment variables  
✅ No secrets stored/logged  
✅ restaurant_id scoping on every query  
✅ Decimal for money values  
✅ create_order_in_db() unchanged  
✅ All other payment flows untouched  
✅ Rider dispatch, webhook deduplication, subscription check, Paystack kitchen gate, catalog sync untouched  
✅ No git operations (read-only status/diff only)  
