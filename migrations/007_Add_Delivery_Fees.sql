ALTER TABLE restaurants
  ADD COLUMN IF NOT EXISTS delivery_fee_type text DEFAULT 'none',
  ADD COLUMN IF NOT EXISTS delivery_fee_flat numeric DEFAULT 0;

CREATE TABLE IF NOT EXISTS delivery_zones (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  restaurant_id uuid REFERENCES restaurants(id) ON DELETE CASCADE,
  zone_name text NOT NULL,
  fee numeric NOT NULL DEFAULT 0,
  is_active boolean DEFAULT true,
  display_order integer DEFAULT 0
);

ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS delivery_fee numeric DEFAULT 0;
