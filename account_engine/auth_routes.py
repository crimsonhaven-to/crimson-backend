"""Sign-in and registration under /auth. Both identity types return a session
token for ``Authorization: Bearer``; the flows live in ``auth.py``."""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from core.rate_limit import limiter

from . import auth
from .db import store
from .deps import bearer_token
from .schemas import (
    AuthResponse,
    ChallengeRequest,
    ChallengeResponse,
    EmailLoginRequest,
    EmailOnlyRequest,
    EmailRegisterRequest,
    EmailTokenRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
)

router = APIRouter(tags=["account"])


@router.post("/auth/challenge", response_model=ChallengeResponse)
@limiter.limit("20/minute")
def auth_challenge(request: Request, body: ChallengeRequest):
    """A one-time challenge for the client to sign before /auth/register or
    /auth/login."""
    pk = auth.normalize_public_key(body.public_key)
    challenge, expires_at = store.create_challenge(pk, auth.CHALLENGE_PURPOSE)
    return ChallengeResponse(public_key=pk, challenge=challenge, expires_at=expires_at)


@router.post("/auth/register", response_model=AuthResponse)
@limiter.limit("10/minute")
def auth_register(request: Request, body: RegisterRequest):
    """Create the account for a public key. 409 if it exists, 403 on a bad invite."""
    pk = auth.normalize_public_key(body.public_key)
    return AuthResponse(**auth.register_mnemonic(
        pk, body.challenge, body.signature, body.invite_code, body.label, request
    ))


@router.post("/auth/login", response_model=AuthResponse)
@limiter.limit("10/minute")
def auth_login(request: Request, body: LoginRequest):
    """Sign in by signing the challenge. 404 if the key is not registered."""
    pk = auth.normalize_public_key(body.public_key)
    return AuthResponse(**auth.login_mnemonic(pk, body.challenge, body.signature, request))


@router.post("/auth/logout")
def auth_logout(token: Optional[str] = Depends(bearer_token)):
    if token:
        store.delete_session(token)
    return {"success": True}


@router.post("/auth/email/register")
@limiter.limit("5/minute")
async def email_register(request: Request, body: EmailRegisterRequest):
    """403 on a bad invite code, 409 if the email is taken."""
    return await run_in_threadpool(auth.register_email, body, request)


@router.post("/auth/email/login")
@limiter.limit("10/minute")
async def email_login(request: Request, body: EmailLoginRequest):
    return await run_in_threadpool(auth.login_email, body.email, body.password, request)


@router.post("/auth/email/verify")
@limiter.limit("20/minute")
async def email_verify(request: Request, body: EmailTokenRequest):
    return await run_in_threadpool(auth.verify_email, body.token, request)


@router.post("/auth/email/resend")
@limiter.limit("5/minute")
async def email_resend(request: Request, body: EmailOnlyRequest):
    """Always reports success, so it is no account-existence oracle."""
    await run_in_threadpool(auth.resend_verification, body.email, request)
    return {"success": True, "message": "If that account exists and is unverified, a new link is on its way."}


@router.post("/auth/email/forgot")
@limiter.limit("5/minute")
async def email_forgot(request: Request, body: EmailOnlyRequest):
    """Always reports success, so it is no account-existence oracle."""
    await run_in_threadpool(auth.request_password_reset, body.email, request)
    return {"success": True, "message": "If that account exists, a reset link is on its way."}


@router.post("/auth/email/reset")
@limiter.limit("5/minute")
async def email_reset(request: Request, body: ResetPasswordRequest):
    await run_in_threadpool(auth.reset_password, body.token, body.password, request)
    return {"success": True, "message": "Password updated. You can now sign in."}
