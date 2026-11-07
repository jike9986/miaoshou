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
  if (!Array.isArray(queryKey) || queryKey.length < 2) {
    return false;
  }

  // 列表摘要 + 商品详情都持久化。
  //
  // 详情原先被排除，理由是「payload 可能很大、含凭据」。实测本项目的
  // payload 只有 8–35KB（含规格/图片/描述），不含妙手凭据（凭据只在
  // accounts 表里单独加密，不进 payload）；而每次打开详情都串一次跨境
  // 请求，等待感明显。所以放进来，靠 MAX_SERIALIZED_LENGTH 兜底——
  // 超大详情会在写入时被 while 循环挤掉，不会撑爆 localStorage。
  //
  // 注意有两种 key 形态：列表首元素之后是对象，详情是字符串 accountId。
  // 早先这里要求 queryKey[1] 必须是对象，会把详情条目当垃圾清掉。
  if (queryKey[0] !== "collect-box" && queryKey[0] !== "warehouse") {
    return false;
  }
  return isPlainObject(queryKey[1]) || typeof queryKey[1] === "string";
}

function identityStoragePrefix(identityKey) {
  return `${STORAGE_PREFIX}${encodeURIComponent(identityKey)}.account.`;
}

function accountIdForQueryKey(queryKey) {
  if (!Array.isArray(queryKey) || queryKey.length < 2) return null;
  if (queryKey[0] !== "collect-box" && queryKey[0] !== "warehouse") return null;

  // 两种 key 形态：
  //   列表 ["warehouse", { accountId, claimAccountId, ... }, ...]
  //   详情 ["warehouse", accountId, id, { includePayload }]  ← accountId 是字符串
  // 详情原先没被持久化，所以这里只处理了对象形态；放开详情后必须同时识别
  // 字符串形态，否则读回来时 accountId 对不上，缓存全被当成无效条目清掉。
  const second = queryKey[1];
  if (typeof second === "string") {
    return second === "" || second === "current" || second === "none" ? null : second;
  }
  if (!isPlainObject(second)) return null;
  // Warehouse rows are scoped by the MiaoShou account used to claim them.
  // Prefer that id when it differs from the software account id.
  const accountId = second.claimAccountId ?? second.accountId;
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
