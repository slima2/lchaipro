# Oncology XAI

Project focused on explainable AI for oncology, combining a research-heavy thesis component (MIL, Choquet, and Pattern classification) with a microservices-based production architecture.

## Project Structure

- `oncology-xai/Thesis/`: Research and experimental codebase.
    - `training/`: Model training scripts (Pattern Classifier, ABMIL, Choquet MIL).
    - `evaluation/`: Metrics extraction and validation scripts.
    - `figures/`: Visualization and plot generation.
    - `logs/`: Experimental results and 5-fold cross-validation metrics.
    - `models/`: Model checkpoints.
- `oncology-xai/apps/`: Production microservices (Python/FastAPI).
    - `inference-service/`: Core ML inference (ABMIL, Choquet, SHAP).
    - `graph-service/`: Ontology and SPARQL queries.
    - `ehr-service/`: Electronic Health Record processing and NER.
    - `case-service/`, `image-service/`, `audit-service/`: Domain-specific data management.
    - `active-learning-service/`: Model retraining and data selection.
    - `api-gateway/`: Entry point for the microservices ecosystem.
    - `ontology-admin-service/`: Literature and deep search for ontology management.
    - `webapp/`: Frontend interface.
- `oncology-xai/infra/`: Infrastructure as Code (Helm charts for Kubernetes).

## Build & Test Commands

### General
- **Linting**: `mypy` is used (config in `mypy.ini`).
- **CI/CD**: Managed via `.gitlab-ci.yml` using an agentic-pipeline.

### Research/Thesis
- **Build/Automation**: `make` commands are available via `oncology-xai/Makefile`.

### Microservices
Each service in `oncology-xai/apps/` follows a similar pattern:
- **Dependency Management**: `pyproject.toml` (Poetry/Pip).
- **Containerization**: `Dockerfile` present in each service directory.
- **Database Migrations**: `alembic` is used for schema management (config in `alembic.ini`).

## Architecture

- **Research Stack**: Python-based ML pipeline utilizing Multiple Instance Learning (MIL), Fuzzy Choquet integrals, and pattern-based classification.
- **Production Stack**: 
    - **Backend**: Microservices architecture using FastAPI.
    - **Async Tasks**: Celery (used in `inference-service`).
    - **Database**: SQL databases with Alembic for migrations.
    - **Gateway**: Centralized API Gateway for routing.
    - **Knowledge Graph**: Graph service utilizing SPARQL for medical ontology.
    - **Frontend**: React-based webapp with runtime configuration via Nginx for environment-agnostic images.
    - **Auth**: Keycloak integration (supporting both internal and external deployments).
    - **Orchestration**: Kubernetes deployment via Helm.

## Conventions

- **Environment**: Use `.env.example` in `oncology-xai/` as a template for local environment variables.
- **Thesis Artifacts**: Training scripts are organized as "artefacts" (e.g., `artefact1_pattern_classifier`).
- **Deployment**: Docker-based deployment with specific runners for large images (e.g., `inference-service` images > 3GB).
- **Webapp Config**: The webapp uses a `/runtime-config.js` endpoint served by Nginx to inject environment-specific variables (Keycloak URL, Realm, etc.) at runtime.

## Despliegue y migración desde el respaldo de AWS (para agentes)

Resumen para humanos: [MIGRACION-AWS.md](MIGRACION-AWS.md). Guías detalladas:
- Despliegue con el chart: `oncology-xai/infra/helm/lchai/README.md`.
- Migración de datos: `oncology-xai/infra/migracion/README.md` (script `migrar-datos.sh`: `bd`, `keycloak`, `s3`, `verificar`).
- Keycloak del cluster: `oncology-xai/infra/keycloak/KEYCLOAK-EXTERNO.md`.

**Origen de los datos.** La producción de AWS se dio de baja el 1-nov-2026. Todo está en el bucket `lchai-respaldo-20261101-632100838024` (cuenta 632100838024, `us-east-1`):
- `db/oncology_xai-20261101.dump`: base, más las tablas de Keycloak con 43 usuarios;
- `s3/lchai-prod-artifacts/`: 555 archivos, 5.9 GB;
- `s3/lchai-prod-models/`: se usan 2 backbones y 150 checkpoints.

Reglas del respaldo: **solo lectura**, con credenciales temporales de `sts get-federation-token` limitadas a Get/List de ese bucket. Nunca escribir en él ni guardar credenciales en el repo.

**Keycloak: dos modos.**
- **Del cluster** (`keycloak.external.enabled: true`, `keycloak.external.url`). Es el caso de Telconet. Los pasos:
  1. crear el realm con `infra/keycloak/preparar-realm.sh https://<dominio> > oncology-realm.json`;
  2. restaurar la base con `migrar-datos.sh bd`;
  3. exportar los usuarios con `infra/keycloak/exportar-usuarios.sh usuarios.json` e importarlos con *Partial import*.
     Si el Keycloak es **compartido y el realm ya existe** (Telconet), no se importa: `infra/keycloak/crear-usuarios.py` los crea por la API de admin con su hash de AWS (`ROLES=realm|cliente`, `EXISTENTES=omitir|clave`, `SIMULAR=1`). Ver la sección 2b de `KEYCLOAK-EXTERNO.md`.

  El paso `migrar-datos.sh keycloak` **no** aplica en este modo.
- **Propio del chart** (`keycloak.external.enabled: false`). Los usuarios llegan con `migrar-datos.sh bd`. Después corre `PUBLIC_URL=https://<dominio> migrar-datos.sh keycloak`, que cambia la clave del admin, endurece el realm y registra el dominio.

**Errores conocidos, ya resueltos (no repetirlos):**
- Keycloak 25+ necesita el client scope `basic` en `oncology-webapp`; sin él, los tokens salen sin `sub` y la API responde 401 `Token validation failed: 'sub'`. Ya está en `realm-export.json`.
- `kc.sh export` necesita `--users same_file` y `KC_SPI_CONNECTIONS_JPA__QUARKUS__MIGRATION_STRATEGY=update`, porque la base viene de Keycloak 23.
- La imagen de Keycloak no trae `tar`: para pasarle archivos usar `kubectl exec -i ... -f - < archivo`, no `kubectl cp`.
- SeaweedFS necesita 3Gi de memoria (`localDeps.s3.resources`) para la migración completa; con 1Gi lo mata el OOM.
- Con ArgoCD hay que pausar el auto-sync durante `migrar-datos.sh bd`, porque apaga los Deployments mientras restaura.
- Los scripts buscan los componentes por la etiqueta `app` (sirven con `workloadPrefix`/`workloadSuffix`). Las imágenes de los pods temporales se cambian con `PG_IMAGE` y `AWS_IMAGE` (`deps/*`).
- Los servicios exigen `POSTGRES_DSN`, `POSTGRES_DSN_SYNC` y `RABBITMQ_URL`, sin valores por defecto: no hay que quitarlo, es una corrección de seguridad.

**Verificación:** `migrar-datos.sh verificar` debe dar 10 pacientes, 7 casos, 7 imágenes y 19 resultados. Después `infra/tests/e2e.py` (con `LCHAI_KEYCLOAK_URL` si Keycloak está en otro dominio) debe dar 0 fallas. Probado el 4-nov-2026: 26/26.

**GPU:** el worker usa CUDA si la hay y si no pasa a CPU, con el mismo resultado. Necesita `inferenceWorker.gpu: true`, el NVIDIA device plugin y `updateStrategy: Recreate`. Detalles en el paso 5 de la guía de migración.
