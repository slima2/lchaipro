#!/usr/bin/env bash
# Migra los datos de lchai (base de datos con los usuarios de Keycloak, y archivos de S3) a un lchai ya
# desplegado con el chart. Los datos pasan por pods dentro del cluster de destino: no se bajan a la laptop.
# Guía completa: infra/migracion/README.md
#
# Uso: NS=lchai ./migrar-datos.sh bd | s3 | keycloak | verificar
#
#   bd        Copia la base completa (pg_dump -> pg_restore) y completa el esquema. Detiene la app mientras tanto.
#             REEMPLAZA todo el contenido de la base de destino (pensado para un lchai recién instalado).
#             Variables: SRC_PG_HOST, SRC_PG_PASSWORD [SRC_PG_USER=oncology SRC_PG_DB=oncology_xai SRC_PG_SSLMODE=require]
#             O desde un volcado en S3 (formato pg_restore): SRC_DUMP=s3://<bucket>/<ruta>.dump (credenciales como en s3)
#   s3        Copia archivos del bucket de origen al S3 del cluster (o a DEST_S3_ENDPOINT).
#             Credenciales de origen: las del perfil de aws cli (AWS_PROFILE) o AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.
#             Variables: [SRC_BUCKET=lchai-prod-artifacts DEST_BUCKET=<igual> PREFIJOS="images/<caso>/ results/<caso>/"]
#             SRC_PREFIX=<carpeta>/ lee desde esa carpeta del bucket de origen (p. ej. un respaldo en
#             s3/lchai-prod-artifacts/) y escribe en la raíz del destino, así las rutas de la base siguen valiendo.
#             INCLUIR="*/ctranspath.pth *.json" copia solo los archivos que coinciden (patrones de aws s3 sync).
#             Sin PREFIJOS copia el bucket completo (solo la versión vigente de cada objeto).
#   keycloak  Deja la clave del admin de Keycloak (realm master) igual a la del Secret, endurece el realm y
#             registra el dominio nuevo. Variable: PUBLIC_URL=https://<domain>
#   verificar Cuenta pacientes, casos, imágenes, resultados, usuarios y objetos en S3.
set -euo pipefail

NS="${NS:-lchai}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
AWS_IMAGE="${AWS_IMAGE:-amazon/aws-cli:2.17.0}"
k() { kubectl -n "$NS" "$@"; }
log() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
cfg() { k get configmap lchai-config -o jsonpath="{.data.$1}"; }
# Deployment de Keycloak por su selector (con workloadPrefix/Suffix el nombre cambia)
kc_deploy() { k get deploy -o jsonpath='{range .items[?(@.spec.selector.matchLabels.app=="keycloak")]}{.metadata.name}{end}'; }

# Corre un pod hasta que termina, muestra su log y lo borra. $1 = nombre, stdin = manifiesto
run_pod() {
  local name=$1
  k delete pod "$name" --ignore-not-found >/dev/null
  k apply -f - >/dev/null
  k wait --for=condition=Ready "pod/$name" --timeout=300s >/dev/null 2>&1 || true
  k logs -f "$name" || true
  local phase
  for _ in $(seq 1 60); do
    phase=$(k get pod "$name" -o jsonpath='{.status.phase}')
    [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
    sleep 5
  done
  k delete pod "$name" --wait=false >/dev/null
  [[ "$phase" == Succeeded ]] || { echo "El pod $name terminó en $phase"; exit 1; }
}

# Secret temporal con las credenciales de AWS de origen: variables de entorno o el perfil de aws cli
secreto_aws_origen() {
  local creds
  if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
    creds=$(aws configure export-credentials --format env-no-export)
    eval "$creds"
  fi
  k create secret generic lchai-migracion-aws --dry-run=client -o yaml \
    --from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    --from-literal=AWS_SESSION_TOKEN="${AWS_SESSION_TOKEN:-}" | k apply -f - >/dev/null
}
aws_env() {
  cat <<YAML
        - { name: AWS_DEFAULT_REGION, value: "${AWS_REGION:-us-east-1}" }
        - name: AWS_ACCESS_KEY_ID
          valueFrom: { secretKeyRef: { name: lchai-migracion-aws, key: AWS_ACCESS_KEY_ID } }
        - name: AWS_SECRET_ACCESS_KEY
          valueFrom: { secretKeyRef: { name: lchai-migracion-aws, key: AWS_SECRET_ACCESS_KEY } }
        - name: AWS_SESSION_TOKEN
          valueFrom: { secretKeyRef: { name: lchai-migracion-aws, key: AWS_SESSION_TOKEN } }
YAML
}

paso_bd() {
  local obtener init=""
  if [[ -z "${SRC_DUMP:-}" ]]; then
    : "${SRC_PG_HOST:?falta SRC_PG_HOST (o SRC_DUMP=s3://...)}" "${SRC_PG_PASSWORD:?falta SRC_PG_PASSWORD}"
  fi
  local replicas
  log "Deteniendo la app (Keycloak y servicios) para que nadie escriba durante la copia"
  replicas=$(k get deploy -o jsonpath='{range .items[*]}{.metadata.name}={.spec.replicas} {end}')
  k scale deploy --all --replicas=0 >/dev/null
  k wait --for=delete pod -l 'app notin (postgres,redis,s3,rabbitmq,fuseki)' --timeout=180s >/dev/null 2>&1 || true

  if [[ -n "${SRC_DUMP:-}" ]]; then
    # Un initContainer baja el volcado a un volumen compartido y el contenedor de PostgreSQL lo restaura
    secreto_aws_origen
    obtener='echo "volcado: $(du -h /dump/lchai.dump | cut -f1)"'
    init="  initContainers:
    - name: bajar-volcado
      image: $AWS_IMAGE
      command: [\"/bin/sh\", \"-c\", \"aws s3 cp '$SRC_DUMP' /dump/lchai.dump --only-show-errors\"]
      env:
$(aws_env)
      volumeMounts: [{ name: dump, mountPath: /dump }]"
    log "Restaurando la base desde $SRC_DUMP -> $(cfg POSTGRES_HOST)"
  else
    k create secret generic lchai-migracion-origen --dry-run=client -o yaml \
      --from-literal=PGPASSWORD="$SRC_PG_PASSWORD" | k apply -f - >/dev/null
    obtener='echo "origen: $(PGPASSWORD=$SRC_PASS psql "$SRC" -Atc "select count(*) from patients") pacientes"
          PGPASSWORD=$SRC_PASS pg_dump "$SRC" -Fc --no-owner --no-privileges -f /dump/lchai.dump
          echo "dump: $(du -h /dump/lchai.dump | cut -f1)"'
    log "Copiando la base: $SRC_PG_HOST -> $(cfg POSTGRES_HOST)"
  fi
  run_pod migrar-bd <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: migrar-bd
  labels: { app: db-schema }      # la NetworkPolicy le permite llegar a PostgreSQL
spec:
  restartPolicy: Never
$init
  containers:
    - name: pg
      image: $PG_IMAGE
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -e
          $obtener
          # Se restaura sobre un esquema vacío: la instalación ya creó tablas (init.sql y Keycloak) y restaurar
          # encima con --clean choca con sus dependencias. Destino recién instalado: no hay nada que perder.
          psql "\$DEST" -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
          pg_restore --no-owner --no-privileges --exit-on-error -d "\$DEST" /dump/lchai.dump
          psql "\$DEST" -v ON_ERROR_STOP=1 -q -f /sql/init.sql      # columnas y tablas nuevas, si faltan
          echo "destino: \$(psql "\$DEST" -Atc 'select count(*) from patients') pacientes"
      env:
        - { name: SRC, value: "host=${SRC_PG_HOST:-} port=${SRC_PG_PORT:-5432} dbname=${SRC_PG_DB:-oncology_xai} user=${SRC_PG_USER:-oncology} sslmode=${SRC_PG_SSLMODE:-require}" }
        - name: SRC_PASS
          valueFrom: { secretKeyRef: { name: lchai-migracion-origen, key: PGPASSWORD, optional: true } }
        - { name: DEST, value: "host=$(cfg POSTGRES_HOST) port=$(cfg POSTGRES_PORT) dbname=$(cfg POSTGRES_DB) user=$(cfg POSTGRES_USER)" }
        - name: PGPASSWORD
          valueFrom: { secretKeyRef: { name: lchai-secrets, key: POSTGRES_PASSWORD } }
        - { name: PGOPTIONS, value: "-c client_min_messages=warning" }
      volumeMounts: [{ name: sql, mountPath: /sql }, { name: dump, mountPath: /dump }]
  volumes: [{ name: sql, configMap: { name: lchai-db-schema } }, { name: dump, emptyDir: {} }]
EOF
  k delete secret lchai-migracion-origen lchai-migracion-aws --ignore-not-found >/dev/null

  log "Levantando la app otra vez"
  for r in $replicas; do k scale deploy "${r%%=*}" --replicas="${r##*=}" >/dev/null; done
  [[ -n "$(kc_deploy)" ]] && k rollout status "deploy/$(kc_deploy)" --timeout=600s
  echo "Siguiente: PUBLIC_URL=https://<domain> $0 keycloak"
}

paso_s3() {
  local src_bucket="${SRC_BUCKET:-lchai-prod-artifacts}" src_prefix="${SRC_PREFIX:-}" dest_bucket dest_ep
  dest_bucket="${DEST_BUCKET:-$src_bucket}"
  dest_ep="${DEST_S3_ENDPOINT:-http://s3:8333}"
  local filtro="" pat
  if [[ -n "${INCLUIR:-}" ]]; then
    filtro='--exclude "*"'
    for pat in $INCLUIR; do filtro="$filtro --include \"$pat\""; done
  fi
  secreto_aws_origen
  log "Copiando s3://$src_bucket/$src_prefix${PREFIJOS:-(todo)} -> $dest_ep/$dest_bucket"
  run_pod migrar-s3 <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: migrar-s3
  labels: { app: s3-buckets }     # la NetworkPolicy le permite llegar al S3 del cluster
spec:
  restartPolicy: Never
  containers:
    - name: aws
      image: $AWS_IMAGE
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -e
          # 4 transferencias a la vez (aws usa 10): con 10 láminas grandes en paralelo, SeaweedFS se quedaba sin memoria
          export AWS_CONFIG_FILE=/data/.aws-config
          printf '[default]\ns3 =\n  max_concurrent_requests = 4\n' > \$AWS_CONFIG_FILE
          for p in \${PREFIJOS:-""}; do
            echo "-> \${p:-bucket completo}"
            aws s3 sync "s3://$src_bucket/$src_prefix\$p" "/data/\$p" --only-show-errors $filtro
            AWS_ACCESS_KEY_ID=\$DEST_AK AWS_SECRET_ACCESS_KEY=\$DEST_SK AWS_SESSION_TOKEN= \
              aws s3 sync "/data/\$p" "s3://$dest_bucket/\$p" --endpoint-url "$dest_ep" --only-show-errors
            rm -rf "/data/\$p"
          done
          echo "objetos en destino: \$(AWS_ACCESS_KEY_ID=\$DEST_AK AWS_SECRET_ACCESS_KEY=\$DEST_SK AWS_SESSION_TOKEN= \
            aws s3 ls "s3://$dest_bucket/" --recursive --endpoint-url "$dest_ep" | wc -l)"
      env:
        - { name: PREFIJOS, value: "${PREFIJOS:-}" }
$(aws_env)
        - name: DEST_AK
          valueFrom: { secretKeyRef: { name: lchai-secrets, key: S3_ACCESS_KEY } }
        - name: DEST_SK
          valueFrom: { secretKeyRef: { name: lchai-secrets, key: S3_SECRET_KEY } }
      volumeMounts: [{ name: data, mountPath: /data }]
  volumes: [{ name: data, emptyDir: {} }]   # se copia por prefijo; el pod necesita disco para el prefijo más grande
EOF
  k delete secret lchai-migracion-aws >/dev/null
}

paso_keycloak() {
  : "${PUBLIC_URL:?falta PUBLIC_URL (https://<domain>)}"
  log "Clave del admin de Keycloak (realm master) = la del Secret lchai-secrets"
  # El realm master llega con la clave del entorno de origen. Se reemplaza su hash (PBKDF2-SHA256, como Keycloak 23).
  local pass sql
  pass=$(k get secret lchai-secrets -o jsonpath='{.data.KEYCLOAK_ADMIN_PASSWORD}' | base64 -d)
  sql=$(KC_PASS="$pass" python3 - <<'PY'
import base64, hashlib, json, os
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac("sha256", os.environ["KC_PASS"].encode(), salt, 27500, 32)
secret = json.dumps({"value": base64.b64encode(dk).decode(), "salt": base64.b64encode(salt).decode(), "additionalParameters": {}})
data = json.dumps({"hashIterations": 27500, "algorithm": "pbkdf2-sha256", "additionalParameters": {}})
print(f"""UPDATE credential SET secret_data = '{secret}', credential_data = '{data}'
WHERE type = 'password' AND user_id IN (SELECT u.id FROM user_entity u JOIN realm r ON r.id = u.realm_id
  WHERE r.name = 'master' AND u.username = 'admin');""")
PY
)
  k exec -i "$(k get pod -l app=postgres -o name | head -1)" -- \
    psql -U "$(cfg POSTGRES_USER)" -d "$(cfg POSTGRES_DB)" -c "$sql" 2>/dev/null \
    || { echo "PostgreSQL no está en el cluster: correr este UPDATE en la base administrada:"; echo "$sql"; }
  k rollout restart "deploy/$(kc_deploy)" >/dev/null && k rollout status "deploy/$(kc_deploy)" --timeout=600s

  log "Endureciendo el realm y registrando $PUBLIC_URL"
  # F001 — la migración siempre apunta a pre-prod/prod (venimos de AWS);
  # forzamos --disable-demo-users para cortar el riesgo de las cuentas
  # demo con contraseñas conocidas (admin1/clinician1/auditor1). El
  # operador puede saltarse esto exportando KC_KEEP_DEMO=1 antes de
  # correr el script (desaconsejado, y queda en los logs).
  _kc_demo_flag="--disable-demo-users"
  if [[ "${KC_KEEP_DEMO:-0}" == "1" ]]; then
    _kc_demo_flag="--keep-demo-users"
    log "AVISO: KC_KEEP_DEMO=1 → los usuarios demo NO se deshabilitarán. F001 no queda cerrado."
  fi
  NS="$NS" PUBLIC_URL="$PUBLIC_URL" KC_ENV="${KC_ENV:-prod}" \
    "$HERE/../keycloak/harden-realm.sh" "$_kc_demo_flag"
}

paso_verificar() {
  log "Base de datos"
  k exec "$(k get pod -l app=postgres -o name | head -1)" -- psql -U "$(cfg POSTGRES_USER)" -d "$(cfg POSTGRES_DB)" -c "
    select (select count(*) from patients) pacientes, (select count(*) from cases) casos,
           (select count(*) from images) imagenes, (select count(*) from result_bundles) resultados,
           (select count(*) from user_entity u join realm r on r.id = u.realm_id where r.name = 'oncology') usuarios"
  log "S3 (objetos por carpeta)"
  local ak sk
  ak=$(k get secret lchai-secrets -o jsonpath='{.data.S3_ACCESS_KEY}' | base64 -d)
  sk=$(k get secret lchai-secrets -o jsonpath='{.data.S3_SECRET_KEY}' | base64 -d)
  k run migrar-ver --rm -i --restart=Never --image="$AWS_IMAGE" --labels=app=s3-buckets \
    --env=AWS_ACCESS_KEY_ID="$ak" --env=AWS_SECRET_ACCESS_KEY="$sk" --env=AWS_DEFAULT_REGION=us-east-1 --command -- \
    sh -c "aws s3 ls s3://$(cfg S3_BUCKET)/ --recursive --endpoint-url http://s3:8333 | awk '{split(\$4,a,\"/\"); n[a[1]]++} END {for (k in n) print n[k], k}'" 2>/dev/null
}

case "${1:-}" in
  bd) paso_bd ;;
  s3) paso_s3 ;;
  keycloak) paso_keycloak ;;
  verificar) paso_verificar ;;
  *) sed -n 2,20p "$0"; exit 1 ;;
esac
