ALTER TABLE restaurants
ADD COLUMN IF NOT EXISTS kitchen_board_message_id bigint,
ADD COLUMN IF NOT EXISTS kitchen_board_message_date date,
ADD COLUMN IF NOT EXISTS kitchen_board_pinned boolean DEFAULT false,
ADD COLUMN IF NOT EXISTS kitchen_rush_alert_date date;
