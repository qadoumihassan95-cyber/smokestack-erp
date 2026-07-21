"""Business Assistant — local, deterministic intelligence layer.

No LLM, no external service, no API key, no recurring cost. The assistant is
built from three parts:

    intent.py   phrase  → intent + entities   (bilingual, deterministic)
    tools.py    intent  → structured ERP data (RBAC-enforced registry)
    engine.py   data    → answer + business rules + next action

Import order matters: engine depends on both, tools depends on neither.
"""
from . import intent, tools, engine  # noqa: F401

__all__ = ["intent", "tools", "engine"]
