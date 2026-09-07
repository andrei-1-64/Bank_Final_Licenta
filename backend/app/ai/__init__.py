"""Self-contained AI layer.

Architecture rule: the model is UNTRUSTED. In this step it can only READ, via a
single stub tool. No write/money tools exist and none may be added without an
explicit user-confirmation flow through the normal validated endpoints.
"""
