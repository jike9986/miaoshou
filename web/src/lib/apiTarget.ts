export interface BootstrapTargetConfig {
  savedBase?: string | null;
  legacyBase?: string | null;
  bootBase?: string | null;
  queryBase?: string | null;
  environmentBase?: string | null;
}

function isLoopbackBase(base: string): boolean {
  try {
    const hostname = new URL(base).hostname;
    return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
  } catch {
    return false;
  }
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
