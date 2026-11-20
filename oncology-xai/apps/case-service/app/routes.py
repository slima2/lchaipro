"""Patient and Case CRUD routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from event_contracts.envelope import EventEnvelope
from event_contracts.publisher import EventPublisher
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from oncology_common.authz import require_case_access
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import CaseDB, PatientDB

logger = logging.getLogger(__name__)

patients_router = APIRouter(prefix="/api/v1", tags=["Patients"])
cases_router = APIRouter(prefix="/api/v1", tags=["Cases"])


# ── Identity / authorization helpers (F6.2) ────────────────────────────
#
# El gateway inyecta ``X-User-Id`` y ``X-Roles`` (el cliente no puede: están
# en ``_CLIENT_FORBIDDEN_HEADERS`` del proxy). El servicio confía en esas
# cabeceras sólo cuando se recibe del gateway interno; en pre-prod se añade
# NetworkPolicy para impedir que pods laterales las falsifiquen.


def _actor(request: Request) -> str:
    return request.headers.get("x-user-id", "unknown")


def _roles(request: Request) -> set[str]:
    raw = request.headers.get("x-roles", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def _require_admin(request: Request) -> str:
    """Defense-in-depth: aunque el gateway ya exige admin para estos DELETE,
    comprobamos el rol en el servicio para que un pod comprometido en el
    cluster no pueda saltarse el control inyectando ``X-User-Id`` a mano
    (F6.2 + mitigación de F002)."""
    if "admin" not in _roles(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required for this operation",
        )
    return _actor(request)


# ── Audit publishing (F6.2) ─────────────────────────────────────────────


def _publish_audit(event_type: str, entity_id: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget audit event. No romper el request si RabbitMQ no está."""
    try:
        pub = EventPublisher(settings.rabbitmq_url)
        pub.publish(
            EventEnvelope(
                event_type=event_type,
                producer="case-service",
                case_id=payload.get("case_id", entity_id),
                payload={"entity_id": entity_id, **payload},
            )
        )
    except Exception:
        logger.warning("Failed to publish audit event %s", event_type, exc_info=True)


# ── F6.5 — Saga de case.deleted: limpiar derivados fuera de "cases" ────


async def _cleanup_case_derivatives(
    db: AsyncSession, case_id: str, actor: str, reason: str, now
) -> dict[str, int | bool | str]:
    """Cascada best-effort tras soft-delete de un caso.

    1. Marca sus **imagenes** como soft-deleted (ocultas en GETs).
    2. **Borra** los snapshots del grafo del caso (derivados, re-generables).
    3. Borra los objetos **S3** bajo ``images/{cid}/`` y ``results/{cid}/``
       vía ``StorageClient.delete_by_prefix`` — sincrónico pero best-effort:
       si falla se loguea y se emite ``case.storage-cleanup-failed`` para que
       un worker reintente, sin bloquear el soft-delete en DB.

    Retorna métricas para la auditoría del `case.deleted`.
    """
    stats: dict[str, int | bool | str] = {
        "images_soft_deleted": 0,
        "graph_snapshots_deleted": 0,
        "storage_deleted": 0,
        "storage_cleanup_ok": True,
    }

    # Imágenes — mismas conexión y transacción que el case.
    # La tabla ``images`` vive en otro servicio y puede no existir en
    # entornos de test que solo importan los modelos de case-service; en
    # ese caso se degrada silenciosamente para no romper el soft-delete.
    try:
        res = await db.execute(
            text(
                "UPDATE images SET deleted_at = :ts, deleted_by = :by, "
                "delete_reason = :why "
                "WHERE case_id = :cid AND deleted_at IS NULL"
            ),
            {
                "ts": now,
                "by": actor,
                "why": f"cascade from case.deleted: {reason}",
                "cid": case_id,
            },
        )
        stats["images_soft_deleted"] = res.rowcount or 0
    except Exception:
        logger.debug("images cleanup skipped (table missing?)", exc_info=True)

    # Graph snapshots — se regeneran, no se conservan tras el delete
    try:
        res = await db.execute(
            text("DELETE FROM case_graph_snapshots WHERE case_id = :cid"),
            {"cid": case_id},
        )
        stats["graph_snapshots_deleted"] = res.rowcount or 0
    except Exception:
        logger.debug(
            "case_graph_snapshots cleanup skipped (table missing?)",
            exc_info=True,
        )

    # S3 — best-effort, fuera de la transacción conceptual; no re-raise
    stats.update(_cleanup_case_storage(case_id))
    return stats


def _cleanup_case_storage(case_id: str) -> dict[str, int | bool | str]:
    """Borrar prefijos S3 del caso. Devuelve métricas para auditoría."""
    if not (settings.s3_endpoint and settings.s3_bucket and settings.s3_access_key):
        logger.warning(
            "S3 cleanup skipped for case %s: storage not configured", case_id
        )
        return {
            "storage_deleted": 0,
            "storage_cleanup_ok": False,
            "storage_cleanup_reason": "not-configured",
        }

    try:
        from oncology_common.storage import (
            StorageClient,  # import local: evita boto3 en tests mínimos
        )

        storage = StorageClient(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key or "",
            secret_key=settings.s3_secret_key or "",
            bucket=settings.s3_bucket,
        )
        total = 0
        for prefix in (f"images/{case_id}/", f"results/{case_id}/"):
            total += storage.delete_by_prefix(prefix)
        return {"storage_deleted": total, "storage_cleanup_ok": True}
    except Exception as exc:
        logger.error("S3 cleanup failed for case %s: %s", case_id, exc, exc_info=True)
        # Emite evento para que un reaper lo reintente; NO re-raise:
        # queremos que el soft-delete en DB persista.
        _publish_audit(
            "case.storage-cleanup-failed",
            case_id,
            {
                "case_id": case_id,
                "error": str(exc),
                "action": "case.storage-cleanup-failed",
            },
        )
        return {
            "storage_deleted": 0,
            "storage_cleanup_ok": False,
            "storage_cleanup_reason": str(exc)[:200],
        }


# ── Patients ───────────────────────────────────────────────────────────


@patients_router.post("/patients", status_code=status.HTTP_201_CREATED)
async def create_patient(
    body: dict[str, Any], request: Request, db: AsyncSession = Depends(get_db)
):
    patient = PatientDB(
        id=str(uuid4()),
        external_id=body.get("external_id"),
        demographics=body.get("demographics", {}),
    )
    db.add(patient)
    await db.commit()
    await db.refresh(patient)
    _publish_audit(
        "patient.created",
        patient.id,
        {"user_id": _actor(request), "action": "patient.created"},
    )
    return _patient_dict(patient)


@patients_router.get("/patients")
async def list_patients(
    request: Request,
    query: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List patients.

    F6.4 — un clinician solo ve pacientes con al menos un caso suyo
    (creado por él y no soft-deleted). admin / auditor ven todos. Antes
    un clinician veía la lista completa, incluyendo pacientes de otros.
    """
    stmt = select(PatientDB).where(PatientDB.deleted_at.is_(None))
    if query:
        stmt = stmt.where(PatientDB.external_id.ilike(f"%{query}%"))

    if "admin" not in _roles(request) and "auditor" not in _roles(request):
        stmt = stmt.where(
            select(CaseDB.id)
            .where(
                CaseDB.patient_id == PatientDB.id,
                CaseDB.created_by == _actor(request),
                CaseDB.deleted_at.is_(None),
            )
            .exists()
        )
    result = await db.execute(stmt)
    return [_patient_dict(p) for p in result.scalars().all()]


@patients_router.get("/patients/{patient_id}")
async def get_patient(
    patient_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    patient = await db.get(PatientDB, patient_id)
    if not patient or patient.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Patient not found")

    # F6.4 — ownership: clinician solo accede si tiene al menos un caso
    # activo sobre el paciente. admin / auditor pueden ver cualquiera.
    if "admin" not in _roles(request) and "auditor" not in _roles(request):
        has_case = (
            await db.execute(
                select(CaseDB.id).where(
                    CaseDB.patient_id == patient_id,
                    CaseDB.created_by == _actor(request),
                    CaseDB.deleted_at.is_(None),
                )
            )
        ).first()
        if not has_case:
            # 404 (no revelar existencia) — mismo patrón que authz
            raise HTTPException(status_code=404, detail="Patient not found")
    return _patient_dict(patient)


@patients_router.delete("/patients/{patient_id}", status_code=status.HTTP_200_OK)
async def delete_patient(
    patient_id: str,
    body: dict[str, Any] | None = None,
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a patient and cascade-soft-delete all its cases.

    * **Rol requerido:** admin (validado en api-gateway y re-verificado aquí).
    * **Body obligatorio:** ``{"reason": "..."}`` para auditoría/cumplimiento.
    * Emite ``patient.deleted`` (+ ``case.deleted`` por cada caso del paciente)
      en el bus de eventos para que audit-service los persista.

    No se hace hard-delete: los datos clínicos quedan recuperables dentro de
    la ventana de retención; el purgado físico se hace por tarea controlada
    (``scripts/purge_soft_deleted.py``), no desde la API pública.
    """
    actor = _require_admin(request)
    reason = (body or {}).get("reason", "").strip() if body else ""
    if len(reason) < 8:
        raise HTTPException(
            status_code=400,
            detail="A 'reason' of at least 8 chars is required for deletion",
        )

    patient = await db.get(PatientDB, patient_id)
    if not patient or patient.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Patient not found")

    now = datetime.now(UTC)

    # Soft-delete en cascada de casos activos del paciente (misma transacción)
    case_rows = await db.execute(
        select(CaseDB).where(
            CaseDB.patient_id == patient_id, CaseDB.deleted_at.is_(None)
        )
    )
    cases = list(case_rows.scalars().all())
    cascade_stats: dict[str, dict] = {}
    for c in cases:
        c.deleted_at = now
        c.deleted_by = actor
        c.delete_reason = f"cascade from patient.deleted: {reason}"
        # F6.5 — cada caso arrastra sus imágenes, snapshots y S3
        cascade_stats[c.id] = await _cleanup_case_derivatives(
            db, c.id, actor, reason, now
        )

    patient.deleted_at = now
    patient.deleted_by = actor
    patient.delete_reason = reason
    await db.commit()

    logger.info(
        "Soft-deleted patient %s by %s (reason=%r); cascaded %d cases",
        patient_id,
        actor,
        reason,
        len(cases),
    )

    # Eventos: uno por caso + uno por paciente
    for c in cases:
        _publish_audit(
            "case.deleted",
            c.id,
            {
                "case_id": c.id,
                "patient_id": patient_id,
                "user_id": actor,
                "reason": c.delete_reason,
                "soft_delete": True,
                "cascade_from": "patient",
                "action": "case.deleted",
                **cascade_stats.get(c.id, {}),
            },
        )
    _publish_audit(
        "patient.deleted",
        patient_id,
        {
            "patient_id": patient_id,
            "user_id": actor,
            "reason": reason,
            "soft_delete": True,
            "cascaded_cases": [c.id for c in cases],
            "action": "patient.deleted",
        },
    )

    return {
        "patient_id": patient_id,
        "soft_deleted": True,
        "cascaded_cases": len(cases),
        "deleted_by": actor,
        "deleted_at": now.isoformat(),
    }


def _patient_dict(p: PatientDB) -> dict:
    return {
        "patient_id": p.id,
        "external_id": p.external_id,
        "demographics": p.demographics or {},
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ── Cases ──────────────────────────────────────────────────────────────


@cases_router.post("/cases", status_code=status.HTTP_201_CREATED)
async def create_case(
    body: dict[str, Any], request: Request, db: AsyncSession = Depends(get_db)
):
    patient = await db.get(PatientDB, body.get("patient_id", ""))
    if not patient or patient.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Patient not found")
    case = CaseDB(
        id=str(uuid4()),
        patient_id=patient.id,
        tags=body.get("tags", []),
        metadata_json=body.get("metadata", {}),
        # F6.4 — created_by desde la identidad validada, nunca del body.
        created_by=_actor(request),
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    _publish_audit(
        "case.created",
        case.id,
        {
            "case_id": case.id,
            "patient_id": patient.id,
            "user_id": _actor(request),
            "action": "case.created",
        },
    )
    return _case_dict(case)


@cases_router.get("/cases")
async def list_cases(
    request: Request,
    patient_id: str | None = Query(None, alias="patientId"),
    db: AsyncSession = Depends(get_db),
):
    """List cases.

    F6.4 — un clinician solo ve casos que creó él. admin / auditor ven
    todos. Antes era visible la lista completa.
    """
    stmt = select(CaseDB).where(CaseDB.deleted_at.is_(None))
    if patient_id:
        stmt = stmt.where(CaseDB.patient_id == patient_id)
    if "admin" not in _roles(request) and "auditor" not in _roles(request):
        stmt = stmt.where(CaseDB.created_by == _actor(request))
    result = await db.execute(stmt)
    return [_case_dict(c) for c in result.scalars().all()]


@cases_router.get("/cases/{case_id}")
async def get_case(case_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    # F6.4 — ownership: clinician solo ve casos propios; supervisores
    # ven cualquiera. 404 si no existe o soft-deleted (consistente con
    # el patrón del helper authz).
    await require_case_access(db, request, case_id)
    case = await db.get(CaseDB, case_id)
    assert case is not None  # require_case_access ya filtró
    return _case_dict(case)


@cases_router.delete("/cases/{case_id}", status_code=status.HTTP_200_OK)
async def delete_case(
    case_id: str,
    body: dict[str, Any] | None = None,
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a single case. Ver ``delete_patient`` para política general."""
    actor = _require_admin(request)
    reason = (body or {}).get("reason", "").strip() if body else ""
    if len(reason) < 8:
        raise HTTPException(
            status_code=400,
            detail="A 'reason' of at least 8 chars is required for deletion",
        )

    case = await db.get(CaseDB, case_id)
    if not case or case.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Case not found")

    now = datetime.now(UTC)
    case.deleted_at = now
    case.deleted_by = actor
    case.delete_reason = reason
    # F6.5 — cascada de imágenes, snapshots y objetos S3 del caso
    cascade_stats = await _cleanup_case_derivatives(db, case_id, actor, reason, now)
    await db.commit()

    logger.info(
        "Soft-deleted case %s by %s (reason=%r); cascade=%s",
        case_id,
        actor,
        reason,
        cascade_stats,
    )
    _publish_audit(
        "case.deleted",
        case_id,
        {
            "case_id": case_id,
            "patient_id": case.patient_id,
            "user_id": actor,
            "reason": reason,
            "soft_delete": True,
            "action": "case.deleted",
            **cascade_stats,
        },
    )
    return {
        "case_id": case_id,
        "soft_deleted": True,
        "deleted_by": actor,
        "deleted_at": now.isoformat(),
        **cascade_stats,
    }


@cases_router.patch("/cases/{case_id}")
async def update_case(
    case_id: str,
    body: dict[str, Any],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Patch a case.

    F6.4 — ownership: solo el creador del caso o un admin pueden mutar.
    Un clinician no puede editar casos de otro aunque conozca su id.
    Rechaza además intentos de reasignar ``created_by`` o resucitar
    casos soft-deleted desde el body.
    """
    # Primero chequeo de ownership (404 si no existe / está soft-deleted,
    # 403 si es de otro clinician y no soy supervisor).
    await require_case_access(db, request, case_id)

    case = await db.get(CaseDB, case_id)
    # require_case_access ya filtró deleted_at / existencia; esto es solo
    # para el linter.
    assert case is not None and case.deleted_at is None

    # F6.4 — allowlist estricto de campos editables.
    # NO permitimos tocar ``created_by`` ni ``patient_id`` desde el PATCH:
    # cambiarlos equivaldría a transferir un caso de dueño/paciente desde
    # un endpoint de "mutación menor".
    for forbidden in ("created_by", "patient_id", "deleted_at", "deleted_by"):
        if forbidden in body:
            raise HTTPException(
                status_code=400,
                detail=f"Field '{forbidden}' cannot be modified via PATCH",
            )

    if "status" in body:
        case.status = body["status"]
    if "tags" in body:
        case.tags = body["tags"]
    if "metadata" in body:
        case.metadata_json = body["metadata"]
    await db.commit()
    await db.refresh(case)
    return _case_dict(case)


def _case_dict(c: CaseDB) -> dict:
    return {
        "case_id": c.id,
        "patient_id": c.patient_id,
        "status": c.status,
        "tags": c.tags or [],
        "metadata": c.metadata_json or {},
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
