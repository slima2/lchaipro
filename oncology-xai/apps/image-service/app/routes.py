"""Image upload / viewer routes."""

from __future__ import annotations

import hashlib
import io
import logging
from uuid import uuid4

from event_contracts.envelope import EventEnvelope
from event_contracts.publisher import EventPublisher
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from oncology_common.authz import (
    InvalidArtifactKey,
    case_id_from_key,
    require_case_access,
)
from oncology_common.storage import StorageClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _lazy_imaging():
    """Imports pesados (numpy + Pillow) sólo cuando el handler los
    necesita, para que los tests F6.7 no exijan openslide/numpy en el
    virtualenv mínimo."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image as PILImage  # noqa: PLC0415

    return np, PILImage


from app.config import settings
from app.database import get_db
from app.models import ImageDB

logger = logging.getLogger(__name__)


def _publish_audit(
    event_type: str, case_id: str, image_id: str, user: str, details: dict | None = None
) -> None:
    """Fire-and-forget audit event to RabbitMQ."""
    try:
        pub = EventPublisher(settings.rabbitmq_url)
        pub.publish(
            EventEnvelope(
                event_type=event_type,
                producer="image-service",
                case_id=case_id,
                payload={
                    "image_id": image_id,
                    "user_id": user,
                    "action": "UPLOAD",
                    "entity_type": "image",
                    "entity_id": image_id,
                    **(details or {}),
                },
            )
        )
    except Exception:
        logger.warning("Failed to publish audit event %s", event_type, exc_info=True)


def _validate_he_stain(data: bytes, ext: str) -> None:
    """Reject images that are not H&E stained. LCHAI was trained exclusively on H&E.

    Analyzes a thumbnail of the image to detect non-H&E color profiles:
    - IHC/DAB (brown chromogen)
    - Alcian Blue, PAS, trichrome (non-pink/purple dominant)
    - Immunofluorescence (very dark with bright spots)
    """
    try:
        if ext in ("svs", "bif", "biff"):
            try:
                import os
                import tempfile

                import openslide

                suffix = {"svs": ".svs", "bif": ".bif", "biff": ".bif"}.get(ext, ".tif")
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                    f.write(data[: min(len(data), 100_000_000)])
                    tmp = f.name
                try:
                    slide = openslide.OpenSlide(tmp)
                    thumb = slide.get_thumbnail((512, 512))
                    slide.close()
                finally:
                    os.unlink(tmp)
                np, PILImage = _lazy_imaging()
                arr = np.array(thumb.convert("RGB"))
            except Exception:
                return
        else:
            np, PILImage = _lazy_imaging()
            img = PILImage.open(io.BytesIO(data))
            img.thumbnail((512, 512))
            arr = np.array(img.convert("RGB"))

        r_mean = float(arr[:, :, 0].mean())
        g_mean = float(arr[:, :, 1].mean())
        b_mean = float(arr[:, :, 2].mean())
        brightness = (r_mean + g_mean + b_mean) / 3

        # H&E tissue: pink (R dominant) or purple (R~B, both > G)
        # Background is white/near-white, tissue is pink/purple
        # Non-H&E indicators:
        is_blue_dominant = (
            b_mean > r_mean + 15 and b_mean > g_mean + 15 and r_mean < 160
        )
        is_brown_dominant = (
            r_mean > 150 and g_mean > 100 and b_mean < 90 and (r_mean - b_mean) > 60
        )
        is_very_dark = brightness < 60
        is_green_dominant = g_mean > r_mean + 10 and g_mean > b_mean + 10

        reasons = []
        if is_blue_dominant:
            reasons.append(
                "blue-dominant stain detected (possible Alcian Blue, trichrome, or special stain)"
            )
        if is_brown_dominant:
            reasons.append("brown chromogen detected (possible IHC/DAB stain)")
        if is_very_dark and not is_brown_dominant:
            reasons.append("very dark image (possible immunofluorescence or unstained)")
        if is_green_dominant:
            reasons.append("green-dominant image (not compatible with H&E)")

        if reasons:
            detail = (
                f"NON-H&E IMAGE REJECTED: {'; '.join(reasons)}. "
                f"Color profile: R={r_mean:.0f} G={g_mean:.0f} B={b_mean:.0f}. "
                f"LCHAI v2.0 was trained exclusively on H&E-stained slides. "
                f"Please upload an H&E-stained image."
            )
            logger.warning("Upload rejected: %s", detail)
            raise HTTPException(status_code=422, detail=detail)

    except HTTPException:
        raise
    except Exception as e:
        logger.warning("H&E validation skipped (could not analyze thumbnail): %s", e)


router = APIRouter(prefix="/api/v1", tags=["Images"])


def _storage() -> StorageClient:
    return StorageClient(
        endpoint=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )


@router.post("/cases/{case_id}/images:upload", status_code=status.HTTP_201_CREATED)
async def upload_image(
    case_id: str,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload histopathological image. Supported: png, jpg, jpeg, tif, tiff, svs, bif, biff."""
    # F6.5 — un clinician solo puede subir imágenes a casos propios
    await require_case_access(db, request, case_id)
    uploaded_by = request.headers.get("x-user-id", "unknown")
    data = await file.read()
    ext = (file.filename or "image.png").rsplit(".", 1)[-1].lower()
    allowed = ("png", "jpg", "jpeg", "tif", "tiff", "svs", "bif", "biff")
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {ext}. Supported: {', '.join(allowed)}",
        )

    # Validate H&E staining color profile (reject IHC, special stains)
    _validate_he_stain(data, ext)

    checksum = hashlib.sha256(data).hexdigest()
    image_id = str(uuid4())
    key = f"images/{case_id}/{image_id}.{ext}"

    storage = _storage()
    uri = storage.upload_bytes(key, data, content_type=file.content_type or "image/png")

    img = ImageDB(
        id=image_id,
        case_id=case_id,
        format=ext,
        storage_uri=uri,
        checksum=checksum,
        size_bytes=len(data),
        uploaded_by=uploaded_by,
    )
    db.add(img)
    await db.commit()
    await db.refresh(img)
    _publish_audit(
        "image.uploaded",
        case_id,
        image_id,
        uploaded_by,
        {"filename": file.filename, "size_bytes": len(data)},
    )
    return _img_dict(img)


ALLOWED_EXTENSIONS = ("png", "jpg", "jpeg", "tif", "tiff", "svs", "bif", "biff")

CT_MAP = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "svs": "application/octet-stream",
    "bif": "application/octet-stream",
    "biff": "application/octet-stream",
}


MULTIPART_THRESHOLD = 4_500_000_000  # 4.5 GB — use multipart for files above this
PART_SIZE = 100_000_000  # 100 MB per part

# F6.7 — topes duros contra DoS en request-upload.
#
# Pentest 2026-11-11 §6.7 midió 1 TB = 10.000 partes (3.75 s, 5.4 MB
# JSON) y proyectó 1 PB = 10.000.000 partes (~1 h CPU, 5.4 GB JSON).
# El bucle HMAC por parte era síncrono y bloqueaba el event loop con
# una sola petición autenticada.
#
# Topes elegidos:
#  * ``MAX_SLIDE_BYTES`` = 50 GB — por encima de cualquier WSI SVS real
#    (<40 GB para 40x) y por debajo del régimen de ataque; filesets
#    legítimos más grandes requieren decisión de admin.
#  * ``MAX_NUM_PARTS``  = 200 — alineado con la recomendación del
#    pentest (<=200). 200 × PART_SIZE (100 MB) = 20 GB, cubre bien el
#    caso válido de multipart; para WSI de 20-50 GB subimos PART_SIZE
#    efectivo (reconfiguración). Si una petición legítima lo rebasa
#    hay que subir PART_SIZE, no el límite de partes.
MAX_SLIDE_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB
MAX_NUM_PARTS = 200

# F6.7 — rate-limit in-memory por (user_id, case_id).
#
# No reemplaza un rate-limit cluster-wide en el gateway (eso vendrá en
# una iteración futura con Redis), pero al estar por-pod bloquea el
# escenario del pentest: un clinician autenticado golpeando el endpoint
# en un bucle. Permitimos ``_UPLOAD_RATE_LIMIT`` peticiones por ventana
# de ``_UPLOAD_RATE_WINDOW`` segundos por usuario-caso.
_UPLOAD_RATE_LIMIT = 20
_UPLOAD_RATE_WINDOW = 60.0
_upload_rate_hits: dict[tuple[str, str], list[float]] = {}


def _upload_rate_check(user_id: str, case_id: str) -> None:
    import time as _time

    now = _time.monotonic()
    key = (user_id, case_id)
    hits = [t for t in _upload_rate_hits.get(key, []) if now - t < _UPLOAD_RATE_WINDOW]
    if len(hits) >= _UPLOAD_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many upload requests for this case; max "
                f"{_UPLOAD_RATE_LIMIT} per {int(_UPLOAD_RATE_WINDOW)}s."
            ),
        )
    hits.append(now)
    _upload_rate_hits[key] = hits
    # Prevención de memoria: si el dict crece sin control, podamos
    # entradas vacías cada vez que superamos 10k keys.
    if len(_upload_rate_hits) > 10_000:
        for k in list(_upload_rate_hits.keys()):
            if not any(now - t < _UPLOAD_RATE_WINDOW for t in _upload_rate_hits[k]):
                _upload_rate_hits.pop(k, None)


def _validated_size_bytes(raw: object) -> int:
    """F6.7 — exige ``int`` estricto y rango ``(0, MAX_SLIDE_BYTES]``.

    Antes ``body.get('size_bytes', 0)`` se pasaba sin validar; tipos
    inválidos ("abc", 1.5e15, [1], None) llegaban al worker y tiraban
    500. Ahora todo lo que no sea un int en rango → 400 explícito.
    """
    # Nota: ``bool`` es subclase de ``int`` en Python — lo excluimos
    # explícitamente porque ``True`` casteado a int sería 1 silencioso
    # y no es lo que el cliente quiso decir.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(
            status_code=400,
            detail="size_bytes must be an integer number of bytes",
        )
    if raw <= 0:
        raise HTTPException(
            status_code=400,
            detail="size_bytes must be > 0",
        )
    if raw > MAX_SLIDE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"size_bytes={raw} exceeds MAX_SLIDE_BYTES={MAX_SLIDE_BYTES}. "
                "Contact an administrator to upload files larger than 50 GiB."
            ),
        )
    return raw


@router.post("/cases/{case_id}/images:request-upload", status_code=status.HTTP_200_OK)
async def request_upload(
    case_id: str, body: dict, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return presigned URL(s) for direct S3 upload.

    For files < 4.5 GB: returns a single presigned PUT URL.
    For files >= 4.5 GB: returns multipart upload URLs (one per 100 MB part),
    capped at :data:`MAX_NUM_PARTS` parts and :data:`MAX_SLIDE_BYTES` total.

    Body: { "filename": "slide.svs", "size_bytes": 10000000000 }

    F6.7 — size_bytes es validado como int en rango; el número de
    partes se calcula y verifica **antes** de inicializar el multipart
    en el backend de objetos, para no dejar uploads colgantes cuando
    la petición resulta abusiva.
    """
    # F6.5 — ownership del caso antes de generar presigned S3
    await require_case_access(db, request, case_id)
    # F6.7 — rate-limit por (usuario, caso) antes de tocar S3
    _upload_rate_check(
        request.headers.get("x-user-id", "unknown"),
        case_id,
    )
    filename = body.get("filename", "image.png")
    # F6.7 — type+range check antes de tocar nada
    size_bytes = _validated_size_bytes(body.get("size_bytes", 0))
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {ext}. Supported: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    image_id = str(uuid4())
    key = f"images/{case_id}/{image_id}.{ext}"
    content_type = CT_MAP.get(ext, "application/octet-stream")
    storage = _storage()
    uploaded_by = request.headers.get("x-user-id", "unknown")

    response: dict = {
        "image_id": image_id,
        "s3_key": key,
        "content_type": content_type,
    }

    def _externalize(url: str) -> str:
        """Replace internal Docker hostname with browser-accessible endpoint."""
        ext_ep = settings.s3_endpoint_external
        if ext_ep and ext_ep != settings.s3_endpoint:
            return url.replace(settings.s3_endpoint, ext_ep, 1)
        return url

    if size_bytes >= MULTIPART_THRESHOLD:
        # F6.7 — calcular num_parts ANTES de inicializar el multipart.
        # Antes el handler iniciaba el multipart y luego entraba al
        # bucle; una petición de 1 PB dejaba el multipart huérfano
        # en SeaweedFS incluso si acabábamos tirando el request.
        num_parts = max(1, -(-size_bytes // PART_SIZE))  # ceil division
        if num_parts > MAX_NUM_PARTS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"num_parts={num_parts} exceeds MAX_NUM_PARTS="
                    f"{MAX_NUM_PARTS} (part_size={PART_SIZE}). Refusing "
                    "to allocate presigned URLs."
                ),
            )
        upload_id = storage.create_multipart_upload(key, content_type=content_type)
        try:
            part_urls = []
            for i in range(1, num_parts + 1):
                url = storage.presigned_upload_part(key, upload_id, i, expires_in=7200)
                part_urls.append({"part_number": i, "presigned_url": _externalize(url)})
        except Exception:
            # Si algo explota a mitad de la generación de URLs, abortamos
            # el multipart para no dejar el upload colgante.
            try:
                storage.abort_multipart_upload(key, upload_id)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to abort orphan multipart %s", upload_id)
            raise
        response["multipart"] = True
        response["upload_id"] = upload_id
        response["part_size"] = PART_SIZE
        response["part_urls"] = part_urls
    else:
        presigned_url = storage.presigned_put_url(
            key, content_type=content_type, expires_in=7200
        )
        response["multipart"] = False
        response["presigned_url"] = _externalize(presigned_url)

    img = ImageDB(
        id=image_id,
        case_id=case_id,
        format=ext,
        storage_uri=f"s3://{settings.s3_bucket}/{key}",
        checksum="pending-upload",
        size_bytes=size_bytes,
        uploaded_by=uploaded_by,
    )
    db.add(img)
    await db.commit()
    await db.refresh(img)

    return response


@router.post(
    "/cases/{case_id}/images:complete-multipart", status_code=status.HTTP_200_OK
)
async def complete_multipart(
    case_id: str,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Complete a multipart upload after all parts have been uploaded.

    Body: { "image_id": "uuid", "upload_id": "...", "parts": [{"ETag": "...", "PartNumber": 1}, ...] }
    """
    await require_case_access(db, request, case_id)
    image_id = body.get("image_id")
    upload_id = body.get("upload_id")
    parts = body.get("parts", [])

    if not image_id or not upload_id or not parts:
        raise HTTPException(
            status_code=400, detail="image_id, upload_id, and parts are required"
        )

    img = await db.get(ImageDB, image_id)
    if not img or img.case_id != case_id:
        raise HTTPException(status_code=404, detail="Image not found")

    storage = _storage()
    key = img.storage_uri.replace(f"s3://{settings.s3_bucket}/", "")

    try:
        storage.complete_multipart_upload(key, upload_id, parts)
    except Exception as exc:
        logger.error("Failed to complete multipart upload for %s: %s", key, exc)
        raise HTTPException(
            status_code=400, detail=f"Failed to complete multipart upload: {exc}"
        )

    try:
        head = storage.head_object(key)
        img.size_bytes = head.get("ContentLength", 0)
        img.checksum = (head.get("ETag") or "").strip('"')
    except Exception as exc:
        logger.error("S3 head_object failed for %s: %s", key, exc)

    await db.commit()
    await db.refresh(img)
    _publish_audit(
        "image.uploaded",
        case_id,
        image_id,
        img.uploaded_by or "unknown",
        {"size_bytes": img.size_bytes},
    )
    return _img_dict(img)


@router.post("/cases/{case_id}/images:abort-upload", status_code=status.HTTP_200_OK)
async def abort_upload(
    case_id: str,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """F6.7 — abortar un multipart upload iniciado por :request-upload.

    Antes no había endpoint público para abortar un multipart, por lo
    que los uploads huérfanos (por error del cliente, timeout o abuso)
    se acumulaban en SeaweedFS consumiendo recursos del backend. Sin
    esto, la mitigación de los topes era parcial: una petición con
    tope == MAX_NUM_PARTS podía dejar su multipart colgante si el
    cliente nunca completaba.

    Body: { "image_id": "uuid", "upload_id": "..." }

    Requiere ownership del caso. Elimina la fila de ``images`` sólo si
    nunca se confirmó la subida (``checksum = 'pending-upload'``); si
    la imagen ya está confirmada o tiene jobs, no la tocamos (hay que
    pasar por DELETE /images/{id} con las reglas de F6.5).
    """
    await require_case_access(db, request, case_id)
    image_id = body.get("image_id")
    upload_id = body.get("upload_id")
    if not image_id or not upload_id:
        raise HTTPException(
            status_code=400, detail="image_id and upload_id are required"
        )

    img = await db.get(ImageDB, image_id)
    if not img or img.case_id != case_id:
        raise HTTPException(status_code=404, detail="Image not found")

    storage = _storage()
    key = img.storage_uri.replace(f"s3://{settings.s3_bucket}/", "")

    try:
        storage.abort_multipart_upload(key, upload_id)
    except Exception as exc:  # noqa: BLE001
        # ``NoSuchUpload`` es idempotente — si ya estaba abortado, seguir.
        logger.warning("abort_multipart_upload %s/%s: %s", key, upload_id, exc)

    aborted_row = False
    if img.checksum == "pending-upload":
        # Nunca se confirmó → la fila es basura; se puede borrar.
        await db.delete(img)
        await db.commit()
        aborted_row = True

    _publish_audit(
        "image.upload_aborted",
        case_id,
        image_id,
        request.headers.get("x-user-id", "unknown"),
        {"upload_id": upload_id, "db_row_deleted": aborted_row},
    )
    return {
        "image_id": image_id,
        "upload_id": upload_id,
        "aborted": True,
        "db_row_deleted": aborted_row,
    }


@router.post("/cases/{case_id}/images:confirm-upload", status_code=status.HTTP_200_OK)
async def confirm_upload(
    case_id: str,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Confirm that a presigned upload completed. Verifies the object exists in S3.

    Body: { "image_id": "uuid" }
    """
    await require_case_access(db, request, case_id)
    image_id = body.get("image_id")
    if not image_id:
        raise HTTPException(status_code=400, detail="image_id is required")

    img = await db.get(ImageDB, image_id)
    if not img or img.case_id != case_id:
        raise HTTPException(status_code=404, detail="Image not found")

    storage = _storage()
    key = img.storage_uri.replace(f"s3://{settings.s3_bucket}/", "")

    try:
        head = storage.head_object(key)
        actual_size = head.get("ContentLength", 0)
        img.size_bytes = actual_size
        img.checksum = (head.get("ETag") or "").strip('"')
    except Exception as exc:
        logger.error("S3 head_object failed for %s: %s", key, exc)
        raise HTTPException(
            status_code=400,
            detail="Upload not found in storage. Did the upload complete?",
        )

    await db.commit()
    await db.refresh(img)
    _publish_audit(
        "image.uploaded",
        case_id,
        image_id,
        img.uploaded_by or "unknown",
        {"size_bytes": actual_size},
    )
    return _img_dict(img)


@router.get("/cases/{case_id}/images")
async def list_images(
    case_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    await require_case_access(db, request, case_id)
    # F6.5 — ocultar imágenes soft-deleted
    result = await db.execute(
        select(ImageDB).where(ImageDB.case_id == case_id, ImageDB.deleted_at.is_(None))
    )
    return [_img_dict(i) for i in result.scalars().all()]


@router.get("/images/{image_id}")
async def get_image(
    image_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    img = await db.get(ImageDB, image_id)
    if not img or img.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Image not found")
    # F6.5 — ownership por el caso de la imagen
    await require_case_access(db, request, img.case_id)
    return _img_dict(img)


@router.get("/images/{image_id}/viewer-url")
async def get_viewer_url(
    image_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    img = await db.get(ImageDB, image_id)
    if not img or img.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Image not found")
    # F6.5 — ownership antes de firmar una URL con lectura S3
    await require_case_access(db, request, img.case_id)
    storage = _storage()
    key = img.storage_uri.replace(f"s3://{settings.s3_bucket}/", "")
    url = storage.presigned_url(key, expires_in=3600)
    return {"image_id": image_id, "viewer_url": url}


@router.get("/artifacts/presigned-url")
async def get_artifact_presigned_url(
    request: Request,
    key: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """F6.8 — Devuelve una URL S3 presigned para el artifact.

    Este endpoint sustituye al flujo F6.8 inseguro
    (``GET /artifacts/presigned?key=...&access_token=<JWT>``):

    * El cliente llama aquí con el JWT en el header ``Authorization``
      (nunca en query).
    * El servidor valida ownership del ``case_id`` extraído de la key
      (igual que el endpoint viejo).
    * Devuelve ``{url, expires_in}`` — una URL S3 ya firmada por
      HMAC propio de S3 con TTL corto. **No contiene el JWT**.
    * El frontend pone esa URL directamente en ``<img src>``. Lo que
      viaja por la barra del navegador/CDN logs es la firma de S3,
      no un token de sesión reutilizable.

    Es el camino recomendado por el pentest §6.8. El endpoint viejo
    sigue existiendo con ``Deprecation: true`` y queda deshabilitado
    en pre-prod/prod vía ``AUTH_QUERY_TOKEN_ENABLED=false``.
    """
    try:
        key_case_id = case_id_from_key(key)
    except InvalidArtifactKey as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await require_case_access(db, request, key_case_id)

    storage = _storage()
    try:
        url = storage.presigned_url(key, expires_in=300)  # 5 min
    except Exception as exc:
        logger.error("Failed to presign artifact key=%s: %s", key, exc)
        raise HTTPException(status_code=404, detail="Artifact not found") from exc

    # No cachear el JSON (contiene URL firmada). El objeto en S3 se
    # puede cachear por el navegador por los 300 s de validez.
    return Response(
        content=f'{{"url": "{url}", "expires_in": 300}}',
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/artifacts/presigned")
async def get_artifact_presigned(
    request: Request,
    key: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Return artifact bytes from MinIO (proxy). Used by frontend <img> tags.

    F6.5 — la key DEBE empezar por un prefijo conocido y contener el
    ``case_id`` del artifact (``images/<cid>/..`` o ``results/<cid>/..``).
    Se valida que:

    1. El formato de la key sea seguro (sin ``..``, slashes iniciales,
       etc.) → 400.
    2. El caller tenga acceso al ``case_id`` extraído → 403.
    3. El caso no esté soft-deleted → 404 (coherente con el resto).

    Antes bastaba con conocer la key (p.ej. logs, response de otro user)
    para leer el objeto: eso rompía el BOLA del caso vecino.
    """
    try:
        key_case_id = case_id_from_key(key)
    except InvalidArtifactKey as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await require_case_access(db, request, key_case_id)

    storage = _storage()
    try:
        data = storage.download_bytes(key)
    except Exception as exc:
        logger.error("Failed to download artifact key=%s: %s", key, exc)
        raise HTTPException(status_code=404, detail=f"Artifact not found: {key}")

    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "bin"
    ct_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "tif": "image/tiff",
        "tiff": "image/tiff",
        "svs": "application/octet-stream",
        "svg": "image/svg+xml",
        "json": "application/json",
        "html": "text/html",
        "csv": "text/csv",
    }
    content_type = ct_map.get(ext, "application/octet-stream")
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
            # F6.8 — señalar a los clientes que este endpoint debe
            # migrarse al nuevo /artifacts/presigned-url, que no
            # requiere JWT en la URL.
            "Deprecation": "true",
            "Link": '</api/v1/artifacts/presigned-url>; rel="successor-version"',
        },  # datos clínicos
    )


def _img_dict(img: ImageDB) -> dict:
    return {
        "image_id": img.id,
        "case_id": img.case_id,
        "format": img.format,
        "storage_uri": img.storage_uri,
        "checksum": img.checksum,
        "size_bytes": img.size_bytes,
        "stain": img.stain,
        "magnification": img.magnification,
        "notes": img.notes,
        "uploaded_by": img.uploaded_by,
        "uploaded_at": img.uploaded_at.isoformat() if img.uploaded_at else None,
    }
