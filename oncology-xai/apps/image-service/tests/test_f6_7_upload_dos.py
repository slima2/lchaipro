"""F6.7 — validaciones de size_bytes, num_parts, rate-limit y abort.

Pentest 2026-11-11 §6.7: un clinician autenticado podía:

  * Enviar ``size_bytes`` de 1 TB → 10.000 URLs presignadas (5.4 MB
    JSON, event loop bloqueado).
  * Enviar ``size_bytes="abc"`` → 500 (type confusion).
  * Dejar multipart uploads huérfanos sin forma de abortarlos.

Estos tests reproducen el abuso y validan la mitigación.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import seed_case


_CLIN = {"x-user-id": "clinician1", "x-roles": "clinician"}


async def _req(
    client: AsyncClient, case_id: str, size_bytes: object, filename: str = "slide.svs"
):
    return await client.post(
        f"/api/v1/cases/{case_id}/images:request-upload",
        headers=_CLIN,
        json={"filename": filename, "size_bytes": size_bytes},
    )


# ── Validación de tipo ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["abc", None, 1.5, [1], {"x": 1}, True],
)
async def test_size_bytes_type_confusion_is_400(
    client: AsyncClient, async_session: AsyncSession, fake_storage, bad
) -> None:
    await seed_case(async_session, "case-1", "clinician1")
    r = await _req(client, "case-1", bad)
    assert r.status_code == 400, f"accepted {bad!r}: {r.text}"
    assert fake_storage.created == []


async def test_size_bytes_zero_is_400(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    await seed_case(async_session, "case-1", "clinician1")
    r = await _req(client, "case-1", 0)
    assert r.status_code == 400


# ── Rango ──────────────────────────────────────────────────────────


async def test_size_bytes_over_max_is_413(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    """El payload clave del pentest: size_bytes=1 PB → NO debe
    inicializar multipart ni generar partes."""
    await seed_case(async_session, "case-1", "clinician1")
    one_pb = 1_000_000_000_000_000
    r = await _req(client, "case-1", one_pb)
    assert r.status_code == 413
    assert fake_storage.created == []
    assert fake_storage.part_urls_generated == 0


async def test_num_parts_cap_rejects_before_create_multipart(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    """Un size_bytes que cae debajo de MAX_SLIDE_BYTES pero daría
    num_parts > MAX_NUM_PARTS debe rechazarse SIN crear el multipart."""
    # 21 GB > 200 partes * 100 MB. Está debajo de MAX_SLIDE_BYTES (50 GB).
    gb = 1024 * 1024 * 1024
    r = await _req(client, "case-1", 21 * gb)
    # Puede caer 404 (no hay caso) o 413 (caso sembrado). Sembramos:
    await seed_case(async_session, "case-1", "clinician1")
    r = await _req(client, "case-1", 21 * gb)
    assert r.status_code == 413
    assert fake_storage.created == []  # NO se inicializó multipart


# ── Camino feliz: tope exactamente OK ─────────────────────────────


async def test_multipart_under_cap_succeeds(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    await seed_case(async_session, "case-1", "clinician1")
    # 16 GiB → ceil(16*2^30 / 1e8) = 172 partes, bajo el cap de 200.
    gib = 1024 * 1024 * 1024
    r = await _req(client, "case-1", 16 * gib)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["multipart"] is True
    # Entre 150 y 200 (verificamos cota y que no se dispara el cap)
    assert 150 < len(body["part_urls"]) <= 200
    assert fake_storage.created == ["mpu-1"]


async def test_single_part_under_threshold(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    await seed_case(async_session, "case-1", "clinician1")
    gb = 1024 * 1024 * 1024
    r = await _req(client, "case-1", gb)  # 1 GB < threshold
    assert r.status_code == 200
    assert r.json()["multipart"] is False
    assert fake_storage.part_urls_generated == 0
    assert fake_storage.put_urls_generated == 1


# ── Rate-limit por (usuario, caso) ────────────────────────────────


async def test_rate_limit_triggers_429(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    """20 req/min por (user, case). El 21º debe caer 429."""
    await seed_case(async_session, "case-1", "clinician1")
    # Peticiones válidas pequeñas para no afectar otros chequeos
    for i in range(20):
        r = await _req(client, "case-1", 1024)
        assert r.status_code == 200, f"req {i}: {r.text}"
    r = await _req(client, "case-1", 1024)
    assert r.status_code == 429
    assert "too many" in r.json()["detail"].lower()


async def test_rate_limit_is_per_case(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    """El límite se aplica por (user, case); otro caso no comparte cuota."""
    await seed_case(async_session, "case-1", "clinician1")
    await seed_case(async_session, "case-2", "clinician1")
    for _ in range(20):
        await _req(client, "case-1", 1024)
    # case-1 exhausto, case-2 debe seguir OK
    r2 = await _req(client, "case-2", 1024)
    assert r2.status_code == 200


# ── Abort endpoint ────────────────────────────────────────────────


async def test_abort_upload_calls_backend_and_cleans_row(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    await seed_case(async_session, "case-1", "clinician1")
    # Iniciar un multipart real (16 GiB → ~172 partes)
    gib = 1024 * 1024 * 1024
    r = await _req(client, "case-1", 16 * gib)
    assert r.status_code == 200
    image_id = r.json()["image_id"]
    upload_id = r.json()["upload_id"]

    r2 = await client.post(
        "/api/v1/cases/case-1/images:abort-upload",
        headers=_CLIN,
        json={"image_id": image_id, "upload_id": upload_id},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["aborted"] is True
    assert body["db_row_deleted"] is True  # checksum = 'pending-upload'
    assert fake_storage.aborted == [
        (f"images/case-1/{image_id}.svs", upload_id),
    ]

    # La fila ya no existe
    rows = (
        await async_session.execute(
            text("SELECT COUNT(*) FROM images WHERE id = :i"), {"i": image_id}
        )
    ).scalar_one()
    assert rows == 0


async def test_abort_upload_requires_ids(
    client: AsyncClient, async_session: AsyncSession, fake_storage
) -> None:
    await seed_case(async_session, "case-1", "clinician1")
    r = await client.post(
        "/api/v1/cases/case-1/images:abort-upload",
        headers=_CLIN,
        json={},
    )
    assert r.status_code == 400
