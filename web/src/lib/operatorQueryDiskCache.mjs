const STORAGE_PREFIX = "aohuang.query-cache.v2.";
const MAX_ENTRIES = 24;
const MAX_SERIALIZED_LENGTH = 1_000_000;
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function storage() {
  try {
    return globalThis.window?.localStorage ?? null;
  } catch {
    return null;
  }
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPersistableQueryKey(queryKey) {
  if (!Array.isArray(queryKey) || queryKey.length < 2 || !isPlainObject(queryKey[1])) {
    return false;
  }

  // Persist list summaries only. Detail queries may contain full product payloads,
  // credentials, or other fields that are unnecessary for a fast first paint.
  return queryKey[0] === "collect-box" || queryKey[0] === "warehouse";
}

function identityStoragePrefix(identityKey) {
  return `${STORAGE_PREFIX}${encodeURIComponent(identityKey)}.account.`;
}

function accountIdForQueryKey(queryKey) {
  if (!Array.isArray(queryKey) || !isPlainObject(queryKey[1])) return null;
  if (queryKey[0] !== "collect-box" && queryKey[0] !== "warehouse") return null;
  // Warehouse rows are scoped by the MiaoShou account used to claim them.
  // Prefer that id when it differs from the software account id.
  const accountId = queryKey[1].claimAccountId ?? queryKey[1].accountId;
  return accountId === null || accountId === undefined || accountId === ""
    ? null
    : String(accountId);
}

function storageKey(identityKey, accountId) {
  return `${identityStoragePrefix(identityKey)}${encodeURIComponent(accountId)}`;
}

function readEntries(identityKey) {
  const target = storage();
  if (!target || !identityKey) return [];

  try {
    const now = Date.now();
    const entries = [];
    const prefix = identityStoragePrefix(identityKey);
    for (let index = target.length - 1; index >= 0; index -= 1) {
      const key = target.key(index);
      if (!key?.startsWith(prefix)) continue;
      try {
        const parsed = JSON.parse(target.getItem(key));
        if (parsed?.version !== 2 || !Array.isArray(parsed.entries) ||
          typeof parsed.accountId !== "string" || key !== storageKey(identityKey, parsed.accountId)) {
          target.removeItem(key);
          continue;
        }
        const fresh = parsed.entries.filter((entry) =>
          isPlainObject(entry) &&
          isPersistableQueryKey(entry.queryKey) &&
          accountIdForQueryKey(entry.queryKey) === parsed.accountId &&
          Number.isFinite(entry.updatedAt) &&
          entry.updatedAt > 0 && entry.updatedAt <= now &&
          now - entry.updatedAt <= MAX_AGE_MS &&
          "data" in entry,
        );
        entries.push(...fresh);
        try {
          if (fresh.length === 0) target.removeItem(key);
          else if (fresh.length !== parsed.entries.length) {
            target.setItem(key, JSON.stringify({ ...parsed, entries: fresh }));
          }
        } catch {
          // A full/read-only store can still provide its existing fresh pages.
        }
      } catch {
        // A corrupt page must not hide other usable accounts' cached lists.
        try { target.removeItem(key); } catch { /* Optional cache cleanup. */ }
      }
    }
    return entries;
  } catch {
    return [];
  }
}

function persist(identityKey, client) {
  const target = storage();
  if (!target || !identityKey) return;

  const entriesByAccount = new Map();
  for (const query of client.getQueryCache().getAll()) {
    const accountId = accountIdForQueryKey(query.queryKey);
    if (query.state.status !== "success" || query.state.data === undefined || !accountId) continue;
    const entries = entriesByAccount.get(accountId) ?? [];
    entries.push({
      queryKey: query.queryKey,
      data: query.state.data,
      // Keep cached content available immediately, but revalidate it after a
      // mutation invalidates the query.
      updatedAt: query.state.isInvalidated ? 0 : query.state.dataUpdatedAt,
    });
    entriesByAccount.set(accountId, entries);
  }

  for (const [accountId, entries] of entriesByAccount) {
    entries.sort((a, b) => b.updatedAt - a.updatedAt);
    entries.splice(MAX_ENTRIES);
    const key = storageKey(identityKey, accountId);
    try {
      let serialized = JSON.stringify({ version: 2, accountId, entries });
      while (serialized.length > MAX_SERIALIZED_LENGTH && entries.length > 1) {
        entries.pop();
        serialized = JSON.stringify({ version: 2, accountId, entries });
      }
      if (serialized.length <= MAX_SERIALIZED_LENGTH) target.setItem(key, serialized);
    } catch {
      // Local cache is an optimization; quota/private-mode failures must not
      // affect server reads or writes.
    }
  }
}

export function hydrateOperatorQueryClient(identityKey, client) {
  for (const entry of readEntries(identityKey)) {
    client.setQueryData(entry.queryKey, entry.data, { updatedAt: entry.updatedAt });
  }
}

export function persistOperatorQueryClient(identityKey, client) {
  if (!identityKey || !storage()) return () => {};
  persist(identityKey, client);
  return client.getQueryCache().subscribe((event) => {
    if (event.type === "updated" &&
      (event.action.type === "success" || event.action.type === "invalidate")) {
      persist(identityKey, client);
    }
  });
}

export function clearOperatorQueryDiskCache(identityKey) {
  const target = storage();
  if (!target || !identityKey) return false;
  try {
    const prefix = identityStoragePrefix(identityKey);
    for (let index = target.length - 1; index >= 0; index -= 1) {
      const key = target.key(index);
      if (key?.startsWith(prefix)) target.removeItem(key);
    }
    return true;
  } catch {
    return false;
  }
}

export function clearAllOperatorQueryDiskCaches() {
  const target = storage();
  if (!target) return;
  try {
    for (let index = target.length - 1; index >= 0; index -= 1) {
      const key = target.key(index);
      if (key?.startsWith(STORAGE_PREFIX)) target.removeItem(key);
    }
  } catch {
    // Clearing an optional cache must not block logout.
  }
}
