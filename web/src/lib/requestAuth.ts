/** Build final request headers, allowing a caller to explicitly use another identity. */
export function withAuthorizationHeader(
  headers: Record<string, string>,
  defaultToken: string | null,
  overrideToken?: string,
): Record<string, string> {
  const token = overrideToken === undefined ? defaultToken : overrideToken;
  return token ? { ...headers, Authorization: `Bearer ${token}` } : { ...headers };
}
