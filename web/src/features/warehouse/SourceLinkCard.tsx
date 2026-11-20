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
import { CheckCircle2, ExternalLink, Link2, RefreshCw, Save, TriangleAlert } from "lucide-react";
import { Badge, Button, Card, InlineError, Input, toast } from "@/components/ui";
import { api, apiErrorMessage } from "@/lib/api";
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
}

export function SourceLinkCard({ product }: SourceLinkCardProps) {
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

  const save = useCallback(async () => {
    const url = draft.trim();
    if (!url) {
      setError("请填写 1688 货源链接");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.post(`/warehouse/${encodeURIComponent(String(product.id))}/source-link`, { url });
      toast.success("货源链接已保存");
      setEditing(false);
      // 刷新详情，让 payload 里的新值生效
      window.location.reload();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }, [draft, product.id]);

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
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setChecking(false);
    }
  }, [product.id]);

  const status = checked?.status ?? (stored ? "ok" : "missing");
  const meta = STATUS_META[status] ?? STATUS_META.unknown;

  return (
    <Card padding="normal">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <SectionTitle>⑧ 1688 货源链接</SectionTitle>
          <Badge variant={meta.tone} size="sm">{meta.label}</Badge>
          {isManual && <Badge variant="neutral" size="sm">手工填写</Badge>}
        </div>
        <Link2 className="size-3.5 text-fg-muted" aria-hidden strokeWidth={1.75} />
      </div>

      <p className="mt-2 text-xs leading-5 text-fg-muted">
        出单后采购员要照着这个链接买货。载体生成会优先使用它，而不是链接池里轮换的那条。
      </p>

      {editing ? (
        <div className="mt-3 space-y-2">
          <Input
            value={draft}
            onChange={(event) => setDraft(event.currentTarget.value)}
            placeholder="https://detail.1688.com/offer/123456789.html 或直接填 offer id"
            autoFocus
          />
          <p className="text-xs text-fg-muted">
            支持粘贴分享链接、移动端链接，或直接填 12 位 offer id。
          </p>
          {error && <InlineError>{error}</InlineError>}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>取消</Button>
            <Button variant="primary" size="sm" icon={<Save />} loading={saving} onClick={() => void save()}>
              保存
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="mt-3 rounded-sm border border-border bg-canvas-inset p-3">
            {stored ? (
              <a
                href={stored}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 break-all text-[13px] font-medium text-brand-700 underline decoration-brand-700/30 hover:decoration-brand-700"
              >
                {stored}
                <ExternalLink className="size-3.5 shrink-0" strokeWidth={1.75} />
              </a>
            ) : (
              <p className="text-[13px] text-fg-muted">尚未记录货源链接</p>
            )}
            {offerId && (
              <p className="mt-1.5 text-xs text-fg-muted">offer id：{offerId}</p>
            )}
            {sourceTitle && (
              <p className="mt-1 text-xs text-fg-secondary">货源标题：{sourceTitle}</p>
            )}
          </div>

          {checked && (
            <div className="mt-2 flex items-start gap-1.5 text-xs">
              {checked.status === "verified" ? (
                <>
                  <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success-700" strokeWidth={1.75} />
                  <span className="text-success-700">
                    妙手采集箱里能找到这条货源
                    {checked.offerTitle ? `：${checked.offerTitle}` : ""}
                  </span>
                </>
              ) : (
                <>
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning-700" strokeWidth={1.75} />
                  <span className="text-fg-secondary">
                    {checked.error || "无法确认（不代表失效）"}
                  </span>
                </>
              )}
            </div>
          )}

          {error && <div className="mt-2"><InlineError>{error}</InlineError></div>}

          {editable && (
            <div className="mt-3 flex justify-end gap-2">
              {stored && (
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<RefreshCw />}
                  loading={checking}
                  onClick={() => void check()}
                >
                  检查有效性
                </Button>
              )}
              <Button variant="secondary" size="sm" icon={<Link2 />} onClick={startEdit}>
                {stored ? "修改链接" : "填写链接"}
              </Button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}