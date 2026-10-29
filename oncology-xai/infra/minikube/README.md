# lchai en minikube

Levanta lchai completo en minikube para probar el chart fuera de AWS. Es el mismo chart y el mismo camino que se usa en Telconet
(guía de despliegue: [infra/helm/lchai/README.md](../helm/lchai/README.md)). El worker de inferencia corre en CPU.

## Qué cambia respecto de AWS

| Pieza | En AWS | En minikube (y en Telconet) |
|---|---|---|
| Entrada web | ALB + ACM | ingress-nginx (`ingress.provider: nginx`) |
| Archivos | S3 + rol IAM | SeaweedFS con llaves (`localDeps`), público en `s3.lchai.localhost` |
| Base de datos | RDS | PostgreSQL 16 en el cluster; el Job `db-schema` crea las tablas |
| Caché | ElastiCache | Redis 7 en el cluster |
| Discos | `gp2` | StorageClass por defecto (`storage.className: ""`) |
| Login | Realm en RDS | Realm importado de `infra/keycloak/realm-export.json` con usuarios de prueba |
| GPU | Tesla T4 | CPU (`inferenceWorker.gpu: false`) |
| Imágenes | ECR de producción | compiladas desde el código dentro de minikube (`sec-20261028`, webapp `portable-20261028`) |

## Requisitos

- minikube, docker, helm, kubectl y aws cli con lectura del bucket de modelos (solo para el paso `modelos`).
- 6 CPU, 12 GB de RAM y al menos 35 GB libres en disco.
- **VPN apagada** para compilar imágenes (con la VPN, minikube no llega a Docker Hub).

## Uso

```bash
cd oncology-xai/infra/minikube
./minikube-up.sh            # todo: cluster, imágenes, despliegue y modelos
```

O paso a paso: `./minikube-up.sh cluster | imagenes | desplegar | modelos`.

Para entrar, deja corriendo en otra terminal:

```bash
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80
```

y abre http://lchai.localhost:8080. Usuarios de prueba: `clinician1` / `Clinico-Local-2026`, `admin1` / `Admin-Local-2026x` y `auditor1` / `Auditor-Local-2026`.
La consola de Keycloak no se publica: `kubectl -n lchai port-forward svc/keycloak 8081:8080` y http://localhost:8081/admin (`admin` / `Lchai-Admin-Local-2026!`).

## Prueba completa

```bash
curl -o /tmp/CMU-1-Small-Region.svs https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs
python3 ../tests/e2e.py http://lchai.localhost:8080 /tmp/CMU-1-Small-Region.svs
```

Revisa login, permisos, paciente, caso, subidas, análisis, artefactos, grafo y auditoría, y borra lo que creó. Resultado esperado: `26 OK, 0 fallas`.

## Limitaciones

- **Fuseki empieza vacío** (la ontología se regenera).
- **OpenAI**: las funciones con LLM necesitan `--set openaiApiKey=sk-...` (decisión pendiente, S-22).
- **NetworkPolicies**: el CNI por defecto de minikube no las aplica.
- Rompe el `docker-compose` del repo: el realm ya no trae usuarios y las contraseñas son obligatorias.
