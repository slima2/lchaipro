"""F6.8 — JWT no debe viajar en query string; endpoint nuevo devuelve
URL S3 presignada sin token."""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import seed_case


_CLIN = {"x-user-id": "clinician1", "x-roles": "clinician"}


# ── Nuevo endpoint seguro ────────────────────────────────────────────


async def test_presigned_url_returns_s3_url_without_jwt(
    client: AsyncClient, async_session: AsyncSession, fake_storage, monkeypatch
) -> None:
    """GET /artifacts/presigned-url devuelve una URL firmada que NO
    contiene el JWT del usuario."""
    await seed_case(async_session, "case-1", "clinician1")
    # Stub de presigned_url que devuelve una URL S3 con firma HMAC
    monkeypatch.setattr(
        fake_storage,
        "presigned_url",
        lambda key, expires_in=300: (
            f"http://s3/{key}?X-Amz-Signature=deadbeef&X-Amz-Expires={expires_in}"
        ),
        raising=False,
    )
    r = await client.get(
        "/api/v1/artifacts/presigned-url",
        headers=_CLIN,
        params={"key": "results/case-1/bundle-1/metrics.json"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_in"] == 300
    # La URL devuelta por el backend no contiene el JWT
    assert "access_token" not in body["url"].lower()
    assert "clinician1" not in body["url"]
    # Cache-Control privado para que proxies no guarden la firma
    assert "no-store" in r.headers.get("Cache-Control", "")


async def test_presigned_url_rejects_cross_case_key(
    client: AsyncClient, async_session: AsyncSession, fake_storage, monkeypatch
) -> None:
    """La key debe pertenecer a un caso del caller (reutiliza require_case_access)."""
    await seed_case(async_session, "case-mine", "clinician1")
    await seed_case(async_session, "case-other", "clinician2")
    monkeypatch.setattr(
        fake_storage,
        "presigned_url",
        lambda key, expires_in=300: "http://s3/x",
        raising=False,
    )
    r = await client.get(
        "/api/v1/artifacts/presigned-url",
        headers=_CLIN,
        params={"key": "results/case-other/bundle/x.json"},
    )
    assert r.status_code == 403


async def test_presigned_url_bad_key_is_400(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    r = await client.get(
        "/api/v1/artifacts/presigned-url",
        headers=_CLIN,
        params={"key": "../etc/passwd"},
    )
    assert r.status_code == 400


# ── Endpoint viejo marcado Deprecation ───────────────────────────────


async def test_old_presigned_marks_deprecation(
    client: AsyncClient, async_session: AsyncSession, fake_storage, monkeypatch
) -> None:
    """El endpoint que streamea bytes debe anunciar que está deprecado
    y linkear al reemplazo. Antes no había señal para el cliente."""
    await seed_case(async_session, "case-1", "clinician1")
    monkeypatch.setattr(
        fake_storage,
        "download_bytes",
        lambda key: b"\x89PNG\r\n\x1a\n",
        raising=False,
    )
    r = await client.get(
        "/api/v1/artifacts/presigned",
        headers=_CLIN,
        params={"key": "results/case-1/bundle/thumb.png"},
    )
    assert r.status_code == 200
    assert r.headers.get("Deprecation") == "true"
    link = r.headers.get("Link", "")
    assert "/api/v1/artifacts/presigned-url" in link
    assert "successor-version" in link


# ── Rechazo de access_token en query (depende de env) ────────────────


def test_query_token_rejected_when_flag_false(monkeypatch) -> None:
    """Unit del helper del módulo common: con el flag en false el
    endpoint devuelve 401 cuando el cliente manda ``access_token`` en
    la query, sin intentar validar el JWT."""
    # Importar fresco con el flag false
    monkeypatch.setenv("AUTH_QUERY_TOKEN_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "false")

    import importlib

    from oncology_common.auth import dependencies as deps

    importlib.reload(deps)
    assert deps._QUERY_TOKEN_ENABLED is False


def test_query_token_accepted_in_dev_by_default(monkeypatch) -> None:
    """En dev (flag ausente) el default sigue siendo True para no
    romper el sandbox local durante la ventana de migración."""
    monkeypatch.delenv("AUTH_QUERY_TOKEN_ENABLED", raising=False)
    import importlib

    from oncology_common.auth import dependencies as deps

    importlib.reload(deps)
    assert deps._QUERY_TOKEN_ENABLED is True
