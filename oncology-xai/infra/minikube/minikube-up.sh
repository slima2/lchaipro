#!/usr/bin/env bash
# Levanta lchai completo en minikube, sin GPU (el worker de inferencia corre en CPU).
# Uso: ./minikube-up.sh [paso]   pasos: cluster | imagenes | desplegar | modelos | todo (por defecto)
# Requisitos: minikube, docker, helm, kubectl y aws cli con lectura del bucket de modelos (cuenta 632100838024).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"          # carpeta oncology-xai
CHART="$ROOT/infra/helm/lchai"
VALUES="$CHART/values-minikube.yaml"
NS="${NS:-lchai}"
AWS_PROFILE="${AWS_PROFILE:-default}"
REGION=us-east-1
REGISTRY=632100838024.dkr.ecr.us-east-1.amazonaws.com
PUBLIC_URL=http://lchai.localhost:8080

TAG=sec-20261028   # etiqueta de values-minikube.yaml

log() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }

check_disk() {
  local free_gb
  free_gb=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
  if (( free_gb < 35 )); then
    echo "Solo hay ${free_gb} GB libres. Se necesitan al menos 35 GB (imágenes ~15 GB descomprimidas + minikube + datos)."
    exit 1
  fi
}

paso_cluster() {
  log "Iniciando minikube (6 CPU, 12 GB RAM, 40 GB disco)"
  check_disk
  minikube start --driver=docker --cpus=6 --memory=12g --disk-size=40g --kubernetes-version=v1.31.0
  minikube addons enable ingress
  minikube addons enable storage-provisioner

  log "DNS interno: s3.lchai.localhost -> servicio s3 (para que las URL firmadas funcionen dentro del cluster)"
  kubectl -n kube-system get configmap coredns -o yaml > /tmp/coredns.yaml
  if ! grep -q "s3.lchai.localhost" /tmp/coredns.yaml; then
    kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' \
      | sed "s/^\(\s*\)ready/\1rewrite name s3.lchai.localhost s3.${NS}.svc.cluster.local\n\1ready/" > /tmp/Corefile
    kubectl -n kube-system create configmap coredns --from-file=Corefile=/tmp/Corefile --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n kube-system rollout restart deployment coredns
  fi
}

paso_imagenes() {
  log "Compilando las 10 imágenes parchadas desde el código fuente dentro de minikube (apagar la VPN)"
  "$ROOT/infra/build-images.sh" "$REGISTRY/lchai-prod" "$TAG" --minikube
}

paso_desplegar() {
  log "Desplegando lchai con Helm en el namespace $NS"
  kubectl -n ingress-nginx wait --for=condition=ready pod -l app.kubernetes.io/component=controller --timeout=180s
  helm upgrade --install lchai "$CHART" -n "$NS" --create-namespace -f "$VALUES" --wait --timeout 15m
  kubectl -n "$NS" get pods
}

paso_modelos() {
  log "Copiando al almacenamiento S3 local solo los modelos necesarios (~330 MB) desde S3 de AWS"
  local tmp; tmp=$(mktemp -d)
  aws s3 cp "s3://lchai-prod-models/backbones/ctranspath.pth" "$tmp/backbones/" --profile "$AWS_PROFILE"
  aws s3 cp "s3://lchai-prod-models/backbones/best_fuzzyarcloss_v2.pth" "$tmp/backbones/" --profile "$AWS_PROFILE"
  aws s3 cp "s3://lchai-prod-models/checkpoints/" "$tmp/checkpoints/" --recursive --exclude "*" --include "*.pth" --profile "$AWS_PROFILE"

  local ak sk pf
  ak=$(kubectl -n "$NS" get secret lchai-secrets -o jsonpath='{.data.S3_ACCESS_KEY}' | base64 -d)
  sk=$(kubectl -n "$NS" get secret lchai-secrets -o jsonpath='{.data.S3_SECRET_KEY}' | base64 -d)
  kubectl -n "$NS" port-forward svc/s3 19000:8333 >/dev/null 2>&1 & pf=$!
  sleep 4
  env -u AWS_PROFILE AWS_ACCESS_KEY_ID="$ak" AWS_SECRET_ACCESS_KEY="$sk" \
    aws s3 sync "$tmp/" s3://lchai-prod-models/ --endpoint-url http://localhost:19000 --region "$REGION"
  kill "$pf"
  rm -rf "$tmp"

  log "Reiniciando el worker para que descargue los modelos"
  kubectl -n "$NS" rollout restart deployment inference-worker
}

acceso() {
  cat <<EOF

Listo. Para entrar:
  1) En otra terminal deja corriendo:
       kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80
  2) Abre $PUBLIC_URL  (Chrome y Firefox resuelven *.localhost solos)
  3) Usuarios de prueba: clinician1 / Clinico-Local-2026, admin1 / Admin-Local-2026x, auditor1 / Auditor-Local-2026
     Consola de Keycloak (no se publica): kubectl -n $NS port-forward svc/keycloak 8081:8080 -> http://localhost:8081/admin
  4) Prueba completa: python3 $ROOT/infra/tests/e2e.py $PUBLIC_URL <lamina.svs>
EOF
}

case "${1:-todo}" in
  cluster) paso_cluster ;;
  imagenes) paso_imagenes ;;
  desplegar) paso_desplegar ;;
  modelos) paso_modelos ;;
  todo) paso_cluster; paso_imagenes; paso_desplegar; paso_modelos; acceso ;;
  *) echo "Paso desconocido: $1"; exit 1 ;;
esac
