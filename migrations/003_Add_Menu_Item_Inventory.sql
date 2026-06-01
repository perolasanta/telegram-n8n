ALTER TABLE menu_items
ADD COLUMN IF NOT EXISTS inventory_count integer NOT NULL DEFAULT 0,
ADD COLUMN IF NOT EXISTS restock_threshold integer NOT NULL DEFAULT 0,
ADD COLUMN IF NOT EXISTS track_inventory boolean NOT NULL DEFAULT false;

ALTER TABLE orders
ADD COLUMN IF NOT EXISTS inventory_deducted boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION deduct_order_inventory(p_order_id uuid)
RETURNS TABLE (
    id uuid,
    name text,
    inventory_count integer,
    restock_threshold integer
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_restaurant_id uuid;
BEGIN
    -- Step 1: Mark the order as deducted and get the restaurant
    UPDATE orders
    SET inventory_deducted = true
    WHERE orders.id = p_order_id
      AND orders.payment_status = 'confirmed'
      AND orders.inventory_deducted = false
    RETURNING restaurant_id INTO v_restaurant_id;

    -- If nothing was updated, order is already deducted or not confirmed — bail out
    IF v_restaurant_id IS NULL THEN
        RETURN;
    END IF;

    -- Step 2: Deduct inventory for tracked items in this order
    UPDATE menu_items mi
    SET
        inventory_count = GREATEST(0, mi.inventory_count - iq.quantity_ordered),
        is_available     = GREATEST(0, mi.inventory_count - iq.quantity_ordered) > 0
    FROM (
        SELECT menu_item_id, SUM(quantity)::integer AS quantity_ordered
        FROM order_items
        WHERE order_id = p_order_id
        GROUP BY menu_item_id
    ) iq
    WHERE mi.id = iq.menu_item_id
      AND mi.track_inventory = true;

    -- Step 3: Return items that are now at or below their restock threshold
    RETURN QUERY
    SELECT mi.id, mi.name, mi.inventory_count, mi.restock_threshold
    FROM menu_items mi
    JOIN order_items oi ON oi.menu_item_id = mi.id
    WHERE oi.order_id = p_order_id
      AND mi.track_inventory = true
      AND mi.inventory_count <= mi.restock_threshold
      AND mi.restaurant_id = v_restaurant_id
    ORDER BY mi.name;
END;
$$;