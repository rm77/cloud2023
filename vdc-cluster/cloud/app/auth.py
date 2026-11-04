# auth.py - CCRA security: who is calling, and which actor (role) they are.
#   provider  = Cloud Provider   (/provider, also reads everything)
#   consumer  = Cloud Consumer   (/tenant, only its own tenant)
#   auditor   = Cloud Auditor    (/audit, read only)
import base64
import hashlib
import hmac
import os
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select

from . import config
from .db import Tenant, User, get_session, now

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token")


# ---------------------------------------------------------------- passwords (stdlib scrypt)
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- tokens
def make_token(user: User, tenant: str | None) -> str:
    claims = dict(sub=user.username, role=user.role, tenant=tenant,
                  exp=now() + timedelta(minutes=config.TOKEN_MINUTES))
    return jwt.encode(claims, config.JWT_SECRET, algorithm="HS256")


class Caller:
    """the authenticated caller, as the routes see it"""
    def __init__(self, user: User, tenant: Tenant | None):
        self.user, self.tenant = user, tenant
        self.username, self.role = user.username, user.role

    @property
    def tenant_name(self):
        return self.tenant.name if self.tenant else ""


def current_caller(token: str = Depends(oauth2), s: Session = Depends(get_session)) -> Caller:
    bad = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token",
                        headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise bad
    user = s.exec(select(User).where(User.username == claims.get("sub"))).first()
    if user is None or user.role != claims.get("role"):        # deleted or changed since login
        raise bad
    tenant = s.get(Tenant, user.tenant_id) if user.tenant_id else None
    if user.role == "consumer" and tenant is None:
        raise bad
    return Caller(user, tenant)


def require_role(*roles):
    """router-level guard: the actor this part of the API belongs to"""
    def dep(c: Caller = Depends(current_caller)) -> Caller:
        if c.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"this endpoint is for the {'/'.join(roles)} role, you are {c.role}")
        return c
    return dep


def require_tenant_admin(c: Caller = Depends(current_caller)) -> Caller:
    if not c.user.tenant_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the tenant admin can manage users")
    return c


def bootstrap_admin(s: Session):
    """create the first provider account from VDC_ADMIN_USER / VDC_ADMIN_PASSWORD"""
    if s.exec(select(User).where(User.role == "provider")).first():
        return None
    if not config.ADMIN_PASSWORD:
        return "no provider account: set VDC_ADMIN_PASSWORD and restart"
    s.add(User(username=config.ADMIN_USER, password_hash=hash_password(config.ADMIN_PASSWORD),
               role="provider"))
    s.commit()
    return f"created provider account {config.ADMIN_USER}"
