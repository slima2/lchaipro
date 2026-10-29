FROM node:22.23.4-alpine AS builder

WORKDIR /app
COPY apps/webapp/package.json apps/webapp/package-lock.json* ./
RUN npm install

COPY apps/webapp/ .

ARG VITE_API_URL=https://lchai.gptfy.biz
ARG VITE_KEYCLOAK_URL=https://lchai.gptfy.biz
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
ENV CSP_S3_ORIGIN=""

# Sin privilegios de administrador (Trivy DS-0002, SonarQube S6471): usuario nginx (uid 101).
# Necesita escribir la configuración generada desde la plantilla, la caché y el archivo de PID.
RUN chown -R nginx:nginx /var/cache/nginx /etc/nginx/conf.d && \
    sed -i 's#^pid .*#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf
USER 101

EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
