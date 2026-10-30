import Keycloak from 'keycloak-js';

// Configuración en runtime: nginx sirve /runtime-config.js con los valores del pod (KEYCLOAK_URL, KEYCLOAK_REALM,
// KEYCLOAK_CLIENT_ID). Así una sola imagen sirve para cualquier entorno y cualquier Keycloak (incluso externo).
const runtime = (window as unknown as { __LCHAI__?: { keycloakUrl?: string; keycloakRealm?: string; keycloakClientId?: string } }).__LCHAI__ ?? {};

const keycloak = new Keycloak({
  url: runtime.keycloakUrl || import.meta.env.VITE_KEYCLOAK_URL ||
    // Sin URL: mismo dominio desde el que se abre (Keycloak detrás del mismo ingress)
    (import.meta.env.DEV ? 'http://localhost:8080' : window.location.origin),
  realm: runtime.keycloakRealm || import.meta.env.VITE_KEYCLOAK_REALM || 'oncology',
  clientId: runtime.keycloakClientId || import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'oncology-webapp',
});

export default keycloak;
