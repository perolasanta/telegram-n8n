ALTER TABLE restaurants
  ADD COLUMN IF NOT EXISTS pickup_enabled boolean DEFAULT false;