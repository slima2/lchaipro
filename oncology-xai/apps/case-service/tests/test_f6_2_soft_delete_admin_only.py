"""F6.2 — Tests de regresión: DELETE de pacientes/casos es admin-only y usa soft-delete.

Pentest 2026-11-19, hallazgo 6.2: un clinician borraba pacientes/casos
destructivamente sin motivo, sin evento y sin rol admin.

Estos tests validan:

1. DELETE sin rol admin → 403 (defense in depth, aun si el gateway falla).
2. DELETE con rol admin pero sin ``reason`` → 400.
3. DELETE con admin + reason → 200 y ``soft_deleted=True``.
4. GET después de DELETE → 404 (soft-deleted no se lista ni se consulta).
5. La fila persiste en DB con ``deleted_at`` / ``deleted_by`` / ``delete_reason``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CaseDB, PatientDB


_CLINICIAN = {"x-user-id": "clinician1", "x-roles": "clinician"}
_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}
_AUDITOR = {"x-user-id": "auditor1", "x-roles": "auditor"}
_REASON = {"reason": "pentest soft-delete regression test"}


async def _seed_patient(client: AsyncClient, external_id: str = "TEST-1") -> str:
    r = await client.post(
        "/api/v1/patients",
        headers=_CLINICIAN,
        json={"external_id": external_id, "demographics": {}},
    )
    assert r.status_code == 201, r.text
    return r.json()["patient_id"]


async def _seed_case(client: AsyncClient, patient_id: str) -> str:
    r = await client.post(
        "/api/v1/cases",
        headers=_CLINICIAN,
        json={"patient_id": patient_id, "tags": []},
    )
    assert r.status_code == 201, r.text
    return r.json()["case_id"]


# ── Patients ───────────────────────────────────────────────────────────


async def test_delete_patient_clinician_is_forbidden(client: AsyncClient) -> None:
    pid = await _seed_patient(client, "F6.2-P-CLIN")
    r = await client.request(
        "DELETE", f"/api/v1/patients/{pid}", headers=_CLINICIAN, json=_REASON
    )
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()


async def test_delete_patient_auditor_is_forbidden(client: AsyncClient) -> None:
    pid = await _seed_patient(client, "F6.2-P-AUD")
    r = await client.request(
        "DELETE", f"/api/v1/patients/{pid}", headers=_AUDITOR, json=_REASON
    )
    assert r.status_code == 403


async def test_delete_patient_admin_without_reason_is_bad_request(
    client: AsyncClient,
) -> None:
    pid = await _seed_patient(client, "F6.2-P-NOREASON")
    r = await client.request(
        "DELETE", f"/api/v1/patients/{pid}", headers=_ADMIN, json={}
    )
    assert r.status_code == 400
    assert "reason" in r.json()["detail"].lower()


async def test_delete_patient_admin_with_reason_soft_deletes(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    pid = await _seed_patient(client, "F6.2-P-OK")
    cid = await _seed_case(client, pid)

    r = await client.request(
        "DELETE", f"/api/v1/patients/{pid}", headers=_ADMIN, json=_REASON
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["soft_deleted"] is True
    assert body["cascaded_cases"] == 1
    assert body["deleted_by"] == "admin1"

    # GET devuelve 404 (soft-deleted)
    r_get = await client.get(f"/api/v1/patients/{pid}", headers=_ADMIN)
    assert r_get.status_code == 404

    # Y el caso cascadeado también
    r_get_case = await client.get(f"/api/v1/cases/{cid}", headers=_ADMIN)
    assert r_get_case.status_code == 404

    # La fila SÍ sigue en DB con los metadatos de borrado
    pat = (
        await async_session.execute(select(PatientDB).where(PatientDB.id == pid))
    ).scalar_one()
    assert pat.deleted_at is not None
    assert pat.deleted_by == "admin1"
    assert pat.delete_reason == _REASON["reason"]

    case = (
        await async_session.execute(select(CaseDB).where(CaseDB.id == cid))
    ).scalar_one()
    assert case.deleted_at is not None
    assert case.deleted_by == "admin1"
    assert "cascade from patient.deleted" in (case.delete_reason or "")


# ── Cases ──────────────────────────────────────────────────────────────


async def test_delete_case_clinician_is_forbidden(client: AsyncClient) -> None:
    pid = await _seed_patient(client, "F6.2-C-CLIN")
    cid = await _seed_case(client, pid)
    r = await client.request(
        "DELETE", f"/api/v1/cases/{cid}", headers=_CLINICIAN, json=_REASON
    )
    assert r.status_code == 403


async def test_delete_case_admin_with_reason_soft_deletes(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    pid = await _seed_patient(client, "F6.2-C-OK")
    cid = await _seed_case(client, pid)

    r = await client.request(
        "DELETE", f"/api/v1/cases/{cid}", headers=_ADMIN, json=_REASON
    )
    assert r.status_code == 200
    assert r.json()["soft_deleted"] is True

    # GET → 404
    r_get = await client.get(f"/api/v1/cases/{cid}", headers=_ADMIN)
    assert r_get.status_code == 404

    # Paciente sigue activo (no cascada inversa)
    r_pat = await client.get(f"/api/v1/patients/{pid}", headers=_ADMIN)
    assert r_pat.status_code == 200

    # Row persiste con metadata
    row = (
        await async_session.execute(select(CaseDB).where(CaseDB.id == cid))
    ).scalar_one()
    assert row.deleted_at is not None
    assert row.deleted_by == "admin1"


async def test_soft_deleted_patients_are_hidden_from_list(
    client: AsyncClient,
) -> None:
    alive = await _seed_patient(client, "F6.2-LIST-ALIVE")
    dead = await _seed_patient(client, "F6.2-LIST-DEAD")
    # F6.4 — un clinician solo ve pacientes con al menos un caso suyo;
    # necesitamos sembrar casos para que clinician1 los vea en el GET.
    await _seed_case(client, alive)
    await _seed_case(client, dead)
    r = await client.request(
        "DELETE", f"/api/v1/patients/{dead}", headers=_ADMIN, json=_REASON
    )
    assert r.status_code == 200

    r_list = await client.get("/api/v1/patients", headers=_CLINICIAN)
    ids = {p["patient_id"] for p in r_list.json()}
    assert alive in ids
    assert dead not in ids


async def test_double_delete_returns_404(client: AsyncClient) -> None:
    pid = await _seed_patient(client, "F6.2-DOUBLE")
    r1 = await client.request(
        "DELETE", f"/api/v1/patients/{pid}", headers=_ADMIN, json=_REASON
    )
    assert r1.status_code == 200
    r2 = await client.request(
        "DELETE", f"/api/v1/patients/{pid}", headers=_ADMIN, json=_REASON
    )
    assert r2.status_code == 404
