"""Authentication endpoints — API §2.2 and §2.3.

🔒 These are the only routes that are reachable *before* an actor exists, which
is why they sit under `/public` and are named in ``EXEMPT_PATHS``. The exemption
is from authorization, not from the pipeline: every one of them still runs
through the request context and the error envelope.

⚠️ Logout is the exception — it lives under `/app` and is authorized like any
other practitioner action, because revoking a session requires knowing whose
session it is.

**Mobile-first** (§4): the refresh token is returned in the response body rather
than set as a cookie. A native client has no cookie jar, and a `Secure; HttpOnly`
cookie is unusable from one. The web client keeps the access token in memory
(ADR-A02) and the refresh token in whatever its platform provides.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import Request, Response, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, EmailStr, Field

from app.kernel.authz import DataScope, register_action
from app.kernel.context import UserRole, get_context
from app.kernel.errors import AuthenticationError
from app.kernel.models import LinkPurpose, TransportType
from app.modules import messaging
from app.platform.http.authz import requires
from app.platform.http.pipeline import (
    get_session,
    public_router,
    realm_router,
    record_audit,
)
from app.platform.identity import service
from app.platform.identity.credentials import MIN_PASSWORD_LENGTH, get_credential_store
from app.platform.identity.tokens import IssuedTokens, utcnow
from app.platform.logging import get_logger

logger = get_logger(__name__)

#: 🔒 On ``PublicRoute``, not a bare ``APIRouter``. Every endpoint below calls
#: ``get_session()``, and only a route class that opens a transaction can
#: supply one — on a plain router all six answered 500 (see `PublicRoute`).
#: Exemption from *authorization* still comes from ``EXEMPT_PATHS``; the route
#: class decides only that the request gets a database.
auth_public = public_router("/api/v1/public/auth", tags=["auth"])

#: 🔒 Logout is authorized. Declared here rather than in a module because the
#: kernel owns sessions — this is the one authentication action that acts on an
#: existing identity rather than establishing one.
SESSION_END = register_action(
    "session.end",
    roles={UserRole.OWNER, UserRole.PRACTITIONER, UserRole.CLIENT},
    data_scope=DataScope.PLATFORM,
    audit_metadata_keys={"realm"},
)

#: 🔒 "Who am I", answered from the verified token.
#:
#: ``TENANT_METADATA``, not ``TENANT_PII``: the response carries the caller's own
#: identifiers and role and no name, email or phone. That is what keeps it out of
#: operator reach by classification rather than by a rule someone must remember.
#:
#: ⚠️ ``operator_access`` is **not** declared. An operator asking "who am I" has
#: no tenant, so the answer would be meaningless — and declaring it would put a
#: tenant-shaped response on a cross-tenant token.
SESSION_READ = register_action(
    "session.read",
    roles={UserRole.OWNER, UserRole.PRACTITIONER, UserRole.CLIENT},
    data_scope=DataScope.TENANT_METADATA,
    is_read=True,
)

app_router = realm_router("/api/v1/app/auth", tags=["auth"])


# ─── Schemas ─────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    """API §2.2. 🔒 Password rules are length plus a denylist, not composition."""

    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    full_name: str = Field(min_length=1, max_length=120)
    practice_name: str = Field(min_length=1, max_length=120)
    # NFR-100 — E.164, matching the `users.mobile` check constraint.
    mobile: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{1,14}$")
    accepted_terms_version: str = Field(min_length=1, max_length=32)


class RegisterResponse(BaseModel):
    """🔒 Carries no tokens and no identifiers.

    Verification precedes access (FR-M0-002), and the body is byte-identical
    whether or not the address was already registered (NFR-043) — returning a
    `tenant_id` on the success path alone would restore the oracle the flat
    response exists to remove.
    """

    email_verification_required: Literal[True] = True


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """What a client needs to make an authenticated request and to renew.

    🔒 No user object. The client fetches its own profile from a dedicated
    endpoint; embedding one here would put a name and email in every token
    refresh and in whatever logs the response passes through.
    """

    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class PasswordResetRequest(BaseModel):
    email: EmailStr = Field(max_length=254)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class AcceptedResponse(BaseModel):
    """🔒 The deliberately uninformative acknowledgement.

    Returned by password reset whether or not the address is registered. The
    message is phrased conditionally — "if an account exists" — so it is honest
    in both cases rather than implying an account was found.
    """

    message: str = "If an account exists for that address, a reset link is on its way."


class PortalAccessRequest(BaseModel):
    mobile_or_email: str = Field(min_length=3, max_length=254)


class PortalRedeemRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)


class PortalTarget(BaseModel):
    """🔒 FR-M7-013 — redemption and navigation in one hop."""

    type: str
    ref: str | None = None


class PortalSessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    target: PortalTarget


# ─── Practitioner endpoints ──────────────────────────────────────────────


@auth_public.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Register a practice",
    operation_id="authRegister",
)
async def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
    """Create a tenant and its owner (FR-M0-001).

    🔒 201 with the same body whether the address was free or already taken. The
    difference is which email the address receives — confirmation, or a "someone
    tried to register with your address" notice (API §2.2).
    """
    result = await service.register_practitioner(
        get_session(request),
        get_credential_store(),
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        practice_name=payload.practice_name,
        mobile=payload.mobile,
        now=utcnow(),
    )

    if result.duplicate_email:
        # ⚠️ S5 sends the "someone tried to register" notice here. Logged for now
        # so the path is visible in operations rather than silently identical.
        logger.info("Registration attempted for an existing address")
    else:
        # ⚠️ S5 sends the verification email. Until then the token is reachable
        # only from this log line, at debug, in local environments.
        logger.debug(
            "Verification token issued",
            extra={"tenant_id": str(result.tenant_id)},
        )

    return RegisterResponse()


@auth_public.post(
    "/verify-email",
    response_model=TokenResponse,
    summary="Confirm an email address",
    operation_id="authVerifyEmail",
)
async def verify_email(payload: VerifyEmailRequest, request: Request) -> TokenResponse:
    """Redeem a verification token and sign the user in (FR-M0-002)."""
    tokens = await service.verify_email(
        get_session(request),
        token=payload.token,
        user_agent=request.headers.get("User-Agent"),
        now=utcnow(),
    )
    return _token_response(tokens)


@auth_public.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in",
    operation_id="authLogin",
)
async def login(payload: LoginRequest, request: Request) -> TokenResponse:
    """🔒 One error for every failure mode (NFR-043)."""
    tokens = await service.sign_in(
        get_session(request),
        get_credential_store(),
        email=payload.email,
        password=payload.password,
        user_agent=request.headers.get("User-Agent"),
        now=utcnow(),
    )
    return _token_response(tokens)


@auth_public.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the session",
    operation_id="authRefresh",
)
async def refresh(payload: RefreshRequest, request: Request) -> TokenResponse:
    """🔒 DDR-05 — rotate, and revoke the family on reuse.

    Public because a caller with an expired access token must still be able to
    renew; the refresh token is the credential being presented.
    """
    tokens = await service.refresh_session(
        get_session(request),
        refresh_token=payload.refresh_token,
        user_agent=request.headers.get("User-Agent"),
        now=utcnow(),
    )
    return _token_response(tokens)


class CurrentSessionResponse(BaseModel):
    """Who the caller is — identifiers and role, nothing else.

    🔒 **No name, email or phone** (NFR-033). ``TokenResponse`` says the client
    "fetches its own profile from a dedicated endpoint"; this is that endpoint,
    and it deliberately answers the *authorization* question rather than the
    display one. The UI needs ``user_id`` to know which notes it may edit
    (FR-M3-020) and ``role`` to know whether to offer access management
    (FR-M0-017) — neither needs a name.
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: UserRole


@app_router.get(
    "/me",
    summary="The current session",
    operation_id="authCurrentSession",
)
@requires(SESSION_READ)
async def current_session(request: Request) -> CurrentSessionResponse:
    """Identify the caller from their verified token.

    🔒 Read from the token, never from a parameter. An endpoint that accepted a
    user id would let any authenticated caller ask about anyone.

    ⚠️ Built in S2 Slice C rather than S1 because it had no consumer until the
    notes UI needed to know who the author is — FR-M3-020's "editable by their
    author" cannot be expressed in an interface that does not know who is
    looking.
    """
    actor = get_context().actor
    if actor.role is None:  # pragma: no cover - authz refuses a roleless actor first
        raise AuthenticationError(
            message="You are not signed in.",
            action="Sign in and try again.",
        )
    return CurrentSessionResponse(
        user_id=actor.require_subject(),
        tenant_id=actor.require_tenant(),
        role=actor.role,
    )


@app_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    operation_id="authLogout",
)
@requires(SESSION_END)
async def logout(request: Request) -> Response:
    """Revoke the current session (NFR-042).

    🔒 Authorized, unlike the other authentication routes: this acts on an
    existing session, so it needs to know whose. The session id comes from the
    verified token, never from the request body — accepting one would let any
    authenticated caller log out any other.
    """
    actor = get_context().actor
    if actor.session_id is None:  # pragma: no cover - authz refuses anonymous first
        raise AuthenticationError(
            message="You are not signed in.",
            action="Sign in and try again.",
        )

    await service.sign_out(get_session(request), session_id=actor.session_id, now=utcnow())
    record_audit(request, metadata={"realm": actor.realm.value if actor.realm else "unknown"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_public.post(
    "/password-reset/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Request a password reset",
    operation_id="authPasswordResetRequest",
)
async def request_password_reset(
    payload: PasswordResetRequest, request: Request
) -> AcceptedResponse:
    """🔒 202 with an identical body whether or not the address is known."""
    token = await service.request_password_reset(
        get_session(request), email=payload.email, now=utcnow()
    )
    if token is not None:
        # ⚠️ S5 delivers this by email — the only channel that proves the
        # requester owns the address.
        logger.debug("Password reset token issued")
    return AcceptedResponse()


@auth_public.post(
    "/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password",
    operation_id="authPasswordResetConfirm",
)
async def confirm_password_reset(payload: PasswordResetConfirm, request: Request) -> Response:
    """🔒 Revokes every session for the account (NFR-042)."""
    await service.confirm_password_reset(
        get_session(request),
        get_credential_store(),
        token=payload.token,
        new_password=payload.password,
        now=utcnow(),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Client portal ───────────────────────────────────────────────────────

portal_router = public_router("/api/v1/public/portal/access", tags=["portal-auth"])


@portal_router.post(
    "/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Request a portal link",
    operation_id="portalAccessRequest",
)
async def request_portal_access(payload: PortalAccessRequest, request: Request) -> AcceptedResponse:
    """🔒 Self-service link re-request (EC-M7-01).

    ⚠️ **On the critical path, not an error path.** With a 15–30 minute expiry, a
    client opening a WhatsApp message an hour later *will* need a new link, and
    this must never require the practitioner.

    🔒 Always 202 with an identical body. Anything else makes this a
    client-enumeration oracle against a practitioner's client list.
    """
    session = get_session(request)
    now_ts = utcnow()

    # 1. Anonymous client lookup (via SECURITY DEFINER)
    from app.platform.identity.repository import find_client_by_contact

    client = await find_client_by_contact(session, payload.mobile_or_email)

    if client is not None and client.can_sign_in:
        # 2. Issue the magic link token
        token = await service.issue_magic_link(
            session,
            tenant_id=client.tenant_id,
            client_id=client.client_id,
            purpose=LinkPurpose.PORTAL_LOGIN,
            target_ref=None,
            transport=TransportType.WHATSAPP,
            now=now_ts,
        )

        # 3. Schedule the dispatch via messaging engine
        import uuid

        from app.platform.config import get_settings

        occasion = f"portal_login:{uuid.uuid4()}"  # Distinct occasion for each request
        base_url = get_settings().app_base_url.rstrip("/")
        link_url = f"{base_url}/portal/access/{token}"

        await messaging.schedule(
            session,
            tenant_id=client.tenant_id,
            request=messaging.MessageRequest(
                template_code="magic_link",
                occasion=occasion,
                scheduled_for=now_ts,
                source_module="identity",
                client_id=client.client_id,
                variables={
                    "link_url": link_url,
                    "expires_in_minutes": str(get_settings().magic_link_ttl_minutes),
                },
            ),
        )

        logger.info("Portal access requested and scheduled")

    return AcceptedResponse(message="If that matches an account, a new link is on its way.")


@portal_router.post(
    "/redeem",
    response_model=PortalSessionResponse,
    summary="Redeem a portal link",
    operation_id="portalAccessRedeem",
)
async def redeem_portal_access(
    payload: PortalRedeemRequest, request: Request
) -> PortalSessionResponse:
    """Exchange a magic link for a client session (FR-M0-005).

    🔒 Single use and expiry are enforced by the redeeming UPDATE (DDR-04), so
    two taps on the same link cannot both open a session.
    """
    portal = await service.redeem_magic_link(
        get_session(request),
        token=payload.token,
        user_agent=request.headers.get("User-Agent"),
        now=utcnow(),
    )
    return PortalSessionResponse(
        access_token=portal.tokens.access_token,
        refresh_token=portal.tokens.refresh_token,
        expires_in=portal.tokens.expires_in_seconds,
        target=PortalTarget(type=portal.target_type, ref=portal.target_ref),
    )


def _token_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in_seconds,
    )


#: Every public authentication path, for the startup exemption list. Derived from
#: the routers rather than retyped, so a new endpoint cannot be added without its
#: exemption — or, if it should not be exempt, without someone noticing here.
PUBLIC_AUTH_PATHS: frozenset[str] = frozenset(
    route.path
    for router in (auth_public, portal_router)
    for route in router.routes
    if isinstance(route, APIRoute)
)
