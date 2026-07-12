"""Device-token registry routes for iOS push (companion SwiftUI app).

POST   /api/v1/devices           {token, platform} — upsert on token
DELETE /api/v1/devices/{token}   — owner only

Upsert semantics: an APNs token identifies a DEVICE, not a user. If the same
device re-registers under a different account (logged out, logged back in as
someone else), the row is REASSIGNED to the current user — otherwise the old
owner would keep receiving the new owner's picks. Every registration bumps
last_seen_at so stale tokens can be aged out later.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.device import DeviceToken

router = APIRouter()


class DeviceRegisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    platform: str = "ios"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def apply_device_upsert(existing, *, user_id, token: str, platform: str,
                        now: datetime) -> dict:
    """Pure upsert decision — no I/O so tests cover it without a DB.

    Given the existing row for this token (or None), return
    {"action": "insert"|"update", "values": {...}}.
    """
    platform = (platform or "ios").strip() or "ios"
    if existing is None:
        return {"action": "insert", "values": {
            "user_id": user_id, "token": token, "platform": platform,
            "created_at": now, "last_seen_at": now,
        }}
    values = {"last_seen_at": now}
    if platform != existing.platform:
        values["platform"] = platform
    if existing.user_id != user_id:
        # Device changed hands — reassign to the current account.
        values["user_id"] = user_id
    return {"action": "update", "values": values}


@router.post("")
async def register_device(
    body: DeviceRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(DeviceToken).where(DeviceToken.token == body.token)
    )).scalar_one_or_none()
    plan = apply_device_upsert(row, user_id=current_user.id, token=body.token,
                               platform=body.platform, now=_utcnow())
    if plan["action"] == "insert":
        row = DeviceToken(**plan["values"])
        db.add(row)
    else:
        for k, v in plan["values"].items():
            setattr(row, k, v)
    await db.commit()
    return {"ok": True, "id": str(row.id), "token": row.token,
            "platform": row.platform}


@router.delete("/{token}")
async def unregister_device(
    token: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(DeviceToken).where(DeviceToken.token == token)
    )).scalar_one_or_none()
    # 404 for both missing and not-owned: don't let other users probe which
    # tokens exist.
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Device token not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}
