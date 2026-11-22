-- Migration 005: Composite Items & Modifier Groups
-- Purpose: support "build-your-own" items like Swallow + Soup + Protein
--          e.g. "Amala with Ewedu and 3x Goat Meat"
--
-- Design rules encoded here:
--   1. Soup is NEVER a standalone menu_item — it only exists as a modifier_option
--      nested under a swallow's modifier_group. This is what makes "soup cannot
--      be sold alone" true by construction, no extra flag required.
--   2. Swallow CAN be sold alone: both the soup group and protein group are
--      optional (min_select = 0), so a customer can skip them entirely.
--   3. Soup itself is unpriced: its modifier_options carry price_delta = 0.
--      Protein is priced and supports quantity (allow_quantity = true).
--
-- Safe to run on the live DB: every change here is additive.
-- No existing column is dropped, renamed, retyped, or given a new NOT NULL
-- constraint. Existing 'simple' menu_items are unaffected — the ordering
-- flow for them stays exactly as it is today.

BEGIN;

-- 1. Mark which menu_items are composite (have modifier groups) vs simple
ALTER TABLE menu_items
  ADD COLUMN IF NOT EXISTS item_type text NOT NULL DEFAULT 'simple'
    CHECK (item_type IN ('simple', 'composite'));

-- 2. Groups of choices attached to a composite menu_item
--    e.g. "Choose your soup", "Choose your protein"
CREATE TABLE IF NOT EXISTS modifier_groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  menu_item_id uuid NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
  name text NOT NULL,                    -- "Choose your soup"
  selection_mode text NOT NULL DEFAULT 'single'
    CHECK (selection_mode IN ('single', 'multi')),
  min_select integer NOT NULL DEFAULT 0, -- 0 = optional group (swallow-alone case)
  max_select integer,                    -- NULL = unlimited (multi only)
  allow_quantity boolean NOT NULL DEFAULT false,  -- true for protein ("how many?")
  display_order integer NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true
);

-- 3. The actual choices inside a group
--    Soup options -> price_delta = 0 always (soup is bundled, never priced)
--    Protein options -> real price_delta, allow_quantity handled at group level
CREATE TABLE IF NOT EXISTS modifier_options (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id uuid NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
  name text NOT NULL,           -- "Ewedu", "Goat meat"
  price_delta numeric NOT NULL DEFAULT 0,
  unit_label text,              -- "piece", "wrap" (mainly for protein)
  is_available boolean NOT NULL DEFAULT true,
  display_order integer NOT NULL DEFAULT 0
);

-- 4. What was actually picked for a given order line
--    unit_price snapshots price_delta at order time (mirrors order_items.unit_price)
CREATE TABLE IF NOT EXISTS order_item_modifiers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_item_id uuid NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
  modifier_option_id uuid NOT NULL REFERENCES modifier_options(id),
  quantity integer NOT NULL DEFAULT 1,
  unit_price numeric NOT NULL DEFAULT 0,   -- snapshot of price_delta at order time
  subtotal numeric GENERATED ALWAYS AS (quantity * unit_price) STORED
);

CREATE INDEX IF NOT EXISTS idx_modifier_groups_menu_item ON modifier_groups(menu_item_id);
CREATE INDEX IF NOT EXISTS idx_modifier_options_group ON modifier_options(group_id);
CREATE INDEX IF NOT EXISTS idx_order_item_modifiers_order_item ON order_item_modifiers(order_item_id);

COMMIT;

-- ---------------------------------------------------------------------
-- Example seed data for a restaurant's "Swallow Combo" composite item
-- (adjust restaurant_id / category_id to real values before running)
-- ---------------------------------------------------------------------
--
-- INSERT INTO menu_items (restaurant_id, category_id, name, description, price, item_type)
-- VALUES ('<restaurant_id>', '<category_id>', 'Swallow Combo', 'Pick your swallow, soup and protein', 500, 'composite')
-- RETURNING id;  -- call this <swallow_item_id> below
--
-- -- Swallow choice (required, single-select, priced via price_delta on top of base 500)
-- INSERT INTO modifier_groups (menu_item_id, name, selection_mode, min_select, max_select, allow_quantity, display_order)
-- VALUES ('<swallow_item_id>', 'Choose your swallow', 'single', 1, 1, false, 1)
-- RETURNING id;  -- <swallow_group_id>
--
-- INSERT INTO modifier_options (group_id, name, price_delta, display_order) VALUES
--   ('<swallow_group_id>', 'Amala', 0, 1),
--   ('<swallow_group_id>', 'Pounded Yam', 100, 2),
--   ('<swallow_group_id>', 'Eba', 0, 3),
--   ('<swallow_group_id>', 'Semo', 0, 4);
--
-- -- Soup choice (OPTIONAL, multi-select up to 2, always price_delta = 0)
-- INSERT INTO modifier_groups (menu_item_id, name, selection_mode, min_select, max_select, allow_quantity, display_order)
-- VALUES ('<swallow_item_id>', 'Choose your soup', 'multi', 0, 2, false, 2)
-- RETURNING id;  -- <soup_group_id>
--
-- INSERT INTO modifier_options (group_id, name, price_delta, display_order) VALUES
--   ('<soup_group_id>', 'Ewedu', 0, 1),
--   ('<soup_group_id>', 'Egusi', 0, 2),
--   ('<soup_group_id>', 'Vegetable', 0, 3),
--   ('<soup_group_id>', 'Ogbono', 0, 4);
--
-- -- Protein choice (OPTIONAL, multi-select, priced, quantity allowed)
-- INSERT INTO modifier_groups (menu_item_id, name, selection_mode, min_select, max_select, allow_quantity, display_order)
-- VALUES ('<swallow_item_id>', 'Choose your protein', 'multi', 0, NULL, true, 3)
-- RETURNING id;  -- <protein_group_id>
--
-- INSERT INTO modifier_options (group_id, name, price_delta, unit_label, display_order) VALUES
--   ('<protein_group_id>', 'Goat meat', 400, 'piece', 1),
--   ('<protein_group_id>', 'Beef', 250, 'piece', 2),
--   ('<protein_group_id>', 'Fish', 600, 'piece', 3),
--   ('<protein_group_id>', 'Chicken', 500, 'piece', 4);
