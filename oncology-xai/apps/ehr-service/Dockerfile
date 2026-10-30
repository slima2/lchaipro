FROM python:3.12.15-slim-trixie

WORKDIR /srv

# Versiones mínimas por seguridad (ver constraints.txt)
COPY constraints.txt /srv/constraints.txt
ENV PIP_CONSTRAINT=/srv/constraints.txt
RUN pip install --no-cache-dir --upgrade "pip>=25.3" "setuptools>=83.0.0"

COPY packages/common /srv/packages/common
COPY packages/event-contracts /srv/packages/event-contracts
COPY packages/langgraph-workflows /srv/packages/langgraph-workflows
RUN pip install --no-cache-dir /srv/packages/common /srv/packages/event-contracts /srv/packages/langgraph-workflows

COPY apps/ehr-service/pyproject.toml .
COPY apps/ehr-service/app/ app/
COPY apps/ehr-service/alembic.ini .
COPY apps/ehr-service/alembic/ alembic/
# Sin privilegios de administrador (Trivy DS-0002, SonarQube S6471). HOME en /tmp para cachés (torch, matplotlib)
RUN pip install --no-cache-dir -e . \
 && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
ENV HOME=/tmp
# pip fuera de la imagen runtime: su SBOM vendorizado (_vendor/bom.cdx.json) es la fuente de
# los hallazgos msgpack/urllib3/setuptools que reporta sec:image-scan (dependencias internas de pip,
# no de la app). Ningun servicio necesita pip en runtime.
RUN pip uninstall -y pip
USER 10001

