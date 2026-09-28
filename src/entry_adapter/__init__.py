# PART: entry-adapter v0.3.0 (parts@45f8315)
from .auth import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS, authenticate_scope

__all__ = [
    "authenticate_scope",
    "SCOPE_KEYS",
    "STG_DEFAULT_SCOPE_ENVS",
]
