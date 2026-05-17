"""
infra/ — Production infrastructure modules.

Hermes-owned. No trading logic. Additive observability and hardening only.

Modules:
    auth_health  — Token/auth lifecycle monitoring
    db_health    — Read-only database health diagnostics
"""

# Package marker — no imports at package level to avoid circular deps
