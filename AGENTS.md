# Instrucciones para agentes (Cursor, Claude Code y otros)

Leer [CLAUDE.md](CLAUDE.md): estructura del proyecto y, en la sección **«Despliegue y migración desde el respaldo de AWS»**, cómo desplegar lchai, migrar los datos desde el respaldo de AWS y configurar Keycloak, sea el del cluster (`keycloak.external`) o el propio del chart.

Reglas mínimas:
- El respaldo de AWS (`lchai-respaldo-20261101-632100838024`) es de **solo lectura**. Contiene datos de pacientes.
- Ninguna credencial, token ni export de usuarios (`oncology-usuarios.json`, que lleva hashes de contraseñas) va al repo.
- No cambiar `oncology-xai/apps/` para resolver problemas de despliegue: ese código pasó el pentest. Los ajustes van en `oncology-xai/infra/`.
