/** A late 401 may clear only the session that issued the rejected request. */
export function shouldClearOperatorSession(status, rejectedToken, currentToken) {
  return status === 401
    && typeof rejectedToken === "string"
    && rejectedToken.length > 0
    && rejectedToken === currentToken;
}

const PUBLIC_AUTH_POSTS = new Set([
  "/auth/login",
  "/auth/register",
  "/auth/registration-code/request",
  "/auth/password-reset/request",
  "/auth/password-reset/confirm",
  "/auth/email-code-login/request",
  "/auth/email-code-login/confirm",
]);

/** Whether an auth request is intentionally usable without an operator token. */
export function isPublicAuthRequest(method, path) {
  if (method === "GET" && path === "/auth/credentials-status") return true;
  return method === "POST" && PUBLIC_AUTH_POSTS.has(path);
}

/**
 * Only authenticated requests should associate a 401 with the active session.
 * Public login failures must not clear a session that happens to be open.
 */
export function requestSessionToken(method, path, currentToken, explicitToken) {
  return !isPublicAuthRequest(method, path) && explicitToken === undefined
    ? currentToken
    : null;
}
