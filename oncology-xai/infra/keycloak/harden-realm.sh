#!/usr/bin/env bash
# Endurece un realm "oncology" que ya existe en la base de datos (por ejemplo, el restaurado desde AWS).
# Aplica lo mismo que infra/keycloak/realm-export.json trae para instalaciones nuevas:
#   fuerza bruta, política de contraseñas, eventos, sin login social, cliente oncology-api solo bearer,
#   sin password grant en la webapp y mapper de audiencia "oncology-api" (lo exige el gateway).
#
# Uso: NS=lchai [PUBLIC_URL=https://lchai.ejemplo.ec] ./harden-realm.sh [--disable-demo-users]
# PUBLIC_URL: dominio nuevo del realm (obligatorio al migrar desde otro entorno).
# Requiere kubectl con acceso al namespace; se ejecuta dentro del pod de Keycloak con kcadm.sh.
set -euo pipefail
NS="${NS:-lchai}"
REALM="${REALM:-oncology}"
POD=deploy/keycloak   # un pod activo (no uno que se está apagando tras un reinicio)
PASS=$(kubectl -n "$NS" get secret lchai-secrets -o jsonpath='{.data.KEYCLOAK_ADMIN_PASSWORD}' | base64 -d)

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

if [[ "${1:-}" == "--disable-demo-users" ]]; then
  echo "Usuarios demo: deshabilitar admin1, clinician1, auditor1"
  for u in admin1 clinician1 auditor1; do
    id=$(kc get users -r "$REALM" -q username="$u" --fields id --format csv --noquotes)
    [[ -n "$id" ]] && kc update "users/$id" -r "$REALM" -s enabled=false && echo "  deshabilitado: $u"
  done
fi
echo "Listo. Revisar en la consola (port-forward) los usuarios con contraseñas débiles y forzar su cambio."
