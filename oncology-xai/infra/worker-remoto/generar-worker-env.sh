#!/usr/bin/env bash
# Genera worker.env para el worker remoto a partir del lchai desplegado (kubectl apuntando al cluster de Telconet).
# Toma las claves del Secret lchai-secrets y reemplaza los hosts internos por las direcciones de los servicios
# *-remoto (remoteWorker.enabled=true en el chart).
#
# Uso: NS=lchai ./generar-worker-env.sh [archivo-salida]
#   NODE_IP=<ip>       con remoteWorker.serviceType=NodePort: IP de un nodo alcanzable desde el worker
#   S3_PUBLICO=<url>   URL de S3 que usará el worker (por defecto, S3_ENDPOINT del ConfigMap)
# El archivo queda con permisos 600: contiene claves. No subirlo a git ni compartirlo por chat.
set -euo pipefail
NS="${NS:-lchai}"
OUT="${1:-worker.env}"
k() { kubectl -n "$NS" "$@"; }
sec() { k get secret lchai-secrets -o jsonpath="{.data.$1}" | base64 -d; }
cfg() { k get configmap lchai-config -o jsonpath="{.data.$1}"; }

# Dirección (host:puerto) de un servicio *-remoto
addr() {
  local svc="$1-remoto" type ip port
  type=$(k get svc "$svc" -o jsonpath='{.spec.type}') || { echo "falta el servicio $svc (remoteWorker.enabled)" >&2; exit 1; }
  if [ "$type" = "NodePort" ]; then
    ip="${NODE_IP:?con NodePort define NODE_IP}"
    port=$(k get svc "$svc" -o jsonpath='{.spec.ports[0].nodePort}')
  else
    ip=$(k get svc "$svc" -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')
    [ -n "$ip" ] || { echo "el balanceador de $svc todavía no tiene IP" >&2; exit 1; }
    port=$(k get svc "$svc" -o jsonpath='{.spec.ports[0].port}')
  fi
  echo "$ip:$port"
}

# Redis y PostgreSQL: los del cluster (localDeps); si son administrados se dejan sus direcciones tal cual
A_RABBIT=$(addr rabbitmq)
A_REDIS=""; k get svc redis-remoto >/dev/null 2>&1 && A_REDIS=$(addr redis)
A_PG="";    k get svc postgres-remoto >/dev/null 2>&1 && A_PG=$(addr postgres)
export A_RABBIT A_REDIS A_PG

export V_RABBITMQ_URL="$(sec RABBITMQ_URL)" V_REDIS_URL="$(sec REDIS_URL)"
export V_POSTGRES_DSN="$(sec POSTGRES_DSN)" V_POSTGRES_DSN_SYNC="$(sec POSTGRES_DSN_SYNC)"
export V_S3_ACCESS_KEY="$(sec S3_ACCESS_KEY)" V_S3_SECRET_KEY="$(sec S3_SECRET_KEY)"
export V_S3_ENDPOINT="${S3_PUBLICO:-$(cfg S3_ENDPOINT)}" V_S3_BUCKET="$(cfg S3_BUCKET)"
export V_MODEL_BACKEND="$(cfg MODEL_BACKEND)" V_V2_MAX_TILES_WSI="$(cfg V2_MAX_TILES_WSI)"
export V_LOG_LEVEL="$(cfg LOG_LEVEL)" V_ENVIRONMENT="$(cfg ENVIRONMENT)"

umask 077
python3 - "$OUT" <<'PY'
import os, re, sys
e = os.environ

def host(url, new):
    """Reemplaza host:puerto en scheme://usuario:clave@host:puerto/... (sin tocar usuario ni clave)."""
    if not new:
        return url
    head, sep, tail = url.rpartition("@")
    return head + sep + re.sub(r"^[^/?]+", new, tail, count=1)

v = {
    "RABBITMQ_URL": host(e["V_RABBITMQ_URL"], e["A_RABBIT"]),
    "REDIS_URL": host(e["V_REDIS_URL"], e["A_REDIS"]),
    "POSTGRES_DSN": host(e["V_POSTGRES_DSN"], e["A_PG"]),
    "POSTGRES_DSN_SYNC": host(e["V_POSTGRES_DSN_SYNC"], e["A_PG"]),
    "S3_ENDPOINT": e["V_S3_ENDPOINT"],
    "S3_ENDPOINT_URL": e["V_S3_ENDPOINT"],
    "S3_BUCKET": e["V_S3_BUCKET"],
    "S3_ACCESS_KEY": e["V_S3_ACCESS_KEY"],
    "S3_SECRET_KEY": e["V_S3_SECRET_KEY"],
    "MODEL_BACKEND": e["V_MODEL_BACKEND"],
    "V2_MAX_TILES_WSI": e["V_V2_MAX_TILES_WSI"],
    "LOG_LEVEL": e["V_LOG_LEVEL"],
    "ENVIRONMENT": e["V_ENVIRONMENT"],
}
with open(sys.argv[1], "w") as f:
    f.write("# Generado por generar-worker-env.sh. Contiene claves: no subir a git.\n")
    f.writelines(f"{k}={val}\n" for k, val in v.items())
PY
echo "listo: $OUT (RabbitMQ $A_RABBIT, Redis ${A_REDIS:-administrado}, PostgreSQL ${A_PG:-administrado})"
