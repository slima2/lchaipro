"""F6.3 — dedupe, idempotencia, cascade y límites en ehr-service."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_HEADERS = {"x-user-id": "clinicianA", "x-roles": "clinician"}
_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}

# Texto con las mismas keywords varias veces: dispara varios matches
# que SIN dedupe se duplicarían en re-ejecuciones del endpoint.
_SAMPLE = (
    "Paciente con adenocarcinoma de pulmón. Mutación EGFR positiva. "
    "Patrón lepidic predominante. Carcinoma. EGFR."
)


async def _ingest(client: AsyncClient, content: str = _SAMPLE) -> str:
    r = await client.post(
        "/api/v1/cases/case-A/ehr:ingest",
        headers=_HEADERS,
        json={"content": content},
    )
    assert r.status_code == 201, r.text
    return r.json()["ehr_id"]


# ── Dedupe + idempotencia ─────────────────────────────────────────────


async def test_extract_and_map_is_idempotent(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    ehr_id = await _ingest(client)

    r1 = await client.post(f"/api/v1/ehr/{ehr_id}:extract-and-map", headers=_HEADERS)
    assert r1.status_code == 200
    body1 = r1.json()
    n_entities = body1["entities_count"]  # matches totales (incluye duplicados intra-call del NER)
    unique_entities_in_db = body1["entities_created"]
    assert n_entities > 0
    assert unique_entities_in_db > 0
    # El NER puede producir duplicados internos (keywords 'EGFR' + 'egfr'
    # con IGNORECASE matchean el mismo span dos veces). Nuestro dedupe
    # intra-call los colapsa → ``entities_existing`` puede ser > 0 ya
    # en el primer call sin que haya nada "preexistente" en DB.
    assert unique_entities_in_db + body1["entities_existing"] == n_entities

    # Segunda llamada: nada nuevo; TODO lo que el NER devuelva debe
    # mapearse a entidades ya existentes.
    r2 = await client.post(f"/api/v1/ehr/{ehr_id}:extract-and-map", headers=_HEADERS)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["entities_count"] == n_entities
    assert body2["entities_created"] == 0
    assert body2["entities_existing"] == n_entities
    assert body2["mappings_created"] == 0

    # Verificamos en DB: hay exactamente ``entities_created`` filas
    # únicas, sin importar cuántas veces se llamó al endpoint.
    rows_entities = (
        await async_session.execute(
            text("SELECT COUNT(*) FROM ehr_entities WHERE ehr_id = :e"),
            {"e": ehr_id},
        )
    ).scalar_one()
    assert rows_entities == unique_entities_in_db
    rows_mappings_1 = (
        await async_session.execute(
            text(
                "SELECT COUNT(*) FROM ehr_mappings m JOIN ehr_entities e "
                "ON m.entity_id = e.id WHERE e.ehr_id = :e"
            ),
            {"e": ehr_id},
        )
    ).scalar_one()
    # Mismo count tras la segunda llamada: el DELETE idempotencia es real
    assert rows_mappings_1 == body1["mappings_created"]


async def test_extract_and_map_third_call_still_stable(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """3 llamadas seguidas no incrementan filas en DB."""
    ehr_id = await _ingest(client)
    r_first = await client.post(
        f"/api/v1/ehr/{ehr_id}:extract-and-map", headers=_HEADERS
    )
    assert r_first.status_code == 200
    unique_after_first = r_first.json()["entities_created"]

    for _ in range(2):  # 2 llamadas adicionales, 3 en total
        r = await client.post(
            f"/api/v1/ehr/{ehr_id}:extract-and-map", headers=_HEADERS
        )
        assert r.status_code == 200
        assert r.json()["entities_created"] == 0
    rows = (
        await async_session.execute(text("SELECT COUNT(*) FROM ehr_entities"))
    ).scalar_one()
    assert rows == unique_after_first


# ── ON DELETE CASCADE (documents → entities → mappings) ───────────────


async def test_deleting_document_cascades_entities_and_mappings(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    ehr_id = await _ingest(client)
    r = await client.post(f"/api/v1/ehr/{ehr_id}:extract-and-map", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["entities_count"] > 0

    # Borramos el document directamente (simula lo que hace la saga de
    # case-service al borrar un caso).
    await async_session.execute(
        text("DELETE FROM ehr_documents WHERE id = :e"), {"e": ehr_id}
    )
    await async_session.commit()

    orphans_entities = (
        await async_session.execute(
            text("SELECT COUNT(*) FROM ehr_entities WHERE ehr_id = :e"),
            {"e": ehr_id},
        )
    ).scalar_one()
    orphans_mappings = (
        await async_session.execute(
            text(
                "SELECT COUNT(*) FROM ehr_mappings m "
                "LEFT JOIN ehr_entities e ON m.entity_id = e.id "
                "WHERE e.id IS NULL"
            )
        )
    ).scalar_one()
    assert orphans_entities == 0
    assert orphans_mappings == 0


# ── Límites de tamaño ─────────────────────────────────────────────────


async def test_ingest_rejects_oversized_content(client: AsyncClient) -> None:
    huge = "adenocarcinoma " * 20_000  # >> MAX_CONTENT_CHARS
    r = await client.post(
        "/api/v1/cases/case-A/ehr:ingest",
        headers=_HEADERS,
        json={"content": huge},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


async def test_ingest_rejects_non_string_content(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/cases/case-A/ehr:ingest",
        headers=_HEADERS,
        json={"content": {"nested": "attack"}},
    )
    assert r.status_code == 400


async def test_extract_rejects_too_many_entities(
    client: AsyncClient, monkeypatch
) -> None:
    """Payload sintético del pentest §6.3 — texto adversarial que dispara
    la combinatoria del NER."""
    ehr_id = await _ingest(client)

    # Monkey-patch extract_entities para forzar muchas entidades sin
    # necesitar un texto masivo.
    import app.routes as routes

    def _flood(_text: str):
        from app.ner import ExtractedEntity

        return [
            ExtractedEntity(
                text="x",
                entity_type="MUTATION",
                start=i,
                end=i + 1,
                confidence=1.0,
            )
            for i in range(6000)
        ]

    monkeypatch.setattr(routes, "extract_entities", _flood)

    r = await client.post(
        f"/api/v1/ehr/{ehr_id}:extract-and-map", headers=_HEADERS
    )
    assert r.status_code == 422
    assert "too many entities" in r.json()["detail"].lower()


# ── Unique constraint directo ─────────────────────────────────────────


async def test_unique_constraint_entity_span(async_session: AsyncSession) -> None:
    """Regresión de BD: el UNIQUE constraint impide duplicar un span."""
    await async_session.execute(
        text(
            "INSERT INTO ehr_documents (id, case_id, version, source, "
            "checksum, created_at) VALUES "
            "('d1', 'c1', 1, 'paste', 'x', CURRENT_TIMESTAMP)"
        )
    )
    _insert_e = (
        "INSERT INTO ehr_entities (id, ehr_id, text, entity_type, "
        '"start", "end", confidence, created_at) VALUES '
    )
    await async_session.execute(
        text(_insert_e + "('e1', 'd1', 'EGFR', 'MUTATION', 10, 14, 1.0, CURRENT_TIMESTAMP)")
    )
    await async_session.commit()

    import pytest
    import sqlalchemy.exc as sa_exc

    with pytest.raises(sa_exc.IntegrityError):
        await async_session.execute(
            text(_insert_e + "('e2', 'd1', 'EGFR', 'MUTATION', 10, 14, 1.0, CURRENT_TIMESTAMP)")
        )
        await async_session.commit()
