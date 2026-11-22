# Telegram Restaurant Bot

A Telegram bot for restaurant ordering with payment integration.

## Features
- **Multi-tenant Architecture**: Supports multiple restaurants with isolated menus, orders, and kitchen operations.
- **Flexible Ordering**:
  - **Dine-in**: Customers scan table-specific QR codes for in-restaurant ordering.
  - **Delivery/Pickup**: Dedicated external QR codes or delivery-only bots enable customers to choose between delivery and pickup, with options for address input or location sharing.
- **Menu Browsing & Cart Management**: Intuitive interface for browsing categories, selecting items, and managing quantities in the cart.
- **Multiple Payment Methods**: Supports Cash Payment, Bank Transfer (with payment proof upload), Pay on Delivery (for delivery orders), and secure Paystack (card payments).
- **Kitchen & Manager Dashboards**:
  - **Kitchen Group**: Receives new orders with real-time updates, payment verification (for bank transfers), and options to mark orders as "Preparing" or "Ready". Includes a live, pinned order board with rush hour alerts.
  - **Manager Commands**: Access to daily, weekly, and monthly sales reports; subscription management; and inventory oversight.
- **Inventory Management**: Track menu item stock, set restock thresholds, receive low-stock alerts, and easily update inventory via kitchen commands.
- **Order History & Reorder**: Customers can view past orders and quickly reorder items, with automatic checks for current item availability.
- **Subscription System**: Manages restaurant subscriptions (trialing, active, expired) with automated checks and manager notifications.
- **n8n Integration**: Webhook integration for external notifications and data synchronization (e.g., Google Sheets).

## Paystack
- Paystack is added as a Telegram-only payment option.
- Add `PAYSTACK_SECRET_KEY` to your environment.
- One-time subaccount creation is available via `/admin/paystack/subaccount/{restaurant_id}`.
- The webhook endpoint is `/webhook/paystack` and verifies `x-paystack-signature`.

## Deployment
Deployed on Render with FastAPI webhook.