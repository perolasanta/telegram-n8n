-- Migration 016: per-restaurant cursor for the scheduled daily report
--
-- Purpose: make the 21:00 (WAT) daily report cursor-based so orders placed
-- after the cutoff roll into the next day's report instead of falling into
-- the gap between the calendar-day window and the next day's 00:00 start.
--
-- Additive only: a single nullable timestamptz column on restaurants. No
-- backfill. A NULL cursor means "first run": the report falls back to the
-- start of today (00:00 WAT) and then advances to the window end on success.
-- After that, every report covers [previous cutoff, this cutoff).
--
-- Safe to run on the live DB: purely additive, no existing column is dropped,
-- renamed, retyped, or given a NOT NULL constraint.

BEGIN;

ALTER TABLE restaurants
    ADD COLUMN IF NOT EXISTS last_daily_report_cutoff timestamptz;

COMMIT;
