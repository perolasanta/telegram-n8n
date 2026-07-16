-- ==========================================
-- DROP WRONG TABLES/TRIGGERS FROM schoolpay
-- ==========================================

DROP TRIGGER IF EXISTS trg_whatsapp_sessions_updated_at
ON schoolpay.whatsapp_sessions;

DROP TABLE IF EXISTS schoolpay.menu_item_catalog_map CASCADE;
DROP TABLE IF EXISTS schoolpay.whatsapp_sessions CASCADE;

DROP FUNCTION IF EXISTS schoolpay.update_whatsapp_sessions_updated_at();


-- ==========================================
-- ALTER EXISTING TABLES IN public
-- ==========================================

ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS whatsapp_phone_number_id TEXT,
    ADD COLUMN IF NOT EXISTS whatsapp_business_account_id TEXT,
    ADD COLUMN IF NOT EXISTS whatsapp_access_token TEXT;

ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS order_channel TEXT NOT NULL DEFAULT 'telegram',
    ADD COLUMN IF NOT EXISTS customer_contact TEXT;

ALTER TABLE public.menu_items
    ADD COLUMN IF NOT EXISTS image_url TEXT;


-- ==========================================
-- CREATE whatsapp_sessions IN public
-- ==========================================

CREATE TABLE IF NOT EXISTS public.whatsapp_sessions (
    phone_number TEXT NOT NULL,
    restaurant_id UUID NOT NULL REFERENCES public.restaurants(id) ON DELETE CASCADE,
    state TEXT,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (phone_number, restaurant_id)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_sessions_lookup
ON public.whatsapp_sessions (phone_number, restaurant_id);


-- ==========================================
-- CREATE menu_item_catalog_map IN public
-- ==========================================

CREATE TABLE IF NOT EXISTS public.menu_item_catalog_map (
    menu_item_id UUID PRIMARY KEY REFERENCES public.menu_items(id) ON DELETE CASCADE,
    restaurant_id UUID NOT NULL REFERENCES public.restaurants(id) ON DELETE CASCADE,
    catalog_retailer_id TEXT NOT NULL
);


-- ==========================================
-- CREATE updated_at TRIGGER FUNCTION
-- ==========================================

CREATE OR REPLACE FUNCTION public.update_whatsapp_sessions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


DROP TRIGGER IF EXISTS trg_whatsapp_sessions_updated_at
ON public.whatsapp_sessions;

CREATE TRIGGER trg_whatsapp_sessions_updated_at
BEFORE UPDATE ON public.whatsapp_sessions
FOR EACH ROW
EXECUTE FUNCTION public.update_whatsapp_sessions_updated_at();
