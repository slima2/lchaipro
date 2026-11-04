# Configurar lchai en el Keycloak del cluster (keycloak.external)

En Telconet lchai no despliega su propio Keycloak: usa el que ya existe en el cluster. Esta guía lista **todo lo que hay que crear y configurar en ese Keycloak**, cómo pasar los usuarios de producción y cómo verificarlo.

Probado el 4-nov-2026 en minikube, con un Keycloak 26.8.1 aparte que hacía de "Keycloak del cluster" (la misma imagen `deps/keycloak:26.8.1-r1`) y lchai desplegado con el chart en modo `keycloak.external`:
- se importaron los 43 usuarios del respaldo de AWS, sin errores;
- los usuarios migrados entraron con su rol;
- `infra/tests/e2e.py` dio 26/26;
- un caso real migrado se leyó completo.

## Resumen

| # | Qué | Dónde | Cómo |
|---|---|---|---|
| 1 | Realm `oncology` con sus roles, clientes y seguridad | Keycloak del cluster | importar `oncology-realm.json` (generado con `preparar-realm.sh`) |
| 2 | Usuarios de producción (43, con sus claves y roles) | Keycloak del cluster | *Partial import* de `oncology-usuarios.json` (generado con `exportar-usuarios.sh`) |
| 3 | Apuntar lchai a ese Keycloak | values del chart | `keycloak.external.enabled: true` y `keycloak.external.url` |
| 4 | Verificar | — | login y `infra/tests/e2e.py` |

## 1. Realm `oncology`

Generar el archivo del realm con el dominio público de lchai:

```bash
infra/keycloak/preparar-realm.sh https://<dominio-de-lchai> > oncology-realm.json
```

Importarlo de una de dos formas:
- **Consola:** *Manage realms* → *Create realm* → *Resource file* → `oncology-realm.json` → *Create*.
- **kcadm:** `kcadm.sh create realms -f oncology-realm.json`.

El archivo no trae usuarios ni secretos. Crea lo siguiente:

| Elemento | Valor | Para qué |
|---|---|---|
| Realm | `oncology` | lchai valida tokens de `<keycloak>/realms/oncology` |
| Roles del realm | `clinician`, `admin`, `auditor` | permisos de la app (el gateway los lee del token) |
| Cliente `oncology-webapp` | público, *Standard flow*, **PKCE S256**, sin *Direct access grants* | login de la webapp en el navegador |
| ↳ Valid redirect URIs | `https://<dominio-de-lchai>/*` | sin esto Keycloak rechaza el login (`Invalid parameter: redirect_uri`) |
| ↳ Web origins | `https://<dominio-de-lchai>` | CORS del navegador |
| ↳ Mapper `oncology-api-audience` | audiencia `oncology-api` en el access token | el gateway exige `aud = oncology-api` |
| ↳ Client scopes por defecto | `basic`, `web-origins`, `acr`, `profile`, `email`, `roles` | **`basic` es obligatorio en Keycloak 25+**: agrega `sub` al token. Sin él, la API responde `Token validation failed: 'sub'` |
| Cliente `oncology-api` | *bearer-only*, sin flujos | nombre de la audiencia; nadie inicia sesión con él |
| Seguridad del realm | fuerza bruta (5 intentos), claves de 12+ caracteres con mayúscula, número y símbolo, sin registro público, token de 5 min, sesión inactiva 30 min | endurecimiento (hallazgos de seguridad de CERT) |
| Eventos | de login y de administración, 90 días | auditoría |

Si se configura a mano en lugar de importar el archivo, hay que crear exactamente lo de esta tabla. Lo más fácil de olvidar es el scope `basic` y el mapper de audiencia.

lchai no usa cuentas de servicio ni *client secrets*: ningún servicio llama a la API de administración de Keycloak.

## 2. Usuarios de producción

Los usuarios de producción, con sus claves cifradas y sus roles, están dentro de la base de lchai que se restaura del respaldo de AWS ([infra/migracion/README.md](../migracion/README.md), paso 2). Después de ese paso se exportan:

```bash
NS=lchai infra/keycloak/exportar-usuarios.sh oncology-usuarios.json
# usuarios: 43  con clave: 43  roles: {'admin': 2, 'clinician': 29, 'default-roles-oncology': 40, 'auditor': 1}
```

El script:
1. copia la base a una base temporal;
2. levanta un pod temporal de Keycloak 26 que la lee y exporta el realm;
3. deja solo los usuarios de personas, sin la cuenta técnica `service-account-*`;
4. borra la base temporal y el pod.

La base de lchai no se modifica.

Importarlos en el Keycloak del cluster **después** de crear el realm:
- **Consola:** realm `oncology` → *Realm settings* → *Action* (arriba a la derecha) → *Partial import* → `oncology-usuarios.json` → *If a resource exists: Skip* → *Import*.
- **kcadm:** `kcadm.sh create partialImport -r oncology -s ifResourceExists=SKIP -o -f oncology-usuarios.json`.

Debe informar 43 agregados y 0 errores. Cada usuario entra con **su misma clave** de producción, porque se importa el hash (pbkdf2-sha256) y no la clave.

**`oncology-usuarios.json` contiene los hashes de las contraseñas:**
- no va al repo, ni por chat ni por correo;
- se genera y se importa en la misma sesión;
- se borra al terminar.

Notas sobre los usuarios:
- Producción conserva los usuarios de demo `admin1`, `clinician1` y `auditor1`. En el repo original su clave era igual al nombre. Conviene deshabilitarlos o cambiarles la clave después de importar.
- La política de claves nueva no afecta las claves importadas. Se aplica cuando cada usuario cambia la suya.

## 3. lchai apuntando al Keycloak del cluster

En los values del entorno:

```yaml
keycloak:
  realm: oncology
  external:
    enabled: true
    url: https://<keycloak-del-cluster>      # sin "/" final y sin /realms/...
auth:
  jwtAudience: oncology-api
```

Con esto el chart:
- no despliega Keycloak ni sus rutas en el ingress, y no exige `keycloak.adminPassword`;
- configura los servicios con `JWT_ISSUER=<url>/realms/oncology` y `JWKS_URL=<url>/realms/oncology/protocol/openid-connect/certs`;
- hace que la webapp lea `keycloakUrl` en `/runtime-config.js`.

**Requisitos de red:**
- Los **pods** de lchai deben llegar a `<keycloak-del-cluster>`, para descargar las llaves (JWKS).
- La **URL tiene que ser la misma** que ven los usuarios. El emisor (`iss`) del token debe coincidir con `JWT_ISSUER`.
- El paso `keycloak` de `migrar-datos.sh` **no aplica**: ese paso endurece un Keycloak propio. Con Keycloak externo, el endurecimiento ya viene en `oncology-realm.json`.

## 4. Verificar

```bash
curl -s https://<dominio-de-lchai>/runtime-config.js          # keycloakUrl debe ser el del cluster
curl -s https://<keycloak-del-cluster>/realms/oncology | head -c 200

LCHAI_KEYCLOAK_URL=https://<keycloak-del-cluster> \
LCHAI_CLINICIAN=usuario:clave LCHAI_ADMIN=usuario:clave LCHAI_AUDITOR=usuario:clave \
  python3 infra/tests/e2e.py https://<dominio-de-lchai> CMU-1-Small-Region.svs     # debe dar 0 fallas
```

`LCHAI_KEYCLOAK_URL` le indica a la prueba que el login es en otro dominio.

## Problemas conocidos

| Síntoma | Causa | Solución |
|---|---|---|
| `Token validation failed: 'sub'` (401 en todo) | el cliente `oncology-webapp` no tiene el scope `basic` | agregar `basic` en *Clients → oncology-webapp → Client scopes* (tipo *Default*) |
| `Invalid parameter: redirect_uri` | el dominio de lchai no está en *Valid redirect URIs* | `https://<dominio>/*` en `oncology-webapp` |
| 401 `Invalid audience` | falta el mapper de audiencia | mapper *Audience* → `oncology-api`, en el access token |
| 401 `Invalid issuer` | la URL de `keycloak.external.url` no es la que Keycloak pone en `iss` | usar exactamente la URL pública de Keycloak (revisar su `KC_HOSTNAME`) |
| Login OK, pero la app dice que no tiene permisos | el usuario no tiene `clinician`, `admin` ni `auditor` | asignar el rol en *Users → Role mapping* |
| `exportar-usuarios.sh`: `Database not up-to-date` | Keycloak no migró la copia | ya resuelto en el script; revisar que se use la imagen de Keycloak 26 (`KC_IMAGE`) |
