"""Admin panel: cross-user read access, plus card-order fulfilment.

Every other module scopes its reads to the session user (`.eq("user_id", ...)`).
This one deliberately does NOT - that is the whole point of an admin view, and
also why `require_admin` is applied to the router as a whole rather than
endpoint by endpoint (see router.py).
"""
