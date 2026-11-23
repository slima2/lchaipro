"""Login en Keycloak como lo hace el navegador (formulario + code + PKCE). Imprime el access token.
Uso: python3 kc_login.py <public_url> <usuario> <clave>
También se usa como módulo: from kc_login import login"""

import base64
import hashlib
import html
import re
import secrets
import sys
import urllib.parse

import requests

TIMEOUT = 30  # segundos por consulta


def login(
    base: str,
    user: str,
    pwd: str,
    realm: str = "oncology",
    client: str = "oncology-webapp",
    kc_base: str | None = None,
) -> str:
    """Devuelve el access token de un usuario siguiendo el flujo del navegador.
    kc_base: URL de Keycloak si no está en el mismo dominio que lchai (keycloak.external)."""
    redirect = base + "/"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    s = requests.Session()
    auth = f"{kc_base or base}/realms/{realm}/protocol/openid-connect"
    r = s.get(
        f"{auth}/auth",
        params={
            "client_id": client,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "openid",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_hex(8),
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if auth.startswith("http://"):
        # Keycloak 26 marca sus cookies como Secure. El navegador las envía igual a http://*.localhost
        # (contexto seguro), pero requests no: solo para pruebas locales sin TLS se quita la marca.
        for c in s.cookies:
            c.secure = False
    m = re.search(r'action="([^"]+)"', r.text)
    if not m:
        raise RuntimeError(f"sin formulario de login: {r.status_code} {r.text[:300]}")
    r = s.post(
        html.unescape(m.group(1)),
        data={"username": user, "password": pwd},
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    loc = r.headers.get("Location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("code", [None])[
        0
    ]
    if not code:
        raise RuntimeError(f"login fallido: HTTP {r.status_code}")
    r = s.post(
        f"{auth}/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client,
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["access_token"]


if __name__ == "__main__":
    try:
        print(login(*sys.argv[1:4]))
    except RuntimeError as e:
        sys.exit(str(e))
