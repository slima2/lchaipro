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

COPY apps/ontology-admin-service/pyproject.toml .
COPY apps/ontology-admin-service/app/ app/
COPY apps/ontology-admin-service/alembic.ini .
COPY apps/ontology-admin-service/alembic/ alembic/
# Sin privilegios de administrador (Trivy DS-0002, SonarQube S6471). HOME en /tmp para cachés (torch, matplotlib)
RUN pip install --no-cache-dir -e . \
 && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
ENV HOME=/tmp
# pip fuera de la imagen runtime: su SBOM vendorizado (_vendor/bom.cdx.json) es la fuente de
# los hallazgos msgpack/urllib3/setuptools que reporta sec:image-scan (dependencias internas de pip,
# no de la app). Ningun servicio necesita pip en runtime.
# Ademas: owlready2 empaqueta los razonadores Java pellet/hermit con jars vulnerables
# (jena-core 2.10.0, CVE-2021-39239); la app no usa razonadores (solo carga OWL), se eliminan.
RUN pip uninstall -y pip && \
    rm -rf /usr/local/lib/python3.12/site-packages/owlready2/pellet \
           /usr/local/lib/python3.12/site-packages/owlready2/hermit
USER 10001

EXPOSE 8006
# Para Docker/compose; en Kubernetes manda la readinessProbe del chart
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/metrics', timeout=4)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8006"]
