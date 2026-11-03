/** Read and clear a pending request to bypass an upstream response cache. */
export function consumeOneShotRefreshFlag(flag) {
  const requested = flag.current === true;
  flag.current = false;
  return requested;
}
