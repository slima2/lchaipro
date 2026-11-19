"""Shared authorization helpers (F6.5).

Todos los servicios de lectura (inference, image, graph) comparten el
mismo modelo de autorización vertical:

* ``X-User-Id`` y ``X-Roles`` los inyecta exclusivamente el api-gateway
  (``_CLIENT_FORBIDDEN_HEADERS`` impide que el cliente los suba).
* Un ``clinician`` solo puede leer/mutar casos que él mismo creó
  (``cases.created_by == x-user-id``).
* ``admin`` y ``auditor`` pueden ver cualquier caso (auditor read-only
  por el RBAC del gateway).
* Casos soft-deleted (``cases.deleted_at IS NOT NULL``) devuelven 404
  a todos, para que no aparezcan en lecturas ni URLs presigned.

Centralizar esto aquí evita que cada endpoint tenga una copia ligeramente
distinta del check, y hace trivial auditarlo en un solo lugar.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Roles con "supervisión" (ven cualquier caso). admin puede además mutar;
# auditor solo lee pero se filtra por RBAC a nivel de gateway.
SUPERVISORY_ROLES: frozenset[str] = frozenset({"admin", "auditor"})


@dataclass(frozen=True)
class Caller:
    user_id: str
    roles: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def is_auditor(self) -> bool:
        return "auditor" in self.roles

    @property
    def is_supervisor(self) -> bool:
        return bool(self.roles & SUPERVISORY_ROLES)


def caller_from_request(request: Request) -> Caller:
    uid = request.headers.get("x-user-id", "unknown")
    raw = request.headers.get("x-roles", "")
    roles = frozenset(r.strip() for r in raw.split(",") if r.strip())
    return Caller(user_id=uid, roles=roles)


async def require_case_access(
    db: AsyncSession, request: Request, case_id: str
) -> Caller:
    """Validate the caller can access ``case_id``.

    * 404 si el caso no existe o está soft-deleted (no filtrar por
      existencia a atacantes).
    * 403 si el caso existe pero fue creado por otro clinician y el
      caller no es admin/auditor.
    * Retorna el :class:`Caller` para que el endpoint pueda hacer más
      checks (p.ej. mutación requiere admin).
    """
    row = (
        await db.execute(
            text("SELECT created_by, deleted_at FROM cases WHERE id = :cid"),
            {"cid": case_id},
        )
    ).fetchone()
    if not row or row[1] is not None:
        # Mismo mensaje para "no existe" y "soft-deleted": no revelar
        # existencia a quien no tiene acceso.
        raise HTTPException(status_code=404, detail="Case not found")

    created_by = row[0]
    caller = caller_from_request(request)

    if caller.is_supervisor:
        return caller
    if created_by and created_by == caller.user_id:
        return caller
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this case",
    )


# ── S3 key / prefix parsing (F6.5) ─────────────────────────────────────


# Prefijos conocidos de nuestro bucket. El ``case_id`` siempre va como
# segundo segmento. Si en el futuro se añaden prefijos nuevos, hay que
# registrarlos aquí para que ``/artifacts/presigned`` los acepte.
_ALLOWED_PREFIXES: tuple[str, ...] = ("images/", "results/", "artifacts/")


class InvalidArtifactKey(ValueError):
    """La key solicitada no cumple el formato ``<prefix>/<case_id>/<rest>``."""


def case_id_from_key(key: str) -> str:
    """Extract ``case_id`` from a bucket key, validating the layout.

    Patrón permitido: ``<prefix>/<case_id>/<rest...>`` donde ``prefix``
    está en :data:`_ALLOWED_PREFIXES` y ``case_id`` no contiene ``..``,
    slashes, backslashes ni es vacío.

    Esto cierra el caso del pentest §6.5 donde un clinician pedía
    ``/artifacts/presigned?key=images/<case_de_otro>/x.png`` sin que el
    servicio verificara nada de la key.
    """
    if not key or key.startswith("/") or ".." in key or "\\" in key:
        raise InvalidArtifactKey(f"Invalid key format: {key!r}")

    for prefix in _ALLOWED_PREFIXES:
        if key.startswith(prefix):
            rest = key[len(prefix) :]
            parts = rest.split("/", 1)
            case_id = parts[0]
            if not case_id or len(parts) < 2 or not parts[1]:
                raise InvalidArtifactKey(
                    f"Key must be {prefix}<case_id>/<file>: {key!r}"
                )
            if "/" in case_id or "\\" in case_id or ".." in case_id:
                raise InvalidArtifactKey(f"Invalid case_id in key: {key!r}")
            return case_id

    raise InvalidArtifactKey(
        f"Key prefix not allowed. Expected one of {_ALLOWED_PREFIXES}"
    )
