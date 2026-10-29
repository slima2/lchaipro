"""Prueba de punta a punta de lchai como la haría la webapp: login, permisos, paciente, caso, subida
(directa y con URL firmada), análisis, resultados, artefactos, grafo y auditoría. Al final borra lo que creó.

Uso: python3 e2e.py <public_url> <lamina.svs>
Usuarios (usuario:clave) por variables de entorno; por defecto, los de prueba de minikube:
  LCHAI_CLINICIAN (rol clinician), LCHAI_ADMIN (admin), LCHAI_AUDITOR (auditor)
Lámina pública de prueba (sin datos de pacientes):
  https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs
"""

import json
import os
import sys
import time

import requests
from kc_login import login  # mismo directorio: Python lo encuentra al ejecutar e2e.py

B, SLIDE = sys.argv[1], sys.argv[2]
API = B + "/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
ok_count = fail_count = 0
TIMEOUT = 120  # segundos por consulta (la subida de láminas puede tardar)


def check(name, cond, extra=""):
    global ok_count, fail_count
    ok_count += bool(cond)
    fail_count += not cond
    print(
        ("  OK   " if cond else "  FALLA")
        + f" {name}"
        + (f"  [{extra}]" if extra else "")
    )


def token(u, p):
    return login(B, u, p)


def h(t):
    return {"Authorization": f"Bearer {t}"}


print("== Login")


def user(var, default):
    return os.environ.get(var, default).split(":", 1)


clin = token(*user("LCHAI_CLINICIAN", "clinician1:Clinico-Local-2026"))
check("login clinician", clin)
adm = token(*user("LCHAI_ADMIN", "admin1:Admin-Local-2026x"))
check("login admin", adm)
aud = token(*user("LCHAI_AUDITOR", "auditor1:Auditor-Local-2026"))
check("login auditor", aud)

print("== Autenticación de la API")
check(
    "sin token -> 401",
    requests.get(f"{API}/patients", timeout=TIMEOUT).status_code == 401,
)
check(
    "token falso -> 401",
    requests.get(
        f"{API}/patients", headers=h(clin[:-6] + "AAAAAA"), timeout=TIMEOUT
    ).status_code
    == 401,
)
r = requests.get(f"{API}/patients", headers=h(clin), timeout=TIMEOUT)
check("clinician lista pacientes -> 200", r.status_code == 200, r.status_code)
r = requests.post(
    f"{API}/patients", headers=h(aud), json={"external_id": "x"}, timeout=TIMEOUT
)
check("auditor no crea pacientes -> 403", r.status_code == 403, r.status_code)

print("== Paciente y caso")
r = requests.post(
    f"{API}/patients",
    headers=h(clin),
    json={"external_id": f"e2e-{int(time.time())}"},
    timeout=TIMEOUT,
)
check("crear paciente", r.status_code in (200, 201), f"{r.status_code} {r.text[:150]}")
pid = r.json()["patient_id"]
r = requests.post(
    f"{API}/cases", headers=h(clin), json={"patient_id": pid}, timeout=TIMEOUT
)
check("crear caso", r.status_code in (200, 201), f"{r.status_code} {r.text[:150]}")
cid = r.json().get("case_id") or r.json().get("id")
r = requests.get(f"{API}/cases/{cid}", headers=h(clin), timeout=TIMEOUT)
check("leer caso", r.status_code == 200, r.status_code)

print("== Subida directa (por el gateway)")
with open(SLIDE, "rb") as f:
    r = requests.post(
        f"{API}/cases/{cid}/images:upload",
        headers=h(clin),
        files={"file": (os.path.basename(SLIDE), f, "application/octet-stream")},
        timeout=TIMEOUT,
    )
check("subir lámina", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
img = r.json()
iid = img.get("image_id") or img.get("id")
print("    imagen:", json.dumps(img)[:300])

print("== Subida con URL firmada (directo a S3, como las láminas grandes)")
size = os.path.getsize(SLIDE)
r = requests.post(
    f"{API}/cases/{cid}/images:request-upload",
    headers=h(clin),
    json={"filename": "firmada.svs", "size_bytes": size},
    timeout=TIMEOUT,
)
check(
    "pedir URL firmada", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}"
)
req = r.json()
if not req.get("multipart"):
    with open(SLIDE, "rb") as f:
        p = requests.put(
            req["presigned_url"],
            data=f,
            headers={
                "Content-Type": req.get("content_type") or "application/octet-stream"
            },
            timeout=TIMEOUT,
        )
    check(
        "PUT a S3 con la URL firmada",
        p.status_code == 200,
        f"{p.status_code} {req['presigned_url'][:60]}",
    )
    r = requests.post(
        f"{API}/cases/{cid}/images:confirm-upload",
        headers=h(clin),
        json={"image_id": req["image_id"]},
        timeout=TIMEOUT,
    )
    check(
        "confirmar subida",
        r.status_code in (200, 201),
        f"{r.status_code} {r.text[:150]}",
    )
r = requests.get(f"{API}/cases/{cid}/images", headers=h(clin), timeout=TIMEOUT)
n = (
    len(r.json())
    if isinstance(r.json(), list)
    else len(r.json().get("images", r.json().get("items", [])))
)
check("el caso tiene 2 imágenes", n == 2, n)

r = requests.get(f"{API}/images/{iid}/viewer-url", headers=h(clin), timeout=TIMEOUT)
check("viewer-url", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

print("== Análisis (worker en CPU)")
r = requests.post(
    f"{API}/images/{iid}:process",
    headers=h(clin),
    json={"case_id": cid, "use_choquet": True},
    timeout=TIMEOUT,
)
check(
    "lanzar análisis",
    r.status_code in (200, 201, 202),
    f"{r.status_code} {r.text[:200]}",
)
jid = r.json().get("job_id") or r.json().get("id")
t0 = time.time()
st = None
while time.time() - t0 < 1200:
    j = requests.get(f"{API}/jobs/{jid}", headers=h(clin), timeout=TIMEOUT).json()
    if j.get("status") != st:
        st = j.get("status")
        print(f"    {int(time.time() - t0):4d}s  {st}  {j.get('progress', '')}")
    if str(st).lower() in (
        "completed",
        "succeeded",
        "success",
        "failed",
        "error",
        "cancelled",
    ):
        break
    time.sleep(5)
check(
    "análisis completado",
    str(st).lower() in ("completed", "succeeded", "success"),
    f"{st} en {int(time.time() - t0)} s",
)
if str(st).lower() not in ("completed", "succeeded", "success"):
    print(json.dumps(j)[:800])

r = requests.get(f"{API}/images/{iid}/results/latest", headers=h(clin), timeout=TIMEOUT)
check("resultados", r.status_code == 200, r.status_code)
res = r.json()
print("    resultado:", json.dumps(res)[:600])
rb = res.get("result_bundle_id") or res.get("id")
r = requests.get(f"{API}/results/{rb}/artifacts", headers=h(clin), timeout=TIMEOUT)
check("lista de artefactos", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
arts = r.json() if isinstance(r.json(), list) else r.json().get("artifacts", [])
uris = [a.get("uri") or a.get("s3_uri") for a in arts if isinstance(a, dict)]
uris = [u for u in uris if u]
print(f"    {len(uris)} artefactos")
if uris:
    import re

    key = re.sub(r"^s3://[^/]+/", "", uris[0])
    u = f"{API}/artifacts/presigned"
    a = requests.get(u, params={"key": key, "access_token": clin}, timeout=TIMEOUT)
    check(
        "artefacto con access_token -> 200",
        a.status_code == 200,
        f"{a.status_code} {a.headers.get('content-type')} {len(a.content)} B",
    )
    check(
        "artefacto sin token -> 401",
        requests.get(u, params={"key": key}, timeout=TIMEOUT).status_code == 401,
    )

print("== Auditoría")
r = requests.get(f"{API}/graph/gene-associations", headers=h(clin), timeout=TIMEOUT)
check(
    "asociaciones gen-patrón (grafo)",
    r.status_code == 200,
    f"{r.status_code} {r.text[:100]}",
)
time.sleep(3)
r = requests.get(f"{API}/audit/events", headers=h(aud), timeout=TIMEOUT)
ev = r.json() if r.status_code == 200 else []
ev = ev if isinstance(ev, list) else ev.get("items", ev.get("events", []))
check(
    "auditor ve los eventos de esta prueba",
    r.status_code == 200 and any(e.get("case_id") == cid for e in ev),
    f"{r.status_code}, {len(ev)} eventos",
)

print("== Limpieza")
r = requests.delete(f"{API}/cases/{cid}", headers=h(clin), timeout=TIMEOUT)
check("borrar caso", r.status_code in (200, 204), f"{r.status_code} {r.text[:120]}")
r = requests.delete(f"{API}/patients/{pid}", headers=h(clin), timeout=TIMEOUT)
check("borrar paciente", r.status_code in (200, 204), f"{r.status_code} {r.text[:120]}")

print(f"\nResultado: {ok_count} OK, {fail_count} fallas")
sys.exit(1 if fail_count else 0)
