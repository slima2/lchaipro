"""Reverse proxy to downstream services with RBAC."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request, Response
from oncology_common.auth.dependencies import require_roles
from oncology_common.auth.jwt import TokenPayload

_DEV_MODE = (
    os.getenv("ENVIRONMENT", "").lower() == "development"
    and os.getenv("AUTH_DEV_BYPASS", "").lower() == "true"
)

logger = logging.getLogger(__name__)

_CLIENT_FORBIDDEN_HEADERS = {
    "host",
    "x-user-id",
    "x-correlation-id",
    "x-forwarded-user",
    "x-roles",
}

# Route → upstream + required roles
ROUTE_MAP: list[dict[str, Any]] = [
    # Case service (patients, cases CRUD)
    {
        "prefix": "/patients",
        "upstream": "case_service_url",
        "roles": ["clinician", "admin"],
    },
    {
        "prefix": "/cases",
        "upstream": "case_service_url",
        "roles": ["clinician", "admin"],
    },
    # Image service
    {
        "prefix": "/artifacts",
        "upstream": "image_service_url",
        "roles": ["clinician", "admin"],
    },
    {
        "prefix": "/images",
        "upstream": "image_service_url",
        "roles": ["clinician", "admin"],
    },
    # Inference service
    {
        "prefix": "/jobs",
        "upstream": "inference_service_url",
        "roles": ["clinician", "admin"],
    },
    {
        "prefix": "/results",
        "upstream": "inference_service_url",
        "roles": ["clinician", "admin"],
    },
    {
        "prefix": "/parameters",
        "upstream": "inference_service_url",
        "roles": ["clinician", "admin"],
    },
    {
        "prefix": "/checkpoints",
        "upstream": "inference_service_url",
        "roles": ["clinician", "admin"],
    },
    {
        "prefix": "/gene-explain",
        "upstream": "inference_service_url",
        "roles": ["clinician", "admin"],
    },
    # EHR service
    {"prefix": "/ehr", "upstream": "ehr_service_url", "roles": ["clinician", "admin"]},
    # Graph service
    {
        "prefix": "/graphs",
        "upstream": "graph_service_url",
        "roles": ["clinician", "admin"],
    },
    # The webapp calls /graph/gene-associations; without this prefix the gateway returned 404
    {
        "prefix": "/graph",
        "upstream": "graph_service_url",
        "roles": ["clinician", "admin"],
    },
    # Ontology Admin + DeepSearch + KG
    {
        "prefix": "/admin/ontologies",
        "upstream": "ontology_admin_service_url",
        "roles": ["admin"],
    },
    {
        "prefix": "/admin/deep-search",
        "upstream": "ontology_admin_service_url",
        "roles": ["admin"],
    },
    {
        "prefix": "/admin/kg",
        "upstream": "ontology_admin_service_url",
        "roles": ["admin"],
    },
    # Audit
    {
        "prefix": "/audit",
        "upstream": "audit_service_url",
        "roles": ["auditor", "admin"],
    },
    # Active Learning
    {
        "prefix": "/active-learning",
        "upstream": "active_learning_service_url",
        "roles": ["clinician", "admin"],
    },
]


def create_proxy_router(settings) -> APIRouter:  # type: ignore[no-untyped-def]
    router = APIRouter()

    async def _proxy(request: Request, upstream_base: str) -> Response:
        """Forward request to upstream, preserving headers."""
        path = request.url.path.replace("/api/v1", "", 1)
        # No permitir recorrer rutas dentro del servicio interno (p. ej. /ehr/../../docs)
        if ".." in path.split("/") or "%2e%2e" in request.url.path.lower():
            return Response(
                status_code=400,
                content=b'{"detail":"Invalid path"}',
                media_type="application/json",
            )
        url = f"{upstream_base}/api/v1{path}"
        # The access token may arrive as a query parameter (artifact URLs); never forward it upstream.
        query = urlencode(
            [
                (k, v)
                for k, v in request.query_params.multi_items()
                if k != "access_token"
            ]
        )
        if query:
            url += f"?{query}"

        # Cabeceras de identidad y trazas las pone solo el gateway: se descartan las que envía el cliente
        # (comparación sin distinguir mayúsculas; si no, httpx reenvía las dos y el servicio lee la del cliente).
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _CLIENT_FORBIDDEN_HEADERS
            and not k.lower().startswith("x-user")
        }
        cid = getattr(request.state, "correlation_id", None)
        if cid:
            headers["X-Correlation-Id"] = cid

        user: TokenPayload | None = getattr(request.state, "user", None)
        if user:
            headers["X-User-Id"] = user.preferred_username or user.user_id
            # F6.2 — el servicio downstream necesita los roles para checks
            # adicionales (p. ej. admin-only para soft-delete). El cliente no
            # puede inyectarlos porque "x-roles" está en _CLIENT_FORBIDDEN_HEADERS.
            if user.roles:
                headers["X-Roles"] = ",".join(sorted(set(user.roles)))
        elif _DEV_MODE:
            headers["X-User-Id"] = "dev-user"
            headers["X-Roles"] = "clinician,admin,auditor"

        body = await request.body()
        timeout = (
            httpx.Timeout(600.0, connect=30.0)
            if len(body) > 10_000_000
            else httpx.Timeout(120.0)
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )

    def _cases_upstream(path: str) -> str:
        """Route /cases/* sub-paths to the correct service."""
        if "/images" in path:
            return settings.image_service_url
        if "/ehr" in path:
            return settings.ehr_service_url
        if "/graph" in path:
            return settings.graph_service_url
        return settings.case_service_url

    def _images_upstream(path: str) -> str:
        """Route /images/* sub-paths: :process and results/latest → inference-service."""
        if ":process" in path or "/results/" in path:
            return settings.inference_service_url
        return settings.image_service_url

    # F6.2 — DELETE de pacientes/casos es solo-admin (soft-delete con motivo+auditoría
    # en case-service). Estas rutas deben declararse ANTES de los catch-all para que
    # FastAPI resuelva al handler específico y no al genérico clinician+admin.
    @router.delete(
        "/patients/{patient_id}",
        dependencies=[Depends(require_roles("admin"))],
        include_in_schema=False,
    )
    async def _delete_patient_admin(request: Request, patient_id: str):
        return await _proxy(request, settings.case_service_url)

    @router.delete(
        "/cases/{case_id}",
        dependencies=[Depends(require_roles("admin"))],
        include_in_schema=False,
    )
    async def _delete_case_admin(request: Request, case_id: str):
        return await _proxy(request, settings.case_service_url)

    # Register /cases with dynamic upstream (must be before generic loop for /cases)
    @router.api_route(
        "/cases/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        dependencies=[Depends(require_roles("clinician", "admin"))],
        include_in_schema=False,
    )
    async def _cases_handler(request: Request, path: str):
        upstream = _cases_upstream(f"/{path}")
        return await _proxy(request, upstream)

    @router.api_route(
        "/cases",
        methods=["GET", "POST"],
        dependencies=[Depends(require_roles("clinician", "admin"))],
        include_in_schema=False,
    )
    async def _cases_root_handler(request: Request):
        return await _proxy(request, settings.case_service_url)

    # Register /images with dynamic upstream (:process, results/latest → inference-service)
    @router.api_route(
        "/images/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        dependencies=[Depends(require_roles("clinician", "admin"))],
        include_in_schema=False,
    )
    async def _images_handler(request: Request, path: str):
        upstream = _images_upstream(f"/{path}")
        return await _proxy(request, upstream)

    @router.api_route(
        "/images",
        methods=["GET", "POST"],
        dependencies=[Depends(require_roles("clinician", "admin"))],
        include_in_schema=False,
    )
    async def _images_root_handler(request: Request):
        return await _proxy(request, settings.image_service_url)

    # Register catch-all routes per prefix (skip /cases and /images - already handled above)
    for route_cfg in ROUTE_MAP:
        prefix = route_cfg["prefix"]
        if prefix in ("/cases", "/images"):
            continue
        upstream_attr = route_cfg["upstream"]
        roles = route_cfg["roles"]

        upstream_url = getattr(settings, upstream_attr)

        @router.api_route(
            f"{prefix}/{{path:path}}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            dependencies=[Depends(require_roles(*roles))],
            include_in_schema=False,
        )
        async def _handler(request: Request, path: str, _url: str = upstream_url):
            return await _proxy(request, _url)

        @router.api_route(
            f"{prefix}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            dependencies=[Depends(require_roles(*roles))],
            include_in_schema=False,
        )
        async def _handler_root(request: Request, _url: str = upstream_url):
            return await _proxy(request, _url)

    return router
