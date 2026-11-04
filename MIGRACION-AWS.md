# Migración de lchai desde el respaldo de AWS

Para: Gabriel Borbor (infraestructura, Telconet). De: Sebastián Ramos.

La producción de lchai en AWS se dio de baja el 1-nov-2026, con la confirmación de Servio. Antes se respaldó todo en un bucket de S3 privado que **solo se lee**. Esta rama trae lo necesario para llevar esos datos al lchai de Telconet:

| Dato | Cantidad |
|---|---|
| Pacientes, casos y resultados | 10 pacientes, 7 casos, 19 resultados |
| Usuarios de producción, con sus claves cifradas y roles | 43 |
| Láminas y resultados (imágenes y mapas) | 555 archivos, 5.9 GB |
| Modelos de IA | 152, unos 330 MB |

## Qué trae esta rama

Parte de `master` del repo (v0.3.2, con las correcciones del pentest) y **no cambia nada de `apps/`**: tu código queda igual. Suma:

| Cambio | Archivos |
|---|---|
| Migración desde el respaldo de AWS: base desde el volcado (`SRC_DUMP`), archivos y modelos desde las carpetas del respaldo (`SRC_PREFIX`, `INCLUIR`), y credenciales de solo lectura | `infra/migracion/migrar-datos.sh`, `infra/migracion/README.md` |
| Compatible con tu chart: busca Keycloak y los demás componentes por la etiqueta `app` (sirve con `workloadPrefix`/`workloadSuffix`), salta el paso de Keycloak con `keycloak.external` y acepta las imágenes `deps/*` (`PG_IMAGE`, `AWS_IMAGE`) | `infra/migracion/migrar-datos.sh`, `infra/keycloak/harden-realm.sh` |
| **Keycloak del cluster:** lo que hay que crear en él, el archivo del realm y la exportación de los usuarios de producción | `infra/keycloak/KEYCLOAK-EXTERNO.md`, `preparar-realm.sh`, `exportar-usuarios.sh` |
| **Corrección para Keycloak 26:** el cliente `oncology-webapp` no tenía el scope `basic`, así que los tokens salían sin `sub` y la API respondía 401 (`Token validation failed: 'sub'`). **También afecta a `keycloak.importRealm: true`** | `infra/keycloak/realm-export.json` |
| SeaweedFS: master, volume y filer quedan en loopback (no piden credenciales) y solo el gateway S3 escucha. NetworkPolicy `allow-to-s3` solo en el puerto 8333. Memoria de 3Gi configurable (`localDeps.s3.resources`): con 1Gi lo mataba el OOM al subir varias láminas grandes | `templates/local-deps.yaml`, `templates/networkpolicy.yaml`, `values.yaml` |
| Las pruebas aceptan Keycloak en otro dominio (`LCHAI_KEYCLOAK_URL`) y Keycloak 26 sobre `http://localhost` | `infra/tests/e2e.py`, `infra/tests/kc_login.py` |

## Probado el 4-nov-2026 (minikube, instalación limpia)

Usé las imágenes compiladas desde esta rama, tus 7 imágenes `deps/*` (Keycloak 26.8.1-r1, PostgreSQL 16.15-r1, RabbitMQ, Fuseki, Redis, SeaweedFS y aws-cli) y **un Keycloak aparte en otro namespace**, que hacía de "Keycloak del cluster" (`keycloak.external`).

| Paso | Resultado |
|---|---|
| `helm install` con tu chart y Keycloak externo | todos los pods arriba en 31 s |
| Base desde el respaldo (`migrar-datos.sh bd`) | 3 min; 10 pacientes, 7 casos, 19 resultados |
| Archivos (`migrar-datos.sh s3`) | 10.5 min; 555 objetos, 5.92 GB; ninguna ruta de la base rota; SeaweedFS sin reinicios |
| Usuarios al Keycloak externo (`exportar-usuarios.sh` + *Partial import*) | 43 de 43, todos con clave y rol (29 clínicos, 2 administradores, 1 auditor) |
| Modelos | 152; el worker los carga |
| `infra/tests/e2e.py` con usuarios migrados | **26/26**: login por rol, pacientes, casos, subida directa y por URL firmada, visor, análisis (40 s en CPU), 18 artefactos, grafo, auditoría y borrado |
| Caso real migrado 860eedbd | igual que en producción: micropapilar, TP53 0.9142, 18/18 artefactos |
| GPU (EC2 g4dn.xlarge temporal, NVIDIA T4, con la imagen `inference-service` de esta rama) | CUDA detectada. Lámina chica: GPU = CPU exacto (micropapilar 78.13 %, mismos 6 genes). CMU-1 completa: 300 cuadros en 39 s y 383 cuadros en 45 s |

## Pasos, en orden

Guía detallada: **[`oncology-xai/infra/migracion/README.md`](oncology-xai/infra/migracion/README.md)**.

1. **Credenciales de solo lectura del respaldo.** Las crea Sebastián y te las pasa por un canal seguro. Solo pueden leer el bucket `lchai-respaldo-20261101-632100838024` y vencen a las 4 h.
2. **Pausar el auto-sync de ArgoCD** de la app: el paso 3 apaga los Deployments mientras restaura.
3. **Base:**
   ```bash
   NS=<ns> SRC_DUMP=s3://lchai-respaldo-20261101-632100838024/db/oncology_xai-20261101.dump \
     infra/migracion/migrar-datos.sh bd
   ```
   Después, reactivar el auto-sync.
4. **Keycloak del cluster**, según [`infra/keycloak/KEYCLOAK-EXTERNO.md`](oncology-xai/infra/keycloak/KEYCLOAK-EXTERNO.md):
   - crear el realm con `preparar-realm.sh https://<dominio>`;
   - exportar los usuarios con `exportar-usuarios.sh` e importarlos con *Partial import*;
   - **si el Keycloak es compartido y el realm es otro:** crearlos con `crear-usuarios.py` por la API de administración. Cada usuario entra con su misma clave de AWS (sección 2b de la guía). En pre-producción se pueden crear a mano con claves de prueba y pasarlos después a la clave de AWS con `EXISTENTES=clave`.
5. **Archivos:**
   ```bash
   NS=<ns> SRC_BUCKET=lchai-respaldo-20261101-632100838024 SRC_PREFIX=s3/lchai-prod-artifacts/ \
     DEST_BUCKET=lchai-prod-artifacts PREFIJOS="images/ results/" infra/migracion/migrar-datos.sh s3
   ```
6. **Modelos y GPU:** paso 5 de la guía. Son dos comandos con `INCLUIR` y un reinicio del worker. Con GPU el análisis tarda segundos; en CPU, minutos.
7. **Verificar:**
   ```bash
   NS=<ns> infra/migracion/migrar-datos.sh verificar
   ```
   Después corre `e2e.py` con `LCHAI_KEYCLOAK_URL`.

Si el cluster no llega a Docker Hub, antes de los pasos 3, 5 y 6:

```bash
export PG_IMAGE=registry.gitlab.telconet.ec/procesos/devops/deploys/txt/solca/lchai/deps/postgres:16.15-r1
export AWS_IMAGE=registry.gitlab.telconet.ec/procesos/devops/deploys/txt/solca/lchai/deps/aws-cli:2.39.2
```

## Lo que necesito confirmar contigo

- **Salida a internet del cluster hacia S3 de AWS** (`s3.us-east-1.amazonaws.com:443`), al menos durante la migración. Puede que CERT tenga que aprobarlo, porque son datos de pacientes.
- **URL del Keycloak del cluster** y quién crea el realm `oncology`.
- **Archivo de usuarios:** `oncology-usuarios.json` lleva los hashes de las contraseñas. ¿Lo generas tú con el script después del paso 3, o lo genero yo y te lo paso por un canal seguro? No va en el repo.
- **GPU:** si hay un nodo con GPU NVIDIA y el device plugin, o si el worker corre en CPU por ahora.
