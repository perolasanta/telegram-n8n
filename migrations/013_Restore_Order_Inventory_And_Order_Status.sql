-- Extend any existing order_status CHECK without discarding its current rule or values.
-- If the database has no order_status CHECK constraint, this block makes no changes.
DO $migration$
DECLARE
    check_row RECORD;
    check_expression TEXT;
    missing_statuses TEXT[];
    missing_status_values TEXT;
BEGIN
    FOR check_row IN
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid = 'public.orders'::regclass
          AND contype = 'c'
          AND position('order_status' IN pg_get_constraintdef(oid)) > 0
    LOOP
        missing_statuses := ARRAY[]::TEXT[];
        IF position(quote_literal('cancelled') IN check_row.definition) = 0 THEN
            missing_statuses := array_append(missing_statuses, 'cancelled');
        END IF;
        IF position(quote_literal('delivered') IN check_row.definition) = 0 THEN
            missing_statuses := array_append(missing_statuses, 'delivered');
        END IF;

        IF cardinality(missing_statuses) > 0 THEN
            check_expression := substr(
                check_row.definition,
                length('CHECK (') + 1,
                length(check_row.definition) - length('CHECK (') - 1
            );
            IF check_expression IS NULL OR check_expression = check_row.definition THEN
                RAISE EXCEPTION 'Could not safely extend orders constraint %: %',
                    check_row.conname, check_row.definition;
            END IF;
            SELECT string_agg(quote_literal(status_value), ', ')
            INTO missing_status_values
            FROM unnest(missing_statuses) AS statuses(status_value);

            EXECUTE format(
                'ALTER TABLE public.orders DROP CONSTRAINT %I',
                check_row.conname
            );
            EXECUTE format(
                'ALTER TABLE public.orders ADD CONSTRAINT %I CHECK ((%s) OR order_status IN (%s))',
                check_row.conname, check_expression, missing_status_values
            );
        END IF;
    END LOOP;
END;
$migration$;

-- Restore only orders whose inventory was previously deducted; the flag update
-- is the idempotency guard and shares the transaction with the stock restoration.
CREATE OR REPLACE FUNCTION public.restore_order_inventory(p_order_id uuid)
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
    SELECT orders.restaurant_id
    INTO v_restaurant_id
    FROM public.orders
    WHERE orders.id = p_order_id
      AND orders.inventory_deducted = true
    FOR UPDATE;

    IF v_restaurant_id IS NULL THEN
        RETURN;
    END IF;

    -- Add back the aggregated quantity for each tracked menu item in this order.
    UPDATE public.menu_items mi
    SET inventory_count = mi.inventory_count + item_quantities.quantity_ordered,
        is_available = CASE
            WHEN mi.inventory_count + item_quantities.quantity_ordered > 0 THEN true
            ELSE mi.is_available
        END
    FROM (
        SELECT order_items.menu_item_id, SUM(order_items.quantity)::integer AS quantity_ordered
        FROM public.order_items
        WHERE order_items.order_id = p_order_id
        GROUP BY order_items.menu_item_id
    ) AS item_quantities
    WHERE mi.id = item_quantities.menu_item_id
      AND mi.restaurant_id = v_restaurant_id
      AND mi.track_inventory = true;

    -- Mark restoration complete after the tracked quantities have been added back.
    UPDATE public.orders
    SET inventory_deducted = false
    WHERE orders.id = p_order_id
      AND orders.restaurant_id = v_restaurant_id
      AND orders.inventory_deducted = true;

    -- Return restored tracked items using deduct_order_inventory's result shape.
    RETURN QUERY
    SELECT mi.id, mi.name, mi.inventory_count, mi.restock_threshold
    FROM public.menu_items mi
    WHERE mi.restaurant_id = v_restaurant_id
      AND mi.track_inventory = true
      AND EXISTS (
          SELECT 1
          FROM public.order_items oi
          WHERE oi.order_id = p_order_id
            AND oi.menu_item_id = mi.id
      )
    ORDER BY mi.name;
END;
$$;
