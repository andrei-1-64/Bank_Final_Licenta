-- Which agent answered a turn, and why (see app/ai/routing.py::RoutingDecision).
-- Written on the assistant message the routed agent produced; NULL on user
-- turns, tool results, and every message written before routing existed - so
-- this is additive and needs no backfill.
--
-- No index: nothing filters or aggregates on it yet. Add one when a real
-- routing-analytics query exists, rather than guessing at its shape now.
--
-- Se ruleaza dupa 0001-0006, in Supabase SQL Editor.

ALTER TABLE messages ADD COLUMN IF NOT EXISTS routing_metadata JSONB;
