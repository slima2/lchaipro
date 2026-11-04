# Migrar los datos de lchai a Telconet

Se hace **después** de desplegar lchai con el chart ([guía de despliegue](../helm/lchai/README.md)).

La producción de AWS se dio de baja el 1-nov-2026. Antes se respaldó todo en el bucket **`lchai-respaldo-20261101-632100838024`** (cuenta 632100838024, `us-east-1`), y los datos se migran desde ahí. El script [`migrar-datos.sh`](migrar-datos.sh) hace la copia con pods dentro del cluster de destino: los datos van del respaldo al cluster sin pasar por ninguna laptop, y **el respaldo solo se lee, nunca se modifica**.

**Probado completo el 2-nov-2026** en minikube, con una instalación limpia y todos los datos del respaldo:

| Paso | Tiempo | Resultado |
|---|---|---|
| Base y usuarios | 4 min | 10 pacientes, 7 casos, 7 imágenes, 19 resultados y 43 usuarios |
| Keycloak | 1.5 min | realm endurecido y dominio nuevo registrado |
| Archivos | 7 min | 555 objetos (5.92 GB), iguales al respaldo; las 347 rutas de la base existen |
| Comprobación | — | un usuario migrado entra, ve los 7 casos y el caso 860eedbd da lo mismo que en producción (micropapilar, TP53 0.9142), con sus 18 archivos |

En total, unos 15 minutos, más los modelos (paso 5).

**Repetido el 4-nov-2026 con el merge del código de Gabo** (`gabo/master` v0.3.2 con correcciones del pentest; imágenes de dependencias `deps/*` de su registro y Keycloak 26 **externo**, como en Telconet): base en 3 min, 555 archivos en 10.5 min, 43 usuarios exportados e importados en el Keycloak externo, modelos (152) y `e2e.py` **26/26** con usuarios migrados. Caso 860eedbd igual que en producción.

## Qué se migra

| Dato | Dónde está en el respaldo | Cómo se migra |
|---|---|---|
| Pacientes, casos, resultados y auditoría | `db/oncology_xai-20261101.dump` (PostgreSQL 16, 344 KB) | paso 2 (`bd`) |
| **Usuarios y contraseñas** (Keycloak) | el mismo volcado (tablas de Keycloak dentro de la base) | Keycloak propio: llegan con el paso 2. **Keycloak del cluster:** se exportan e importan (paso 3). En los dos casos cada usuario entra con su misma clave |
| Láminas y resultados (imágenes y mapas) | `s3/lchai-prod-artifacts/` (`images/` 4.2 GB, `results/` 1.7 GB) | paso 4 (`s3`) |
| Modelos de IA | `s3/lchai-prod-models/` (5 GB; se usan 330 MB) | paso 5 |
| Ontología (Fuseki) | — | no se migra: se regenera |

## Quién hace qué

| Quién | Qué |
|---|---|
| Quien tenga acceso a la cuenta de AWS | Crea las credenciales de solo lectura (paso 1) y se las pasa a Gabo **por un canal seguro**, nunca por el repo ni por chat |
| Gabo | Corre los pasos 2 a 6 en el cluster de Telconet |

## Antes de empezar

- lchai desplegado con el chart y funcionando (`infra/tests/e2e.py` en 0 fallas).
- `kubectl` apuntando al cluster de destino, más `aws` (CLI v2) y `python3` en la máquina desde donde se corre el script.
- **El cluster necesita salida a internet hacia S3 de AWS** (`s3.us-east-1.amazonaws.com`, puerto 443): los pods descargan de ahí.
- **La migración reemplaza toda la base de destino.** Está pensada para un lchai recién instalado: se pierde lo que se haya creado ahí, como usuarios o casos de prueba.
- **Con ArgoCD (preprod de Telconet): pausar la sincronización automática de la app** durante el paso 2, y reactivarla al terminar. El paso 2 apaga los Deployments mientras restaura la base; con *self-heal*, ArgoCD los volvería a encender a mitad de la copia.
- Los pods temporales del script usan `postgres:16-alpine` y `amazon/aws-cli` de Docker Hub. Si el cluster no llega a Docker Hub, usar las del registro de dependencias:
  `export PG_IMAGE=registry.gitlab.telconet.ec/procesos/devops/deploys/txt/solca/lchai/deps/postgres:16.15-r1 AWS_IMAGE=registry.gitlab.telconet.ec/procesos/devops/deploys/txt/solca/lchai/deps/aws-cli:2.39.2`
- Los nombres con prefijo (`workloadPrefix`/`workloadSuffix`, p. ej. `txt-solca-lchai-keycloak-pre`) no afectan: el script busca cada componente por su etiqueta `app`. Con `keycloak.external` el paso 3 se hace en el Keycloak del cluster ([KEYCLOAK-EXTERNO.md](../keycloak/KEYCLOAK-EXTERNO.md)).
- Disco: unos 6 GB en el volumen de S3 del cluster (`localDeps.s3.storage`; `values-telconet.yaml` pide 100Gi). El pod que copia los archivos usa un disco temporal (`emptyDir`) de hasta 4.2 GB, el tamaño de `images/`, y lo libera al terminar.

## Paso 1. Credenciales de solo lectura

Son credenciales temporales que **solo pueden leer el bucket de respaldo**: no pueden escribir ni borrar, ni ver otro bucket. Vencen solas a las 4 horas. Las crea alguien con acceso a la cuenta de AWS:

```bash
cat > solo-lectura.json <<'JSON'
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject"],"Resource":"arn:aws:s3:::lchai-respaldo-20261101-632100838024/*"},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::lchai-respaldo-20261101-632100838024"}]}
JSON
umask 077
aws sts get-federation-token --name migracion-lectura --duration-seconds 14400 --policy file://solo-lectura.json \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' --output text > credenciales-lectura.txt
```

`credenciales-lectura.txt` trae la clave, el secreto, el token y la hora en que vencen. Se entrega por un canal seguro y se borra cuando termina la migración.

Gabo las carga en su terminal antes de correr los pasos 2, 4 y 5:

```bash
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN _ < credenciales-lectura.txt
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_REGION=us-east-1
unset AWS_PROFILE

aws sts get-caller-identity --query Arn --output text           # ...:federated-user/migracion-lectura
aws s3 ls s3://lchai-respaldo-20261101-632100838024/db/          # debe listar el volcado
```

El script pasa las credenciales a los pods en un Secret temporal (`lchai-migracion-aws`) y lo borra al terminar cada paso.

## Paso 2. Base de datos y usuarios (4 min)

```bash
NS=lchai SRC_DUMP=s3://lchai-respaldo-20261101-632100838024/db/oncology_xai-20261101.dump \
  infra/migracion/migrar-datos.sh bd
```

1. Detiene Keycloak y los servicios, para que nadie escriba durante la copia.
2. Un pod descarga el volcado y lo restaura con `pg_restore` en el PostgreSQL del cluster.
3. Completa el esquema con `init.sql`.
4. Vuelve a levantar la app.

Debe terminar mostrando `destino: 10 pacientes`.

## Paso 3. Keycloak

**Con el Keycloak del cluster (`keycloak.external`, el caso de Telconet):** seguir [infra/keycloak/KEYCLOAK-EXTERNO.md](../keycloak/KEYCLOAK-EXTERNO.md). Ahí se crea el realm en ese Keycloak, se exportan los usuarios de la base restaurada en el paso 2 con `infra/keycloak/exportar-usuarios.sh` y se importan con *Partial import*. Probado el 4-nov-2026: 43 usuarios importados, entran con su clave y e2e 26/26. El comando de abajo no aplica en este caso.

**Con un Keycloak propio desplegado por el chart (1.5 min):**

```bash
NS=lchai PUBLIC_URL=https://<domain> infra/migracion/migrar-datos.sh keycloak
```

- Reemplaza la clave del `admin` de la consola (realm master), que llega con la de AWS, por la de `secrets-telconet.yaml` (`keycloak.adminPassword`).
- Aplica `harden-realm.sh`: protección contra fuerza bruta, política de contraseñas, PKCE y audiencia del token.
- **Registra `PUBLIC_URL` como URL de redirección.** Sin esto el login se rechaza, porque el realm solo conoce el dominio de AWS.
- Borra a propósito la cuenta técnica `service-account-oncology-api`, como parte del endurecimiento. Por eso queda un usuario menos que en el origen.

## Paso 4. Archivos: láminas y resultados (7 min)

```bash
NS=lchai SRC_BUCKET=lchai-respaldo-20261101-632100838024 SRC_PREFIX=s3/lchai-prod-artifacts/ \
  DEST_BUCKET=lchai-prod-artifacts PREFIJOS="images/ results/" infra/migracion/migrar-datos.sh s3
```

Debe terminar con `objetos en destino: 555`.

- `SRC_PREFIX` lee desde la carpeta del respaldo y escribe en la raíz del bucket de destino. Así siguen valiendo las rutas que guarda la base (`s3://lchai-prod-artifacts/...`).
- Copia una carpeta a la vez y hace 4 transferencias en paralelo.
- Usa `sync`: si se corta, se vuelve a correr el mismo comando y sigue donde quedó.
- Si el S3 de destino no está dentro del cluster sino que es un servicio administrado de Telconet, agregar `DEST_S3_ENDPOINT=https://...`.
- Para probar con un solo caso: `PREFIJOS="images/<case_id>/ results/<case_id>/"`.

## Paso 5. Modelos de IA y GPU (los carga Gabo)

Sin los modelos, lchai abre los casos y resultados existentes, pero **no puede analizar láminas nuevas**: el worker de inferencia (`inference-worker`) los necesita.

### Qué modelos

En el respaldo están en `s3/lchai-prod-models/` (5 GB), pero el worker usa solo unos 330 MB:

| Archivo | Para qué |
|---|---|
| `backbones/ctranspath.pth` | extrae las características de cada cuadro de la lámina |
| `backbones/best_fuzzyarcloss_v2.pth` | clasifica los patrones histológicos |
| `checkpoints/*.pth` (150 archivos, 112 MB) | mutaciones (TP53, EGFR, KRAS...) |

El resto de `backbones/` (4.8 GB) son modelos que lchai no carga: no hace falta copiarlos.

### Copiarlos al S3 del cluster

Con las mismas credenciales de solo lectura del paso 1:

```bash
M="SRC_BUCKET=lchai-respaldo-20261101-632100838024 SRC_PREFIX=s3/lchai-prod-models/ DEST_BUCKET=lchai-prod-models"
env NS=lchai $M PREFIJOS="backbones/" INCLUIR="ctranspath.pth best_fuzzyarcloss_v2.pth" infra/migracion/migrar-datos.sh s3
env NS=lchai $M PREFIJOS="checkpoints/" INCLUIR="*.pth" infra/migracion/migrar-datos.sh s3
kubectl -n lchai rollout restart deploy/inference-worker      # al arrancar baja los modelos del S3 del cluster
```

Debe terminar con `objetos en destino: 152`. Probado el 2-nov-2026 en minikube: cargan los 152 modelos.

### GPU (recomendado)

El worker usa la GPU si la encuentra y, si no, pasa solo a CPU. Los resultados son **idénticos**; lo que cambia es el tiempo. Medido el 2-nov-2026 y repetido el 4-nov-2026 con la imagen `inference-service` del merge (EC2 g4dn.xlarge temporal, torch 2.6.0+cu124). En la lámina chica, la GPU y la CPU dieron exactamente los mismos patrones y genes:

| | Lámina completa de prueba (383 cuadros) |
|---|---|
| GPU NVIDIA T4 (16 GB) | 45 s |
| CPU | unos 6 min |

**Con GPU en el cluster:**
1. Hace falta un nodo con GPU NVIDIA (basta una T4 o superior, con 16 GB de VRAM) y el **NVIDIA device plugin** instalado: el nodo debe anunciar el recurso `nvidia.com/gpu`.
   ```bash
   kubectl get nodes -o custom-columns=NODO:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu
   ```
2. En `values-telconet.yaml`, sección 6:
   - `inferenceWorker.gpu: true`, que ya viene así;
   - `nodeSelector.gpu`, si el nodo de GPU tiene una etiqueta propia (por ejemplo `{ nvidia.com/gpu.present: "true" }`).

   El worker ya tolera el taint `nvidia.com/gpu`.
3. Dejar `updateStrategy: Recreate`: con una sola GPU, RollingUpdate deja el pod nuevo en `Pending`.
4. Comprobar que usa la GPU:
   ```bash
   kubectl -n lchai logs deploy/inference-worker | grep -iE "Inference device|GPU:"
   # Inference device: cuda (CUDA available: True)
   # GPU: Tesla T4, VRAM: ... GB
   ```
   El mensaje aparece con el primer análisis, no al arrancar.

**Sin GPU en el cluster** hay dos opciones:
- `inferenceWorker.gpu: false`: funciona igual, pero cada lámina tarda minutos. Sirve para empezar.
- Un worker con GPU fuera del cluster (por ejemplo, una EC2 g4dn.xlarge) conectado por VPN: [infra/worker-remoto/README.md](../worker-remoto/README.md).

## Paso 6. Verificar

```bash
NS=lchai infra/migracion/migrar-datos.sh verificar
```

Debe mostrar 10 pacientes, 7 casos, 7 imágenes, 19 resultados y 43 usuarios, y en S3, `10 images` y `545 results`. Después:

1. Entrar en la webapp con un usuario real y abrir un caso con resultados: deben verse la lámina, los patrones y los mapas.
2. Correr `infra/tests/e2e.py` (guía de despliegue, paso 7): sube una lámina pública de prueba, la analiza y la borra. Así se comprueba que los modelos y la GPU funcionan.
3. Borrar `credenciales-lectura.txt`. Igual vencen solas a las 4 horas.

**Rutas de los archivos:** la base guarda rutas `s3://lchai-prod-artifacts/<carpeta>/<case_id>/...`. Mientras el bucket de destino se llame igual, sirven sin cambios. Las láminas de producción figuran como subidas por `dev-user`, porque producción corría sin autenticación (S-01): se migran así.

## Problemas conocidos

| Síntoma | Causa | Solución |
|---|---|---|
| `AccessDenied` al leer el respaldo | credenciales vencidas (4 h) o quedó un `AWS_PROFILE` activo | crear credenciales nuevas (paso 1) y hacer `unset AWS_PROFILE` |
| `Could not connect to the endpoint URL: http://s3:8333/...` a mitad del paso 4 | SeaweedFS reiniciado por falta de memoria (`OOMKilled`) | ya corregido: límite de 3Gi (`localDeps.s3.resources`) y 4 transferencias en paralelo. Si vuelve a pasar, subir el límite y repetir el paso 4, que sigue donde quedó |
| El pod del paso 4 queda `Evicted` | el nodo no tiene disco temporal para `images/` (4.2 GB) | correr por caso: `PREFIJOS="images/<case_id>/ ..."` |
| Login rechazado (`Invalid parameter: redirect_uri`) | el realm no conoce el dominio nuevo | paso 3 con `PUBLIC_URL` |
| La consola de Keycloak no acepta la clave | el realm master trae la clave de AWS | paso 3 |
| Un caso abre, pero el visor queda en blanco | faltan sus archivos en S3 | paso 4 |
| Error al analizar una lámina nueva | faltan los modelos | paso 5 |

## Anexo: desde una RDS temporal

Es la alternativa al volcado del paso 2. Sirve si se quiere el estado exacto del snapshot final de RDS, que fue tomado minutos después del volcado. Necesita permisos para crear recursos en la cuenta de AWS y salida del cluster hacia el puerto 5432:

```bash
IP_SALIDA=<ip pública de salida del cluster>                     # curl -s https://checkip.amazonaws.com desde un pod
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC --query 'Subnets[].SubnetId' --output text)
SG=$(aws ec2 create-security-group --group-name lchai-migracion-sg --description "Temporal migracion lchai" \
  --vpc-id $VPC --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 5432 --cidr $IP_SALIDA/32
aws rds create-db-subnet-group --db-subnet-group-name lchai-migracion-subnets \
  --db-subnet-group-description "Temporal migracion lchai" --subnet-ids $SUBNETS
aws rds restore-db-instance-from-db-snapshot --db-instance-identifier lchai-migracion \
  --db-snapshot-identifier lchai-prod-postgres-final-20261101 --db-instance-class db.t3.micro \
  --db-subnet-group-name lchai-migracion-subnets --vpc-security-group-ids $SG --publicly-accessible --no-multi-az
aws rds wait db-instance-available --db-instance-identifier lchai-migracion          # 10-15 min
CLAVE_COPIA="Mig-$(openssl rand -hex 10)"                                          # clave solo para la copia
aws rds modify-db-instance --db-instance-identifier lchai-migracion --master-user-password "$CLAVE_COPIA" --apply-immediately
sleep 60; aws rds wait db-instance-available --db-instance-identifier lchai-migracion
HOST=$(aws rds describe-db-instances --db-instance-identifier lchai-migracion --query 'DBInstances[0].Endpoint.Address' --output text)

NS=lchai SRC_PG_HOST=$HOST SRC_PG_PASSWORD="$CLAVE_COPIA" infra/migracion/migrar-datos.sh bd

# Al terminar, borrar la base temporal (el snapshot final no se toca)
aws rds delete-db-instance --db-instance-identifier lchai-migracion --skip-final-snapshot --delete-automated-backups
aws rds wait db-instance-deleted --db-instance-identifier lchai-migracion
aws rds delete-db-subnet-group --db-subnet-group-name lchai-migracion-subnets
aws ec2 delete-security-group --group-id $SG
```

Después se siguen los pasos 3 a 6 como arriba.
