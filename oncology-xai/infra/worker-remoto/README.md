# Worker de IA remoto (GPU fuera de Telconet)

Para cuando el cluster de Telconet no tiene GPU: el análisis de láminas corre en una máquina con GPU (p. ej. una
EC2 `g4dn.xlarge` en AWS, NVIDIA T4) conectada al lchai de Telconet. El resto de lchai (webapp, API, base de datos,
S3, Keycloak) sigue en Telconet.

Alternativa sin GPU: dejar el worker en el cluster en CPU (`inferenceWorker.gpu: false`). Una lámina completa
tarda unos 6 min con 4 núcleos (92 s en una T4), con el mismo resultado.

## Cómo funciona

```
 Telconet (Kubernetes)                                  AWS (EC2 con GPU)
 ┌──────────────────────────────┐   VPN privada   ┌──────────────────────────┐
 │ webapp, API, inference-svc    │                 │ worker (docker compose)  │
 │ RabbitMQ  :5672  (tareas)  ◄──┼─────────────────┼── toma las tareas        │
 │ Redis     :6379  (estado)  ◄──┼─────────────────┼── informa el avance      │
 │ PostgreSQL:5432  (resultados)◄┼─────────────────┼── guarda los resultados  │
 │ S3 (https://s3.<domain>)   ◄──┼─────────────────┼── lee láminas, sube PNG  │
 └──────────────────────────────┘                 │ modelos: S3 de AWS       │
                                                  └──────────────────────────┘
```

**Importante para CERT:** las láminas de los pacientes viajan de Telconet a la máquina de AWS para analizarse, y
los resultados vuelven. La conexión debe ir por una **VPN privada** (site-to-site entre AWS y Telconet). No se
deben exponer RabbitMQ, Redis ni PostgreSQL a internet abierto: no tienen TLS en el chart.

## Paso 1. En Telconet: apagar el worker del cluster y abrir el acceso al worker remoto

En `values-telconet.yaml`:

```yaml
inferenceWorker:
  enabled: false                 # el worker corre fuera del cluster
remoteWorker:
  enabled: true
  serviceType: LoadBalancer      # o NodePort si llegan a los nodos por la VPN
  allowedCidrs: ["<ip-privada-del-worker>/32"]
  annotations: {}                # p. ej. balanceador interno, según el proveedor de Telconet
```

El chart crea `rabbitmq-remoto`, `redis-remoto` y `postgres-remoto`, limitados a esa IP en el balanceador
(`loadBalancerSourceRanges`) y en la NetworkPolicy. Si `allowedCidrs` está vacío, el chart no se instala.

## Paso 2. Generar la configuración del worker

Con `kubectl` apuntando al cluster de Telconet:

```bash
cd infra/worker-remoto
NS=lchai ./generar-worker-env.sh            # con NodePort: NODE_IP=<ip-de-un-nodo> ./generar-worker-env.sh
cp .env.example .env                        # y completar WORKER_IMAGE
```

`worker.env` contiene las claves (RabbitMQ, Redis, PostgreSQL y S3): permisos 600, ignorado por git. Pasarlo a
la máquina del worker por un canal seguro (scp), nunca por chat.

## Paso 3. La máquina con GPU (AWS)

1. EC2 `g4dn.xlarge` (4 vCPU, 16 GB, T4 16 GB) con la AMI "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu)",
   que trae el driver de NVIDIA, Docker y nvidia-container-toolkit. Disco de 100 GB.
2. **Rol IAM** de la instancia con lectura de `lchai-prod-models` (los modelos se bajan de ahí, sin claves).
3. En la subred de la VPN con Telconet, **sin IP pública** y con un security group sin puertos de entrada.
   Acceso para administrarla: SSM Session Manager.
4. Imagen: la misma `inference-service` del despliegue. Desde el Harbor de Telconet (por la VPN) o desde un
   repositorio de ECR propio de este worker.
5. Costo aproximado: USD 0.53 por hora encendida (~USD 380 al mes las 24 horas). Se puede apagar fuera de horario.

## Paso 4. Arrancar

```bash
docker login <registro>
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
docker compose logs -f worker          # debe terminar en "celery@... ready."
nvidia-smi                             # durante un análisis se ve el proceso de Python usando la GPU
```

La primera vez, el servicio `modelos` descarga los modelos (~330 MB más los checkpoints) y luego arranca el
worker. Al reiniciar solo baja lo que falte.

## Verificar

Desde la webapp, subir una lámina y apretar **Analyze Slide**, o con `infra/tests/e2e.py`. En los logs del
worker remoto debe aparecer `Task inference.process_image[...] received` y luego `succeeded`.

## Problemas conocidos

| Síntoma | Causa | Solución |
|---|---|---|
| El análisis queda en `PENDING` | el worker no llega a RabbitMQ | VPN, `allowedCidrs`, `docker compose logs worker` |
| `connection refused` a PostgreSQL o Redis | servicio `*-remoto` sin IP o NetworkPolicy | `kubectl -n lchai get svc \| grep remoto` |
| El análisis falla al leer la lámina | el worker no resuelve o no llega a `s3.<domain>` | DNS y ruta por la VPN; probar `curl https://s3.<domain>` |
| `could not select device driver "nvidia"` | falta nvidia-container-toolkit | usar la AMI indicada o instalar el toolkit |
