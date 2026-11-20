#!/usr/bin/env bash
# Endurece un realm "oncology" que ya existe en la base de datos (por ejemplo, el restaurado desde AWS).
# Aplica lo mismo que infra/keycloak/realm-export.json trae para instalaciones nuevas:
#   fuerza bruta, política de contraseñas, eventos, sin login social, cliente oncology-api solo bearer,
#   sin password grant en la webapp y mapper de audiencia "oncology-api" (lo exige el gateway).
#
# Uso:
#   NS=lchai KC_ENV=prod [PUBLIC_URL=https://lchai.ejemplo.ec] \
#     ./harden-realm.sh [--disable-demo-users|--keep-demo-users]
#
#   NS          namespace de kubernetes (default: lchai).
#   REALM       nombre del realm (default: oncology).
#   PUBLIC_URL  dominio nuevo del realm (obligatorio al migrar desde otro entorno).
#   KC_ENV      dev | preprod | prod (default: dev si no se detecta otro).
#               En preprod / prod el script DESHABILITA Y ROTA la contraseña
#               de los usuarios demo (admin1, clinician1, auditor1) salvo que
#               se pase explícitamente --keep-demo-users (desaconsejado).
#
# F001 (pentest 2026-11-19) — los usuarios demo con contraseñas débiles
# (admin1/clinician1/auditor1) NO deben vivir en entornos productivos.
# El script fallará duro si detecta preprod/prod y no se decide qué hacer.
#
# Requiere kubectl con acceso al namespace; se ejecuta dentro del pod de Keycloak con kcadm.sh.
set -euo pipefail
NS="${NS:-lchai}"
REALM="${REALM:-oncology}"
POD=deploy/keycloak   # un pod activo (no uno que se está apagando tras un reinicio)
PASS=$(kubectl -n "$NS" get secret lchai-secrets -o jsonpath='{.data.KEYCLOAK_ADMIN_PASSWORD}' | base64 -d)

# ── F001: parsear flag explícito ─────────────────────────────────────
DEMO_ACTION="auto"   # auto | disable | keep
for arg in "$@"; do
  case "$arg" in
    --disable-demo-users) DEMO_ACTION="disable" ;;
    --keep-demo-users)    DEMO_ACTION="keep" ;;
    *) ;;
  esac
done

# ── F001: inferir el entorno si el usuario no lo dijo ────────────────
KC_ENV="${KC_ENV:-}"
if [[ -z "$KC_ENV" ]]; then
  # Heurística conservadora: si el dominio huele a prod, tratamos como prod.
  if [[ "${PUBLIC_URL:-}" =~ (prod|preprod|telconet) ]]; then
    KC_ENV="prod"
  else
    KC_ENV="dev"
  fi
fi

if [[ "$KC_ENV" != "dev" && "$DEMO_ACTION" == "auto" ]]; then
  # Fail-safe (F001): en pre-prod/prod obligamos a tomar decisión explícita.
  # El default de facto es --disable-demo-users: dejar la cuenta demo activa
  # en prod es la raíz del F001 que no queremos re-abrir.
  echo "ERROR: KC_ENV=$KC_ENV. Debe pasarse --disable-demo-users o --keep-demo-users." >&2
  echo "       Para pre-prod/prod el estándar es --disable-demo-users." >&2
  exit 2
fi
if [[ "$DEMO_ACTION" == "auto" ]]; then
  DEMO_ACTION="keep"   # dev: no tocar demos sin orden explícita
fi

kc() { kubectl -n "$NS" exec "$POD" -- /opt/keycloak/bin/kcadm.sh "$@"; }

kc config credentials --server http://localhost:8080 --realm master --user admin --password "$PASS" >/dev/null

echo "Realm: fuerza bruta, política de contraseñas, eventos y sesiones"
kc update "realms/$REALM" \
  -s bruteForceProtected=true -s permanentLockout=false -s failureFactor=5 \
  -s waitIncrementSeconds=60 -s maxFailureWaitSeconds=900 -s minimumQuickLoginWaitSeconds=60 \
  -s 'passwordPolicy="length(12) and digits(1) and upperCase(1) and lowerCase(1) and specialChars(1) and notUsername and passwordHistory(5)"' \
  -s eventsEnabled=true -s eventsExpiration=7776000 -s adminEventsEnabled=true -s adminEventsDetailsEnabled=true \
  -s sslRequired=external -s accessTokenLifespan=300 -s ssoSessionIdleTimeout=1800 -s ssoSessionMaxLifespan=36000 \
  -s revokeRefreshToken=true -s refreshTokenMaxReuse=0

echo "Rol por defecto: quitar 'clinician' de default-roles"
DEF_ROLE="default-roles-$REALM"
kc remove-roles -r "$REALM" --rname "$DEF_ROLE" --rolename clinician 2>/dev/null || true

echo "Login social: deshabilitar identity providers"
for idp in $(kc get identity-provider/instances -r "$REALM" --fields alias --format csv --noquotes 2>/dev/null); do
  kc update "identity-provider/instances/$idp" -r "$REALM" -s enabled=false && echo "  deshabilitado: $idp"
done

client_id() { kc get clients -r "$REALM" -q clientId="$1" --fields id --format csv --noquotes; }

echo "Cliente oncology-api: solo bearer, sin flujos ni redirecciones"
API=$(client_id oncology-api)
kc update "clients/$API" -r "$REALM" -s bearerOnly=true -s standardFlowEnabled=false -s directAccessGrantsEnabled=false \
  -s serviceAccountsEnabled=false -s implicitFlowEnabled=false -s 'redirectUris=[]' -s 'webOrigins=[]'
kc create "clients/$API/client-secret" -r "$REALM" >/dev/null   # rota el secreto que estaba en el repo

echo "Cliente oncology-webapp: sin password grant, PKCE S256 y audiencia oncology-api"
WEB=$(client_id oncology-webapp)
kc update "clients/$WEB" -r "$REALM" -s directAccessGrantsEnabled=false -s implicitFlowEnabled=false \
  -s 'attributes."pkce.code.challenge.method"=S256'
if [[ -n "${PUBLIC_URL:-}" ]]; then
  # Realm migrado de otro entorno: sus URL de redirección apuntan al dominio viejo y el login se rechaza
  echo "Cliente oncology-webapp: URL de redirección y origen -> $PUBLIC_URL"
  kc update "clients/$WEB" -r "$REALM" -s "redirectUris=[\"$PUBLIC_URL/*\"]" -s "webOrigins=[\"$PUBLIC_URL\"]" \
    -s "rootUrl=$PUBLIC_URL" -s "baseUrl=/"
fi
if ! kc get "clients/$WEB/protocol-mappers/models" -r "$REALM" --fields name --format csv --noquotes | grep -qx oncology-api-audience; then
  kc create "clients/$WEB/protocol-mappers/models" -r "$REALM" -s name=oncology-api-audience -s protocol=openid-connect \
    -s protocolMapper=oidc-audience-mapper -s 'config."included.client.audience"=oncology-api' \
    -s 'config."access.token.claim"=true' -s 'config."id.token.claim"=false'
fi

# ── F001: aplicar la decisión sobre los demo users ──────────────────
case "$DEMO_ACTION" in
  disable)
    echo "Usuarios demo: deshabilitar y ROTAR contraseña de admin1, clinician1, auditor1"
    for u in admin1 clinician1 auditor1; do
      id=$(kc get users -r "$REALM" -q username="$u" --fields id --format csv --noquotes)
      if [[ -n "$id" ]]; then
        # 1. Desactivar la cuenta
        kc update "users/$id" -r "$REALM" -s enabled=false
        # 2. Rotar contraseña a una aleatoria (32 bytes base64) para que,
        #    si alguien vuelve a enabled=true por error en la consola,
        #    la contraseña débil original ya no sirva.
        rand_pwd=$(LC_ALL=C tr -dc 'A-Za-z0-9!@#%^*' </dev/urandom | head -c 32)
        kc set-password -r "$REALM" --userid "$id" --new-password "$rand_pwd" >/dev/null
        # 3. Marcar como "cambio obligatorio" por si acaso se reactiva.
        kc update "users/$id" -r "$REALM" -s 'requiredActions=["UPDATE_PASSWORD"]' >/dev/null || true
        echo "  deshabilitado + rotado: $u"
      else
        echo "  no existe en el realm: $u (ok)"
      fi
    done
    ;;
  keep)
    if [[ "$KC_ENV" != "dev" ]]; then
      echo "AVISO (F001): usuarios demo permanecen habilitados en KC_ENV=$KC_ENV por --keep-demo-users."
      echo "              Esto NO es aceptable en producción; documentar excepción y timebox."
    else
      echo "Usuarios demo: se dejan como están (KC_ENV=dev)."
    fi
    ;;
esac

echo "Listo. Revisar en la consola (port-forward) los usuarios con contraseñas débiles y forzar su cambio."
