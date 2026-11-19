"""F6.1 — Tests de regresión del escenario del pentest §6.1.

Cadena que se bloquea:

* clinician B envía ``POST /images/<imagen-inexistente>:process`` con el
  ``case_id`` de clinician A → antes 202 (creaba job huérfano), ahora
  404 "Image not found".
* clinician B cancela su propio job → antes ejecutaba cascada
  destructiva sobre el caso de A (``DELETE FROM xai_artifacts, ...,
  cases``), ahora solo cambia status a ``CANCELLED`` y no toca nada más.
* clinician B intenta cancelar el job de clinician A → 403.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MLJobDB
from tests.conftest import seed_image


_A = {"x-user-id": "clinicianA", "x-roles": "clinician"}
_B = {"x-user-id": "clinicianB", "x-roles": "clinician"}
_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}


# ── :process — resolución server-side de case_id ───────────────────────


async def test_process_image_rejects_nonexistent_image(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """Pentest §6.1 Paso 2 (payload exacto)."""
    r = await client.post(
        "/api/v1/images/00000000-0000-0000-0000-0000000000a1:process",
        headers=_B,
        json={"case_id": "bb0908c3-6d7c-424c-a539-c7af7b423d41"},
    )
    assert r.status_code == 404
    assert "image" in r.json()["detail"].lower()


async def test_process_image_rejects_unconfirmed_upload(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(async_session, image_id=img, case_id="caseA", confirmed=False)
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    assert r.status_code == 409
    assert "confirm" in r.json()["detail"].lower()


async def test_process_image_uses_server_side_case_id(
    client: AsyncClient,
    async_session: AsyncSession,
    dispatched_calls: list,
) -> None:
    """Aunque el body traiga un ``case_id`` ajeno, el job se crea con el
    ``case_id`` REAL de la imagen (resuelto server-side)."""
    img = str(uuid4())
    # clinician A es el dueño del caso y del flujo
    await seed_image(async_session, image_id=img, case_id="caseA")
    forged = "case-of-another-clinician"
    r = await client.post(
        f"/api/v1/images/{img}:process",
        headers=_A,
        json={"case_id": forged, "thresholds": {"pattern": 0.0}},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["case_id"] == "caseA"
    assert body["case_id"] != forged

    # El job persistido también
    job = (
        await async_session.execute(
            select(MLJobDB).where(MLJobDB.id == body["job_id"])
        )
    ).scalar_one()
    assert job.case_id == "caseA"
    assert job.created_by == "clinicianA"

    # Thresholds del body se descartan: la Celery task recibe None
    assert len(dispatched_calls) == 1
    _, args, _ = dispatched_calls[0]
    assert args[3] is None  # thresholds


# ── :cancel — ownership + sin cascada destructiva ──────────────────────


async def test_cancel_job_cross_clinician_is_forbidden(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """Pentest §6.1 Paso 3: B cancelaba el job de A y disparaba la cascada."""
    img = str(uuid4())
    await seed_image(async_session, image_id=img, case_id="caseA")
    r = await client.post(
        f"/api/v1/images/{img}:process",
        headers=_A,
        json={},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    r_cancel = await client.post(f"/api/v1/jobs/{job_id}:cancel", headers=_B)
    assert r_cancel.status_code == 403
    assert "creator" in r_cancel.json()["detail"].lower()


async def test_cancel_job_admin_can_cancel_anyone(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(async_session, image_id=img, case_id="caseA")
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    job_id = r.json()["job_id"]

    r_cancel = await client.post(f"/api/v1/jobs/{job_id}:cancel", headers=_ADMIN)
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "cancelled"


async def test_cancel_job_does_not_destroy_case_data(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """Pentest §6.1 Paso 3→4: después del cancel, el caso y sus bundles
    DEBEN seguir existiendo. Antes cancel ejecutaba DELETE en cascada."""
    img = str(uuid4())
    case_id = "case-persistent"
    await seed_image(async_session, image_id=img, case_id=case_id)

    # Simulamos bundle ya existente (para probar que NO se borra)
    await async_session.execute(
        text(
            "INSERT INTO result_bundles (id, case_id, image_id, job_id, "
            "model_profile, model_version, thresholds, pattern_composition, "
            "evidence_source, intended_use, created_at) VALUES "
            "('rb-sentinel', :cid, :img, 'job-prev', 'm', 'v', '{}', '{}', "
            "'THESIS_INTERNAL', 'research', CURRENT_TIMESTAMP)"
        ),
        {"cid": case_id, "img": img},
    )
    await async_session.commit()

    # A lanza un job nuevo y lo cancela
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    job_id = r.json()["job_id"]
    r_cancel = await client.post(f"/api/v1/jobs/{job_id}:cancel", headers=_A)
    assert r_cancel.status_code == 200
    assert r_cancel.json()["cleaned"] is False  # ya no hace cleanup

    # Bundle sigue existiendo (regresión de la cascada)
    row = await async_session.execute(
        text("SELECT id FROM result_bundles WHERE id = 'rb-sentinel'")
    )
    assert row.fetchone() is not None

    # Imagen sigue existiendo
    row = await async_session.execute(
        text("SELECT id FROM images WHERE id = :id"), {"id": img}
    )
    assert row.fetchone() is not None

    # El job propio quedó marcado como CANCELLED
    job = await async_session.get(MLJobDB, job_id)
    assert job.status == "CANCELLED"
    assert job.ended_at is not None


async def test_cancel_already_terminal_job_is_idempotent(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(async_session, image_id=img, case_id="caseA")
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    job_id = r.json()["job_id"]

    # Simulamos que terminó
    job = await async_session.get(MLJobDB, job_id)
    job.status = "COMPLETED"
    await async_session.commit()

    r_cancel = await client.post(f"/api/v1/jobs/{job_id}:cancel", headers=_A)
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "completed"
    assert r_cancel.json()["cleaned"] is False
