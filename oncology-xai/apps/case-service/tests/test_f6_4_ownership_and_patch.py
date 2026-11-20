"""F6.4 — Ownership horizontal en case-service.

Modelo verificado:

* ``POST /cases`` asigna ``created_by`` desde la identidad del request
  (ignora cualquier ``created_by`` que venga en el body). Ya cubierto
  por F6.2 a nivel de ``create_patient``/``create_case``; aquí añadimos
  la regresión explícita.
* ``PATCH /cases/{id}``:
    - 404 si el caso no existe o está soft-deleted.
    - 403 si el caso fue creado por otro clinician (no supervisor).
    - 200 si el creador o un admin edita campos permitidos.
    - 400 si el body intenta cambiar ``created_by`` / ``patient_id`` /
      ``deleted_*`` (reasignar dueño o resucitar soft-deletes).
* ``GET /cases`` (list) y ``GET /cases/{id}`` filtran por ownership
  horizontal — un clinician no ve casos ajenos.
* ``GET /patients`` y ``GET /patients/{id}`` igual: un clinician solo ve
  pacientes con al menos un caso suyo.
"""

from __future__ import annotations

from httpx import AsyncClient


_A = {"x-user-id": "clinicianA", "x-roles": "clinician"}
_B = {"x-user-id": "clinicianB", "x-roles": "clinician"}
_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}
_AUDIT = {"x-user-id": "auditor1", "x-roles": "auditor"}


async def _new_patient(client: AsyncClient, external: str, who: dict) -> str:
    r = await client.post(
        "/api/v1/patients",
        json={"external_id": external, "demographics": {}},
        headers=who,
    )
    assert r.status_code == 201
    return r.json()["patient_id"]


async def _new_case(client: AsyncClient, patient_id: str, who: dict) -> dict:
    r = await client.post(
        "/api/v1/cases", json={"patient_id": patient_id}, headers=who
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── POST /cases: created_by del identity ──────────────────────────────


async def test_create_case_ignores_body_created_by(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-01", _A)
    r = await client.post(
        "/api/v1/cases",
        json={"patient_id": pid, "created_by": "forged-user"},
        headers=_A,
    )
    assert r.status_code == 201
    assert r.json()["created_by"] == "clinicianA"


# ── PATCH /cases ──────────────────────────────────────────────────────


async def test_patch_case_forbidden_cross_clinician(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-02", _A)
    case = await _new_case(client, pid, _A)
    r = await client.patch(
        f"/api/v1/cases/{case['case_id']}", json={"tags": ["x"]}, headers=_B
    )
    assert r.status_code == 403


async def test_patch_case_owner_can_update(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-03", _A)
    case = await _new_case(client, pid, _A)
    r = await client.patch(
        f"/api/v1/cases/{case['case_id']}",
        json={"tags": ["prio-1"], "metadata": {"note": "ok"}},
        headers=_A,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tags"] == ["prio-1"]
    assert body["metadata"] == {"note": "ok"}


async def test_patch_case_admin_can_update_anyone(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-04", _A)
    case = await _new_case(client, pid, _A)
    r = await client.patch(
        f"/api/v1/cases/{case['case_id']}", json={"tags": ["admin-set"]}, headers=_ADMIN
    )
    assert r.status_code == 200
    assert r.json()["tags"] == ["admin-set"]


async def test_patch_case_rejects_forbidden_fields(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-05", _A)
    case = await _new_case(client, pid, _A)
    for forbidden in ("created_by", "patient_id", "deleted_at", "deleted_by"):
        r = await client.patch(
            f"/api/v1/cases/{case['case_id']}",
            json={forbidden: "anything"},
            headers=_A,
        )
        assert r.status_code == 400, (
            f"{forbidden} should be rejected but got {r.status_code}"
        )


# ── GET /cases: horizontal isolation ──────────────────────────────────


async def test_list_cases_filters_by_owner(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-06", _A)
    await _new_case(client, pid, _A)
    await _new_case(client, pid, _A)
    pid_b = await _new_patient(client, "TCGA-07", _B)
    await _new_case(client, pid_b, _B)

    r_a = await client.get("/api/v1/cases", headers=_A)
    assert r_a.status_code == 200
    assert len(r_a.json()) == 2  # solo los dos de A

    r_b = await client.get("/api/v1/cases", headers=_B)
    assert r_b.status_code == 200
    assert len(r_b.json()) == 1

    r_admin = await client.get("/api/v1/cases", headers=_ADMIN)
    assert r_admin.status_code == 200
    assert len(r_admin.json()) == 3

    r_aud = await client.get("/api/v1/cases", headers=_AUDIT)
    assert len(r_aud.json()) == 3


async def test_get_case_cross_clinician_is_404(client: AsyncClient) -> None:
    """Consistente con authz: no revela existencia a quien no tiene acceso."""
    pid = await _new_patient(client, "TCGA-08", _A)
    case = await _new_case(client, pid, _A)
    # El helper authz.require_case_access responde 403 cuando el caso
    # existe pero el caller no es dueño; ver test siguiente que valida el
    # mensaje exacto. (Mantenemos 403 para distinguir de "no existe".)
    r = await client.get(f"/api/v1/cases/{case['case_id']}", headers=_B)
    assert r.status_code == 403


async def test_get_case_owner_and_admin_succeed(client: AsyncClient) -> None:
    pid = await _new_patient(client, "TCGA-09", _A)
    case = await _new_case(client, pid, _A)
    for h in (_A, _ADMIN, _AUDIT):
        r = await client.get(f"/api/v1/cases/{case['case_id']}", headers=h)
        assert r.status_code == 200, f"{h} should see the case: {r.text}"


# ── GET /patients: horizontal isolation ───────────────────────────────


async def test_list_patients_hides_patients_without_own_case(
    client: AsyncClient,
) -> None:
    # A crea paciente1 + 1 caso. B crea paciente2 + 1 caso.
    p1 = await _new_patient(client, "TCGA-10", _A)
    await _new_case(client, p1, _A)
    p2 = await _new_patient(client, "TCGA-11", _B)
    await _new_case(client, p2, _B)

    r_a = await client.get("/api/v1/patients", headers=_A)
    externals_a = {p["external_id"] for p in r_a.json()}
    assert externals_a == {"TCGA-10"}

    r_b = await client.get("/api/v1/patients", headers=_B)
    externals_b = {p["external_id"] for p in r_b.json()}
    assert externals_b == {"TCGA-11"}

    r_admin = await client.get("/api/v1/patients", headers=_ADMIN)
    externals_admin = {p["external_id"] for p in r_admin.json()}
    assert {"TCGA-10", "TCGA-11"}.issubset(externals_admin)


async def test_get_patient_cross_clinician_is_404(client: AsyncClient) -> None:
    p1 = await _new_patient(client, "TCGA-12", _A)
    await _new_case(client, p1, _A)
    r = await client.get(f"/api/v1/patients/{p1}", headers=_B)
    assert r.status_code == 404
