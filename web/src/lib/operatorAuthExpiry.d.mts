export function shouldClearOperatorSession(
  status: number,
  rejectedToken: string | null,
  currentToken: string | null,
): boolean;

export function isPublicAuthRequest(method: string, path: string): boolean;

export function requestSessionToken(
  method: string,
  path: string,
  currentToken: string | null,
  explicitToken: string | undefined,
): string | null;
