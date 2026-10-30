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

## Conventions

- **Environment**: Use `.env.example` in `oncology-xai/` as a template for local environment variables.
- **Thesis Artifacts**: Training scripts are organized as "artefacts" (e.g., `artefact1_pattern_classifier`).
- **Deployment**: Docker-based deployment with specific runners for large images (e.g., `inference-service` images > 3GB).
