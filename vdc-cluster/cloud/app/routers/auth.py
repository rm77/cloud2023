# /auth - CCRA security: authentication for every actor
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from ..auth import Caller, current_caller, make_token, verify_password
from ..db import AuditEvent, Tenant, User, get_session
from ..schemas import Token, UserOut

TAG = "Security: authentication"
router = APIRouter(prefix="/auth", tags=[TAG])


@router.post("/token", response_model=Token, summary="Log in: username + password -> bearer token")
def login(form: OAuth2PasswordRequestForm = Depends(), s: Session = Depends(get_session)):
    user = s.exec(select(User).where(User.username == form.username)).first()
    if user is None or not verify_password(form.password, user.password_hash):
        s.add(AuditEvent(username=form.username[:32], role="-", action="auth.login", result="denied"))
        s.commit()
        raise HTTPException(401, "wrong username or password", headers={"WWW-Authenticate": "Bearer"})
    tenant = s.get(Tenant, user.tenant_id).name if user.tenant_id else None
    s.add(AuditEvent(username=user.username, role=user.role, tenant=tenant or "", action="auth.login", result="ok"))
    s.commit()
    return Token(access_token=make_token(user, tenant), role=user.role, tenant=tenant)


@router.get("/me", response_model=UserOut, summary="Who am I")
def me(c: Caller = Depends(current_caller)):
    return UserOut(username=c.username, role=c.role, tenant=c.tenant_name or None,
                   tenant_admin=c.user.tenant_admin, created=c.user.created)
