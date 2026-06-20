ALTER TABLE restaurants
  ADD COLUMN IF NOT EXISTS paystack_subaccount_code text,
  ADD COLUMN IF NOT EXISTS paystack_enabled boolean DEFAULT false;

ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS paystack_reference text,
  ADD COLUMN IF NOT EXISTS paystack_subaccount_code text;
