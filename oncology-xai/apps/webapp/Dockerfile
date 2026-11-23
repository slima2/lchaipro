FROM node:22.23.4-alpine AS builder

WORKDIR /app
COPY apps/webapp/package.json apps/webapp/package-lock.json* ./
RUN npm install

COPY apps/webapp/ .

# Sin URL por defecto: la webapp usa el dominio desde el que se abre (una sola imagen para cualquier entorno).
# Keycloak se configura en runtime (KEYCLOAK_URL/REALM/CLIENT_ID del contenedor, ver nginx.conf.template).
ARG VITE_API_URL=
ARG VITE_KEYCLOAK_URL=
ARG VITE_KEYCLOAK_REALM=oncology
ARG VITE_KEYCLOAK_CLIENT_ID=oncology-webapp

ENV VITE_API_URL=${VITE_API_URL} \
    VITE_KEYCLOAK_URL=${VITE_KEYCLOAK_URL} \
    VITE_KEYCLOAK_REALM=${VITE_KEYCLOAK_REALM} \
    VITE_KEYCLOAK_CLIENT_ID=${VITE_KEYCLOAK_CLIENT_ID}

RUN npx vite build

FROM nginx:1.31.8-alpine
COPY --from=builder /app/dist /usr/share/nginx/html

# Configuración con cabeceras de seguridad (CSP, X-Frame-Options, nosniff, Referrer-Policy, HSTS) y server_tokens off
RUN rm -f /etc/nginx/conf.d/default.conf
COPY apps/webapp/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV CSP_S3_ORIGIN="" \
    CSP_KEYCLOAK_ORIGIN="" \
    KEYCLOAK_URL="" \
    KEYCLOAK_REALM="oncology" \
    KEYCLOAK_CLIENT_ID="oncology-webapp"

# Sin privilegios de administrador (Trivy DS-0002, SonarQube S6471): usuario nginx (uid 101).
# Necesita escribir la configuración generada desde la plantilla, la caché y el archivo de PID.
RUN chown -R nginx:nginx /var/cache/nginx /etc/nginx/conf.d && \
    sed -i 's#^pid .*#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf
# CVEs de alpine 3.24 (CVE-2026-111497 brotli-libs, CVE-2026-92392 curl/libcurl,
# CVE-2026-113351/117610/118370 libexpat 2.9.0 -> 2.10.0): parchear
# los paquetes del sistema a la version con fix antes de arrancar.
# Se unpinea a proposito: siempre la ultima version parcheada del repo alpine 3.24.
# hadolint ignore=DL3018
RUN apk add --no-cache --upgrade brotli-libs curl libcurl libexpat
LABEL ec.telconet.stack="txt-solca-lchai"
USER 101

EXPOSE 3000
# Para Docker/compose; en Kubernetes manda la readinessProbe del chart
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:3000/"]
CMD ["nginx", "-g", "daemon off;"]
