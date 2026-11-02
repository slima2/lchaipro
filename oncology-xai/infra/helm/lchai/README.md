# Despliegue de lchai en Kubernetes (Telconet u otro cluster)

Chart único para AWS, minikube y cualquier Kubernetes. Los valores por defecto (`values.yaml`) son los de AWS.
Para Telconet se usa `values-telconet.yaml`: completar los `TODO` y seguir estos pasos.

Probado de punta a punta en minikube el 28-oct-2026 con una instalación desde cero (`infra/tests/e2e.py`: 26/26).

## Qué se despliega

| Pieza | Qué es |
|---|---|
| webapp | React servida con nginx (puerto 3000). Toma el dominio del navegador: una imagen para cualquier dominio |
| api-gateway + 8 microservicios | FastAPI. Solo el gateway recibe tráfico de afuera (`/api/v1`) |
| inference-worker | Celery + PyTorch. Con GPU (`nvidia.com/gpu: 1`) o en CPU |
| Keycloak 23 | Login (`/realms/oncology`). La consola de administración no se publica |
| RabbitMQ, Fuseki | Cola de tareas y ontología |
| `localDeps` (opcional) | PostgreSQL 16, Redis 7 y S3 (SeaweedFS) dentro del cluster |
| Job `db-schema-<rev>` | Crea o completa las tablas en cada instalación (idempotente) |
| Job `s3-buckets-<rev>` | Crea los buckets y su CORS (solo con `localDeps`) |

## Requisitos

- Kubernetes 1.27+, Helm 3, un ingress controller (o un proxy externo, ver opción d) y una StorageClass.
- GPU: NVIDIA device plugin. Sin GPU: `inferenceWorker.gpu: false` (unas 4 veces más lento), o un worker con GPU fuera del cluster conectado por VPN: [infra/worker-remoto/README.md](../../worker-remoto/README.md).
- Recursos (requests): unos 4 vCPU y 5 GB de RAM para todo menos el worker, que pide 2 vCPU, 4 GB y una GPU. Recomendado: 8 vCPU y 16 GB en total.
- **Dos nombres DNS** apuntando al ingress si se usa `localDeps`: `<domain>` y `s3.<domain>`.
  El navegador abre las láminas y sube las grandes directo a S3 con URL firmadas.

## Paso 1. Imágenes

```bash
cd oncology-xai
docker login <registro>
infra/build-images.sh <registro>/lchai sec-20261029 --push
```

Compila los 9 servicios y la webapp portable (`portable-20261029`). En `values-telconet.yaml` van `image.registry`, `image.prefix` y `image.tag`.

Las imágenes corren **sin root**: los servicios con el usuario `app` (uid 10001) y la webapp con `nginx` (uid 101). El chart lo exige con `podSecurity.hardened: true`.

**Imágenes base:** los Dockerfile parten de `python:3.12.15-slim-trixie`, `node:22.23.4-alpine` y `nginx:1.31.8-alpine` de Docker Hub. Si la política de Telconet exige un registro permitido (Hadolint DL3026 en el escaneo de `code-lint`), hay que usar sus copias en Harbor y cambiar la línea `FROM` de cada Dockerfile.

**Registro privado:** crear el secret y asignarlo a la cuenta de servicio del namespace:

```bash
kubectl create namespace lchai
kubectl -n lchai create secret docker-registry regcred --docker-server=<registro> --docker-username=... --docker-password=...
kubectl -n lchai patch serviceaccount default -p '{"imagePullSecrets":[{"name":"regcred"}]}'
```

## Paso 2. Entrada: dominio, proxy y TLS

El dominio va en un solo lugar: `domain`. De ahí salen la URL de Keycloak, el emisor del token que valida el gateway, el CORS de S3, la CSP de la webapp y el host `s3.<domain>`.

Rutas que debe atender la entrada (las arma el chart):

| Ruta | Servicio | Puerto |
|---|---|---|
| `<domain>/api/v1` | api-gateway | 8000 |
| `<domain>/realms/oncology`, `/resources`, `/js` | keycloak | 8080 |
| `<domain>/` | webapp | 3000 |
| `s3.<domain>/` (con `localDeps`) | s3 | 8333 |

Requisitos del proxy, para cualquier opción:
- **Sin límite de tamaño de subida** (las láminas pesan cientos de MB o varios GB).
- **Búfer de cabeceras de 16k.** Con el de 4k, el login de Keycloak da `502 upstream sent too big header`.
- **Tiempo de espera de 600 s** hacia el backend.
- Enviar `X-Forwarded-Proto` y `X-Forwarded-For`, y conservar el `Host`.

### a) ingress-nginx (recomendado)

```yaml
ingress:
  provider: nginx
  className: nginx
```

El chart agrega solas las anotaciones de subida, búfer, tiempos y `ssl-redirect`.

**Access log (CERT, S-20):** las URL de artefactos llevan `?access_token=`. En el ConfigMap del controller conviene registrar la ruta sin query:

```yaml
# ConfigMap ingress-nginx-controller
log-format-upstream: '$remote_addr - [$time_local] "$request_method $uri $server_protocol" $status $body_bytes_sent $request_time "$http_user_agent"'
```

### b) Traefik

```yaml
ingress:
  provider: traefik
  className: traefik
networkPolicy:
  ingressNamespace: traefik     # o kube-system, donde corra Traefik
```

Traefik no limita el tamaño de subida, pero en v3 el `readTimeout` del entrypoint es de 60 s: subirlo a 600 s en la configuración estática (`entryPoints.websecure.transport.respondingTimeouts.readTimeout=600s`).

### c) Otro ingress controller

`provider: generic`, su `className` y las anotaciones equivalentes en `ingress.annotations`.

### d) Proxy externo sin Ingress (F5, nginx de CERT, Gateway API, OpenShift Routes)

`provider: none` no crea ningún Ingress. El proxy o las rutas propias deben replicar la tabla de arriba hacia los Service. Si el proxy está **fuera** del cluster y no llega a los Service, lo más simple es dejar el ingress-nginx (opción a) y poner el proxy delante de él:

```nginx
# Proxy de CERT delante del ingress del cluster. Repetir el server para s3.<domain>.
server {
    listen 443 ssl;
    server_name lchai.ejemplo.telconet.ec;
    ssl_certificate     /etc/ssl/lchai.crt;
    ssl_certificate_key /etc/ssl/lchai.key;

    client_max_body_size 0;          # láminas grandes
    proxy_request_buffering off;
    proxy_buffer_size 16k;           # cabeceras grandes de Keycloak
    proxy_buffers 8 16k;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://<ip-del-ingress-del-cluster>;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
    }
}
```

### TLS

| Caso | Valores |
|---|---|
| cert-manager | `tls.enabled: true`, `tls.clusterIssuer: <issuer>`; crea `lchai-tls` y `lchai-s3-tls` |
| Certificado propio | `kubectl -n lchai create secret tls lchai-tls --cert=... --key=...` (y `lchai-s3-tls` para `s3.<domain>`) |
| TLS en un proxy o balanceador delante | `tls.enabled: false`. `publicUrl` queda en `https://`, que es lo que ve el usuario |

Un certificado comodín `*.<dominio-padre>` sirve para los dos nombres: usar el mismo secret en `secretName` y `s3SecretName`.

## Paso 3. Contraseñas

Son obligatorias: el chart no instala sin ellas. Van en un archivo aparte que **no se sube a git**:

```bash
gen() { openssl rand -base64 24 | tr -d '/+=' ; }
cat > secrets-telconet.yaml <<EOF
postgres: { password: $(gen) }
redis:    { password: $(gen) }
rabbitmq: { password: $(gen) }
fuseki:   { adminPassword: $(gen) }
keycloak: { adminPassword: $(gen) }
s3:       { accessKey: lchai, secretKey: $(gen) }
# openaiApiKey: sk-...    # solo si se habilitan las funciones con LLM (decisión pendiente, S-22)
EOF
chmod 600 secrets-telconet.yaml
```

Con servicios administrados (opción B de datos), poner ahí las contraseñas reales de PostgreSQL, Redis y S3.

## Paso 4. Instalar

```bash
helm upgrade --install lchai infra/helm/lchai -n lchai --create-namespace \
  -f infra/helm/lchai/values-telconet.yaml -f secrets-telconet.yaml --wait --timeout 20m
kubectl -n lchai get pods
```

La primera vez Keycloak tarda 2 o 3 minutos: crea su esquema e importa el realm. `--wait` espera a que esté listo.

Para desplegar por partes: `--set layers.app=false,layers.inference=false` (solo plataforma), luego se encienden.

## Paso 5. Modelos (~330 MB)

El worker los descarga del bucket `lchai-prod-models` al arrancar. Para copiarlos desde AWS al S3 del cluster (necesita `aws` con acceso a la cuenta de AWS y `kubectl` apuntando al cluster):

```bash
NS=lchai infra/minikube/minikube-up.sh modelos     # funciona con cualquier cluster, no solo minikube
```

Si `lchai-prod-models` ya no existe, los modelos están en el bucket de respaldo del 1-nov-2026: ver «Modelos del worker» en el paso 4 de [infra/migracion/README.md](../../migracion/README.md).

## Paso 6. Usuarios

- **Instalación nueva** (`keycloak.importRealm: true`): el realm llega sin usuarios. Crearlos en la consola de Keycloak con los roles `clinician`, `admin` o `auditor`:
  `kubectl -n lchai port-forward svc/keycloak 8081:8080` y abrir http://localhost:8081/admin (usuario `admin`, la clave de `secrets-telconet.yaml`).
- **Migración desde AWS** (`importRealm: false`): los usuarios vienen con el respaldo de PostgreSQL. Después de restaurar, correr `NS=lchai infra/keycloak/harden-realm.sh`.

**Migración desde AWS:** para llevar los datos (base, usuarios y archivos), seguir [infra/migracion/README.md](../../migracion/README.md) después de este paso.

## Paso 7. Verificar

```bash
curl -o CMU-1-Small-Region.svs https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs
LCHAI_CLINICIAN=usuario:clave LCHAI_ADMIN=usuario:clave LCHAI_AUDITOR=usuario:clave \
  python3 infra/tests/e2e.py https://<domain> CMU-1-Small-Region.svs
```

Debe terminar en `0 fallas`. La prueba crea un paciente de prueba con la lámina pública y lo borra al final.

## Problemas conocidos

| Síntoma | Causa | Solución |
|---|---|---|
| `502` al iniciar sesión | búfer de cabeceras chico en el proxy | 16k (paso 2) |
| `502` en `/realms/...` justo después de instalar | Keycloak aún arranca | esperar; `--wait` ya lo hace |
| `413` al subir una lámina | límite de tamaño del proxy | `client_max_body_size 0` o equivalente |
| El visor no carga la lámina | `s3.<domain>` sin DNS o sin TLS | paso 2 |
| `ERR_CONNECTION_REFUSED` al iniciar sesión | `publicUrl` no coincide con la URL real (puerto) | ajustar `publicUrl` |
| Se queda en "Authentication required" y el botón Login no hace nada | el navegador solo permite la criptografía del login (PKCE) con `https` o en `localhost` | publicar con TLS; para pruebas sin certificado, usar `localhost` |
| `Invalid issuer` o redirección rechazada tras cambiar el dominio | el realm guarda las URL de redirección de la primera instalación | agregar el dominio nuevo en Keycloak: cliente `oncology-webapp`, *Valid redirect URIs* y *Web origins* |
| Las NetworkPolicies no bloquean nada | el CNI no las aplica | Calico, Cilium o similar |
| El worker nuevo queda `Pending` tras un upgrade | RollingUpdate pide una segunda GPU mientras el viejo sigue vivo | `inferenceWorker.updateStrategy: Recreate` (ya está en `values-telconet.yaml`) |
