#!/usr/bin/env bash
# Genera el archivo del realm "oncology" listo para importar en un Keycloak externo (el del cluster de Telconet),
# con la URL pública de lchai como redirección y origen de la webapp. No trae usuarios ni secretos: se puede versionar.
#
# Uso: infra/keycloak/preparar-realm.sh https://<domain> > oncology-realm.json
# Luego: consola de Keycloak -> "Create realm" -> "Resource file" -> oncology-realm.json  (o kcadm, ver KEYCLOAK-EXTERNO.md)
set -euo pipefail
PUBLIC_URL="${1:?falta la URL pública de lchai, p. ej. https://lchai.telconet.ec}"
PUBLIC_URL="${PUBLIC_URL%/}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLIC_URL="$PUBLIC_URL" python3 - "$HERE/realm-export.json" <<'PY'
import json, os, sys
url = os.environ["PUBLIC_URL"]
realm = json.load(open(sys.argv[1]))
for c in realm["clients"]:
    if c["clientId"] == "oncology-webapp":
        c["redirectUris"] = [f"{url}/*"]
        c["webOrigins"] = [url]
        c.setdefault("attributes", {})["post.logout.redirect.uris"] = f"{url}/*"
realm.pop("users", None)
json.dump(realm, sys.stdout, indent=2, ensure_ascii=False)
print()
PY
