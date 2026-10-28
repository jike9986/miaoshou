// features/warehouse —— 创建批次（POST /batch）
//
// 后端建批次只认 detailId（`_clean_detail_ids` 要求纯数字），而仓库列表里的
// 选中项是 warehouse id，所以这里必须先「解析」一遍：当前页的商品直接用
// 列表里已有的 detailId，跨页选中的商品按 id 逐个查详情补上。
//
// 一个批次只允许一个店铺（同账号同店铺同时只允许一条未完成批次），
// 跨店铺选择在这里就要拦掉，不能让后端在跑第一件时才发现。

import { useEffect, useMemo, useRef, useState } from "react";
import { Layers } from "lucide-react";
import { api, ApiError, apiErrorMessage } from "@/lib/api";
import { LANGUAGE_NAMES } from "@/lib/constants";
import { useCreateBatch, useGroups, useShops, useTemplates } from "@/lib/queries";
import type { Product, ProductDetailResponse } from "@/lib/types";
import {
  Badge,
  Button,
  Dialog,
  Field,
  InlineError,
  NumberInput,
  Select,
  SkeletonRows,
  Switch,
  toast,
} from "@/components/ui";

export interface CreateBatchDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 已选中的仓库商品 id（跨页保留） */
  ids: string[];
  /** 当前页商品，用来免掉一轮详情查询 */
  products?: Product[];
  /** 当前账号；建批次时一并落库 */
  accountId?: string | null;
  onCreated?: (batchId: string, count: number) => void;
}

interface ResolvedProduct {
  id: string;
  detailId: string;
  shopId: string;
  title: string;
}

/** 详情查询的并发上限：本地服务扛得住，但没必要一次打 200 个 */
const RESOLVE_CONCURRENCY = 6;

export function CreateBatchDialog({
  open,
  onOpenChange,
  ids,
  products = [],
  accountId = null,
  onCreated,
}: CreateBatchDialogProps) {
  const createBatch = useCreateBatch();
  const templatesQuery = useTemplates(open);
  const groupsQuery = useGroups(open);
  const shopsQuery = useShops(accountId, { enabled: open });

  const [template, setTemplate] = useState("");
  const [language, setLanguage] = useState("");
  const [groupId, setGroupId] = useState("");
  const [processImages, setProcessImages] = useState(false);
  const [mainImageCount, setMainImageCount] = useState<number | null>(3);
  const [error, setError] = useState<string | null>(null);

  const [resolved, setResolved] = useState<ResolvedProduct[]>([]);
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  // 解析是异步的：关掉弹窗后回来的结果不能再写进 state
  const resolveToken = useRef(0);
  // products 是仓库列表的派生数组，列表一刷新就换引用。放进 effect 依赖会让
  // 「打开着填表单」时突然重跑一遍解析，所以用 ref 取最新值，依赖只留 open + 选择集。
  const productsRef = useRef(products);
  productsRef.current = products;

  const templates = templatesQuery.data?.templates ?? [];
  const groups = groupsQuery.data?.groups ?? [];

  /* ---------------------------------------------------------------- *
   * 选中项 → detailId 解析
   * ---------------------------------------------------------------- */
  const idsKey = ids.join(",");
  useEffect(() => {
    if (!open) return;
    const token = ++resolveToken.current;
    const known = new Map<string, Product>();
    for (const product of productsRef.current) known.set(String(product.id), product);

    const initial: ResolvedProduct[] = [];
    const pending: string[] = [];
    for (const id of idsKey ? idsKey.split(",") : []) {
      const hit = known.get(String(id));
      if (hit) {
        initial.push({
          id: String(hit.id),
          detailId: String(hit.detailId ?? ""),
          shopId: String(hit.shopId ?? ""),
          title: hit.title ?? "",
        });
      } else {
        pending.push(String(id));
      }
    }

    setResolving(pending.length > 0);
    setResolveError(null);
    setError(null);
    setResolved(initial);
    if (pending.length === 0) return;

    void (async () => {
      const fetched: ResolvedProduct[] = [];
      let failure: string | null = null;
      let cursor = 0;

      const worker = async (): Promise<void> => {
        while (cursor < pending.length) {
          const index = cursor;
          cursor += 1;
          const id = pending[index];
          try {
            const detail = await api.get<ProductDetailResponse>(
              `/warehouse/${encodeURIComponent(id)}`
            );
            fetched.push({
              id: String(detail.product.id),
              detailId: String(detail.product.detailId ?? ""),
              shopId: String(detail.product.shopId ?? ""),
              title: detail.product.title ?? "",
            });
          } catch (cause) {
            failure = failure ?? apiErrorMessage(cause);
          }
        }
      };

      await Promise.all(
        Array.from({ length: Math.min(RESOLVE_CONCURRENCY, pending.length) }, worker)
      );

      if (token !== resolveToken.current) return;
      setResolved([...initial, ...fetched]);
      setResolving(false);
      if (failure) setResolveError(failure);
    })();
  }, [open, idsKey]);

  /* ---------------------------------------------------------------- *
   * 店铺 / 语言
   * ---------------------------------------------------------------- */
  const shopIds = useMemo(
    () => Array.from(new Set(resolved.map((item) => item.shopId).filter(Boolean))),
    [resolved]
  );
  const shopId = shopIds.length === 1 ? shopIds[0] : "";
  const shop = useMemo(
    () => shopsQuery.data?.shops.find((item) => item.shopId === shopId) ?? null,
    [shopsQuery.data?.shops, shopId]
  );

  // 站点接受的语言；拿不到就用全局的语言表兜底（不限制用户选择）
  const languageOptions = useMemo(() => {
    const siteLanguages = shopsQuery.data?.siteLanguages ?? {};
    const codes = shop?.languages?.length
      ? shop.languages
      : (shop ? siteLanguages[shop.site] : undefined) ?? Object.keys(LANGUAGE_NAMES);
    return codes.map((code) => ({ value: code, label: LANGUAGE_NAMES[code] ?? code }));
  }, [shop, shopsQuery.data?.siteLanguages]);

  // 换店铺后原语言可能不在新站点里，直接清掉，别让后端报 SITE_LANGUAGE_MISMATCH
  useEffect(() => {
    if (!language) return;
    if (!languageOptions.some((option) => option.value === language)) setLanguage("");
  }, [language, languageOptions]);

  const templateOptions = templates.map((item) => ({
    value: item.name,
    label: item.invalid ? `${item.name}（不可用）` : item.name,
    disabled: item.invalid,
  }));

  const detailIds = useMemo(() => {
    const seen = new Set<string>();
    const result: string[] = [];
    for (const item of resolved) {
      if (!item.detailId || seen.has(item.detailId)) continue;
      seen.add(item.detailId);
      result.push(item.detailId);
    }
    return result;
  }, [resolved]);

  const submit = (): void => {
    setError(null);
    if (ids.length === 0) {
      setError("请先在仓库里选中要处理的商品");
      return;
    }
    if (resolving) {
      setError("还在读取商品明细，请稍候");
      return;
    }
    if (shopIds.length > 1) {
      setError("一个批次只能包含同一个店铺的商品，请分开创建");
      return;
    }
    if (!shopId) {
      setError("选中的商品没有店铺信息，请重新选择");
      return;
    }
    if (detailIds.length === 0) {
      setError("选中的商品没有明细 id，无法建批次");
      return;
    }

    createBatch.mutate(
      {
        detailIds,
        shopId,
        accountId: accountId || null,
        // 后端 BatchCreateRequest 只有 settings 一个扩展位（老版散在 body 顶层的
        // template / language / groupId 在这里统一收进 settings），键名与
        // /collect-box/{id}/process 的 ProcessRequest 保持一致。
        settings: {
          ...(template ? { template } : {}),
          ...(language ? { language } : {}),
          ...(groupId ? { groupId } : {}),
          // processImages 只在真入仓（save）时生效，save 由后端默认 true，
          // 这里不传，避免把「只预览」的语义偷偷塞进批次里。
          ...(processImages ? { processImages: true, mainImageCount: mainImageCount ?? 0 } : {}),
        },
      },
      {
        onSuccess: (result) => {
          toast.success(`已创建批次，共 ${detailIds.length} 件`, {
            description: "去「批次」标签查看进度",
          });
          onCreated?.(result.batch.id, detailIds.length);
          onOpenChange(false);
        },
        onError: (cause) => {
          const code = cause instanceof ApiError ? cause.code : null;
          if (code === "CONFLICT_BUSY") {
            toast.warning("已有批次正在运行，请等它跑完", {
              description: apiErrorMessage(cause),
            });
            return;
          }
          setError(apiErrorMessage(cause));
        },
      }
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="md"
      title="创建批次"
      description="按选择顺序一件一件处理：每推进一步跑一件，可随时暂停或停止。"
      closeOnOverlay={false}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={createBatch.isPending}
            disabled={resolving || detailIds.length === 0 || shopIds.length > 1}
            onClick={submit}
          >
            创建批次（{detailIds.length || ids.length} 件）
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border-subtle bg-canvas-inset/60 px-3 py-2.5">
          <Layers className="size-4 shrink-0 text-fg-muted" />
          <span className="text-[13px] text-fg">
            已选 <span className="num font-medium">{ids.length}</span> 件
          </span>
          {shopId && (
            <Badge variant="neutral">
              {shop ? `${shop.name} · ${shop.siteName || shop.site}` : `店铺 ${shopId}`}
            </Badge>
          )}
          {resolving && <span className="text-2xs text-fg-muted">正在读取明细…</span>}
        </div>

        {shopIds.length > 1 && (
          <InlineError>
            选中的商品来自 {shopIds.length} 个店铺。一个批次只能跑同一个店铺的商品，请分开创建。
          </InlineError>
        )}
        {resolveError && <InlineError>{resolveError}</InlineError>}

        {templatesQuery.isLoading ? (
          <SkeletonRows rows={3} />
        ) : (
          <Field
            label="定价模板"
            hint={
              templates.length === 0
                ? "还没有保存定价模板，可留空（按商品自带价格入仓）"
                : "入仓时按模板重算售价"
            }
          >
            <Select
              value={template}
              onChange={setTemplate}
              options={templateOptions}
              placeholder="不指定模板"
            />
          </Field>
        )}

        <div className="grid grid-cols-2 gap-3">
          <Field
            label="语言"
            hint={
              shop
                ? `站点接受：${languageOptions.map((option) => option.label).join(" / ") || "未声明"}`
                : "选择店铺后显示站点语言"
            }
          >
            <Select
              value={language}
              onChange={setLanguage}
              options={languageOptions}
              placeholder="不指定语言"
              disabled={languageOptions.length === 0}
            />
          </Field>

          <Field label="分组" hint="入仓后归到该分组（可留空）">
            <Select
              value={groupId}
              onChange={setGroupId}
              options={groups.map((group) => ({
                value: group.id,
                label: `${group.name}（${group.count}）`,
              }))}
              placeholder="不指定分组"
              disabled={groupsQuery.isLoading}
            />
          </Field>
        </div>

        <div className="rounded-md border border-border-subtle p-3">
          <Switch
            checked={processImages}
            onChange={setProcessImages}
            label="同时处理主图翻译"
            showState
          />
          <p className="mt-1.5 text-xs leading-5 text-fg-muted">
            图片翻译是附加步骤：失败只降级，不会让整件商品算失败。
          </p>
          {processImages && (
            <div className="mt-3 flex items-center gap-3">
              <span className="shrink-0 text-[13px] text-fg-secondary">主图数量</span>
              <NumberInput
                value={mainImageCount}
                onChange={setMainImageCount}
                min={0}
                max={20}
                step={1}
                decimals={0}
                className="w-[120px]"
                suffix="张"
              />
              <span className="text-2xs text-fg-muted">0 = 不限</span>
            </div>
          )}
        </div>

        {error && <InlineError>{error}</InlineError>}
      </div>
    </Dialog>
  );
}

export default CreateBatchDialog;
