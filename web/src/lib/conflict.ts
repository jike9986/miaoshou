// 并发冲突（REVISION_CONFLICT）—— 从错误信封里取出版本号与后端原话
//
// 后端契约（409 + code=REVISION_CONFLICT）：
//   details = { expected: number, actual: number, hint: string }
//   · expected —— 你这次写请求里带的版本号（也就是你打开编辑器时看到的）
//   · actual   —— 库里现在的版本号（别人已经改成这一版了）
//   · hint     —— 后端写给人看的一句话，界面**原样**展示，不翻译不改写
//
// 注意：409 也可能是 CONFLICT_BUSY（有任务在跑），两者必须靠 code 区分，
// 不能靠 HTTP 状态码 —— 见 api.ts 的 fallbackCode(409) === "CONFLICT_BUSY"。

import { isApiError } from "@/lib/api";

export interface RevisionConflict {
  /** 你写请求里带的版本（拿不到时为 null） */
  expected: number | null;
  /** 库里现在的版本（拿不到时为 null） */
  actual: number | null;
  /** 后端原话，界面逐字展示 */
  hint: string;
}

/** 后端没给 hint 时的兜底说明（只在字段缺失时使用，绝不替换后端原文） */
export const REVISION_CONFLICT_FALLBACK_HINT =
  "这件商品在你打开编辑器之后被另一个人改过了。直接保存会把对方的修改覆盖掉，所以这次没有写入。";

/** 字段可能叫 expected/actual，也可能沿用老命名的 expectedRevision/currentRevision */
function readNumber(details: Record<string, unknown> | null, keys: string[]): number | null {
  if (!details) return null;
  for (const key of keys) {
    const raw = details[key];
    if (typeof raw === "number" && Number.isFinite(raw)) return raw;
    if (typeof raw === "string" && raw.trim() !== "" && Number.isFinite(Number(raw))) {
      return Number(raw);
    }
  }
  return null;
}

/**
 * 从错误里解析冲突信息；不是 REVISION_CONFLICT 时返回 null。
 *
 * try { await patch.mutateAsync(...) }
 * catch (e) { const c = revisionConflictOf(e); if (c) setConflict(c) }
 */
export function revisionConflictOf(error: unknown): RevisionConflict | null {
  if (!isApiError(error) || error.code !== "REVISION_CONFLICT") return null;
  const details = error.details;
  const hintRaw = details?.["hint"];
  const hint = typeof hintRaw === "string" && hintRaw.trim() !== "" ? hintRaw : REVISION_CONFLICT_FALLBACK_HINT;
  return {
    expected: readNumber(details, ["expected", "expectedRevision"]),
    actual: readNumber(details, ["actual", "currentRevision"]),
    hint,
  };
}

/** 是否为「有任务在跑」的 409（这时不该当错误弹窗，只提示一句） */
export function isConflictBusy(error: unknown): boolean {
  return isApiError(error) && error.code === "CONFLICT_BUSY";
}
