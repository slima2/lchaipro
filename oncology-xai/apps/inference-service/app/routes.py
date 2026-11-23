"""Inference API routes — v2 (ABMIL + Choquet + SHAP Decomposition)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from oncology_common.authz import require_case_access
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import MLJobDB, ResultBundleDB, XAIArtifactDB

# Nota: ``process_image_task`` se importa lazy (dentro de :process) para que
# los tests y los endpoints de solo-lectura no carguen torch / openslide.

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Inference"])


# ── Identity helpers (F6.1 / F6.5) ──────────────────────────────────────


def _actor(request: Request) -> str:
    return request.headers.get("x-user-id", "unknown")


def _roles(request: Request) -> set[str]:
    raw = request.headers.get("x-roles", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def _is_admin(request: Request) -> bool:
    return "admin" in _roles(request)


# ── Image lookup (server-side, no confiar en el body) ───────────────────


async def _load_confirmed_image(db: AsyncSession, image_id: str) -> tuple[str, str]:
    """Resuelve ``(case_id, storage_uri)`` desde la tabla ``images``.

    Exige que la imagen exista y esté *confirmada* (``checksum`` distinto
    del placeholder ``"pending-upload"`` que deja ``request_upload``). Así
    cerramos el escenario del pentest §6.1: un clinician inyectaba
    ``case_id`` ajeno en el body y procesaba una imagen con UUID
    inventado (``00000000-0000-0000-0000-0000000000a1``) para disparar
    la cascada destructiva al cancelar.
    """
    row = (
        await db.execute(
            text("SELECT case_id, checksum, storage_uri FROM images WHERE id = :id"),
            {"id": image_id},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Image not found")
    case_id, checksum, storage_uri = row
    if not checksum or checksum == "pending-upload":
        raise HTTPException(
            status_code=409,
            detail=(
                "Image upload not confirmed yet. "
                "Call /cases/{case_id}/images:confirm-upload first."
            ),
        )
    return case_id, storage_uri


@router.post("/images/{image_id}:process", status_code=status.HTTP_202_ACCEPTED)
async def process_image(
    image_id: str,
    request: Request,
    body: dict[str, Any] | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Start async inference job (DERCAS UC-IMG-02).

    * Resuelve ``case_id`` desde la imagen en DB — el cliente ya no puede
      inyectarlo (pentest §6.1 / §6.4). Si el body lo trae y no coincide
      con el real, se registra un warning y se usa el de la DB.
    * Rechaza imágenes inexistentes o sin confirmar.
    * Los ``thresholds`` del body se ignoran: usar siempre los presets
      del backend (pentest §6.4 — ``pattern=0.0`` hacía todo "concluyente").
    """
    body = body or {}

    # F6.1 — resolver case_id server-side. Si el body trae uno distinto,
    # loguear para forense (intento de BOLA), pero seguir con el real.
    real_case_id, _ = await _load_confirmed_image(db, image_id)
    claimed = body.get("case_id")
    if claimed and claimed != real_case_id:
        logger.warning(
            "process_image: body.case_id=%r != image.case_id=%r (user=%s)",
            claimed,
            real_case_id,
            _actor(request),
        )

    # F6.5 — además de resolver, verificar que el caller tiene acceso al
    # caso. Sin esto, cualquier clinician que conozca un image_id de otro
    # podía dispararle jobs de inferencia.
    await require_case_access(db, request, real_case_id)

    # F6.4 — thresholds del usuario no se propagan al pipeline.
    # El worker usa los defaults de settings. Si el body venía con
    # ``thresholds``, se descarta silenciosamente (no romper la API
    # pero ignorar el input).
    if "thresholds" in body:
        logger.info(
            "process_image: ignoring client-supplied thresholds (user=%s)",
            _actor(request),
        )

    job_id = str(uuid4())
    job = MLJobDB(
        id=job_id,
        case_id=real_case_id,
        image_id=image_id,
        job_type="IMAGE_INFERENCE",
        status="PENDING",
        created_by=_actor(request),
    )
    db.add(job)
    await db.commit()

    # Dispatch Celery task — thresholds=None fuerza defaults en el worker.
    from app.tasks import process_image_task  # lazy: evita torch en boot

    process_image_task.delay(job_id, image_id, real_case_id, None)

    return {
        "job_id": job_id,
        "image_id": image_id,
        "case_id": real_case_id,
        "status": "PENDING",
        "message": "Inference job queued",
    }


@router.post("/jobs/{job_id}:cancel")
async def cancel_job(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Cancel a running job.

    * Ownership (pentest §6.1): solo el creador del job o un ``admin``
      pueden cancelarlo. Otro clinician → 403.
    * **Ya NO ejecuta cascada destructiva** sobre el caso. La cancelación
      marca el job como ``CANCELLED`` y revoca la tarea Celery.
      Para borrar un caso hay que llamar al endpoint admin-only
      ``DELETE /cases/{id}`` (soft-delete, F6.2).
    """
    job = await db.get(MLJobDB, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    actor = _actor(request)
    if job.created_by and job.created_by != actor and not _is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="Only the job creator or an admin can cancel this job",
        )

    if job.status in ("COMPLETED", "FAILED", "CANCELLED"):
        # Idempotente para jobs ya terminados.
        return {
            "status": job.status.lower(),
            "job_id": job_id,
            "case_id": job.case_id,
            "cleaned": False,
        }

    if job.celery_task_id:
        try:
            from app.celery_app import celery

            celery.control.revoke(job.celery_task_id, terminate=True)
        except Exception:
            logger.debug(
                "could not revoke celery task", exc_info=True
            )  # antes se ignoraba en silencio (Bandit S110)

    job.status = "CANCELLED"
    job.ended_at = datetime.now(UTC)
    await db.commit()

    logger.info(
        "Cancelled job %s by %s (case_id=%s) — no cascade", job_id, actor, job.case_id
    )
    return {
        "status": "cancelled",
        "job_id": job_id,
        "case_id": job.case_id,
        "cleaned": False,  # mantener la clave por compat; pero ahora SIEMPRE false
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Poll job status.

    F6.5 — exige que el caller tenga acceso al caso del job, no solo que
    conozca el ``job_id``.
    """
    job = await db.get(MLJobDB, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.case_id:
        await require_case_access(db, request, job.case_id)
    return {
        "job_id": job.id,
        "image_id": job.image_id,
        "status": job.status,
        "progress": job.progress,
        "result_bundle_id": job.result_bundle_id,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "stage": job.error_detail if job.status == "RUNNING" else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@router.get("/images/{image_id}/results/latest")
async def get_latest_results(
    image_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Get latest ResultBundle for an image.

    F6.5 — ownership por caso de la imagen. Un clinician no puede
    enumerar resultados de imágenes ajenas conociendo el ``image_id``.
    """
    # Resolver case_id desde la imagen y validar acceso
    img_case_id, _ = await _load_confirmed_image(db, image_id)
    await require_case_access(db, request, img_case_id)

    stmt = (
        select(ResultBundleDB)
        .where(ResultBundleDB.image_id == image_id)
        .options(
            selectinload(ResultBundleDB.pattern_results),
            selectinload(ResultBundleDB.genetic_results),
            selectinload(ResultBundleDB.xai_artifacts),
            selectinload(ResultBundleDB.morphologic_profile),
        )
        .order_by(ResultBundleDB.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    bundle = result.scalar_one_or_none()
    if not bundle:
        raise HTTPException(status_code=404, detail="No results found")
    return _bundle_dict(bundle)


@router.get("/results/{result_bundle_id}")
async def get_result_bundle(
    result_bundle_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(ResultBundleDB)
        .where(ResultBundleDB.id == result_bundle_id)
        .options(
            selectinload(ResultBundleDB.pattern_results),
            selectinload(ResultBundleDB.genetic_results),
            selectinload(ResultBundleDB.xai_artifacts),
            selectinload(ResultBundleDB.morphologic_profile),
        )
    )
    result = await db.execute(stmt)
    bundle = result.scalar_one_or_none()
    if not bundle:
        raise HTTPException(status_code=404, detail="Result bundle not found")
    # F6.5 — ownership por el caso del bundle
    await require_case_access(db, request, bundle.case_id)
    return _bundle_dict(bundle)


@router.get("/results/{result_bundle_id}/artifacts")
async def get_artifacts(
    result_bundle_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Primero resolver el case_id del bundle para el ownership check.
    row = (
        await db.execute(
            text("SELECT case_id FROM result_bundles WHERE id = :rid"),
            {"rid": result_bundle_id},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Result bundle not found")
    await require_case_access(db, request, row[0])

    stmt = select(XAIArtifactDB).where(
        XAIArtifactDB.result_bundle_id == result_bundle_id
    )
    result = await db.execute(stmt)
    return [
        {
            "artifact_id": a.id,
            "artifact_type": a.artifact_type,
            "gene": a.gene,
            "uri": a.uri,
            "hash": a.hash,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in result.scalars().all()
    ]


@router.get("/checkpoints/status")
async def get_checkpoint_status():
    """Return which v2 checkpoints are loaded per gene."""
    try:
        from app.ml.checkpoints.loader import CheckpointLoader

        loader = CheckpointLoader(settings.v2_checkpoint_dir, "cpu")
        return loader.get_checkpoint_status()
    except Exception as e:
        return {"error": str(e), "checkpoint_dir": settings.v2_checkpoint_dir}


@router.get("/parameters")
async def get_parameters():
    """Return current system parameters."""
    from app.ml.checkpoints.loader import (
        AUROC_THRESHOLD,
        BEST_FOLD,
        BEST_METHOD,
        GENE_MUTATION_THRESHOLD,
        PROPOSED_AUROC,
    )

    return {
        "auroc_values": PROPOSED_AUROC,
        "auroc_threshold": AUROC_THRESHOLD,
        "best_method": BEST_METHOD,
        "best_fold": BEST_FOLD,
        "mutation_threshold": settings.mutation_threshold,
        "gene_mutation_thresholds": GENE_MUTATION_THRESHOLD,
        "top_k_tiles": settings.v2_top_k_tiles,
        "max_tiles": settings.v2_max_tiles_wsi,
        "permutation_repeats": 10,
    }


# F6.4 — allowlist de rangos admitidos para los parámetros del pipeline.
#
# Antes cualquier clinician podía llamar ``PUT /parameters`` con
# ``mutation_threshold=0.0`` y dejar todas las predicciones futuras
# marcadas como "concluyentes" para TODO el deployment (variable de
# proceso, no por caso). Dos cambios:
#
# 1. El endpoint requiere rol ``admin`` en el servicio (ya lo pedía el
#    gateway como clinician+admin — bajamos a admin only en runtime).
# 2. Cada campo tiene un rango válido. Valores fuera de rango → 400.
#    El worker (celery) no lee directamente del body; lee de
#    ``settings.*`` / módulos globales, así que el único vector es este
#    endpoint.
_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "auroc_threshold": (0.5, 0.95),
    "mutation_threshold": (0.3, 0.9),
    "pattern_threshold": (0.3, 0.9),
    "per_gene_auroc": (0.5, 0.99),
    "per_gene_mutation": (0.3, 0.9),
    # Enteros
    "top_k_tiles": (1, 500),
    "max_tiles": (1, 20_000),
}

_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"abmil", "choquet", "xgboost", "pattern_informed", "fuzzy_choquet"}
)


def _clamp_float(name: str, raw: Any) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name}: must be a number")
    lo, hi = _PARAM_RANGES[name]
    if not (lo <= val <= hi):
        raise HTTPException(
            status_code=400,
            detail=f"{name}={val} out of allowed range [{lo}, {hi}]",
        )
    return val


def _clamp_int(name: str, raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name}: must be an integer")
    lo, hi = _PARAM_RANGES[name]
    if not (lo <= val <= hi):
        raise HTTPException(
            status_code=400,
            detail=f"{name}={val} out of allowed range [{int(lo)}, {int(hi)}]",
        )
    return val


@router.put("/parameters")
async def update_parameters(request: Request, body: dict[str, Any]):
    """Update system parameters at runtime.

    F6.4 — admin-only + rangos validados. Los valores fuera del
    allowlist se rechazan con 400 y no afectan al estado global.
    """
    if not _is_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required to mutate pipeline parameters",
        )

    import app.ml.checkpoints.loader as loader_mod

    updated: list[str] = []
    actor = _actor(request)

    if "auroc_values" in body and isinstance(body["auroc_values"], dict):
        for gene, raw in body["auroc_values"].items():
            if gene not in loader_mod.PROPOSED_AUROC:
                continue  # ignorar genes desconocidos
            val = _clamp_float("per_gene_auroc", raw)
            loader_mod.PROPOSED_AUROC[gene] = val
            updated.append(f"AUROC[{gene}]={val}")

    if "auroc_threshold" in body:
        val = _clamp_float("auroc_threshold", body["auroc_threshold"])
        loader_mod.AUROC_THRESHOLD = val
        updated.append(f"threshold={val}")

    if "best_method" in body and isinstance(body["best_method"], dict):
        for gene, method in body["best_method"].items():
            if gene not in loader_mod.BEST_METHOD:
                continue
            method_str = str(method).lower()
            if method_str not in _ALLOWED_METHODS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"best_method[{gene}]={method!r} not in allowed "
                        f"set {sorted(_ALLOWED_METHODS)}"
                    ),
                )
            loader_mod.BEST_METHOD[gene] = method_str
            updated.append(f"method[{gene}]={method_str}")

    if "mutation_threshold" in body:
        val = _clamp_float("mutation_threshold", body["mutation_threshold"])
        settings.mutation_threshold = val
        updated.append(f"mutation_threshold={val}")

    if "gene_mutation_thresholds" in body and isinstance(
        body["gene_mutation_thresholds"], dict
    ):
        for gene, raw in body["gene_mutation_thresholds"].items():
            if gene not in loader_mod.GENE_MUTATION_THRESHOLD:
                continue
            val = _clamp_float("per_gene_mutation", raw)
            loader_mod.GENE_MUTATION_THRESHOLD[gene] = val
            updated.append(f"gene_threshold[{gene}]={val}")

    if "top_k_tiles" in body:
        settings.v2_top_k_tiles = _clamp_int("top_k_tiles", body["top_k_tiles"])
        updated.append(f"top_k_tiles={settings.v2_top_k_tiles}")

    if "max_tiles" in body:
        settings.v2_max_tiles_wsi = _clamp_int("max_tiles", body["max_tiles"])
        updated.append(f"max_tiles={settings.v2_max_tiles_wsi}")

    logger.info("Parameters updated by %s: %s", actor, updated)
    return {"status": "updated", "changes": updated, "actor": actor}


_DISALLOWED_PATTERN_NAMES = frozenset({"mucinous"})


def _pattern_results_public(prs: list | None) -> list[dict]:
    """Drop legacy disallowed pattern rows from API payloads."""
    if not prs:
        return []
    out: list[dict] = []
    for pr in prs:
        name = (getattr(pr, "pattern", None) or "").lower()
        if name in _DISALLOWED_PATTERN_NAMES:
            continue
        out.append(
            {
                "pattern": pr.pattern,
                "score": pr.score,
                "percentage": pr.percentage,
                "is_conclusive": pr.is_conclusive,
                "overlay_uri": pr.overlay_uri,
            }
        )
    return out


def _predominant_public(raw: str | None, prs: list | None) -> str | None:
    if raw and raw.lower() not in _DISALLOWED_PATTERN_NAMES:
        return raw
    filtered = [
        pr
        for pr in (prs or [])
        if (getattr(pr, "pattern", None) or "").lower() not in _DISALLOWED_PATTERN_NAMES
    ]
    if not filtered:
        return None
    top = max(filtered, key=lambda x: getattr(x, "percentage", 0.0) or 0.0)
    return getattr(top, "pattern", None)


def _sanitize_choquet_payload(payload: dict | None) -> dict | None:
    if not payload:
        return payload
    out = dict(payload)
    sv = out.get("shapley_values")
    if isinstance(sv, dict):
        out["shapley_values"] = {
            k: v for k, v in sv.items() if k.lower() not in _DISALLOWED_PATTERN_NAMES
        }
    ix = out.get("interaction_indices")
    if isinstance(ix, dict):
        cleaned: dict[str, float] = {}
        for pair, val in ix.items():
            parts = pair.split("_", 1)
            if len(parts) == 2 and all(
                p.lower() not in _DISALLOWED_PATTERN_NAMES for p in parts
            ):
                cleaned[pair] = val
        out["interaction_indices"] = cleaned
    return out


def _bundle_dict(b: ResultBundleDB) -> dict:
    mp = b.morphologic_profile
    prs = b.pattern_results or []
    pr_public = _pattern_results_public(prs)
    pred_pub = _predominant_public(b.predominant_pattern, prs)
    return {
        "result_bundle_id": b.id,
        "case_id": b.case_id,
        "image_id": b.image_id,
        "job_id": b.job_id,
        "model_profile": b.model_profile,
        "model_version": b.model_version,
        "pipeline_version": getattr(b, "pipeline_version", None) or "1.0.0",
        "use_choquet": getattr(b, "use_choquet", None),
        "thresholds": b.thresholds,
        "pattern_composition": b.pattern_composition,
        "predominant_pattern": pred_pub,
        "evidence_source": b.evidence_source,
        "intended_use": b.intended_use,
        "pattern_results": pr_public,
        "genetic_results": [
            {
                "mutation": gr.mutation,
                "score": gr.score,
                "status": gr.status,
                "confidence_label": getattr(gr, "confidence_label", None),
                "auroc_threshold": getattr(gr, "auroc_threshold", None),
                "prediction_method": getattr(gr, "prediction_method", None),
                "disclaimer": getattr(gr, "disclaimer", None),
                "shap_decomposition": {
                    "embedding_contribution_pct": getattr(
                        gr, "shap_embedding_pct", None
                    ),
                    "pattern_contribution_pct": getattr(gr, "shap_pattern_pct", None),
                    "top_pattern_dims": getattr(gr, "shap_top_patterns", None),
                    "pattern_shap_signed": getattr(gr, "shap_pattern_signed", None),
                    "pattern_shap_directions": getattr(
                        gr, "shap_pattern_directions", None
                    ),
                }
                if getattr(gr, "shap_embedding_pct", None) is not None
                else None,
                "choquet_shapley": _sanitize_choquet_payload(
                    {
                        "shapley_values": getattr(gr, "choquet_shapley_values", None),
                        "interaction_indices": getattr(
                            gr, "choquet_interaction_indices", None
                        ),
                    }
                )
                if getattr(gr, "choquet_shapley_values", None) is not None
                else None,
                "ablation": getattr(gr, "ablation_data", None),
                "permutation": getattr(gr, "permutation_data", None),
                "evidence_source": gr.evidence_source,
                "intended_use": gr.intended_use,
            }
            for gr in (b.genetic_results or [])
        ],
        "morphologic_profile": {
            "n_tiles_total": mp.n_tiles_total,
            "pct_lepidic": mp.pct_lepidic,
            "pct_acinar": mp.pct_acinar,
            "pct_papillary": mp.pct_papillary,
            "pct_micropapillary": mp.pct_micropapillary,
            "pct_solid": mp.pct_solid,
            # Stored in DB column pct_mucinous (legacy name); value is cribriform % from pipeline
            "pct_cribriform": mp.pct_mucinous,
        }
        if mp
        else None,
        "attention_overlay_uri": getattr(b, "attention_overlay_uri", None),
        "xai_artifacts": [
            {"artifact_id": a.id, "type": a.artifact_type, "gene": a.gene, "uri": a.uri}
            for a in (b.xai_artifacts or [])
        ],
        "disclaimers": [
            "This is a research tool, NOT a clinical diagnostic device.",
            "v2 mutation predictions use Pattern-Informed ABMIL + Fuzzy Choquet MIL (thesis Artifacts 2 & 3).",
            "Inconclusive genes (AUROC < 0.70) require molecular testing for confirmation.",
            "Always confirm with standard molecular testing.",
        ],
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


@router.post("/gene-explain")
async def gene_explain(body: dict[str, Any]):
    """Generate LLM explanation for a gene prediction using backend OpenAI key."""
    import os

    import httpx

    prompt = body.get("prompt", "")
    language = body.get("language", "en")
    lang_map = {
        "en": "English",
        "es": "Spanish",
        "de": "German",
        "fr": "French",
        "pt": "Portuguese",
    }
    lang_name = lang_map.get(language, "English")

    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key or not prompt:
        return {"explanation": None}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.3,
                    "max_tokens": 400,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"You are an expert computational pathologist explaining AI mutation predictions. Be concise and clinical (4-6 sentences). Respond in {lang_name}.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                txt = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"explanation": txt}
    except Exception:
        logger.debug(
            "LLM explanation request failed", exc_info=True
        )  # antes se ignoraba en silencio (Bandit S110)
    return {"explanation": None}
