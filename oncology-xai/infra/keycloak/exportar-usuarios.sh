#!/usr/bin/env bash
# Exporta los usuarios del realm "oncology" (claves cifradas y roles) desde la base de lchai restaurada del respaldo,
# para importarlos en un Keycloak externo (el del cluster de Telconet) con "Partial import".
#
# Se corre DESPUÉS de `migrar-datos.sh bd`. Las tablas de Keycloak llegan en esa base aunque lchai use un Keycloak externo.
# 1. Copia la base a una base temporal (kc_export), para no tocar la de lchai.
# 2. Un pod temporal de Keycloak lee esa copia y exporta el realm con sus usuarios (kc.sh export).
# 3. Deja solo los usuarios de personas (sin la cuenta técnica service-account-*) en el archivo de salida.
# 4. Borra la base temporal y el pod.
#
# Uso: NS=lchai [KC_IMAGE=<imagen de Keycloak 26>] infra/keycloak/exportar-usuarios.sh <archivo-salida.json>
# El archivo trae los hashes de las contraseñas: NO va al repo; se entrega por un canal seguro y se borra al importar.
set -euo pipefail
NS="${NS:-lchai}"
OUT="${1:?falta el archivo de salida (p. ej. oncology-usuarios.json)}"
KC_IMAGE="${KC_IMAGE:-registry.gitlab.telconet.ec/procesos/devops/deploys/txt/solca/lchai/deps/keycloak:26.8.1-r1}"
k() { kubectl -n "$NS" "$@"; }
cfg() { k get configmap lchai-config -o jsonpath="{.data.$1}"; }
PG_POD=$(k get pod -l app=postgres -o name | head -1)
PG_USER=$(cfg POSTGRES_USER); PG_DB=$(cfg POSTGRES_DB)
psql() { k exec -i "$PG_POD" -- psql -U "$PG_USER" -v ON_ERROR_STOP=1 -q "$@"; }

echo "==> Copia temporal de la base ($PG_DB -> kc_export)"
psql -d postgres -c "DROP DATABASE IF EXISTS kc_export;" -c "CREATE DATABASE kc_export;"
k exec "$PG_POD" -- sh -c "pg_dump -U '$PG_USER' -d '$PG_DB' --no-owner | psql -U '$PG_USER' -d kc_export -q -v ON_ERROR_STOP=1 >/dev/null"

limpiar() {
  k delete pod kc-export --ignore-not-found --wait=false >/dev/null 2>&1 || true
  psql -d postgres -c "DROP DATABASE IF EXISTS kc_export;" >/dev/null 2>&1 || true
}
trap limpiar EXIT

echo "==> Exportando el realm con Keycloak ($KC_IMAGE)"
k delete pod kc-export --ignore-not-found >/dev/null
k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: kc-export
  labels: { app: db-schema }      # la NetworkPolicy le permite llegar a PostgreSQL
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: keycloak
      image: $KC_IMAGE
      command: ["/bin/sh", "-c"]
      # La primera vez Keycloak actualiza el esquema de la copia (venía de Keycloak 23); después exporta y espera
      # a que el script lea el archivo.
      args:
        - /opt/keycloak/bin/kc.sh export --optimized --realm oncology --users same_file --file /out/oncology.json
          && touch /out/listo || touch /out/error; sleep 600
      env:
        - { name: KC_DB_URL, value: "jdbc:postgresql://postgres:5432/kc_export" }
        # El export no migra el esquema por su cuenta: se le permite actualizar la copia (venía de Keycloak 23)
        - { name: KC_SPI_CONNECTIONS_JPA__QUARKUS__MIGRATION_STRATEGY, value: update }
        - { name: KC_DB_USERNAME, value: "$PG_USER" }
        - name: KC_DB_PASSWORD
          valueFrom: { secretKeyRef: { name: lchai-secrets, key: POSTGRES_PASSWORD } }
      volumeMounts: [{ name: out, mountPath: /out }, { name: tmp, mountPath: /tmp }]
      resources: { limits: { cpu: "1", memory: 1Gi } }
  volumes: [{ name: out, emptyDir: {} }, { name: tmp, emptyDir: {} }]
EOF
for _ in $(seq 1 90); do
  k exec kc-export -- test -f /out/listo 2>/dev/null && break
  k exec kc-export -- test -f /out/error 2>/dev/null && { echo "El export falló:"; k logs kc-export | tail -20; exit 1; }
  sleep 5
done
k exec kc-export -- test -f /out/listo || { echo "El export no terminó"; k logs kc-export | tail -20; exit 1; }

umask 077
k exec kc-export -- cat /out/oncology.json | python3 -c '
import json, sys
realm = json.load(sys.stdin)
users = [u for u in realm.get("users", []) if not u["username"].startswith("service-account-")]
for u in users:
    u.pop("id", None)                      # Keycloak asigna ids nuevos al importar
json.dump({"users": users}, open(sys.argv[1], "w"), indent=1, ensure_ascii=False)
con_clave = sum(1 for u in users if any(c.get("type") == "password" for c in u.get("credentials", [])))
roles = {}
for u in users:
    for r in u.get("realmRoles", []):
        roles[r] = roles.get(r, 0) + 1
print(f"usuarios: {len(users)}  con clave: {con_clave}  roles: {roles}")
' "$OUT"
echo "Listo: $OUT (permisos 600). Contiene hashes de contraseñas: entregarlo por un canal seguro y borrarlo al importar."
