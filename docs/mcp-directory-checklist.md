# MCP Connector Directory — Checklist de requisitos y pruebas

Guía completa para verificar que un servidor MCP remoto cumple todos los requisitos para ser publicado en el directorio oficial de conectores de Claude.

Referencia oficial: https://support.claude.com/en/articles/12922490-remote-mcp-server-submission-guide

---

## Cómo usar este documento

Copia el prompt del final de este documento y pégalo en una conversación con Claude (o cualquier otra IA). Sustituye las variables marcadas con `{{...}}` por los valores de tu servidor. La IA ejecutará todas las pruebas y te dará un informe detallado.

---

## 1. Requisitos técnicos obligatorios

### 1.1 Transport y endpoint

- [ ] El servidor expone un endpoint MCP sobre **Streamable HTTP** (POST)
- [ ] El endpoint responde con `content-type: text/event-stream`
- [ ] Las respuestas siguen el formato JSON-RPC 2.0 sobre SSE (`event: message\ndata: {...}`)
- [ ] Cada resultado de tool no supera los **25.000 tokens**

### 1.2 HTTPS / TLS

- [ ] El servidor usa **HTTPS** con certificado válido de una CA reconocida
- [ ] HTTP redirige a HTTPS (301)
- [ ] Headers de seguridad presentes:
  - `Strict-Transport-Security` (HSTS)
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`

### 1.3 Autenticación (OAuth 2.0)

- [ ] `GET /.well-known/oauth-protected-resource` devuelve HTTP 200 con metadata válida
- [ ] `GET /.well-known/oauth-authorization-server` devuelve HTTP 200 con metadata válida
- [ ] `GET /.well-known/openid-configuration` devuelve HTTP 200
- [ ] El endpoint MCP sin token devuelve **HTTP 401** con header `WWW-Authenticate: Bearer resource_metadata="..."`
- [ ] OAuth flow es **authorization code + PKCE (S256)**
- [ ] Las siguientes **callback URLs** están allowlisteadas como redirect URIs válidos:
  - `http://localhost:6274/oauth/callback`
  - `http://localhost:6274/oauth/callback/debug`
  - `https://claude.ai/api/mcp/auth_callback`
  - `https://claude.com/api/mcp/auth_callback`

### 1.4 CORS

- [ ] Preflight `OPTIONS` con `Origin: https://claude.ai` devuelve:
  - `Access-Control-Allow-Origin: https://claude.ai`
  - `Access-Control-Allow-Methods` incluye `POST`
  - `Access-Control-Allow-Headers` incluye `content-type, authorization`
- [ ] Lo mismo para `Origin: https://claude.com`

### 1.5 Safety annotations (OBLIGATORIO)

Cada tool DEBE tener un bloque `annotations` en la respuesta `tools/list` con al menos:

- [ ] `title` — nombre legible para humanos
- [ ] `readOnlyHint` — `true` para tools que solo leen datos
- [ ] `destructiveHint` — `true` para tools que escriben, modifican o tienen efectos externos
- [ ] `idempotentHint` — `true` si llamar repetidamente con los mismos args no tiene efecto adicional
- [ ] `openWorldHint` — `true` si interactúa con sistemas externos, `false` si solo datos locales

**Reglas de la decision matrix:**

| Tipo de operación | readOnlyHint | destructiveHint |
|---|---|---|
| Solo lectura | `true` | `false` |
| Escritura / modificación | `false` | `true` |
| Creación de ficheros temporales | `false` | `true` |
| Envío externo (emails, webhooks, posts en RRSS) | `false` | `true` |
| Caché interna solamente | `true` | `false` |

### 1.6 OAuth metadata completa

Campos obligatorios en `/.well-known/oauth-protected-resource`:

- [ ] `resource` — URL del endpoint MCP
- [ ] `authorization_servers` — array con al menos un issuer
- [ ] `scopes_supported` — scopes disponibles
- [ ] `bearer_methods_supported` — debe incluir `"header"`
- [ ] `resource_documentation` — **NO vacío**, URL a la documentación
- [ ] `resource_policy_uri` — URL a la privacy policy
- [ ] `resource_tos_uri` — URL a los términos de servicio

### 1.7 IP allowlisting (solo si hay firewall)

- [ ] Si el servidor está detrás de un firewall, las IPs de Claude deben estar allowlisteadas
- [ ] IPs disponibles en: https://docs.claude.com/en/api/ip-addresses
- [ ] No requerido para Claude Code (conecta desde la máquina del usuario)

---

## 2. Requisitos de documentación

La documentación debe ser pública y accesible. Puede ser un README en GitHub o una página web.

- [ ] **Descripción del servidor** — qué hace y para qué sirve
- [ ] **Features** — capacidades clave y casos de uso
- [ ] **Setup instructions** — cómo conectar desde Claude.ai, Claude Desktop, y Claude Code
- [ ] **Autenticación** — explicación del flujo OAuth y scopes
- [ ] **Uso / ejemplos** — mínimo **3 ejemplos funcionales** con:
  - Prompt del usuario
  - Qué tools se llaman
  - Output esperado
- [ ] **Privacy policy** — enlace a la política de privacidad
- [ ] **Soporte** — canal de contacto dedicado (email, help center, o GitHub issues)

---

## 3. Requisitos de producción

- [ ] El servidor está en **General Availability** (no beta/alpha/dev)
- [ ] Todas las features están implementadas y testeadas
- [ ] Error handling con mensajes útiles
- [ ] Infraestructura escalable
- [ ] **Monitoring y alerting** recomendados (health endpoint + uptime checks)
- [ ] El servidor funciona desde Claude.ai, Claude Desktop y Claude Code

---

## 4. Cuenta de test para revisores

- [ ] Cuenta con acceso a **todas las tools**
- [ ] **Datos de ejemplo** representativos
- [ ] Permisos completos para testing
- [ ] **Activa** durante la review y después (para re-reviews periódicas)
- [ ] Credenciales compartidas de forma segura (1Password o similar)

---

## 5. Prompt para ejecutar las pruebas automáticamente

Copia y pega lo siguiente en una conversación con Claude, sustituyendo las variables:

```
Necesito que verifiques si un servidor MCP remoto cumple todos los requisitos
para ser publicado en el directorio oficial de conectores de Claude.

Servidor: {{URL_DEL_ENDPOINT_MCP}}
Ejemplo: https://mi-servidor.com/mcp

Documentación: {{URL_DOCUMENTACION}}
Ejemplo: https://github.com/mi-org/mi-mcp-server

Token de autenticación (opcional, para verificar tools/list y annotations):
{{TOKEN_BEARER}}

Ejecuta las siguientes pruebas en orden y genera un informe con ✅/⚠️/❌:

### Bloque A — Transport y TLS
1. Haz un POST al endpoint MCP sin auth y verifica que devuelve HTTP 401
   con header WWW-Authenticate que incluya resource_metadata URL.
2. Verifica que el servidor usa HTTPS con certificado válido.
3. Comprueba los security headers: HSTS, X-Content-Type-Options, X-Frame-Options.

### Bloque B — OAuth discovery
4. GET /.well-known/oauth-protected-resource → debe ser 200 con JSON válido.
   Verifica que contiene: resource, authorization_servers, scopes_supported,
   bearer_methods_supported, resource_documentation (NO vacío),
   resource_policy_uri, resource_tos_uri.
5. GET /.well-known/oauth-authorization-server → debe ser 200 con JSON válido.
   Verifica que contiene: issuer, authorization_endpoint, token_endpoint,
   code_challenge_methods_supported (debe incluir S256).
6. GET /.well-known/openid-configuration → debe ser 200.

### Bloque C — CORS
7. Envía un preflight OPTIONS al endpoint MCP con:
   - Origin: https://claude.ai
   - Access-Control-Request-Method: POST
   - Access-Control-Request-Headers: content-type, authorization
   Verifica que la respuesta incluye Access-Control-Allow-Origin
   y Access-Control-Allow-Methods.
8. Repite con Origin: https://claude.com.

### Bloque D — MCP Protocol (requiere token válido)
9. Envía una request initialize con protocolVersion "2025-03-26" y verifica
   que devuelve una respuesta válida con mcp-session-id en los headers.
10. Envía tools/list usando el session ID y verifica:
    - Que cada tool tiene un bloque "annotations"
    - Que cada annotation incluye: title, readOnlyHint, destructiveHint,
      idempotentHint, openWorldHint
    - Que las tools de solo lectura tienen readOnlyHint: true
    - Que las tools de escritura tienen destructiveHint: true
    - Que ningún campo de annotations está ausente o null

### Bloque E — Health y monitoring
11. GET /health → verifica si existe y devuelve 200.

### Bloque F — Documentación (si se proporciona URL)
12. Accede a la URL de documentación y verifica que contiene las 7 secciones
    obligatorias: descripción, features, setup instructions, autenticación,
    mínimo 3 ejemplos de uso, privacy policy, y soporte.

Genera el informe final en formato tabla con:
- Check número
- Descripción
- Estado (✅/⚠️/❌)
- Detalle / acción necesaria

Al final, lista los problemas ordenados por prioridad:
1. ❌ Bloquantes (impiden la aprobación)
2. ⚠️ Recomendados (pueden causar rechazo)
3. 💡 Mejoras opcionales
```

---

## 6. Motivos comunes de rechazo

1. **Annotations ausentes** — ~30% de los rechazos. Cada tool necesita annotations completas.
2. **OAuth mal configurado** — Callbacks de Claude no allowlisteados, o metadata incompleta.
3. **Documentación incompleta** — Faltan secciones obligatorias o no hay mínimo 3 ejemplos.
4. **Servidor no production-ready** — Errores no manejados, marcado como beta.
5. **Privacy policy o soporte ausentes** — Deben ser accesibles públicamente.

---

## 7. Post-publicación

Una vez aprobado, para evitar que Anthropic retire el conector:

- Monitorizar uptime y rendimiento
- Responder a issues de usuarios
- Mantener dependencias actualizadas
- Cumplir con políticas que evolucionen
- Mantener la cuenta de test activa para re-reviews periódicas
