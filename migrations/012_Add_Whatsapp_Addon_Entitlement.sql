-- Add the WhatsApp add-on entitlement flag; new tenants default to disabled.
ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS whatsapp_addon_enabled boolean NOT NULL DEFAULT false;

-- Add an optional WhatsApp add-on expiry; NULL means no separate expiry.
ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS whatsapp_addon_expires_at timestamptz NULL;

-- Grandfather tenants already configured with WhatsApp credentials as enabled.
UPDATE public.restaurants
SET whatsapp_addon_enabled = true
WHERE whatsapp_phone_number_id IS NOT NULL
  AND whatsapp_access_token IS NOT NULL;
