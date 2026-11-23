FROM python:3.12.15-slim-trixie

WORKDIR /srv

# Versiones mínimas por seguridad (ver constraints.txt)
COPY constraints.txt /srv/constraints.txt
ENV PIP_CONSTRAINT=/srv/constraints.txt
RUN pip install --no-cache-dir --upgrade "pip>=25.3" "setuptools>=83.0.0"

COPY packages/common /srv/packages/common
COPY packages/event-contracts /srv/packages/event-contracts
RUN pip install --no-cache-dir /srv/packages/common /srv/packages/event-contracts

COPY apps/active-learning-service/pyproject.toml .
COPY apps/active-learning-service/app/ app/

# torch con versión fija (Hadolint DL3013): la misma que traía la imagen sec-20261028.
# Sin privilegios de administrador (Trivy DS-0002, SonarQube S6471). HOME en /tmp para cachés (torch, matplotlib)
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -e . \
 && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
ENV HOME=/tmp
# pip fuera de la imagen runtime: su SBOM vendorizado (_vendor/bom.cdx.json) es la fuente de
# los hallazgos msgpack/urllib3/setuptools que reporta sec:image-scan (dependencias internas de pip,
# no de la app). Ningun servicio necesita pip en runtime.
RUN pip uninstall -y pip
LABEL ec.telconet.stack="txt-solca-lchai"
USER 10001

EXPOSE 8008
# Para Docker/compose; en Kubernetes manda la readinessProbe del chart
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/health', timeout=4)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]
