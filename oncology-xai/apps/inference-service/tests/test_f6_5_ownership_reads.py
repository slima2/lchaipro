"""F6.5 — Ownership por caso en reads (jobs/results/artifacts) y en
``:process``.

Modelo verificado:

* Un clinician NO puede ver ``GET /jobs/{id}``, ``/results/{id}``,
  ``/results/{id}/artifacts`` ni ``/images/{id}/results/latest`` de otro
  clinician (403) conociendo solo el id.
* admin y auditor SÍ pueden.
* Caso soft-deleted → 404 para todos (no revela existencia).
* ``:process`` sobre una imagen de un caso ajeno → 403.
"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import seed_case, seed_image


_A = {"x-user-id": "clinicianA", "x-roles": "clinician"}
_B = {"x-user-id": "clinicianB", "x-roles": "clinician"}
_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}
_AUDIT = {"x-user-id": "auditor1", "x-roles": "auditor"}


# ── Helpers de seed para results/artifacts ────────────────────────────


async def _seed_bundle_with_artifact(
    session: AsyncSession,
    *,
    case_id: str,
    image_id: str,
    bundle_id: str,
    artifact_id: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO result_bundles (id, case_id, image_id, job_id, "
            "model_profile, model_version, thresholds, pattern_composition, "
            "evidence_source, intended_use, created_at) VALUES "
            "(:b, :c, :i, 'job-x', 'm', 'v', '{}', '{}', "
            "'THESIS_INTERNAL', 'research', CURRENT_TIMESTAMP)"
        ),
        {"b": bundle_id, "c": case_id, "i": image_id},
    )
    await session.execute(
        text(
            "INSERT INTO xai_artifacts (id, result_bundle_id, artifact_type, "
            "uri, hash, created_at) VALUES "
            "(:a, :b, 'shap', 's3://x/foo', 'h', CURRENT_TIMESTAMP)"
        ),
        {"a": artifact_id, "b": bundle_id},
    )
    await session.commit()


# ── :process ─────────────────────────────────────────────────────────────


async def test_process_image_across_clinicians_is_forbidden(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """clinician B intenta :process sobre una imagen cuyo caso lo creó A."""
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    r = await client.post(f"/api/v1/images/{img}:process", headers=_B, json={})
    assert r.status_code == 403


async def test_process_image_admin_can_process_anyones_image(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    r = await client.post(f"/api/v1/images/{img}:process", headers=_ADMIN, json={})
    assert r.status_code == 202


# ── GET /jobs/{id} ────────────────────────────────────────────────────


async def test_get_job_forbidden_cross_clinician(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    job_id = r.json()["job_id"]

    r_b = await client.get(f"/api/v1/jobs/{job_id}", headers=_B)
    assert r_b.status_code == 403

    r_a = await client.get(f"/api/v1/jobs/{job_id}", headers=_A)
    assert r_a.status_code == 200


async def test_get_job_auditor_can_read(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    job_id = r.json()["job_id"]

    r_aud = await client.get(f"/api/v1/jobs/{job_id}", headers=_AUDIT)
    assert r_aud.status_code == 200


async def test_get_job_on_softdeleted_case_is_404(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseSD", case_created_by="clinicianA"
    )
    r = await client.post(f"/api/v1/images/{img}:process", headers=_A, json={})
    job_id = r.json()["job_id"]

    # Soft-delete del caso
    await async_session.execute(
        text("UPDATE cases SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'caseSD'")
    )
    await async_session.commit()

    # Hasta el dueño ve 404 (consistente con lecturas de /cases)
    for h in (_A, _ADMIN):
        r_read = await client.get(f"/api/v1/jobs/{job_id}", headers=h)
        assert r_read.status_code == 404, f"with {h}: {r_read.text}"


# ── GET /results/{id} + /results/{id}/artifacts ─────────────────────────


async def test_get_result_bundle_forbidden_cross_clinician(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    bundle_id = "rb-" + img[:8]
    await _seed_bundle_with_artifact(
        async_session,
        case_id="caseA",
        image_id=img,
        bundle_id=bundle_id,
        artifact_id="art-1",
    )

    r_b = await client.get(f"/api/v1/results/{bundle_id}", headers=_B)
    assert r_b.status_code == 403
    r_a = await client.get(f"/api/v1/results/{bundle_id}", headers=_A)
    assert r_a.status_code == 200


async def test_get_artifacts_forbidden_cross_clinician(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    bundle_id = "rb-art-" + img[:8]
    await _seed_bundle_with_artifact(
        async_session,
        case_id="caseA",
        image_id=img,
        bundle_id=bundle_id,
        artifact_id="art-2",
    )

    r_b = await client.get(f"/api/v1/results/{bundle_id}/artifacts", headers=_B)
    assert r_b.status_code == 403

    r_a = await client.get(f"/api/v1/results/{bundle_id}/artifacts", headers=_A)
    assert r_a.status_code == 200
    data = r_a.json()
    assert len(data) == 1


async def test_get_latest_results_forbidden_cross_clinician(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    img = str(uuid4())
    await seed_image(
        async_session, image_id=img, case_id="caseA", case_created_by="clinicianA"
    )
    await _seed_bundle_with_artifact(
        async_session,
        case_id="caseA",
        image_id=img,
        bundle_id="rb-latest",
        artifact_id="art-latest",
    )

    r_b = await client.get(f"/api/v1/images/{img}/results/latest", headers=_B)
    assert r_b.status_code == 403
    r_a = await client.get(f"/api/v1/images/{img}/results/latest", headers=_A)
    assert r_a.status_code == 200
