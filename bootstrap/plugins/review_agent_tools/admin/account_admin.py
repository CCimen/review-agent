"""Account administration and password changes in one account transaction."""

import uuid

from fastapi import HTTPException
from fastapi_users import exceptions, schemas
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..domain.access import Role
from ..postgres.audit import AuditAction
from .accounts import (
    Account,
    AccountPage,
    AccountUpdate,
    NewAccount,
    PasswordChange,
    User,
    UserCreate,
    UserManager,
    UserUpdate,
    account_audit,
    lock_accounts,
    revoke_account_sessions,
)


async def list_accounts(
    session: AsyncSession, *, offset: int, limit: int
) -> AccountPage:
    totals = (
        select(
            func.count().label("total"),
            func.count().filter(User.__table__.c.is_superuser).label("admins"),
            func.count().filter(~User.__table__.c.is_active).label("disabled"),
        )
        .select_from(User)
        .cte("totals")
    )
    page = (
        select(User)
        .order_by(func.lower(User.email), User.__table__.c.id)
        .offset(offset)
        .limit(limit + 1)
        .subquery()
    )
    account = aliased(User, page)
    result = await session.execute(
        select(account, totals.c.total, totals.c.admins, totals.c.disabled)
        .select_from(totals.outerjoin(page, true()))
        .order_by(func.lower(account.email), page.c.id)
    )
    rows = result.all()
    items = [Account.from_user(row[0]) for row in rows if row[0] is not None]
    return AccountPage(
        items=items[:limit],
        total=rows[0][1],
        admin_count=rows[0][2],
        disabled_count=rows[0][3],
        has_more=len(items) > limit,
    )


async def create_account(
    session: AsyncSession,
    actor: User,
    account: NewAccount,
    *,
    manager: UserManager,
) -> Account:
    await lock_accounts(session)
    await session.refresh(actor)
    if not actor.is_active or not actor.is_superuser:
        raise HTTPException(403, "Administrator access required")
    if account.role in (Role.ADMIN, Role.OWNER) and not actor.is_platform_owner:
        raise HTTPException(403, "Only owners can manage privileged accounts")
    try:
        user = await manager.create(
            UserCreate(
                email=account.email,
                password=account.password.get_secret_value(),
                is_superuser=account.role in (Role.ADMIN, Role.OWNER),
                is_platform_owner=account.role is Role.OWNER,
                is_global_viewer=account.role is Role.VIEWER,
            )
        )
    except exceptions.UserAlreadyExists as exc:
        raise HTTPException(
            409, "An account with this email already exists"
        ) from exc
    await account_audit(
        session,
        actor=actor,
        user=user,
        action=AuditAction.ACCOUNT_CREATED,
        reason=account.reason,
        details={"email": user.email, "role": account.role.value},
    )
    await session.commit()
    return Account.from_user(user)


async def update_account(
    session: AsyncSession,
    actor: User,
    user_id: uuid.UUID,
    change: AccountUpdate,
    *,
    manager: UserManager,
) -> Account:
    await lock_accounts(session)
    await session.refresh(actor)
    if not actor.is_active or not actor.is_superuser:
        raise HTTPException(403, "Administrator access required")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Account not found")
    if not actor.is_platform_owner and (
        user.is_superuser or change.role in (Role.ADMIN, Role.OWNER)
    ):
        raise HTTPException(403, "Only owners can manage privileged accounts")
    if (
        user.is_active
        and user.is_platform_owner
        and (
            change.active is False
            or (change.role is not None and change.role is not Role.OWNER)
        )
    ):
        count = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.__table__.c.is_active, User.__table__.c.is_platform_owner
            )
        )
        if count is None or count <= 1:
            raise HTTPException(409, "Keep at least one active owner")
    previous = Account.from_user(user)
    actor_snapshot = Account.from_user(actor)
    values = UserUpdate()
    if change.role is not None:
        values.is_superuser = change.role in (Role.ADMIN, Role.OWNER)
        values.is_platform_owner = change.role is Role.OWNER
        values.is_global_viewer = change.role is Role.VIEWER
    if change.active is not None:
        values.is_active = change.active
    if change.password is not None:
        values.password = change.password.get_secret_value()
    await revoke_account_sessions(session, user)
    updated = await manager.update(values, user)
    await account_audit(
        session,
        actor=actor,
        user=updated,
        action=AuditAction.ACCOUNT_UPDATED,
        reason=change.reason,
        was_privileged=previous.role in (Role.ADMIN, Role.OWNER),
        actor_role=actor_snapshot.role,
        details={
            "previous_role": previous.role.value,
            "role": Account.from_user(updated).role.value,
            "previous_active": previous.active,
            "active": updated.is_active,
            "password_reset": change.password is not None,
        },
    )
    await session.commit()
    return Account.from_user(updated)


async def change_password(
    session: AsyncSession,
    user: User,
    change: PasswordChange,
    *,
    manager: UserManager,
) -> None:
    await lock_accounts(session)
    await session.refresh(user)
    if not user.is_active:
        raise HTTPException(401, "Sign in to continue")
    verified, _ = manager.password_helper.verify_and_update(
        change.current_password.get_secret_value(), user.hashed_password
    )
    if not verified:
        raise HTTPException(400, "Current password is incorrect")
    await revoke_account_sessions(session, user)
    await manager.update(
        schemas.BaseUserUpdate(password=change.password.get_secret_value()),
        user,
        safe=True,
    )
    await account_audit(
        session,
        actor=user,
        user=user,
        action=AuditAction.PASSWORD_CHANGED,
        reason="Password changed by account holder",
        details={},
    )
    await session.commit()
