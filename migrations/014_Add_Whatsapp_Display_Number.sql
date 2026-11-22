-- Store the dialable WhatsApp number in international format, digits only, for wa.me links.
-- whatsapp_phone_number_id is Meta's internal id and is not dialable.
ALTER TABLE public.restaurants
    ADD COLUMN IF NOT EXISTS whatsapp_display_number text NULL;
