FROM python:3.12.15-slim-trixie

WORKDIR /srv

# Versiones mínimas por seguridad (ver constraints.txt)
COPY constraints.txt /srv/constraints.txt
ENV PIP_CONSTRAINT=/srv/constraints.txt
RUN pip install --no-cache-dir --upgrade "pip>=25.3" "setuptools>=83.0.0"

COPY packages/common /srv/packages/common
COPY packages/event-contracts /srv/packages/event-contracts
RUN pip install --no-cache-dir /srv/packages/common /srv/packages/event-contracts

COPY apps/audit-service/pyproject.toml .
COPY apps/audit-service/app/ app/
COPY apps/audit-service/alembic.ini .
COPY apps/audit-service/alembic/ alembic/
RUN pip install --no-cache-dir -e .

# Sin privilegios de administrador (Trivy DS-0002, SonarQube S6471). HOME en /tmp para cachés (torch, matplotlib)
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
ENV HOME=/tmp
USER 10001

EXPOSE 8007
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007"]
