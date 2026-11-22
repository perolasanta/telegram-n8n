-- Migration 005: Multi-Bot Delivery Support
-- Adds support for restaurant-specific delivery-only Telegram bots

ALTER TABLE restaurants
ADD COLUMN IF NOT EXISTS delivery_bot_token text;

COMMENT ON COLUMN restaurants.delivery_bot_token IS
  'Telegram bot token for this restaurant''s dedicated delivery-only bot. NULL = no delivery bot (uses shared QR bot instead).';
