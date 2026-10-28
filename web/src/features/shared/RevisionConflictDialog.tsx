// features/shared —— 并发冲突提示（409 REVISION_CONFLICT）
//
// 为什么必须停下来问：
//   两个人同时编辑同一件商品时，后保存的那个会**无声地**盖掉前一个人的改动。
//   后端现在用 revision 做乐观锁，冲突时回 409 并带上双方版本号；
//   这里把选择权交回给人 —— 绝不自动覆盖，也绝不静默重试。
//
// 两个按钮的分工（文案写死，别改）：
//   · 「刷新看最新」：丢掉自己这一份未提交的修改，重新拉一次商品详情，关掉弹窗。
//     选它是「我认了，对方的版本为准」。
//   · 「保留我的修改」：把 actual（库里当前版本）当作新的 expectedRevision 再提交一次，
//     也就是「我知道冲突了，仍然要用我的改法覆盖」—— 这是一次显式的覆盖授权，
//     所以必须由人点，不能由代码自动做。

import { AlertTriangle, RefreshCw, ShieldAlert } from "lucide-react";
import { Badge, Button, Dialog } from "@/components/ui";
import type { RevisionConflict } from "@/lib/conflict";

export interface RevisionConflictDialogProps {
  /** 非 null 即打开；内容来自 revisionConflictOf(error) */
  conflict: RevisionConflict | null;
  onOpenChange: (open: boolean) => void;
  /** 「刷新看最新」：重新拉详情并关闭弹窗 */
  onRefresh: () => void;
  /** 「保留我的修改」：用 actual 作为新的 expectedRevision 再提交一次 */
  onKeepMine: () => void;
  /** 重试请求进行中（两个按钮都禁用，防连点产生两次覆盖） */
  busy?: boolean;
  /** 对象称呼，默认「这件商品」（批次/图片场景可传「这批商品」） */
  subject?: string;
}

/** 版本号展示：拿不到就写「未知版本」，不要显示 null */
function versionText(value: number | null): string {
  return value === null ? "未知版本" : `v${value}`;
}

export function RevisionConflictDialog({
  conflict,
  onOpenChange,
  onRefresh,
  onKeepMine,
  busy = false,
  subject = "这件商品",
}: RevisionConflictDialogProps) {
  const open = conflict !== null;
  const expected = conflict?.expected ?? null;
  const actual = conflict?.actual ?? null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // 冲突没解决就关掉弹窗等于「悄悄放弃」，允许关（用户可能想先去看看列表），
        // 但不提供「关掉就等于保存成功」的错觉：什么都不写。
        if (!next && !busy) onOpenChange(false);
      }}
      size="md"
      title={`${subject}已被修改`}
      showClose={!busy}
      // 冲突是「必须做选择」的弹窗，点遮罩不该消失
      closeOnOverlay={false}
      footer={
        <>
          <Button
            variant="secondary"
            icon={<RefreshCw />}
            disabled={busy}
            onClick={onRefresh}
          >
            刷新看最新
          </Button>
          <Button variant="primary" loading={busy} onClick={onKeepMine}>
            保留我的修改
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3.5">
        {/* 后端原话，逐字展示：它是唯一知道「到底哪里不一样」的一方 */}
        <div className="flex gap-2.5 rounded-md border border-warning-600/25 bg-warning-50 px-3 py-2.5">
          <AlertTriangle
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-warning-700"
            strokeWidth={2}
          />
          <p className="min-w-0 whitespace-pre-line break-words text-[13px] leading-5 text-warning-700">
            {conflict?.hint}
          </p>
        </div>

        {/* 两个版本号并排：差在哪一眼就能看出来 */}
        <div className="grid grid-cols-2 gap-2">
          <div className="rounded-md border border-border bg-canvas-inset/60 px-3 py-2">
            <p className="text-2xs leading-4 text-fg-muted">你编辑的版本</p>
            <p className="num mt-0.5 text-[15px] font-semibold leading-[22px] text-fg-secondary">
              {versionText(expected)}
            </p>
          </div>
          <div className="rounded-md border border-brand-500/40 bg-brand-50 px-3 py-2">
            <p className="flex items-center gap-1 text-2xs leading-4 text-brand-800">
              <ShieldAlert aria-hidden className="size-3" strokeWidth={2} />
              库里现在的版本
            </p>
            <p className="num mt-0.5 text-[15px] font-semibold leading-[22px] text-brand-800">
              {versionText(actual)}
            </p>
          </div>
        </div>

        <div className="flex flex-col gap-1.5 text-xs leading-[18px] text-fg-muted">
          <p>
            <span className="font-medium text-fg-secondary">刷新看最新</span>
            ：放弃你这次没保存的修改，重新读取对方改过的内容。
          </p>
          <p>
            <span className="font-medium text-fg-secondary">保留我的修改</span>
            ：用你的内容覆盖对方的改动（对方刚写的那一版会被替换掉）。
          </p>
        </div>

        {expected !== null && actual !== null && expected === actual && (
          // 版本号相同还报冲突，说明是别的字段维度（比如批量操作的整体校验），
          // 这时别让用户以为是「版本号写错了」
          <Badge variant="neutral" size="sm">
            版本号相同，冲突来自其他字段
          </Badge>
        )}
      </div>
    </Dialog>
  );
}

export default RevisionConflictDialog;
