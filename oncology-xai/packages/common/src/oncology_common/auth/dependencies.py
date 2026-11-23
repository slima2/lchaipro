"""FastAPI auth dependencies — JWT + RBAC."""

import logging
import os
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from oncology_common.auth.jwt import JWTValidator, TokenPayload

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

# Dev-mode auth bypass: only when explicitly requested with AUTH_DEV_BYPASS=true AND
# ENVIRONMENT=development. ENVIRONMENT=development alone no longer disables authentication.
_DEV_MODE = (
    os.getenv("ENVIRONMENT", "").lower() == "development"
    and os.getenv("AUTH_DEV_BYPASS", "").lower() == "true"
)

# Endpoints loaded by the browser through <img src>/fetch without an Authorization header:
# they may carry the access token in the "access_token" query parameter instead.
_QUERY_TOKEN_PATHS = ("/artifacts/presigned",)

# F6.8 (pentest 2026-11-11) — ``access_token`` en query string queda
# **opt-in explícito por entorno**. En dev el flujo del viewer sigue
# funcionando sin cambios, pero en pre-prod y prod el flag arranca en
# ``false`` y el endpoint devuelve 401 cuando alguien manda el JWT por
# la URL (Helm fuerza ``AUTH_QUERY_TOKEN_ENABLED=false`` en values de
# staging/prod).
#
# El fix definitivo que recomienda el pentest es migrar el frontend a
# ``GET /api/v1/artifacts/presigned-url?key=...`` (ver el nuevo
# endpoint): el gateway devuelve una presigned URL S3 ya firmada,
# **sin JWT en la query**, y el ``<img>`` carga directo desde S3.
_QUERY_TOKEN_ENABLED = os.getenv("AUTH_QUERY_TOKEN_ENABLED", "true").lower() == "true"

_DEV_USER = TokenPayload(
    sub="dev-user",
    email="dev@lchai.local",
    name="Development User",
    realm_roles=["clinician", "admin", "auditor"],
    exp=9999999999,
    iat=0,
)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> TokenPayload:
    """Extract and validate JWT, return token payload."""
    # Development bypass — no JWT required
    if _DEV_MODE:
        request.state.user = _DEV_USER
        return _DEV_USER

    if (
        not credentials
        and request.method == "GET"
        and request.url.path.endswith(_QUERY_TOKEN_PATHS)
    ):
        query_token = request.query_params.get("access_token")
        if query_token:
            if not _QUERY_TOKEN_ENABLED:
                # F6.8 — deshabilitado explícitamente para este entorno.
                # No usamos el token; forzamos el 401 de abajo para que
                # el cliente migre a /artifacts/presigned-url.
                logger.warning(
                    "F6.8: rejecting access_token in query on %s "
                    "(AUTH_QUERY_TOKEN_ENABLED=false)",
                    request.url.path,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=(
                        "JWT in query string is disabled in this environment. "
                        "Use GET /artifacts/presigned-url to obtain an S3 "
                        "signed URL and load it directly."
                    ),
                    headers={
                        "WWW-Authenticate": "Bearer",
                        "Deprecation": "true",
                        "Link": (
                            '</api/v1/artifacts/presigned-url>; rel="successor-version"'
                        ),
                    },
                )
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=query_token
            )

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jwt_validator: JWTValidator | None = getattr(
        request.app.state, "jwt_validator", None
    )
    if not jwt_validator:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT validator not configured",
        )

    try:
        payload = await jwt_validator.validate_token(credentials.credentials)
        if payload.is_expired:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.user = payload
        return payload
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def require_roles(*required_roles: str) -> Callable:
    """Dependency factory for RBAC.

    Usage::

        @router.get("/admin", dependencies=[Depends(require_roles("admin"))])
        async def admin_only(): ...
    """

    async def _checker(
        current_user: Annotated[TokenPayload, Depends(get_current_user)],
    ) -> TokenPayload:
        # In dev mode, skip role check
        if _DEV_MODE:
            return _DEV_USER
        if not set(required_roles).intersection(current_user.roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required roles: {', '.join(required_roles)}",
            )
        return current_user

    return _checker
