-- Add the pay-on-delivery setting and backfill existing restaurants only
-- when this migration creates the column, preserving later opt-outs on reruns.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'restaurants'
          AND column_name = 'pay_on_delivery_enabled'
    ) THEN
        ALTER TABLE public.restaurants
            ADD COLUMN IF NOT EXISTS pay_on_delivery_enabled boolean NOT NULL DEFAULT false;

        UPDATE public.restaurants
        SET pay_on_delivery_enabled = true;
    END IF;
END;
$$;

-- Add an optional restaurant brand color for customer-facing displays.
ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS brand_color text NULL;

-- Add an optional restaurant logo URL for customer-facing displays.
ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS logo_url text NULL;

-- Store the Telegram kitchen notification message id for later updates.
ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS kitchen_message_id bigint NULL;

-- Store the text sent with the Telegram kitchen notification.
ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS kitchen_message_text text NULL;
