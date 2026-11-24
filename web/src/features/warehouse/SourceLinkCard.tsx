/**
 * 1688 货源链接卡片（负责人 2026-11-20）。
 *
 * ## 为什么需要这个卡片
 *
 * 载体生成时用的 1688 链接来自**池子轮换**（`take_link()` 挑最闲的一条），
 * 跟这件商品真实货源无关。用成品数据覆盖载体后，标题/图/价/SKU 全对，
 * 但采购链接还是载体自己那条 —— 店铺出单后采购员照着错误链接买货。
 *
 * 负责人原话：「店铺出单采购是最开始载体的链接而不是成品仓库的 1688 链接，
 * 导致我需要去重新复制链接重新弄。」
 *
 * ## 实测约束（决定了这个卡片能做什么）
 *
 * - 存量 532 件商品 **全部没有**货源字段（入仓时从来没存过）
 * - 妙手 `get_shop_collect_item_info` **不返回**货源链接（只有价格数字）
 * - **公共采集箱**的 `edit_common_collect_box_detail` **可以改** sourceList
 *   （实测写入后回读一致，且不消耗每天 10 个采集配额）
 * - 店铺采集箱 `save_shop_collect_item_info` **改不了**（传了也静默忽略）
 *
 * 所以这里做三件事：
 *   1. 显示链接（有就显示，没有提示补录）
 *   2. 老师/管理员可以手工改（改完载体生成就用对的）
 *   3. 一键检查有效性 —— 用妙手公共箱反查（不消耗采集配额）
 */
import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, ExternalLink, Link2, RefreshCw, TriangleAlert } from "lucide-react";
import { Badge, Card, InlineError, Input, toast } from "@/components/ui";
import { api, apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { currentOperator } from "@/lib/operator";
import type { ProductDetailProduct } from "@/lib/types";

/** 与 ProductDetailPage 里的同名局部组件保持同一视觉（不跨文件导出，各页自带）。 */
function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-[15px] font-semibold tracking-tight text-fg">{children}</h2>;
}

/** 后端 `source_link.STATUS_*` 的中文文案与色调。 */
const STATUS_META: Record<string, { label: string; tone: "success" | "danger" | "warning" | "neutral" | "info" }> = {
  ok: { label: "已记录", tone: "info" },
  verified: { label: "已确认存在", tone: "success" },
  missing: { label: "未记录货源", tone: "warning" },
  malformed: { label: "链接格式不对", tone: "danger" },
  dead: { label: "链接已失效", tone: "danger" },
  unknown: { label: "无法确认", tone: "neutral" },
};

interface CheckResult {
  status: string;
  offerId?: string;
  offerTitle?: string;
  error?: string;
}

interface SourceLinkCardProps {
  product: ProductDetailProduct;
  /** 保存成功后的回调：让父级重新取详情（替代原来的整页刷新） */
  onSaved?: () => void;
}

export function SourceLinkCard({ product, onSaved }: SourceLinkCardProps) {
  const payload = product.payload;
  const stored = String(payload?.sourceUrl ?? "").trim();
  const offerId = String(payload?.sourceItemId ?? "").trim();
  const sourceTitle = String(payload?.sourceTitle ?? "").trim();
  const isManual = Boolean(payload?.sourceManual);

  // 学员只读；老师和管理员可改（与受限分组同口径）
  const editable = (() => {
    const role = currentOperator()?.role;
    return role === "teacher" || role === "admin";
  })();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(stored);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState<CheckResult | null>(null);

  // 商品切换时重置本地状态，避免把上一个商品的链接带到这一件
  useEffect(() => {
    setEditing(false);
    setDraft(stored);
    setError(null);
    setChecked(null);
  }, [product.id, stored]);

  const startEdit = useCallback(() => {
    setDraft(stored);
    setError(null);
    setEditing(true);
  }, [stored]);

  /**
   * 失焦/回车自动保存。
   *
   * 三处必须注意：
   *   1. 值没变就直接退出 —— 点一下输入框也会触发 blur，不判断的话
   *      每次点击都发一次请求。
   *   2. **不能 `window.location.reload()`**。原来保存完刷新整页，
   *      在详情页里等于把用户正在看的东西全丢了，滚动位置、展开的图
   *      全部复位。现在改成让父级重新取详情。
   *   3. 失败时退回展示态并把错误显示出来，否则用户以为存好了。
   */
  const commit = useCallback(async () => {
    const url = draft.trim();
    if (url === stored.trim()) {
      setEditing(false);
      return;
    }
    if (!url) {
      // 清空不是有效操作：货源链接要么填要么别动，别静默删掉已有值
      setError("请填写 1688 货源链接");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.post(`/warehouse/${encodeURIComponent(String(product.id))}/source-link`, { url });
      toast.success("货源链接已保存");
      setEditing(false);
      // 让父级重新拉详情（不刷新整页，用户的滚动位置和展开状态都保留）
      onSaved?.();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }, [draft, stored, product.id, onSaved]);

  const check = useCallback(async () => {
    setChecking(true);
    setError(null);
    try {
      const result = await api.get<{ rows: CheckResult[] }>("/warehouse/source-links", {
        query: { live: 1, limit: 2000 },
      });
      const hit = (result.rows ?? []).find(
        (row) => String((row as unknown as { productId: number }).productId) === String(product.id),
      );
      setChecked(hit ?? { status: "missing" });
    } catch {
      // 自动核验失败保持静默，不覆盖用户在编辑的链接输入
    } finally {
      setChecking(false);
    }
  }, [product.id]);

  // 有货源链接就自动核验一次，不再要用户点「检查有效性」。
  // 失败不弹错：这只是个辅助判断，不该因为它报错打断编辑。
  useEffect(() => {
    if (!stored) return;
    void check().catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stored, product.id]);

  const status = checked?.status ?? (stored ? "ok" : "missing");
  const meta = STATUS_META[status] ?? STATUS_META.unknown;

  return (
    <Card padding="normal">
      {/* 2026-11-24 负责人：「这个 1688 货源链接右边不是比较空吗，这个
          officeid 可以移到右边，下面这个详情一打开就自动显示什么正在核验
          货源这个有什么用，可以移到上面 1688 货源链接名字的右边」。
          所以：状态徽章和核验结果都在标题行，offer id 在右侧和链接同一行。 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <SectionTitle>⑧ 1688 货源链接</SectionTitle>
          <Badge variant={meta.tone} size="sm">{meta.label}</Badge>
          {isManual && <Badge variant="neutral" size="sm">手工填写</Badge>}
          {/* 核验状态跟着标题走，不再占详情下方一块 */}
          {checking && !checked && (
            <span className="inline-flex items-center gap-1 text-xs text-fg-muted">
              <RefreshCw className="size-3 animate-spin" />
              正在核验货源
            </span>
          )}
          {checked && (
            <span className="inline-flex items-center gap-1 text-xs">
              {checked.status === "verified" ? (
                <>
                  <CheckCircle2 className="size-3.5 shrink-0 text-success-700" strokeWidth={1.75} />
                  <span className="text-success-700">
                    妙手采集箱里能找到这条货源
                    {checked.offerTitle ? `：${checked.offerTitle}` : ""}
                  </span>
                </>
              ) : (
                <>
                  <TriangleAlert className="size-3.5 shrink-0 text-warning-700" strokeWidth={1.75} />
                  <span className="text-fg-secondary">
                    {checked.error || "无法确认（不代表失效）"}
                  </span>
                </>
              )}
            </span>
          )}
        </div>
        <Link2 className="size-3.5 text-fg-muted" aria-hidden strokeWidth={1.75} />
      </div>

      {editing ? (
        /* 点进来就是一个输入框，改完失焦（或按回车）自动保存 ——
           没有「保存 / 取消」按钮（负责人 2026-11-23：
           「这个输入框我是说直接点击就可以输入而不是要点前面的链接，
            而且怎么还有保存的按键」）。
           值没变就不提交，避免点一下发一次请求。 */
        <div className="mt-3">
          <Input
            value={draft}
            onChange={(event) => setDraft(event.currentTarget.value)}
            onBlur={() => void commit()}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void commit();
                (event.currentTarget as HTMLInputElement).blur();
              }
              if (event.key === "Escape") setEditing(false);
            }}
            placeholder="粘贴 1688 链接或 offer id"
            autoFocus
            suffix={saving ? "保存中…" : undefined}
          />
          {error && <div className="mt-1"><InlineError>{error}</InlineError></div>}
        </div>
      ) : (
        <>
          <div
            role={editable ? "button" : undefined}
            tabIndex={editable ? 0 : undefined}
            onClick={editable ? startEdit : undefined}
            onKeyDown={
              editable
                ? (event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      startEdit();
                    }
                  }
                : undefined
            }
            className={cn(
              "mt-3 rounded-sm border border-border bg-canvas-inset p-3",
              editable && "cursor-text transition-colors hover:border-border-strong"
            )}
          >
            {/* 链接和 offer id 同一行：不换行时链接占左、id 靠右，
                窄屏才折下来 —— 右边原本空着一大片。 */}
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <div className="min-w-0">
                {stored ? (
                  <a
                    href={stored}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(event) => event.stopPropagation()}
                    className="inline-flex items-center gap-1.5 break-all text-[13px] font-medium text-brand-700 underline decoration-brand-700/30 hover:decoration-brand-700"
                  >
                    {stored}
                    <ExternalLink className="size-3.5 shrink-0" strokeWidth={1.75} />
                  </a>
                ) : (
                  <p className="text-[13px] text-fg-muted">
                    {editable ? "粘贴 1688 链接或 offer id" : "尚未记录货源链接"}
                  </p>
                )}
              </div>
              {offerId && (
                <p className="num shrink-0 text-xs text-fg-muted">offer id：{offerId}</p>
              )}
            </div>
            {sourceTitle && (
              <p className="mt-1 text-xs text-fg-secondary">货源标题：{sourceTitle}</p>
            )}
          </div>

          {error && <div className="mt-2"><InlineError>{error}</InlineError></div>}

        </>
      )}
    </Card>
  );
}