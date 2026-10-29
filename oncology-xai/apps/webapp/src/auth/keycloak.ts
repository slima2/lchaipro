import Keycloak from 'keycloak-js';

const keycloak = new Keycloak({
  url: import.meta.env.VITE_KEYCLOAK_URL ||
    // Compilada sin URL: mismo dominio desde el que se abre (una sola imagen para cualquier dominio)
    (import.meta.env.DEV ? 'http://localhost:8080' : window.location.origin),
  realm: import.meta.env.VITE_KEYCLOAK_REALM || 'oncology',
  clientId: import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'oncology-webapp',
});

export default keycloak;
