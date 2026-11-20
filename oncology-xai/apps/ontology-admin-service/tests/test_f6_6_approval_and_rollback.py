"""F6.6 — tests para validación de evidencia, aprobación humana,
rollback real y feature-flag del batch DeepSearch."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deep_search import evidence_supports_relation, validate_relations
from app.models import DiscoveredRelationDB, OntologyUpdateProposalDB


_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}
_CLINICIAN = {"x-user-id": "clinician1", "x-roles": "clinician"}


# ── Evidence support (unit) ───────────────────────────────────────────


def test_evidence_supports_relation_matches_subject_and_object() -> None:
    rel = {
        "subject": "lepidic pattern",
        "object": "EGFR",
        "evidence_quote": "The lepidic pattern was strongly associated with EGFR mutations.",
    }
    assert evidence_supports_relation(rel) is True


def test_evidence_supports_relation_rejects_generic_quote() -> None:
    rel = {
        "subject": "lepidic pattern",
        "object": "EGFR",
        "evidence_quote": "The authors reported several findings in LUAD.",
    }
    assert evidence_supports_relation(rel) is False


def test_evidence_supports_relation_rejects_empty_quote() -> None:
    rel = {"subject": "KRAS", "object": "solid pattern", "evidence_quote": ""}
    assert evidence_supports_relation(rel) is False


def test_validate_relations_marks_unsupported_evidence_as_invalid() -> None:
    rels = [
        {
            "subject": "lepidic pattern",
            "subject_type": "pattern",
            "predicate": "associatedWithMutation",
            "object": "EGFR",
            "object_type": "gene",
            "linked": True,
            "evidence_quote": "The lepidic pattern correlated with EGFR+ tumors.",
        },
        {
            # Estructura OK + linked OK pero quote no soporta la relación
            "subject": "solid pattern",
            "subject_type": "pattern",
            "predicate": "associatedWithMutation",
            "object": "KRAS",
            "object_type": "gene",
            "linked": True,
            "evidence_quote": "Methods section; nothing about this relation.",
        },
    ]
    result = validate_relations(rels)
    assert len(result["valid"]) == 1
    assert len(result["invalid"]) == 1
    assert result["invalid"][0]["validation_reason"] == "evidence_unsupported"


# ── Feature flag bloquea DeepSearch (incluyendo batch) ────────────────


@pytest.fixture(autouse=True)
def _reset_flag(monkeypatch: pytest.MonkeyPatch):
    """Garantiza un valor conocido del feature flag entre tests."""
    monkeypatch.setattr(settings, "ontology_deep_search_enabled", False)
    yield


async def test_deep_search_blocked_by_feature_flag(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/api/v1/admin/deep-search",
        headers=_ADMIN,
        json={"text": "lepidic pattern is associated with EGFR"},
    )
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


async def test_batch_deep_search_blocked_by_feature_flag(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/api/v1/admin/deep-search/batch",
        headers=_ADMIN,
        json={"queries": ["lepidic EGFR"], "sources": ["pubmed"]},
    )
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


async def test_deep_search_works_when_flag_enabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ontology_deep_search_enabled", True)
    r = await client.post(
        "/api/v1/admin/deep-search",
        headers=_ADMIN,
        json={"text": "lepidic pattern is associated with EGFR mutations in LUAD."},
    )
    # 201 COMPLETED o FAILED son ambos aceptables; lo clave es que el
    # flag ya no bloquea.
    assert r.status_code == 201


# ── Aprobación humana de discovered relations ─────────────────────────


async def _seed_relation(
    session: AsyncSession, *, is_active: bool = False, rolled_back: bool = False
) -> str:
    rel_id = str(uuid4())
    rb_at = datetime.now(UTC) if rolled_back else None
    rel = DiscoveredRelationDB(
        id=rel_id,
        job_id="job-1",
        subject="lepidic pattern",
        predicate="associatedWithMutation",
        object="EGFR",
        subject_iri="http://ex/lepidic",
        object_iri="http://ex/EGFR",
        subject_type="HistologicalPattern",
        object_type="Gene",
        evidence_quote="Lepidic pattern was linked to EGFR mutations.",
        is_active=is_active,
        rolled_back_at=rb_at,
    )
    session.add(rel)
    await session.commit()
    return rel_id


async def test_discovered_relations_default_inactive(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """Con el nuevo default, una relación sembrada sin ``is_active``
    explícito debe quedar inactiva."""
    rel_id = await _seed_relation(async_session)
    row = (
        await async_session.execute(
            text("SELECT is_active FROM discovered_relations WHERE id = :i"),
            {"i": rel_id},
        )
    ).fetchone()
    assert row[0] in (False, 0)  # SQLite devuelve 0/1


async def test_list_discovered_hides_pending_by_default(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    await _seed_relation(async_session, is_active=False)
    r = await client.get("/api/v1/admin/deep-search/discovered", headers=_ADMIN)
    assert r.status_code == 200
    assert r.json() == []


async def test_list_discovered_status_pending_shows_pending(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    rel_id = await _seed_relation(async_session, is_active=False)
    r = await client.get(
        "/api/v1/admin/deep-search/discovered?status_filter=pending",
        headers=_ADMIN,
    )
    ids = {row["id"] for row in r.json()}
    assert rel_id in ids


async def test_approve_requires_admin(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    rel_id = await _seed_relation(async_session)
    r = await client.post(
        f"/api/v1/admin/deep-search/discovered/{rel_id}:approve",
        headers=_CLINICIAN,
    )
    assert r.status_code == 403


async def test_approve_sets_is_active_and_metadata(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    rel_id = await _seed_relation(async_session)
    r = await client.post(
        f"/api/v1/admin/deep-search/discovered/{rel_id}:approve",
        headers=_ADMIN,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_active"] is True
    assert body["approved_by"] == "admin1"
    assert body["approved_at"] is not None

    # Ahora SÍ aparece en el listing default (active)
    r2 = await client.get("/api/v1/admin/deep-search/discovered", headers=_ADMIN)
    ids = {row["id"] for row in r2.json()}
    assert rel_id in ids


async def test_approve_on_rolled_back_relation_is_409(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    rel_id = await _seed_relation(async_session, rolled_back=True)
    r = await client.post(
        f"/api/v1/admin/deep-search/discovered/{rel_id}:approve",
        headers=_ADMIN,
    )
    assert r.status_code == 409


async def test_reject_requires_reason(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    rel_id = await _seed_relation(async_session, is_active=True)
    r = await client.post(
        f"/api/v1/admin/deep-search/discovered/{rel_id}:reject",
        headers=_ADMIN,
        json={},
    )
    assert r.status_code == 400


async def test_reject_deactivates_and_marks(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    rel_id = await _seed_relation(async_session, is_active=True)
    r = await client.post(
        f"/api/v1/admin/deep-search/discovered/{rel_id}:reject",
        headers=_ADMIN,
        json={"reason": "spurious evidence per curator review"},
    )
    assert r.status_code == 200
    assert r.json()["is_active"] is False
    assert r.json()["rolled_back_by"] == "admin1"


# ── Rollback de proposal: desactiva relaciones del job ────────────────


async def test_rollback_proposal_requires_admin(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    prop = OntologyUpdateProposalDB(
        id=str(uuid4()),
        targets=[{"job_id": "job-rb-1"}],
        mode="offline",
        status="PUBLISHED",
    )
    async_session.add(prop)
    await async_session.commit()
    r = await client.post(
        f"/api/v1/admin/ontologies/proposals/{prop.id}:rollback",
        headers=_CLINICIAN,
        json={"reason": "invalid literature"},
    )
    assert r.status_code == 403


async def test_rollback_proposal_requires_reason(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    prop = OntologyUpdateProposalDB(
        id=str(uuid4()),
        targets=[{"job_id": "job-rb-2"}],
        mode="offline",
        status="PUBLISHED",
    )
    async_session.add(prop)
    await async_session.commit()
    r = await client.post(
        f"/api/v1/admin/ontologies/proposals/{prop.id}:rollback",
        headers=_ADMIN,
        json={},
    )
    assert r.status_code == 400


async def test_rollback_proposal_deactivates_related_relations(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """La proposal referencia ``job_id = 'job-rb-3'`` y 2 relaciones ya
    activas de ese job; tras rollback todas quedan inactivas con
    ``rolled_back_*`` setados."""
    job_id = "job-rb-3"
    for _ in range(2):
        async_session.add(
            DiscoveredRelationDB(
                id=str(uuid4()),
                job_id=job_id,
                subject="s",
                predicate="associatedWithMutation",
                object="o",
                is_active=True,
            )
        )
    # Una relación ajena de otro job que NO debe tocarse
    other_id = str(uuid4())
    async_session.add(
        DiscoveredRelationDB(
            id=other_id,
            job_id="other",
            subject="s2",
            predicate="treatedWith",
            object="drug",
            is_active=True,
        )
    )
    prop = OntologyUpdateProposalDB(
        id=str(uuid4()),
        targets=[{"job_id": job_id}],
        mode="offline",
        status="PUBLISHED",
    )
    async_session.add(prop)
    await async_session.commit()

    r = await client.post(
        f"/api/v1/admin/ontologies/proposals/{prop.id}:rollback",
        headers=_ADMIN,
        json={"reason": "hallucination detected post-review"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ROLLED_BACK"
    assert r.json()["rolled_relations"] == 2

    rows = (
        await async_session.execute(
            text(
                "SELECT COUNT(*) FROM discovered_relations "
                "WHERE job_id = :j AND is_active = 0"
            ),
            {"j": job_id},
        )
    ).scalar_one()
    assert rows == 2

    # La relación de otro job no se tocó
    other_active = (
        await async_session.execute(
            text("SELECT is_active FROM discovered_relations WHERE id = :i"),
            {"i": other_id},
        )
    ).scalar_one()
    assert bool(other_active) is True


async def test_rollback_proposal_is_idempotent(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    prop = OntologyUpdateProposalDB(
        id=str(uuid4()), targets=[], mode="offline", status="ROLLED_BACK"
    )
    async_session.add(prop)
    await async_session.commit()
    r = await client.post(
        f"/api/v1/admin/ontologies/proposals/{prop.id}:rollback",
        headers=_ADMIN,
        json={"reason": "retry idempotency check"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ROLLED_BACK"
