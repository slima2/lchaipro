"""F6.4 — PUT /parameters admin-only con allowlist + sin placeholder image.

El pentest §6.4 demostró que:

* un clinician podía llamar ``PUT /api/v1/parameters`` con
  ``mutation_threshold = 0.0`` y dejar todas las futuras predicciones
  "concluyentes" para todo el deployment;
* el worker Celery generaba un placeholder gris 448x448 si la fila de
  ``images`` no existía, creando un ``ResultBundle`` sintético para un
  ``image_id`` arbitrario. La defensa en profundidad del worker (fail
  hard sin placeholder) se verifica aquí con inspección estática — el
  mock completo del pipeline Celery queda fuera de alcance para los
  tests mínimos.
"""

from __future__ import annotations

from httpx import AsyncClient


_A = {"x-user-id": "clinicianA", "x-roles": "clinician"}
_ADMIN = {"x-user-id": "admin1", "x-roles": "clinician,admin"}
_AUDIT = {"x-user-id": "auditor1", "x-roles": "auditor"}


# ── PUT /parameters ─────────────────────────────────────────────────────


async def test_put_parameters_clinician_is_forbidden(client: AsyncClient) -> None:
    r = await client.put(
        "/api/v1/parameters", headers=_A, json={"mutation_threshold": 0.5}
    )
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()


async def test_put_parameters_auditor_is_forbidden(client: AsyncClient) -> None:
    r = await client.put(
        "/api/v1/parameters", headers=_AUDIT, json={"mutation_threshold": 0.5}
    )
    assert r.status_code == 403


async def test_put_parameters_rejects_mutation_threshold_zero(
    client: AsyncClient,
) -> None:
    """Payload exacto del pentest §6.4 — todo se vuelve 'concluyente'."""
    r = await client.put(
        "/api/v1/parameters", headers=_ADMIN, json={"mutation_threshold": 0.0}
    )
    assert r.status_code == 400
    assert "range" in r.json()["detail"].lower()


async def test_put_parameters_rejects_pattern_threshold_zero(
    client: AsyncClient,
) -> None:
    r = await client.put(
        "/api/v1/parameters",
        headers=_ADMIN,
        json={"auroc_threshold": 0.1},  # valor absurdamente bajo
    )
    assert r.status_code == 400


async def test_put_parameters_rejects_absurd_top_k(client: AsyncClient) -> None:
    r = await client.put(
        "/api/v1/parameters", headers=_ADMIN, json={"top_k_tiles": 10_000_000}
    )
    assert r.status_code == 400


async def test_put_parameters_rejects_non_numeric(client: AsyncClient) -> None:
    r = await client.put(
        "/api/v1/parameters",
        headers=_ADMIN,
        json={"mutation_threshold": "zero"},
    )
    assert r.status_code == 400


async def test_put_parameters_rejects_unknown_method(client: AsyncClient) -> None:
    r = await client.put(
        "/api/v1/parameters",
        headers=_ADMIN,
        json={"best_method": {"EGFR": "attacker-controlled"}},
    )
    assert r.status_code == 400


async def test_put_parameters_admin_valid_values_accepted(
    client: AsyncClient,
) -> None:
    r = await client.put(
        "/api/v1/parameters",
        headers=_ADMIN,
        json={
            "mutation_threshold": 0.55,
            "top_k_tiles": 32,
            "max_tiles": 2000,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["actor"] == "admin1"
    assert any("mutation_threshold=0.55" in c for c in body["changes"])


# ── tasks.py — sin placeholder (inspección estática) ───────────────────


def test_tasks_py_no_longer_builds_placeholder_image() -> None:
    """Regresión: el worker ya NO contiene el fallback de PIL que generaba
    una imagen gris 448x448. Antes era la causa raíz del BOLA §6.4 en el
    worker. Hacemos una lectura directa del archivo para que el test
    detecte una re-introducción accidental (p.ej. un revert de este fix).
    """
    from pathlib import Path

    src = Path(__file__).parent.parent / "app" / "tasks.py"
    content = src.read_text(encoding="utf-8")

    # Las dos marcas inequívocas del placeholder original
    assert 'PILImage.new("RGB", (448, 448)' not in content
    assert "use placeholder" not in content.lower()

    # Y debe existir el fail-hard explícito
    assert "refusing to run" in content
    assert 'not found in DB' in content
