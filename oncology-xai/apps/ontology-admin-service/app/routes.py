"""Ontology Admin routes (DERCAS 9.7) + DeepSearch + KG Versioning."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


# ── Identity helpers (F6.6) ─────────────────────────────────────────────


def _actor(request: Request) -> str:
    return request.headers.get("x-user-id", "unknown")


def _roles(request: Request) -> set[str]:
    raw = request.headers.get("x-roles", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def _require_admin(request: Request) -> str:
    """Defensa en profundidad: el gateway ya pide ``admin`` para todo
    ``/admin/*``, pero el servicio también lo verifica para que un pod
    lateral comprometido no pueda saltárselo con ``X-Roles`` falso.
    """
    if "admin" not in _roles(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return _actor(request)


def _require_deep_search_enabled() -> None:
    """F6.6 — el feature flag ``ontology_deep_search_enabled`` tiene que
    bloquear **tanto** el pipeline single (:func:`run_deep_search`) como
    el batch (:func:`start_batch_deep_search`). Antes el flag existía
    pero nadie lo chequeaba y el batch seguía andando.
    """
    if not settings.ontology_deep_search_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "DeepSearch pipeline is disabled. Set "
                "ontology_deep_search_enabled=true to enable."
            ),
        )


from app.database import async_session, get_db
from app.deep_search import (
    deduplicate,
    entity_link,
    extract_relations_llm,
    run_pipeline,
    validate_relations,
)
from app.literature_search import DEFAULT_QUERIES, batch_literature_search
from app.models import (
    DeepSearchJobDB,
    DiscoveredRelationDB,
    KGChangelogEntryDB,
    KGSnapshotDB,
    OntologyUpdateProposalDB,
    OntologyVersionDB,
)

logger = logging.getLogger(__name__)

# Tareas en segundo plano en curso (ver S7502 más abajo)
_BACKGROUND_TASKS: set = set()

router = APIRouter(prefix="/api/v1/admin", tags=["Ontology Admin"])


@router.get("/ontologies")
async def list_ontologies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(OntologyVersionDB).order_by(OntologyVersionDB.imported_at.desc())
    )
    return [
        {
            "ontology_version_id": v.id,
            "name": v.name,
            "version_tag": v.version_tag,
            "source_uri": v.source_uri,
            "hash": v.hash,
            "is_active": v.is_active,
            "imported_at": v.imported_at.isoformat() if v.imported_at else None,
        }
        for v in result.scalars().all()
    ]


@router.post("/ontologies:update-proposal", status_code=status.HTTP_201_CREATED)
async def create_proposal(body: dict[str, Any], db: AsyncSession = Depends(get_db)):
    proposal = OntologyUpdateProposalDB(
        id=str(uuid4()),
        targets=body.get("targets", []),
        mode=body.get("mode", "offline"),
        status="DRAFT",
        created_by=body.get("created_by"),
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    return _proposal_dict(proposal)


@router.get("/ontologies/proposals/{proposal_id}")
async def get_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(OntologyUpdateProposalDB, proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _proposal_dict(p)


@router.post("/ontologies/proposals/{proposal_id}:run-validation")
async def run_validation(proposal_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(OntologyUpdateProposalDB, proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    p.status = "VALIDATING"
    # Mock validation
    p.validation_results = {
        "consistency": True,
        "sparql_tests_passed": 5,
        "sparql_tests_failed": 0,
    }
    p.status = "VALIDATED"
    await db.commit()
    return _proposal_dict(p)


@router.post("/ontologies/proposals/{proposal_id}:approve-and-publish")
async def approve_and_publish(
    proposal_id: str,
    body: dict[str, Any] | None = None,
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(OntologyUpdateProposalDB, proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if p.status not in ("VALIDATED", "PENDING_APPROVAL"):
        raise HTTPException(
            status_code=400, detail=f"Cannot publish proposal in status {p.status}"
        )

    p.status = "PUBLISHED"
    p.approved_by = (body or {}).get("approved_by", "admin")
    from datetime import datetime

    p.published_at = datetime.utcnow()
    await db.commit()
    return _proposal_dict(p)


@router.post("/ontologies/proposals/{proposal_id}:rollback")
async def rollback_proposal(
    proposal_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Rollback **real** de una proposal.

    F6.6 — antes sólo cambiaba ``status='ROLLED_BACK'`` en la proposal
    y dejaba vivas las relaciones que la proposal había activado. Ahora:

    1. Pone la proposal en ``ROLLED_BACK`` con ``rolled_back_*``.
    2. Desactiva (``is_active=False``) todas las
       :class:`DiscoveredRelationDB` del ``job_id`` referenciado por la
       proposal (si lo trae en ``targets``).
    3. Marca el ``KGSnapshotDB`` activo vigente como "invalidado por
       rollback" en una entrada de changelog.

    Requiere ``reason`` (min 8 chars) en el body para auditoría.
    """
    actor = _require_admin(request)
    reason = ((body or {}).get("reason") or "").strip()
    if len(reason) < 8:
        raise HTTPException(
            status_code=400,
            detail="A 'reason' of at least 8 chars is required for rollback",
        )

    p = await db.get(OntologyUpdateProposalDB, proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if p.status == "ROLLED_BACK":
        return _proposal_dict(p)

    now = datetime.now(UTC)
    p.status = "ROLLED_BACK"

    # Desactivar relaciones de los job_ids referenciados por la proposal.
    rolled_relations = 0
    targets = p.targets or []
    job_ids: list[str] = []
    if isinstance(targets, list):
        for t in targets:
            if isinstance(t, dict) and t.get("job_id"):
                job_ids.append(str(t["job_id"]))
    for jid in job_ids:
        res = await db.execute(
            select(DiscoveredRelationDB).where(
                DiscoveredRelationDB.job_id == jid,
                DiscoveredRelationDB.is_active.is_(True),
            )
        )
        for r in res.scalars().all():
            r.is_active = False
            r.rolled_back_by = actor
            r.rolled_back_at = now
            r.rollback_reason = reason
            rolled_relations += 1

    # Changelog estructural del snapshot más reciente (si lo hay)
    snap_rows = await db.execute(
        select(KGSnapshotDB).order_by(KGSnapshotDB.created_at.desc()).limit(1)
    )
    latest_snap = snap_rows.scalar_one_or_none()
    if latest_snap is not None:
        db.add(
            KGChangelogEntryDB(
                id=str(uuid4()),
                snapshot_id=latest_snap.id,
                action="REMOVED",
                entity_type="rollback",
                entity_id=proposal_id,
                detail=json.dumps(
                    {"reason": reason, "rolled_relations": rolled_relations}
                ),
                provenance=f"rollback by {actor}",
            )
        )

    await db.commit()
    logger.info(
        "Rollback proposal=%s by %s: %d relations deactivated",
        proposal_id,
        actor,
        rolled_relations,
    )
    out = _proposal_dict(p)
    out["rolled_relations"] = rolled_relations
    return out


def _proposal_dict(p: OntologyUpdateProposalDB) -> dict:
    return {
        "proposal_id": p.id,
        "targets": p.targets,
        "mode": p.mode,
        "status": p.status,
        "diff_report_uri": p.diff_report_uri,
        "impact": p.impact,
        "reasoner_report_uri": p.reasoner_report_uri,
        "validation_results": p.validation_results,
        "created_by": p.created_by,
        "approved_by": p.approved_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "published_at": p.published_at.isoformat() if p.published_at else None,
    }


# ──────────────────────────────────────────────────────────────────────
# DeepSearch Pipeline
# ──────────────────────────────────────────────────────────────────────


@router.post("/deep-search", status_code=status.HTTP_201_CREATED)
async def run_deep_search(body: dict[str, Any], db: AsyncSession = Depends(get_db)):
    """Run DeepSearch pipeline: ingest → extract → link → dedup → validate."""
    # F6.6 — feature flag obligatorio
    _require_deep_search_enabled()
    text = body.get("text", "")
    source_type = body.get("source_type", "text")
    if not text:
        raise HTTPException(400, "text is required")

    job = DeepSearchJobDB(
        id=str(uuid4()),
        source_text=text[:5000],
        source_type=source_type,
        status="RUNNING",
    )
    db.add(job)
    await db.flush()

    try:
        result = await run_pipeline(
            text,
            source_type,
            llm_provider=settings.llm_provider,
            openai_api_key=settings.openai_api_key,
            anthropic_api_key=settings.anthropic_api_key,
        )
        job.extracted_relations = result.get("valid_relations", [])
        job.linked_entities = {
            "raw_count": result["raw_count"],
            "linked_count": result["linked_count"],
            "deduped_count": result["deduped_count"],
            "valid_count": result["valid_count"],
            "invalid_count": result["invalid_count"],
        }
        job.validation_result = {
            "valid": result["valid_relations"],
            "invalid": result["invalid_relations"],
        }
        job.status = "COMPLETED"
        job.completed_at = datetime.now(UTC)
    except Exception as exc:
        job.status = "FAILED"
        job.error = str(exc)[:500]

    await db.commit()
    await db.refresh(job)
    return _job_dict(job)


@router.get("/deep-search/jobs")
async def list_deep_search_jobs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DeepSearchJobDB).order_by(DeepSearchJobDB.created_at.desc()).limit(20)
    )
    return [_job_dict(j) for j in result.scalars().all()]


@router.get("/deep-search/jobs/{job_id}")
async def get_deep_search_job(job_id: str, db: AsyncSession = Depends(get_db)):
    j = await db.get(DeepSearchJobDB, job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return _job_dict(j)


def _job_dict(j: DeepSearchJobDB) -> dict:
    return {
        "job_id": j.id,
        "status": j.status,
        "source_type": j.source_type,
        "extracted_relations": j.extracted_relations,
        "linked_entities": j.linked_entities,
        "validation_result": j.validation_result,
        "error": j.error,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }


# ──────────────────────────────────────────────────────────────────────
# KG Versioning
# ──────────────────────────────────────────────────────────────────────


@router.post("/kg/snapshots", status_code=status.HTTP_201_CREATED)
async def create_kg_snapshot(body: dict[str, Any], db: AsyncSession = Depends(get_db)):
    """Create a new KG version snapshot from validated DeepSearch results."""
    version_tag = body.get("version_tag", datetime.now(UTC).strftime("v%Y%m%d-%H%M"))
    description = body.get("description", "")
    relations = body.get("relations", [])

    snap = KGSnapshotDB(
        id=str(uuid4()),
        version_tag=version_tag,
        description=description,
        nodes_count=len(
            {r.get("subject_iri") for r in relations}
            | {r.get("object_iri") for r in relations}
        ),
        edges_count=len(relations),
        sources=body.get("sources", ["DeepSearch"]),
        format="jsonld",
        created_by=body.get("created_by"),
    )
    db.add(snap)

    # Create changelog entries
    for rel in relations:
        entry = KGChangelogEntryDB(
            id=str(uuid4()),
            snapshot_id=snap.id,
            action="ADDED",
            entity_type="edge",
            entity_id=f"{rel.get('subject', '?')} → {rel.get('predicate', '?')} → {rel.get('object', '?')}",
            detail=json.dumps(rel, default=str),
            provenance=rel.get("evidence_quote", ""),
        )
        db.add(entry)

    await db.commit()
    await db.refresh(snap)
    return _snapshot_dict(snap)


@router.get("/kg/snapshots")
async def list_kg_snapshots(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KGSnapshotDB).order_by(KGSnapshotDB.created_at.desc()).limit(50)
    )
    return [_snapshot_dict(s) for s in result.scalars().all()]


@router.get("/kg/snapshots/{snapshot_id}")
async def get_kg_snapshot(snapshot_id: str, db: AsyncSession = Depends(get_db)):
    s = await db.get(KGSnapshotDB, snapshot_id)
    if not s:
        raise HTTPException(404, "Snapshot not found")
    return _snapshot_dict(s)


@router.get("/kg/snapshots/{snapshot_id}/changelog")
async def get_kg_changelog(snapshot_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KGChangelogEntryDB)
        .where(KGChangelogEntryDB.snapshot_id == snapshot_id)
        .order_by(KGChangelogEntryDB.created_at.desc())
    )
    return [
        {
            "id": e.id,
            "action": e.action,
            "entity_type": e.entity_type,
            "entity_id": e.entity_id,
            "detail": e.detail,
            "provenance": e.provenance,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in result.scalars().all()
    ]


def _snapshot_dict(s: KGSnapshotDB) -> dict:
    return {
        "snapshot_id": s.id,
        "version_tag": s.version_tag,
        "description": s.description,
        "nodes_count": s.nodes_count,
        "edges_count": s.edges_count,
        "snapshot_uri": s.snapshot_uri,
        "format": s.format,
        "sources": s.sources,
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


# ──────────────────────────────────────────────────────────────────────
# Batch Literature DeepSearch (long-running background job)
# ──────────────────────────────────────────────────────────────────────


@router.post("/deep-search/batch", status_code=status.HTTP_202_ACCEPTED)
async def start_batch_deep_search(
    body: dict[str, Any] | None = None, db: AsyncSession = Depends(get_db)
):
    """Launch batch literature search across PubMed, arXiv, Semantic Scholar.

    Runs in background: searches → extracts relations via LLM → links → validates → persists.

    F6.6 — el feature flag ``ontology_deep_search_enabled`` también
    bloquea esta variante batch (antes sólo se chequeaba en una futura
    iteración que nunca llegó). Las relaciones persistidas por el
    worker quedan ``is_active=False`` y requieren aprobación humana
    (``:approve``) antes de aparecer en el KG.
    """
    _require_deep_search_enabled()
    body = body or {}
    queries = body.get("queries") or DEFAULT_QUERIES
    sources = body.get("sources") or ["pubmed", "semantic_scholar", "arxiv"]

    job = DeepSearchJobDB(
        id=str(uuid4()),
        source_type="batch_literature",
        status="RUNNING",
        source_text=json.dumps(
            {"queries": queries[:3], "sources": sources, "total_queries": len(queries)}
        ),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Fire and forget background task. Se guarda una referencia hasta que termine: sin ella Python puede
    # borrar la tarea a mitad de camino (SonarQube python:S7502).
    task = asyncio.create_task(_run_batch_job(job.id, queries, sources))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return {
        "job_id": job.id,
        "status": "RUNNING",
        "message": f"Batch search started: {len(queries)} queries across {', '.join(sources)}. This may take several minutes.",
        "queries_count": len(queries),
        "sources": sources,
    }


async def _run_batch_job(job_id: str, queries: list[str], sources: list[str]) -> None:
    """Background: search literature, extract & validate relations, persist."""
    async with async_session() as db:
        job = await db.get(DeepSearchJobDB, job_id)
        if not job:
            return

        try:
            # Step 1: Batch literature search
            papers = await batch_literature_search(queries, sources, max_per_source=3)
            logger.info("Batch job %s: found %d papers", job_id[:8], len(papers))

            all_valid: list[dict] = []
            all_invalid: list[dict] = []
            papers_processed = 0

            # Step 2-5: For each paper, extract → link → dedup → validate
            for paper in papers:
                abstract = paper.get("abstract", "")
                if not abstract or len(abstract) < 50:
                    continue

                try:
                    raw = await extract_relations_llm(
                        abstract,
                        llm_provider=settings.llm_provider,
                        openai_api_key=settings.openai_api_key,
                        anthropic_api_key=settings.anthropic_api_key,
                    )
                    linked = entity_link(raw)
                    deduped = deduplicate(linked)
                    validation = validate_relations(deduped)

                    # Attach paper metadata to each relation
                    for rel in validation["valid"]:
                        rel["paper_source"] = paper.get("source", "")
                        rel["paper_id"] = paper.get("id", "")
                        rel["paper_title"] = paper.get("title", "")
                        rel["paper_url"] = paper.get("url", "")
                    all_valid.extend(validation["valid"])
                    all_invalid.extend(validation["invalid"])
                    papers_processed += 1
                except Exception as e:
                    logger.warning(
                        "Extraction failed for paper %s: %s",
                        paper.get("id", "?")[:20],
                        e,
                    )

            # Deduplicate across all papers
            seen: set[tuple] = set()
            unique_valid: list[dict] = []
            for rel in all_valid:
                key = (
                    rel.get("subject_iri"),
                    rel.get("predicate"),
                    rel.get("object_iri"),
                )
                if key not in seen and all(key):
                    seen.add(key)
                    unique_valid.append(rel)

            # Persist discovered relations
            for rel in unique_valid:
                dr = DiscoveredRelationDB(
                    id=str(uuid4()),
                    job_id=job_id,
                    subject=rel.get("subject", ""),
                    predicate=rel.get("predicate", ""),
                    object=rel.get("object", ""),
                    subject_iri=rel.get("subject_iri"),
                    object_iri=rel.get("object_iri"),
                    subject_type=rel.get("subject_type"),
                    object_type=rel.get("object_type"),
                    evidence_quote=rel.get("evidence_quote", "")[:500],
                    paper_source=rel.get("paper_source", ""),
                    paper_id=rel.get("paper_id", ""),
                    paper_title=rel.get("paper_title", "")[:300],
                    paper_url=rel.get("paper_url", ""),
                    confidence="high" if rel.get("linked") else "medium",
                )
                db.add(dr)

            # Update job
            job.status = "COMPLETED"
            job.completed_at = datetime.now(UTC)
            job.linked_entities = {
                "papers_found": len(papers),
                "papers_processed": papers_processed,
                "raw_relations": len(all_valid) + len(all_invalid),
                "valid_unique": len(unique_valid),
                "invalid_count": len(all_invalid),
            }
            job.extracted_relations = unique_valid[:100]  # store first 100
            job.validation_result = {
                "valid_count": len(unique_valid),
                "invalid_count": len(all_invalid),
                "sources": list({r.get("paper_source", "") for r in unique_valid}),
            }
            await db.commit()
            logger.info(
                "Batch job %s COMPLETED: %d papers → %d unique valid relations",
                job_id[:8],
                papers_processed,
                len(unique_valid),
            )

        except Exception as exc:
            job.status = "FAILED"
            job.error = str(exc)[:500]
            job.completed_at = datetime.now(UTC)
            await db.commit()
            logger.error("Batch job %s FAILED: %s", job_id[:8], exc)


@router.get("/deep-search/discovered")
async def list_discovered_relations(
    limit: int = 50,
    status_filter: str = "active",
    db: AsyncSession = Depends(get_db),
):
    """List discovered relations from batch searches.

    * ``status_filter=active`` (default) — solo relaciones aprobadas
      (visibles en el KG). Coincide con el comportamiento previo.
    * ``status_filter=pending`` — relaciones descubiertas pero aún no
      aprobadas (lo que el admin debería revisar).
    * ``status_filter=all`` — todas, para auditoría.
    """
    stmt = select(DiscoveredRelationDB)
    if status_filter == "active":
        stmt = stmt.where(DiscoveredRelationDB.is_active.is_(True))
    elif status_filter == "pending":
        stmt = stmt.where(
            DiscoveredRelationDB.is_active.is_(False),
            DiscoveredRelationDB.rolled_back_at.is_(None),
        )
    elif status_filter == "all":
        pass
    else:
        raise HTTPException(
            status_code=400,
            detail="status_filter must be one of: active, pending, all",
        )
    stmt = stmt.order_by(DiscoveredRelationDB.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    return [
        {
            "id": r.id,
            "subject": r.subject,
            "predicate": r.predicate,
            "object": r.object,
            "subject_iri": r.subject_iri,
            "object_iri": r.object_iri,
            "subject_type": r.subject_type,
            "object_type": r.object_type,
            "evidence_quote": r.evidence_quote,
            "paper_source": r.paper_source,
            "paper_id": r.paper_id,
            "paper_title": r.paper_title,
            "paper_url": r.paper_url,
            "confidence": r.confidence,
            "is_active": r.is_active,
            "approved_by": r.approved_by,
            "approved_at": r.approved_at.isoformat() if r.approved_at else None,
            "rolled_back_at": (
                r.rolled_back_at.isoformat() if r.rolled_back_at else None
            ),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in result.scalars().all()
    ]


@router.post("/deep-search/discovered/{relation_id}:approve")
async def approve_discovered_relation(
    relation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """F6.6 — aprobar manualmente una relación descubierta.

    * Admin-only.
    * Setea ``is_active=True`` + ``approved_by`` + ``approved_at``.
    * Idempotente: si ya estaba aprobada → 200 con misma shape.
    * No permite reaprobar una relación revocada por rollback (hay que
      volver a crear el descubrimiento).
    """
    actor = _require_admin(request)
    rel = await db.get(DiscoveredRelationDB, relation_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relation not found")
    if rel.rolled_back_at is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Relation was rolled back; re-run DeepSearch to rediscover "
                "before re-approving."
            ),
        )
    if not rel.is_active:
        rel.is_active = True
        rel.approved_by = actor
        rel.approved_at = datetime.now(UTC)
        await db.commit()
        logger.info("Relation %s approved by %s", relation_id, actor)
    return {
        "id": rel.id,
        "is_active": rel.is_active,
        "approved_by": rel.approved_by,
        "approved_at": rel.approved_at.isoformat() if rel.approved_at else None,
    }


@router.post("/deep-search/discovered/{relation_id}:reject")
async def reject_discovered_relation(
    relation_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    db: AsyncSession = Depends(get_db),
):
    """F6.6 — rechazar una relación individual.

    Mismo efecto que un rollback pero scope relación única. Requiere
    ``reason`` (min 8 chars).
    """
    actor = _require_admin(request)
    reason = ((body or {}).get("reason") or "").strip()
    if len(reason) < 8:
        raise HTTPException(
            status_code=400,
            detail="A 'reason' of at least 8 chars is required to reject",
        )
    rel = await db.get(DiscoveredRelationDB, relation_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relation not found")
    rel.is_active = False
    rel.rolled_back_by = actor
    rel.rolled_back_at = datetime.now(UTC)
    rel.rollback_reason = reason
    await db.commit()
    logger.info("Relation %s rejected by %s: %s", relation_id, actor, reason)
    return {
        "id": rel.id,
        "is_active": rel.is_active,
        "rolled_back_by": rel.rolled_back_by,
        "rolled_back_at": rel.rolled_back_at.isoformat(),
        "rollback_reason": rel.rollback_reason,
    }
