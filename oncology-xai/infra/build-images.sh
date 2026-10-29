#!/usr/bin/env bash
# Compila las 10 imágenes de lchai desde el código fuente (9 servicios Python + webapp portable).
# Uso:  infra/build-images.sh <registro>/<prefijo> <etiqueta> [--push] [--minikube]
#   ej: infra/build-images.sh harbor.telconet.ec/lchai sec-20261028 --push
#       infra/build-images.sh 632100838024.dkr.ecr.us-east-1.amazonaws.com/lchai-prod sec-20261028 --minikube
# La webapp se compila SIN URL fija (usa el dominio desde el que se abre) y lleva la etiqueta portable-<fecha>.
set -euo pipefail

REPO="${1:?falta <registro>/<prefijo>}"
TAG="${2:?falta la etiqueta}"
shift 2
PUSH=false
for a in "$@"; do
  case "$a" in
    --push) PUSH=true ;;
    --minikube) eval "$(minikube docker-env)" ;;   # compila dentro de minikube (pullPolicy: Never)
    *) echo "Opción desconocida: $a"; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # carpeta oncology-xai
WEBAPP_TAG="portable-${TAG#sec-}"
SERVICES=(api-gateway case-service image-service inference-service ehr-service graph-service
          ontology-admin-service audit-service active-learning-service)

for s in "${SERVICES[@]}"; do
  echo "==> $s"
  docker build -f "$ROOT/apps/$s/Dockerfile" -t "$REPO/$s:$TAG" "$ROOT"
done
echo "==> webapp"
docker build -f "$ROOT/apps/webapp/Dockerfile" --build-arg VITE_API_URL= --build-arg VITE_KEYCLOAK_URL= \
  -t "$REPO/webapp:$WEBAPP_TAG" "$ROOT"

if $PUSH; then
  for s in "${SERVICES[@]}"; do docker push "$REPO/$s:$TAG"; done
  docker push "$REPO/webapp:$WEBAPP_TAG"
fi

cat <<EOF

Listo. En values-<entorno>.yaml:
  image:
    registry: ${REPO%/*}
    prefix: ${REPO##*/}
    tag: $TAG
    tags:
      webapp: $WEBAPP_TAG
EOF
