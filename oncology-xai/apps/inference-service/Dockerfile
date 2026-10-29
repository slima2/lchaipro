FROM python:3.12.15-slim-trixie

RUN sed -i 's|http://deb.debian.org|https://cdn-aws.deb.debian.org|g' /etc/apt/sources.list.d/*.sources 2>/dev/null; \
    sed -i 's|http://deb.debian.org|https://cdn-aws.deb.debian.org|g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libopenslide0 libvips42 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Versiones mínimas por seguridad (ver constraints.txt)
COPY constraints.txt /srv/constraints.txt
ENV PIP_CONSTRAINT=/srv/constraints.txt
RUN pip install --no-cache-dir --upgrade "pip>=25.3" "setuptools>=83.0.0"

COPY packages/common /srv/packages/common
COPY packages/event-contracts /srv/packages/event-contracts
COPY packages/langgraph-workflows /srv/packages/langgraph-workflows
RUN pip install --no-cache-dir /srv/packages/common /srv/packages/event-contracts /srv/packages/langgraph-workflows

COPY apps/inference-service/pyproject.toml .
ARG CACHEBUST=
COPY apps/inference-service/app/ app/
COPY apps/inference-service/alembic.ini .
COPY apps/inference-service/alembic/ alembic/

# Install PyTorch with CUDA 12.4 support (bundled CUDA libs), then project deps.
# Fijado a la versión de producción; PyPI como índice extra para que se cumplan las restricciones de seguridad.
RUN pip install --no-cache-dir \
    torch==2.6.0 torchvision==0.21.0 \
    --index-url https://download.pytorch.org/whl/cu124 --extra-index-url https://pypi.org/simple && \
    pip install --no-cache-dir -e .

EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
