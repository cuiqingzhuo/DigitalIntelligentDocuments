import json
import os
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException

from .routing import IDENTIFIER


@dataclass(frozen=True)
class Actor:
    actor: str
    role: str
    library: str


def principals(demo):
    settings = os.getenv("DIW_TOKENS_JSON") or "{}"
    data = json.loads(settings)
    if not isinstance(data, dict):
        raise RuntimeError("DIW_TOKENS_JSON must be an object")
    if demo:
        data = {**{
            "demo-librarian": {"actor": "演示馆员", "role": "librarian", "library": "demo"},
            "demo-admin": {"actor": "演示管理员", "role": "admin", "library": "demo"},
            "demo-verifier": {"actor": "演示核验员", "role": "verifier", "library": "demo"},
        }, **data}
    for token, value in data.items():
        if (not isinstance(value, dict) or not token.isascii()
                or (len(token) < 32 and not demo)
                or value.get("role") not in ("librarian", "admin", "verifier")
                or not IDENTIFIER.fullmatch(value.get("library", ""))
                or not 0 < len(value.get("actor", "")) <= 100):
            raise RuntimeError("Invalid token configuration")
    return {token: Actor(**value) for token, value in data.items()}


def authorize(roles):
    def guard(authorization: str | None = Header(default=None)):
        from .main import actors
        scheme, _, token = (authorization or "").partition(" ")
        for expected, actor in actors.items():
            if scheme.lower() == "bearer" and secrets.compare_digest(expected.encode(), token.encode()):
                if actor.role not in roles:
                    raise HTTPException(403, "当前角色无权进行此操作")
                return actor
        raise HTTPException(401, "请登录工作台", headers={"WWW-Authenticate": "Bearer"})
    return guard
