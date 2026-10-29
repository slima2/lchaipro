# Migrar los datos de lchai desde AWS

Se hace **después** de desplegar lchai con el chart ([guía de despliegue](../helm/lchai/README.md)).
El script [`migrar-datos.sh`](migrar-datos.sh) copia todo con pods dentro del cluster de destino: los datos van de AWS al cluster sin pasar por una laptop.

Probado el 28-oct-2026 en minikube con datos reales (copia del snapshot del día): base completa, 43 usuarios y 2 casos de S3. Resultado idéntico al de producción (caso 860eedbd: TP53 91.4%, micropapilar 98.7%).

## Qué se migra

| Dato | Dónde está en AWS | Cómo se migra |
|---|---|---|
| Pacientes, casos, resultados, auditoría | RDS `lchai-prod-postgres`, base `oncology_xai` | `migrar-datos.sh bd` |
| **Usuarios y contraseñas** (Keycloak) | la misma base (Keycloak la comparte) | vienen con `bd`; los usuarios entran con su misma clave |
| Láminas y resultados (imágenes, mapas) | S3 `lchai-prod-artifacts` (`images/`, `results/`), 5.9 GB vigentes | `migrar-datos.sh s3` |
| Modelos de IA | S3 `lchai-prod-models` | `NS=lchai infra/minikube/minikube-up.sh modelos` (guía de despliegue, paso 5) |
| Ontología (Fuseki) | Fuseki en EKS | no se migra: se regenera |

## Antes de empezar

- lchai desplegado con el chart y funcionando (`infra/tests/e2e.py` en 0 fallas).
- `kubectl` apuntando al cluster de destino, `aws` con lectura de la cuenta de AWS y `python3`.
- **La migración reemplaza toda la base de destino.** Está pensada para un lchai recién instalado: lo que se haya creado ahí (usuarios de prueba, casos) se pierde.
- El cluster de destino necesita salida a internet hacia RDS (puerto 5432) y S3 de AWS.

## Paso 1. Acceso a la base de AWS sin tocar producción

La base de producción no es pública. Se restaura el último snapshot en una **base temporal** con acceso solo desde la IP de salida del cluster de destino, y se borra al terminar. Producción no se modifica.

```bash
IP_SALIDA=<ip pública de salida del cluster de destino>      # curl -s https://checkip.amazonaws.com desde un pod
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC --query 'Subnets[].SubnetId' --output text)
SNAP=$(aws rds describe-db-snapshots --db-instance-identifier lchai-prod-postgres \
  --query 'reverse(sort_by(DBSnapshots,&SnapshotCreateTime))[0].DBSnapshotIdentifier' --output text)

SG=$(aws ec2 create-security-group --group-name lchai-migracion-sg --description "Temporal migracion lchai" \
  --vpc-id $VPC --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 5432 --cidr $IP_SALIDA/32
aws rds create-db-subnet-group --db-subnet-group-name lchai-migracion-subnets \
  --db-subnet-group-description "Temporal migracion lchai" --subnet-ids $SUBNETS
aws rds restore-db-instance-from-db-snapshot --db-instance-identifier lchai-migracion \
  --db-snapshot-identifier $SNAP --db-instance-class db.t3.micro --db-subnet-group-name lchai-migracion-subnets \
  --vpc-security-group-ids $SG --publicly-accessible --no-multi-az
aws rds wait db-instance-available --db-instance-identifier lchai-migracion          # 10-15 min

# Clave nueva solo para la copia temporal (la de producción no se conoce ni se cambia)
CLAVE_COPIA="Mig-$(openssl rand -hex 10)"
aws rds modify-db-instance --db-instance-identifier lchai-migracion --master-user-password "$CLAVE_COPIA" --apply-immediately
sleep 60; aws rds wait db-instance-available --db-instance-identifier lchai-migracion
HOST=$(aws rds describe-db-instances --db-instance-identifier lchai-migracion --query 'DBInstances[0].Endpoint.Address' --output text)
```

Para el corte definitivo (paso 7) conviene un snapshot manual tomado justo después de congelar AWS, en lugar del automático de la madrugada.

## Paso 2. Base de datos y usuarios

```bash
NS=lchai SRC_PG_HOST=$HOST SRC_PG_PASSWORD="$CLAVE_COPIA" infra/migracion/migrar-datos.sh bd
```

Detiene la app, vacía la base de destino, copia todo (`pg_dump` → `pg_restore`), completa el esquema con `init.sql` y vuelve a levantar la app. Debe mostrar el mismo número de pacientes en origen y destino. Tardó 1.5 min (el dump comprimido pesa ~350 KB).

## Paso 3. Keycloak

```bash
NS=lchai PUBLIC_URL=https://<domain> infra/migracion/migrar-datos.sh keycloak
```

- La clave del `admin` de la consola (realm master) llega con la de AWS. El script la reemplaza por la de `secrets-telconet.yaml` (`keycloak.adminPassword`).
- Aplica `harden-realm.sh`: fuerza bruta, política de contraseñas, PKCE y audiencia del token. **Registra `PUBLIC_URL` como URL de redirección**: sin eso, el login se rechaza porque el realm conoce solo el dominio de AWS.
- Borra la cuenta técnica `service-account-oncology-api`, a propósito, por el endurecimiento. Por eso queda un usuario menos que en el origen.

## Paso 4. Archivos (S3)

Todo el bucket (solo la versión vigente de cada archivo, ~5.9 GB):

```bash
NS=lchai infra/migracion/migrar-datos.sh s3
```

Solo algunos casos, para una prueba:

```bash
NS=lchai PREFIJOS="images/<case_id>/ results/<case_id>/" infra/migracion/migrar-datos.sh s3
```

- Toma las credenciales de AWS del perfil de `aws` (`AWS_PROFILE`). Las pasa al pod en un Secret temporal que se borra al terminar.
- Copia carpeta por carpeta. El pod necesita disco temporal para la carpeta más grande; con el bucket completo, poner `PREFIJOS="images/ results/"`.
- Si el S3 de destino no es el del cluster (servicio administrado de Telconet): `DEST_S3_ENDPOINT=https://...`.
- Referencia: 716 MB en 2 min.

## Paso 5. Verificar

```bash
NS=lchai infra/migracion/migrar-datos.sh verificar
```

Debe coincidir con el origen: 10 pacientes, 7 casos, 7 imágenes, 19 resultados y 43 usuarios (al 28-oct). Después:
- Entrar en la webapp con un usuario real y abrir un caso con resultados: deben verse la lámina, los patrones y los mapas.
- Si un caso abre pero el visor queda en blanco, faltan sus archivos en S3 (paso 4).

**Mapeo de archivos:** la base guarda rutas `s3://lchai-prod-artifacts/<carpeta>/<case_id>/...` (láminas, artefactos y mapas). Mientras el bucket de destino se llame igual, las rutas sirven sin cambios. Al 28-oct hay 480 referencias en la base: todas existen en AWS y ninguna está rota. Las láminas de producción figuran como subidas por `dev-user`, porque producción corre sin autenticación (S-01): se migra así.

## Paso 6. Borrar la base temporal

```bash
aws rds delete-db-instance --db-instance-identifier lchai-migracion --skip-final-snapshot --delete-automated-backups
aws rds wait db-instance-deleted --db-instance-identifier lchai-migracion
aws rds delete-db-subnet-group --db-subnet-group-name lchai-migracion-subnets
aws ec2 delete-security-group --group-id $SG
```

## Paso 7. Corte definitivo (cuando se cambie de AWS a Telconet)

1. Avisar a los usuarios y detener la escritura en AWS.
2. Snapshot manual de RDS y pasos 1 a 6 con ese snapshot. Con 6 GB, todo toma alrededor de 45 minutos, incluida la base temporal.
3. Cambiar el DNS del dominio público a Telconet. Si el dominio cambia, los usuarios conservan sus claves; solo hay que registrar el dominio (paso 3).
4. Dejar AWS apagado, pero sin borrarlo, hasta confirmar que todo funciona.

## Problemas conocidos

| Síntoma | Causa | Solución |
|---|---|---|
| `connection timed out` hacia la base temporal | la IP de salida del cluster no está en el security group | revisar `IP_SALIDA` (paso 1) |
| Login rechazado (`Invalid parameter: redirect_uri`) | el realm no conoce el dominio nuevo | paso 3 con `PUBLIC_URL` |
| La consola de Keycloak no acepta la clave | el realm master trae la clave de AWS | paso 3 |
| Visor en blanco en un caso | faltan sus archivos en S3 | paso 4 |
