export interface BootstrapTargetConfig {
  savedBase?: string | null;
  legacyBase?: string | null;
  bootBase?: string | null;
  queryBase?: string | null;
  environmentBase?: string | null;
}

export function isLoopbackBase(base: string): boolean {
  try {
    const hostname = new URL(base).hostname;
    return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
  } catch {
    return false;
  }
}

export function officialUpdateUrl(value?: string | null): string | null {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "https:" && url.hostname === "aohuang888.com"
      && !url.username && !url.password && (!url.port || url.port === "443")
      && url.pathname.toLowerCase().endsWith(".exe") ? url.href : null;
  } catch {
    return null;
  }
}

/** Only local development/desktop bootstrap needs an injected machine token. */
export function shouldShowLocalTokenNotice({
  apiBase,
  hasRuntimeToken,
  hasSoftwareSession,
}: {
  apiBase: string;
  hasRuntimeToken: boolean;
  hasSoftwareSession: boolean;
}): boolean {
  return !hasRuntimeToken && !hasSoftwareSession && (!apiBase || isLoopbackBase(apiBase));
}

export function allowsLocalBootstrapToken(config: BootstrapTargetConfig): boolean {
  if (config.savedBase || config.legacyBase) return false;

  const bootBase = config.bootBase?.replace(/\/+$/, "");
  const queryBase = config.queryBase?.replace(/\/+$/, "");
  if (bootBase || queryBase) {
    if (!bootBase || (queryBase && queryBase !== bootBase)) return false;
    return isLoopbackBase(bootBase);
  }

  const environmentBase = config.environmentBase?.replace(/\/+$/, "");
  return !environmentBase || isLoopbackBase(environmentBase);
}
