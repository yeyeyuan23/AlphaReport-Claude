"""FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from reportagent.db.engine import get_db
from reportagent.models.database import User

__all__ = ["get_db", "get_or_create_user"]


def get_or_create_user(
    x_user_email: str | None = Header(None),
    db: Session = Depends(get_db),
) -> dict:
    """Extract user from X-User-Email header. Auto-creates account on first visit.

    Returns {"id": user.id, "email": user.email} or {"id": None, "email": ""}.
    """
    if not x_user_email:
        return {"id": None, "email": ""}
    email = x_user_email.strip().lower()
    if "@" not in email:
        return {"id": None, "email": ""}
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
    return {"id": user.id, "email": user.email}
