# Keycloak — hardening y gestión de usuarios demo (F001)

Este directorio contiene el export del realm (`realm-export.json`) y el
script `harden-realm.sh` que endurece un realm ya existente.

## F001 — usuarios demo con contraseñas débiles

El pentest de 2026-11-19 detectó que el realm `oncology` seguía
teniendo activos los usuarios de ejemplo del entorno de desarrollo:

| usuario     | contraseña (pentest) | rol       |
|-------------|----------------------|-----------|
| `admin1`    | `admin1`             | admin     |
| `clinician1`| `clinician1`         | clinician |
| `auditor1`  | `auditor1`           | auditor   |

En cualquier entorno accesible desde Internet (pre-prod o prod) esto
equivale a un bypass total del flujo clínico. El cierre del hallazgo
requiere **deshabilitar y rotar** la contraseña de los tres usuarios
antes de abrir el realm a la red.

### Qué hace el script

`harden-realm.sh` acepta dos flags mutuamente excluyentes:

* `--disable-demo-users` — por cada usuario demo:
  1. `enabled = false`;
  2. rota la contraseña a una cadena aleatoria de 32 caracteres
     (ya no es útil si alguien vuelve a poner `enabled=true` por error);
  3. marca `requiredActions = ["UPDATE_PASSWORD"]` como defensa extra.
* `--keep-demo-users` — opt-out explícito (solo dev).

Si no se pasa ningún flag:

* Con `KC_ENV=dev` (o detectado como tal) el script asume `keep` y no
  toca los usuarios.
* Con `KC_ENV=preprod` o `KC_ENV=prod` (o si `PUBLIC_URL` contiene
  `prod`/`preprod`/`telconet`) el script **falla duro** y obliga a
  tomar una decisión explícita. Es el mecanismo que cierra F001: ya
  no es posible "olvidar" el flag en una instalación productiva.

### Ejecuciones recomendadas

```bash
# Desarrollo (sandbox local)
NS=lchai KC_ENV=dev ./harden-realm.sh

# Pre-prod (al hacer go-live)
NS=lchai KC_ENV=preprod PUBLIC_URL=https://lchai-preprod.telconet.ec \
  ./harden-realm.sh --disable-demo-users

# Prod
NS=lchai KC_ENV=prod PUBLIC_URL=https://lchai.telconet.ec \
  ./harden-realm.sh --disable-demo-users
```

### Migración desde AWS

`infra/migracion/migrar-datos.sh` invoca `harden-realm.sh` al final de
la restauración y, por default, pasa `--disable-demo-users`. Para
mantener los demos (p.ej. durante una prueba de migración en staging)
exportar `KC_KEEP_DEMO=1` antes de correr la migración — queda en los
logs y **no es aceptable en prod**.

### Verificación post-ejecución

```bash
kubectl -n lchai exec deploy/keycloak -- \
  /opt/keycloak/bin/kcadm.sh get users -r oncology \
    -q username=admin1 --fields username,enabled
# → enabled debe ser false en pre-prod/prod
```

### Alcance restante

Este script trabaja sobre un realm ya importado. La política completa
(`passwordPolicy`, `bruteForceProtected`, mapper de audiencia, PKCE,
revocación de refresh tokens, etc.) también la aplica el mismo script;
ver el bloque `kc update "realms/$REALM"` al inicio para la lista
completa.
