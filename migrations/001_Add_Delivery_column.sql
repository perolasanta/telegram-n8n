ALTER TABLE orders 
ADD COLUMN IF NOT EXISTS delivery_address text,
ADD COLUMN IF NOT EXISTS delivery_lat numeric,
ADD COLUMN IF NOT EXISTS delivery_lon numeric;
