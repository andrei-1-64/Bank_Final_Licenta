#!/usr/bin/env bash
# Runs inside the backend container. No local database to wait for or
# migrate - the schema lives in Supabase, applied via
# backend/supabase/migrations/*.sql pasted into the Supabase SQL Editor.
set -euo pipefail

# --log-level info so the agent's tool-loop trace ("executing tool=...") is
# visible; at uvicorn's default of WARNING those lines are silently dropped.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
