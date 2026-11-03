/** Verify cached rows once per query/mount; normal polling leaves selection usable. */
export function isCollectBoxReadOnly({ loading, verified, placeholder, degraded, failed }) {
  return Boolean(loading || !verified || placeholder || degraded || failed);
}
