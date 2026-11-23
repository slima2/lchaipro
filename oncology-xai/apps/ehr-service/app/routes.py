"""EHR Service routes (DERCAS 9.5)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import EHRDocumentDB, EHREntityDB, EHRMappingDB
from app.ner import extract_entities, map_entity_to_ontology

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["EHR"])


# F6.3 — límites duros sobre entradas del usuario.
#
# Antes:
#   * ``content`` del body no tenía tope → un POST podía meter 500 MB
#     de texto y detonar el worker al re-tokenizar.
#   * ``extract_entities`` producía N entidades proporcional al tamaño
#     del texto (regex por cada keyword); sin tope, un documento
#     adversarial podía generar cientos de miles de rows en una sola
#     llamada.
#
# Los límites se eligen generosos para notas clínicas largas pero
# bloquean el abuso.
MAX_CONTENT_CHARS = 200_000  # ≈ 40–50 páginas de nota clínica
MAX_ENTITIES_PER_CALL = 5_000


@router.post("/cases/{case_id}/ehr:ingest", status_code=status.HTTP_201_CREATED)
async def ingest_ehr(
    case_id: str, body: dict[str, Any], db: AsyncSession = Depends(get_db)
):
    content = body.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")

    # F6.3 — tope de tamaño en la ingestión
    if len(content) > MAX_CONTENT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"content too large: {len(content)} chars > {MAX_CONTENT_CHARS} max"
            ),
        )

    # Check version
    stmt = (
        select(EHRDocumentDB)
        .where(EHRDocumentDB.case_id == case_id)
        .order_by(EHRDocumentDB.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    latest = result.scalar_one_or_none()
    version = (latest.version + 1) if latest else 1

    doc = EHRDocumentDB(
        id=str(uuid4()),
        case_id=case_id,
        version=version,
        source=body.get("source", "paste"),
        content_text=content,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return _doc_dict(doc)


@router.get("/cases/{case_id}/ehr/versions")
async def list_ehr_versions(case_id: str, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(EHRDocumentDB)
        .where(EHRDocumentDB.case_id == case_id)
        .order_by(EHRDocumentDB.version.desc())
    )
    result = await db.execute(stmt)
    return [_doc_dict(d) for d in result.scalars().all()]


@router.get("/ehr/{ehr_id}")
async def get_ehr(ehr_id: str, db: AsyncSession = Depends(get_db)):
    doc = await db.get(EHRDocumentDB, ehr_id)
    if not doc:
        raise HTTPException(status_code=404, detail="EHR document not found")
    return _doc_dict(doc)


@router.post("/ehr/{ehr_id}:extract-and-map", status_code=status.HTTP_200_OK)
async def extract_and_map(ehr_id: str, db: AsyncSession = Depends(get_db)):
    """Extract entities from EHR and map to ontologies.

    F6.3 — **idempotente**. Si el endpoint se llama N veces sobre el
    mismo documento, el estado final es el mismo que llamarlo una sola
    vez:

    * Para cada entidad extraída, chequeamos la clave
      ``(ehr_id, start, end, entity_type)`` contra ``ehr_entities``:
      si existe, se reusa el id; si no, se inserta.
    * Para cada mapping de esa entidad, idem contra
      ``(entity_id, ontology, iri)``.

    La response trae dos contadores separados: ``*_created`` (nuevas)
    vs ``*_existing`` (ya estaban). El total ``*_count`` es todo lo
    extraído (lo que ahora está en DB), compatible con el shape anterior.

    Además:
    * Rechaza el documento si ``extract_entities`` devuelve más de
      :data:`MAX_ENTITIES_PER_CALL` — bloquea texto adversarial que
      explote la combinatoria del dicccionario de keywords.
    """
    doc = await db.get(EHRDocumentDB, ehr_id)
    if not doc:
        raise HTTPException(status_code=404, detail="EHR not found")

    text = doc.content_text or ""
    entities = extract_entities(text)

    # F6.3 — cap duro al número de entidades extraídas por llamada
    if len(entities) > MAX_ENTITIES_PER_CALL:
        raise HTTPException(
            status_code=422,
            detail=(
                f"too many entities extracted ({len(entities)} > "
                f"{MAX_ENTITIES_PER_CALL}); refusing to persist."
            ),
        )

    # Precargar entidades existentes para evitar N queries individuales.
    existing_rows = (
        await db.execute(
            select(
                EHREntityDB.id,
                EHREntityDB.start,
                EHREntityDB.end,
                EHREntityDB.entity_type,
            ).where(EHREntityDB.ehr_id == ehr_id)
        )
    ).all()
    existing_entity_ids: dict[tuple[int, int, str], str] = {
        (r.start, r.end, r.entity_type): r.id for r in existing_rows
    }

    entities_list: list[dict] = []
    entities_created = 0
    entities_existing = 0
    mappings_list: list[dict] = []
    mappings_created = 0
    mappings_existing = 0

    # Ids de entidades que hemos "tocado" en este call, para precargar
    # sus mappings existentes en bulk.
    touched_ids: set[str] = set()

    # ── 1. Entidades ────────────────────────────────────────────────
    for ent in entities:
        key = (ent.start, ent.end, ent.entity_type)
        if key in existing_entity_ids:
            ent_id = existing_entity_ids[key]
            entities_existing += 1
        else:
            ent_id = str(uuid4())
            db.add(
                EHREntityDB(
                    id=ent_id,
                    ehr_id=ehr_id,
                    text=ent.text,
                    entity_type=ent.entity_type,
                    start=ent.start,
                    end=ent.end,
                    confidence=ent.confidence,
                    section=ent.section,
                )
            )
            existing_entity_ids[key] = ent_id
            entities_created += 1
        touched_ids.add(ent_id)
        entities_list.append(
            {"entity_id": ent_id, "text": ent.text, "type": ent.entity_type}
        )

    # Flush para que los INSERTs de entidades sean visibles a la query
    # de mappings de abajo en la MISMA transacción.
    if entities_created:
        await db.flush()

    # ── 2. Mappings ─────────────────────────────────────────────────
    if touched_ids:
        existing_maps = (
            await db.execute(
                select(
                    EHRMappingDB.entity_id,
                    EHRMappingDB.ontology,
                    EHRMappingDB.iri,
                ).where(EHRMappingDB.entity_id.in_(touched_ids))
            )
        ).all()
        existing_map_keys: set[tuple[str, str, str]] = {
            (r.entity_id, r.ontology, r.iri) for r in existing_maps
        }
    else:
        existing_map_keys = set()

    for ent in entities:
        key = (ent.start, ent.end, ent.entity_type)
        ent_id = existing_entity_ids[key]
        for m in map_entity_to_ontology(ent):
            map_key = (ent_id, m["ontology"], m["iri"])
            if map_key in existing_map_keys:
                mappings_existing += 1
                mappings_list.append({"entity_id": ent_id, "status": "exists", **m})
                continue
            map_id = str(uuid4())
            db.add(
                EHRMappingDB(
                    id=map_id,
                    entity_id=ent_id,
                    ontology=m["ontology"],
                    iri=m["iri"],
                    label=m["label"],
                    confidence=m["confidence"],
                    mapping_method=m["mapping_method"],
                )
            )
            existing_map_keys.add(map_key)
            mappings_created += 1
            mappings_list.append({"mapping_id": map_id, "entity_id": ent_id, **m})

    await db.commit()
    logger.info(
        "extract_and_map ehr=%s entities(+%d/=%d) mappings(+%d/=%d)",
        ehr_id,
        entities_created,
        entities_existing,
        mappings_created,
        mappings_existing,
    )
    return {
        "ehr_id": ehr_id,
        "entities_count": len(entities_list),
        "mappings_count": len(mappings_list),
        "entities_created": entities_created,
        "entities_existing": entities_existing,
        "mappings_created": mappings_created,
        "mappings_existing": mappings_existing,
        "entities": entities_list,
        "mappings": mappings_list,
    }


@router.get("/ehr/{ehr_id}/entities")
async def get_entities(ehr_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(EHREntityDB).where(EHREntityDB.ehr_id == ehr_id)
    result = await db.execute(stmt)
    return [
        {
            "entity_id": e.id,
            "text": e.text,
            "type": e.entity_type,
            "start": e.start,
            "end": e.end,
            "confidence": e.confidence,
        }
        for e in result.scalars().all()
    ]


@router.get("/ehr/{ehr_id}/mappings")
async def get_mappings(ehr_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(EHRMappingDB).join(EHREntityDB).where(EHREntityDB.ehr_id == ehr_id)
    result = await db.execute(stmt)
    return [
        {
            "mapping_id": m.id,
            "entity_id": m.entity_id,
            "ontology": m.ontology,
            "iri": m.iri,
            "label": m.label,
            "confidence": m.confidence,
        }
        for m in result.scalars().all()
    ]


def _doc_dict(d: EHRDocumentDB) -> dict:
    return {
        "ehr_id": d.id,
        "case_id": d.case_id,
        "version": d.version,
        "source": d.source,
        "checksum": d.checksum,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }
