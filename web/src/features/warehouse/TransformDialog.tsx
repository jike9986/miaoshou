import { useEffect, useMemo, useRef, useState } from "react";
import { Percent, Trash2 } from "lucide-react";
import {
  Badge,
  Button,
  Dialog,
  Input,
  toast,
} from "@/components/ui";
import { apiErrorMessage } from "@/lib/api";
import { isConflictBusy, revisionConflictOf, type RevisionConflict } from "@/lib/conflict";
import { useTransform, type TransformBody, type TransformResponse } from "@/lib/queries";
import { RevisionConflictDialog } from "@/features/shared/RevisionConflictDialog";
import { cn, formatCount } from "@/lib/utils";

/**
 * 批量改价 / 删图。
 *
 * 两步走：先预览（dryRun），看到每件改前改后再确认落库。
 * 改价是不可撤销的——直接写库一旦倍数填错，几百件的价格全废，
 * 所以默认永远先算一遍给人看。
 */
type Mode = "multiplier" | "target";

export interface TransformDialogProps {
  open: boolean;
  ids: string[];
  /**
   * 选中商品各自的当前版本号（id → revision）。
   * 真写库（非预览）时带上，任何一件被别人改过就整体回滚并回 409 —— 不静默覆盖。
   * 老后端列表不回 revision，这里就是空对象，请求里不带 expectedRevisions。
   */
  revisions?: Record<string, number>;
  /** 「刷新看最新」时重新拉一次商品列表 */
  onRefresh?: () => void;
  onClose: () => void;
  onApplied?: (result: TransformResponse) => void;
}

export function TransformDialog({
  open,
  ids,
  revisions,
  onRefresh,
  onClose,
  onApplied,
}: TransformDialogProps) {
  const transform = useTransform();
  const [mode, setMode] = useState<Mode>("multiplier");
  const [multiplier, setMultiplier] = useState("1.35");
  const [target, setTarget] = useState("");
  const [targetIncludeVat, setTargetIncludeVat] = useState("");
  const [removeIndexes, setRemoveIndexes] = useState("");
  const [preview, setPreview] = useState<TransformResponse | null>(null);
  const [conflict, setConflict] = useState<RevisionConflict | null>(null);
  // 冲突后点「保留我的修改」要原样重发同一个请求体（只去掉版本约束）
  const pendingBodyRef = useRef<TransformBody | null>(null);

  useEffect(() => {
    if (open) {
      setPreview(null);
      setConflict(null);
      pendingBodyRef.current = null;
      transform.reset();
    }
    // transform 是 useMutation 的稳定引用，不进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const parsedIndexes = useMemo(
    () =>
      removeIndexes
        .split(/[,，\s]+/)
        .map((part) => Number.parseInt(part, 10))
        .filter((value) => Number.isFinite(value) && value > 0),
    [removeIndexes],
  );

  const buildBody = (dryRun: boolean): TransformBody => ({
    ids,
    priceMultiplier: mode === "multiplier" ? multiplier : undefined,
    targetPrice: mode === "target" ? target : undefined,
    targetPriceIncludeVat:
      mode === "target" && targetIncludeVat ? targetIncludeVat : undefined,
    removeImageIndexes: parsedIndexes,
    dryRun,
    // 只有真写库才带版本约束：预览不写数据，别人改了不该连预览都失败
    ...(!dryRun && revisions && Object.keys(revisions).length > 0
      ? { expectedRevisions: revisions }
      : {}),
  });

  const run = (dryRun: boolean): void => {
    const body = buildBody(dryRun);
    pendingBodyRef.current = body;
    transform.mutate(body, {
      onSuccess: (result) => {
        if (dryRun) {
          setPreview(result);
        } else {
          pendingBodyRef.current = null;
          onApplied?.(result);
          onClose();
        }
      },
      onError: (error) => {
        // 409 REVISION_CONFLICT：这批里有人先改过，停下来问人
        const found = revisionConflictOf(error);
        if (found) {
          setConflict(found);
          return;
        }
        // 409 CONFLICT_BUSY：有任务在跑，提示一句就好（下面那行错误条会显示后端原话）
        if (isConflictBusy(error)) {
          toast.warning(apiErrorMessage(error));
        }
      },
    });
  };

  /** 「刷新看最新」：放弃这次改动，重新拉列表 */
  const refreshAfterConflict = (): void => {
    setConflict(null);
    pendingBodyRef.current = null;
    onRefresh?.();
    onClose();
    toast.info("已重新拉取商品列表", {
      description: "这次没应用的改动已放弃，请确认最新内容后再改。",
    });
  };

  /** 「保留我的修改」：去掉版本约束重发（覆盖对方改动，由人显式授权） */
  const keepMineAfterConflict = (): void => {
    const body = pendingBodyRef.current;
    setConflict(null);
    if (!body) return;
    const { expectedRevisions: _ignored, ...withoutRevisions } = body;
    transform.mutate(withoutRevisions, {
      onSuccess: (result) => {
        pendingBodyRef.current = null;
        toast.success(`已应用改动（覆盖了对方的修改）`, {
          description: `共 ${formatCount(result.changedCount)} 件有改动`,
        });
        onApplied?.(result);
        onClose();
      },
      onError: (error) => {
        const found = revisionConflictOf(error);
        if (found) {
          setConflict(found);
          return;
        }
        if (isConflictBusy(error)) toast.warning(apiErrorMessage(error));
      },
    });
  };

  const nothingToDo =
    (mode === "multiplier" && !multiplier.trim()) ||
    (mode === "target" && !target.trim());
  const busy = transform.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={`批量改价（${formatCount(ids.length)} 件）`}
      size="md"
      footer={
        <>
          <Button variant="ghost" size="md" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            variant="secondary"
            size="md"
            onClick={() => run(true)}
            loading={busy && !preview}
            disabled={nothingToDo}
          >
            预览改动
          </Button>
          <Button
            variant="primary"
            size="md"
            onClick={() => run(false)}
            loading={busy && Boolean(preview)}
            disabled={!preview || preview.changedCount === 0}
          >
            应用到 {preview ? formatCount(preview.changedCount) : 0} 件
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3.5">
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setMode("multiplier")}
            className={cn(
              "flex flex-col gap-0.5 rounded-md border px-3 py-2 text-left transition-colors",
              mode === "multiplier"
                ? "border-brand-500 bg-brand-50"
                : "border-border hover:bg-canvas-inset",
            )}
          >
            <span className="text-xs font-medium text-fg">按倍数</span>
            <span className="text-2xs text-fg-muted">在现价上乘一个系数</span>
          </button>
          <button
            type="button"
            onClick={() => setMode("target")}
            className={cn(
              "flex flex-col gap-0.5 rounded-md border px-3 py-2 text-left transition-colors",
              mode === "target"
                ? "border-brand-500 bg-brand-50"
                : "border-border hover:bg-canvas-inset",
            )}
          >
            <span className="text-xs font-medium text-fg">指定价格</span>
            <span className="text-2xs text-fg-muted">所有 SKU 统一成一个价</span>
          </button>
        </div>

        {mode === "multiplier" ? (
          <label className="flex flex-col gap-1">
            <span className="text-xs text-fg-secondary">倍数</span>
            <Input
              value={multiplier}
              onChange={(event) => setMultiplier(event.target.value)}
              placeholder="1.35"
              inputMode="decimal"
            />
            <span className="text-2xs text-fg-muted">
              1.35 表示在原价基础上加价 35%；含税价按同比例同步缩放。
            </span>
          </label>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-fg-secondary">目标售价</span>
              <Input
                value={target}
                onChange={(event) => setTarget(event.target.value)}
                placeholder="39.90"
                inputMode="decimal"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-fg-secondary">含税价（可空）</span>
              <Input
                value={targetIncludeVat}
                onChange={(event) => setTargetIncludeVat(event.target.value)}
                placeholder="留空按原比例换算"
                inputMode="decimal"
              />
            </label>
          </div>
        )}

        <label className="flex flex-col gap-1">
          <span className="text-xs text-fg-secondary">删除主图（序号，逗号分隔）</span>
          <Input
            value={removeIndexes}
            onChange={(event) => setRemoveIndexes(event.target.value)}
            placeholder="例如 3,7 表示删掉第 3 张和第 7 张"
          />
          <span className="text-2xs text-fg-muted">
            序号从 1 开始。留空则不改图片。
          </span>
        </label>

        {transform.isError && (
          <div className="rounded-md border border-danger-600/20 bg-danger-50 p-2.5 text-xs text-danger-700">
            {apiErrorMessage(transform.error)}
          </div>
        )}

        {preview && (
          <div className="flex flex-col gap-2 rounded-md border border-border bg-canvas-inset/50 p-2.5">
            <div className="flex items-center gap-2 text-xs">
              <Badge variant={preview.changedCount > 0 ? "brand" : "neutral"}>
                {preview.changedCount > 0
                  ? `${formatCount(preview.changedCount)} 件有改动`
                  : "没有需要改动的商品"}
              </Badge>
              {preview.warnings.length > 0 && (
                <span className="text-fg-muted">
                  {formatCount(preview.warnings.length)} 件跳过
                </span>
              )}
            </div>
            <ul className="flex max-h-[180px] flex-col gap-1 overflow-y-auto text-2xs">
              {preview.items.slice(0, 20).map((item) => {
                const price = item.report.prices;
                const images = item.report.images;
                return (
                  <li key={item.warehouseId} className="flex flex-col gap-0.5">
                    <span className="line-clamp-1 text-fg">{item.title || "（无标题）"}</span>
                    <span className="num flex items-center gap-2 text-fg-muted">
                      {price && price.changedCount > 0 && (
                        <span>
                          {price.samples[0]?.priceBefore} → {price.samples[0]?.priceAfter}
                          {price.changedCount > 1 ? ` 等 ${price.changedCount} 个 SKU` : ""}
                        </span>
                      )}
                      {images && images.removedCount > 0 && (
                        <span className="flex items-center gap-0.5">
                          <Trash2 size={10} /> 删 {images.removedCount} 张
                        </span>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
            {preview.warnings.length > 0 && (
              <ul className="flex flex-col gap-0.5 text-2xs text-fg-muted">
                {preview.warnings.slice(0, 5).map((warning) => (
                  <li key={`${warning.index}-${warning.code}`}>{warning.message}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {!preview && !transform.isError && (
          <div className="flex items-center gap-1.5 text-2xs text-fg-muted">
            <Percent size={12} />
            先点「预览改动」看清每件改前改后，满意再应用。
          </div>
        )}
      </div>

      {/* 应用时的并发冲突：这批里有商品被别人改过，停下来问人 */}
      <RevisionConflictDialog
        conflict={conflict}
        onOpenChange={(next) => {
          if (!next) setConflict(null);
        }}
        onRefresh={refreshAfterConflict}
        onKeepMine={keepMineAfterConflict}
        busy={transform.isPending}
        subject="这批商品"
      />
    </Dialog>
  );
}
