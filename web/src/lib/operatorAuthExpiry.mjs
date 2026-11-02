/** A late 401 may clear only the session that issued the rejected request. */
export function shouldClearOperatorSession(status, rejectedToken, currentToken) {
  return status === 401
    && typeof rejectedToken === "string"
    && rejectedToken.length > 0
    && rejectedToken === currentToken;
}
