-- ==========================================
-- WhatsApp webhook idempotency
-- ==========================================
-- Used to skip reprocessing the same Meta webhook delivery (Meta retries on
-- timeout/error) or an accidental duplicate customer resend of the same message.

CREATE TABLE IF NOT EXISTS public.whatsapp_processed_messages (
    message_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Optional cleanup: old rows are safe to prune periodically (e.g. > 30 days),
-- since Meta does not redeliver indefinitely. No automatic job is created here.


-- ==========================================
-- Dispatch rider handoff
-- ==========================================

ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS dispatch_group_id BIGINT;

ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS dispatch_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dispatch_sent_by TEXT;

-- NOTE: if orders.order_status has a CHECK constraint restricting allowed
-- values, add 'delivered' to that constraint's allowed list before deploying
-- the rider_marked_delivered_handler in bot.py. This migration does not assume
-- the constraint's current definition and does not attempt to alter it blindly.


-- ==========================================
-- WhatsApp catalog availability sync
-- ==========================================
-- Catalog id (Commerce Manager catalog, distinct from whatsapp_business_account_id)
-- needed to push item availability via the Meta Catalog Batch API.

ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS whatsapp_catalog_id TEXT;