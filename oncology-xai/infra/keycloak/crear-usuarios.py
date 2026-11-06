#!/usr/bin/env python3
"""Crea los usuarios de lchai en un Keycloak existente (compartido, con otro realm) con la MISMA clave que en AWS.

Usa la API de administración de Keycloak: no toca la base de Keycloak ni hace falta importar un realm.
Cada usuario se crea con su hash (pbkdf2-sha256) tal como viene del respaldo, así que entra con su clave de siempre.

Entrada: el archivo de `exportar-usuarios.sh` ({"users": [...]}, con los hashes). NO va al repo.

Variables:
  KC_URL           URL de Keycloak, sin "/" final (p. ej. https://sso.telconet.ec)
  REALM            realm de destino (el de Telconet; no tiene que llamarse oncology)
  Autenticación, una de dos:
    KC_CLIENT_ID + KC_CLIENT_SECRET   cliente confidencial con cuenta de servicio del realm de destino,
                                      con los roles manage-users y view-clients (de realm-management)
    KC_ADMIN_USER + KC_ADMIN_PASS     usuario administrador (por admin-cli)
  KC_AUTH_REALM    realm donde se autentica (por defecto REALM; "master" para el admin global)
  ROLES            "realm" (por defecto): roles del realm clinician/admin/auditor
                   "cliente": roles del cliente oncology-api (para no crear roles globales en un realm compartido)
  EXISTENTES       qué hacer si el usuario ya existe (p. ej. creado en pre-prod con otra clave):
                   "omitir" (por defecto) | "clave": le reemplaza la clave por la de AWS y le asigna el rol
  SIMULAR=1        solo muestra lo que haría
  EXCLUIR          usuarios a no crear, separados por coma (por defecto los de demo: admin1,clinician1,auditor1)

Uso:
  KC_URL=... REALM=... KC_CLIENT_ID=... KC_CLIENT_SECRET=... python3 infra/keycloak/crear-usuarios.py usuarios.json
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROLES_LCHAI = ("clinician", "admin", "auditor")
CLIENTE_ROLES = "oncology-api"

KC = os.environ["KC_URL"].rstrip("/")
REALM = os.environ["REALM"]
AUTH_REALM = os.environ.get("KC_AUTH_REALM", REALM)
MODO_ROLES = os.environ.get("ROLES", "realm")
EXISTENTES = os.environ.get("EXISTENTES", "omitir")
SIMULAR = os.environ.get("SIMULAR") == "1"
EXCLUIR = {
    u.strip()
    for u in os.environ.get("EXCLUIR", "admin1,clinician1,auditor1").split(",")
    if u.strip()
}
if MODO_ROLES not in ("realm", "cliente") or EXISTENTES not in ("omitir", "clave"):
    sys.exit("ROLES debe ser realm|cliente y EXISTENTES omitir|clave")

_token = None


def token():
    global _token
    if os.environ.get("KC_CLIENT_ID"):
        datos = {
            "grant_type": "client_credentials",
            "client_id": os.environ["KC_CLIENT_ID"],
            "client_secret": os.environ["KC_CLIENT_SECRET"],
        }
    else:
        datos = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.environ["KC_ADMIN_USER"],
            "password": os.environ["KC_ADMIN_PASS"],
        }
    req = urllib.request.Request(
        f"{KC}/realms/{AUTH_REALM}/protocol/openid-connect/token",
        data=urllib.parse.urlencode(datos).encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        _token = json.load(r)["access_token"]


def api(metodo, ruta, cuerpo=None, reintento=True):
    req = urllib.request.Request(
        f"{KC}/admin/realms/{REALM}{ruta}",
        method=metodo,
        data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
        headers={
            "Authorization": f"Bearer {_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read()
            return r.status, (json.loads(texto) if texto else None), r.headers
    except urllib.error.HTTPError as e:
        if (
            e.code == 401 and reintento
        ):  # el token de admin dura poco: se renueva una vez
            token()
            return api(metodo, ruta, cuerpo, reintento=False)
        return e.code, e.read().decode(errors="replace")[:300], e.headers


def roles_destino():
    """Busca en el destino los roles de lchai, como roles del realm o del cliente oncology-api."""
    if MODO_ROLES == "realm":
        base = ""
    else:
        st, clientes, _ = api("GET", f"/clients?clientId={CLIENTE_ROLES}")
        if st != 200 or not clientes:
            sys.exit(
                f"No existe el cliente {CLIENTE_ROLES} en el realm {REALM} (ROLES=cliente)"
            )
        base = f"/clients/{clientes[0]['id']}"
    roles = {}
    for nombre in ROLES_LCHAI:
        st, rol, _ = api("GET", f"{base}/roles/{nombre}")
        if st == 200:
            roles[nombre] = rol
    faltan = [n for n in ROLES_LCHAI if n not in roles]
    if faltan:
        print(
            f"AVISO: el realm {REALM} no tiene los roles {faltan} ({MODO_ROLES}); esos usuarios quedan sin ese rol"
        )
    ruta_mapeo = (
        "realm" if MODO_ROLES == "realm" else f"clients/{base.rsplit('/', 1)[1]}"
    )
    return roles, ruta_mapeo


def roles_de(u):
    return [r for r in u.get("realmRoles", []) if r in ROLES_LCHAI]


def clave(u):
    return [
        {
            k: c[k]
            for k in (
                "type",
                "secretData",
                "credentialData",
                "createdDate",
                "userLabel",
            )
            if k in c
        }
        for c in u.get("credentials", [])
        if c.get("type") == "password"
    ]


def main():
    archivo = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
    usuarios = json.load(open(archivo))["users"]
    token()
    roles, ruta_mapeo = roles_destino()
    print(
        f"Destino: {KC} realm {REALM} | roles: {MODO_ROLES} | existentes: {EXISTENTES}"
        + (" | SIMULACIÓN" if SIMULAR else "")
    )
    cuenta = {
        "creados": 0,
        "clave reemplazada": 0,
        "ya existían": 0,
        "excluidos": 0,
        "sin rol de lchai": 0,
        "errores": 0,
    }

    for u in usuarios:
        nombre = u["username"]
        mis_roles = roles_de(u)
        if nombre in EXCLUIR:
            cuenta["excluidos"] += 1
            continue
        if not mis_roles:  # sin clinician/admin/auditor no pueden usar lchai
            cuenta["sin rol de lchai"] += 1
            continue
        cred = clave(u)
        st, encontrados, _ = api(
            "GET", f"/users?username={urllib.parse.quote(nombre)}&exact=true"
        )
        existente = encontrados[0] if st == 200 and encontrados else None

        if existente and EXISTENTES == "omitir":
            print(f"  = {nombre}: ya existe, no se toca")
            cuenta["ya existían"] += 1
            continue
        if SIMULAR:
            print(
                f"  ~ {nombre}: {'reemplazar clave' if existente else 'crear'} con roles {mis_roles}"
            )
            continue

        if existente:
            uid = existente["id"]
            # Se borra la clave actual (la de pre-prod) y se pone el hash de AWS
            _, creds, _ = api("GET", f"/users/{uid}/credentials")
            for c in creds or []:
                if c.get("type") == "password":
                    api("DELETE", f"/users/{uid}/credentials/{c['id']}")
            st, err, _ = api("PUT", f"/users/{uid}", {"credentials": cred})
            accion = "clave reemplazada"
        else:
            cuerpo = {
                k: u[k]
                for k in (
                    "username",
                    "email",
                    "firstName",
                    "lastName",
                    "emailVerified",
                    "enabled",
                    "requiredActions",
                    "attributes",
                )
                if k in u
            }
            cuerpo["credentials"] = cred
            st, err, cab = api("POST", "/users", cuerpo)
            uid = cab.get("Location", "").rsplit("/", 1)[-1] if st == 201 else None
            accion = "creados"
        if st not in (201, 204) or not uid:
            print(f"  ! {nombre}: error {st} {err}")
            cuenta["errores"] += 1
            continue

        a_dar = [roles[r] for r in mis_roles if r in roles]
        if a_dar:
            st, err, _ = api("POST", f"/users/{uid}/role-mappings/{ruta_mapeo}", a_dar)
            if st not in (200, 204):
                print(f"  ! {nombre}: creado pero sin rol ({st} {err})")
                cuenta["errores"] += 1
                continue
        print(
            f"  + {nombre}: {'clave de AWS puesta' if existente else 'creado'} con roles {mis_roles}"
        )
        cuenta[accion] += 1

    print("Resumen: " + ", ".join(f"{k} {v}" for k, v in cuenta.items()))
    sys.exit(1 if cuenta["errores"] else 0)


if __name__ == "__main__":
    main()
