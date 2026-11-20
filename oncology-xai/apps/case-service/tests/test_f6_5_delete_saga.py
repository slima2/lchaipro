"""F6.5 — Saga de ``case.deleted``: la cascada alcanza images, snapshots y S3.

Los tests crean las tablas ``images`` y ``case_graph_snapshots`` a mano
(los models viven en otros servicios) para verificar que la saga:

* soft-deletea las imágenes activas del caso,
* elimina los graph snapshots del caso,
* invoca ``_cleanup_case_storage`` (S3) y propaga sus métricas al evento.

El cleanup de S3 se mockea con un spy que cuenta llamadas y devuelve
métricas sintéticas.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import routes


ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}


async def _create_sibling_tables(session: AsyncSession) -> None:
    """Crear tablas que viven en otros servicios para que la saga tenga
    sobre qué actuar en el test."""
    await session.execute(
        text(
            "CREATE TABLE images ("
            "  id TEXT PRIMARY KEY,"
            "  case_id TEXT NOT NULL,"
            "  format TEXT,"
            "  storage_uri TEXT,"
            "  checksum TEXT,"
            "  size_bytes INTEGER DEFAULT 0,"
            "  uploaded_by TEXT,"
            "  uploaded_at TIMESTAMP,"
            "  deleted_at TIMESTAMP,"
            "  deleted_by TEXT,"
            "  delete_reason TEXT"
            ")"
        )
    )
    await session.execute(
        text(
            "CREATE TABLE case_graph_snapshots ("
            "  id TEXT PRIMARY KEY,"
            "  case_id TEXT NOT NULL,"
            "  nodes_json TEXT,"
            "  edges_json TEXT"
            ")"
        )
    )
    # F6.3 — la saga también borra ehr_documents del caso. Añadimos la
    # tabla para verificar la cascada en los tests.
    await session.execute(
        text(
            "CREATE TABLE ehr_documents ("
            "  id TEXT PRIMARY KEY,"
            "  case_id TEXT NOT NULL,"
            "  version INTEGER DEFAULT 1,"
            "  checksum TEXT NOT NULL"
            ")"
        )
    )
    await session.commit()


async def _create_case_tree(
    client: AsyncClient, patient_id: str = "p1"
) -> tuple[str, str]:
    """Crear patient + case por API; devuelve (patient_id, case_id)."""
    r = await client.post(
        "/api/v1/patients",
        json={"external_id": patient_id, "demographics": {}},
        headers=ADMIN,
    )
    patient = r.json()["patient_id"]
    r = await client.post(
        "/api/v1/cases",
        json={"patient_id": patient},
        headers=ADMIN,
    )
    return patient, r.json()["case_id"]


async def _seed_image(session: AsyncSession, case_id: str, img_id: str) -> None:
    await session.execute(
        text(
            "INSERT INTO images (id, case_id, format, storage_uri, checksum, "
            "size_bytes, uploaded_by) VALUES "
            "(:i, :c, 'png', 's3://b/images/'||:c||'/'||:i||'.png', 'x', 1, 'u')"
        ),
        {"i": img_id, "c": case_id},
    )


async def _seed_snapshot(session: AsyncSession, case_id: str, snap_id: str) -> None:
    await session.execute(
        text(
            "INSERT INTO case_graph_snapshots (id, case_id) VALUES (:s, :c)"
        ),
        {"s": snap_id, "c": case_id},
    )


async def test_delete_case_softdeletes_images_and_drops_snapshots(
    client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Después del soft-delete del caso:

    * ``images`` del caso quedan ``deleted_at IS NOT NULL``
    * ``case_graph_snapshots`` del caso se eliminan
    * se llama a ``_cleanup_case_storage`` exactamente una vez
    """
    await _create_sibling_tables(async_session)
    _, case_id = await _create_case_tree(client)
    await _seed_image(async_session, case_id, "img-A")
    await _seed_image(async_session, case_id, "img-B")
    await _seed_snapshot(async_session, case_id, "snap-1")
    # F6.3 — ehr_document del caso (debe desaparecer tras la saga)
    await async_session.execute(
        text(
            "INSERT INTO ehr_documents (id, case_id, version, checksum) "
            "VALUES (:i, :c, 1, 'cs')"
        ),
        {"i": "ehr-A", "c": case_id},
    )
    # Imagen de otro caso que NO se debe tocar
    await _seed_image(async_session, "case-other", "img-other")
    await async_session.commit()

    storage_calls: list[str] = []

    def _spy_storage(cid: str):
        storage_calls.append(cid)
        return {
            "storage_deleted": 7,
            "storage_cleanup_ok": True,
        }

    monkeypatch.setattr(routes, "_cleanup_case_storage", _spy_storage)

    r = await client.request(
        "DELETE",
        f"/api/v1/cases/{case_id}",
        json={"reason": "retención vencida — pentest §6.5"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["soft_deleted"] is True
    assert body["images_soft_deleted"] == 2
    assert body["graph_snapshots_deleted"] == 1
    assert body["ehr_documents_deleted"] == 1
    assert body["storage_cleanup_ok"] is True
    assert body["storage_deleted"] == 7

    # Verificación directa en DB
    rows = await async_session.execute(
        text(
            "SELECT id, deleted_at FROM images WHERE case_id = :c"
        ),
        {"c": case_id},
    )
    for _id, deleted_at in rows:
        assert deleted_at is not None

    snaps = (
        await async_session.execute(
            text("SELECT COUNT(*) FROM case_graph_snapshots WHERE case_id = :c"),
            {"c": case_id},
        )
    ).scalar_one()
    assert snaps == 0

    # Imagen de otro caso intacta
    other = (
        await async_session.execute(
            text("SELECT deleted_at FROM images WHERE id = 'img-other'")
        )
    ).scalar_one()
    assert other is None

    # Storage cleanup llamado una sola vez y con el case correcto
    assert storage_calls == [case_id]


async def test_delete_patient_cascades_saga_per_case(
    client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Al borrar un patient con N casos, la saga corre una vez por caso."""
    await _create_sibling_tables(async_session)

    patient, case1 = await _create_case_tree(client, patient_id="p-multi")
    r = await client.post(
        "/api/v1/cases", json={"patient_id": patient}, headers=ADMIN
    )
    case2 = r.json()["case_id"]

    await _seed_image(async_session, case1, "img-1")
    await _seed_image(async_session, case2, "img-2")
    await _seed_snapshot(async_session, case1, "snap-1")
    await _seed_snapshot(async_session, case2, "snap-2")
    await async_session.commit()

    storage_calls: list[str] = []
    monkeypatch.setattr(
        routes,
        "_cleanup_case_storage",
        lambda cid: (
            storage_calls.append(cid)
            or {"storage_deleted": 3, "storage_cleanup_ok": True}
        ),
    )

    r = await client.request(
        "DELETE",
        f"/api/v1/patients/{patient}",
        json={"reason": "right-to-be-forgotten request"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert r.json()["cascaded_cases"] == 2
    assert sorted(storage_calls) == sorted([case1, case2])

    # Las dos imágenes marcadas, los dos snapshots borrados
    imgs = (
        await async_session.execute(
            text("SELECT COUNT(*) FROM images WHERE deleted_at IS NOT NULL")
        )
    ).scalar_one()
    assert imgs == 2
    snaps = (
        await async_session.execute(text("SELECT COUNT(*) FROM case_graph_snapshots"))
    ).scalar_one()
    assert snaps == 0


async def test_delete_case_without_sibling_tables_still_succeeds(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    """Regresión — si ``images``/``case_graph_snapshots`` NO existen
    (p.ej. test environments acotados), el soft-delete en ``cases`` debe
    funcionar igual sin re-raise."""
    # NO creamos las sibling tables → UPDATE/DELETE fallarían sin el try
    _, case_id = await _create_case_tree(client)
    r = await client.request(
        "DELETE",
        f"/api/v1/cases/{case_id}",
        json={"reason": "resilience test for saga"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["soft_deleted"] is True
    assert body["images_soft_deleted"] == 0
    assert body["graph_snapshots_deleted"] == 0
