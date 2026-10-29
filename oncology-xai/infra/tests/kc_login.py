"""Login en Keycloak como lo hace el navegador (formulario + code + PKCE). Imprime el access token.
Uso: python3 kc_login.py <public_url> <usuario> <clave>"""
import base64, hashlib, html, re, secrets, sys, urllib.parse
import requests

base, user, pwd = sys.argv[1:4]
realm, client = "oncology", "oncology-webapp"
redirect = base + "/"
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

s = requests.Session()
auth = f"{base}/realms/{realm}/protocol/openid-connect"
r = s.get(f"{auth}/auth", params={
    "client_id": client, "redirect_uri": redirect, "response_type": "code", "scope": "openid",
    "code_challenge": challenge, "code_challenge_method": "S256", "state": secrets.token_hex(8)})
r.raise_for_status()
m = re.search(r'action="([^"]+)"', r.text)
if not m:
    sys.exit(f"sin formulario de login: {r.status_code} {r.text[:300]}")
r = s.post(html.unescape(m.group(1)), data={"username": user, "password": pwd}, allow_redirects=False)
loc = r.headers.get("Location", "")
code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("code", [None])[0]
if not code:
    sys.exit(f"login fallido: HTTP {r.status_code}")
r = s.post(f"{auth}/token", data={"grant_type": "authorization_code", "client_id": client, "code": code,
                                  "redirect_uri": redirect, "code_verifier": verifier})
r.raise_for_status()
print(r.json()["access_token"])
