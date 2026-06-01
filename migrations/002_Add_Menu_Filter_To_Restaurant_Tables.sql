ALTER TABLE restaurant_tables
ADD COLUMN IF NOT EXISTS menu_filter text DEFAULT NULL;
