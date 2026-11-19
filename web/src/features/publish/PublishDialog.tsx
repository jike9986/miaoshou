// features/publish —— 发布确认弹窗（api.md §8.1）
// 三重保险：显式商品列表 + 目标店铺 + 「confirm: true」二次勾选。
// 提交成功后不关窗：结果区常驻展示 repriced 明细表与 warning 告警块（可回看），
// 直到用户主动关闭弹窗；「刷新回执」直接放在结果区，不再只靠 8 秒 toast。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, PackageCheck, RefreshCw, Send } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { apiErrorMessage, api } from "@/lib/api";
import { toPublishItems } from "@/lib/constants";
import {
  useAuthStatus,
  usePublish,
  useRefreshPublishLogs,
  useShops,
  useTemplates,
  useSelectTemplate,
} from "@/lib/queries";
import type { PublishResponse, RepricedEntry, Shop } from "@/lib/types";
import {
  Badge,
  Button,
  Dialog,
  Field,
  InlineError,
  Select,
  Separator,
  SkeletonRows,
  toast,
} from "@/components/ui";

export interface PublishDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 仓库来源的商品 id（items[].warehouseId） */
  warehouseIds: string[];
  /** 当前店铺（顶部下拉）作为默认目标店铺 */
  defaultShopId: string | null;
  /** 是否允许把仓库商品认领到采集箱；学员只能发布已认领商品 */
  allowClaimOnly?: boolean;
  /** 每件商品自己的来源 shopId，用来判断跨店铺发布（要建载体） */
  sourceShopIds?: Record<string, string>;
  connected: boolean;
  /** 保留给「发布结果区」用（任务化之后正常路径不再走它）。 */
  onSubmitted?: (result: PublishResponse) => void;
  /** 发布任务创建成功：父组件据此刷新批次列表并切到批次页。 */
  onJobsCreated?: (batchIds: string[]) => void;
  /** 老师多账号模式：同时向多个账号发布 */
  accounts?: Array<{ id: string; name: string }>;
}

/** 五国站点固定顺序：马来 / 菲律宾 / 泰国 / 越南 / 新加坡。 */
const SITE_ORDER: string[] = ["MY", "PH", "TH", "VN", "SG"];

/** 站点码 → 中文名（勾选框上显示）。 */
const SITE_LABELS: Record<string, string> = {
  MY: "马来",
  PH: "菲律宾",
  TH: "泰国",
  VN: "越南",
  SG: "新加坡",
};

export function PublishDialog({
  open,
  onOpenChange,
  warehouseIds,
  defaultShopId,
  allowClaimOnly = true,
  connected,
  onJobsCreated,
  accounts = [],
}: PublishDialogProps) {
  const navigate = useNavigate();
  // 多账号模式：只要有多个账号就显示多账号选择器
  const isTeacher = accounts.length > 1;
  const { data: auth } = useAuthStatus();
  const shopsQuery = useShops(auth?.accountId, { enabled: open && connected });
  const templatesQuery = useTemplates(open && connected);
  const selectTemplate = useSelectTemplate();
  const publish = usePublish();
  const refreshLogs = useRefreshPublishLogs();

  // 老师多账号状态
  const [checkedAccountIds, setCheckedAccountIds] = useState<string[]>([]);
  const [shopsByAccount, setShopsByAccount] = useState<Record<string, Shop[] | null>>({});
  const [selectedShopIdsByAccount, setSelectedShopIdsByAccount] = useState<Record<string, string[]>>({});
  const [multiPublishing, setMultiPublishing] = useState(false);

  const shops = useMemo(() => shopsQuery.data?.shops ?? [], [shopsQuery.data?.shops]);
  const templates = templatesQuery.data?.templates ?? [];
  const usableTemplates = templates.filter((item) => !item.invalid);
  const requiresTemplate = !allowClaimOnly;

  const [shopId, setShopId] = useState<string | null>(defaultShopId);

  /** 模板名 → 站点。用于「按目标店铺站点自动选对应模板」。 */
  const templateSiteByName = useMemo(() => {
    const map: Record<string, string> = {};
    for (const item of usableTemplates) {
      const site = String(
        (item.template as { site?: unknown } | undefined)?.site ?? ""
      ).trim().toUpperCase();
      if (site) map[item.name] = site;
    }
    return map;
  }, [usableTemplates]);

  /** 站点 → 该站点的模板名（一个站点一套，取第一个）。 */
  const templateNameBySite = useMemo(() => {
    const map: Record<string, string> = {};
    for (const item of usableTemplates) {
      const site = templateSiteByName[item.name];
      if (site && !map[site]) map[site] = item.name;
    }
    return map;
  }, [usableTemplates, templateSiteByName]);

  /** 有模板的站点，按固定顺序 —— 决定一行显示哪几个勾选框。 */
  const templateSites = useMemo(
    () => SITE_ORDER.filter((site) => templateNameBySite[site]),
    [templateNameBySite]
  );

  /**
   * 上方选中的店铺覆盖了哪些站点（决定哪些国家的模板可勾）。
   *
   * 多账号：勾选账号 × 该账号下勾选的店铺；没勾店铺时按「回落第一个」算，
   *         和 submit 的分支保持一致。
   * 单账号：就是当前「目标店铺」那个站点。
   */
  const selectedShopSites = useMemo(() => {
    const sites = new Set<string>();
    if (isTeacher) {
      for (const accId of checkedAccountIds) {
        const accShops = shopsByAccount[accId] ?? [];
        const picked = selectedShopIdsByAccount[accId] ?? [];
        const targets = picked.length > 0
          ? accShops.filter((shop) => picked.includes(shop.shopId))
          : accShops.slice(0, 1);
        for (const shop of targets) {
          const site = String(shop.site ?? "").trim().toUpperCase();
          if (site) sites.add(site);
        }
      }
    } else {
      const target = shops.find((shop) => shop.shopId === shopId) ?? shops[0];
      const site = String(target?.site ?? "").trim().toUpperCase();
      if (site) sites.add(site);
    }
    return SITE_ORDER.filter((site) => sites.has(site));
  }, [isTeacher, checkedAccountIds, shopsByAccount, selectedShopIdsByAccount,
      shops, shopId]);

  /** 用户勾了「这次要用模板」的站点。 */
  const [useTemplateSites, setUseTemplateSites] = useState<string[]>([]);
  /** 用户主动取消的站点：自动默认勾选时不能把它又勾回来。 */
  const dismissedSitesRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!open) {
      dismissedSitesRef.current.clear();
      return;
    }
    setUseTemplateSites((prev) => {
      const keep = prev.filter(
        (site) => selectedShopSites.includes(site) && !dismissedSitesRef.current.has(site)
      );
      const add = selectedShopSites.filter(
        (site) => templateNameBySite[site]
          && !prev.includes(site)
          && !dismissedSitesRef.current.has(site)
      );
      const next = [...keep, ...add];
      const same = next.length === prev.length
        && next.every((site, index) => site === prev[index]);
      return same ? prev : next;
    });
  }, [open, selectedShopSites, templateNameBySite]);

  /** 某个站点这次用哪套模板；没勾或没模板就返回 null（不套模板）。 */
  const templateForSiteName = useCallback(
    (site: string | null | undefined): string | null => {
      const key = String(site ?? "").trim().toUpperCase();
      if (!key || !useTemplateSites.includes(key)) return null;
      return templateNameBySite[key] ?? null;
    },
    [useTemplateSites, templateNameBySite]
  );
  // 「本次要套模板的站点」是否都已经选好模板。
  // 学员发布必填模板：一个站点都没勾就等于没选模板，必须拦住。
  const hasValidTemplate = useTemplateSites.length > 0
    && useTemplateSites.every((site) => Boolean(templateNameBySite[site]));

  // 负责人 2026-11-18：删掉「汇率覆盖」「语言」「接受语言不匹配」三块死逻辑。
  //   · 汇率：界面上从来没有输入框，state 恒为空串，等于没传；
  //   · 语言：一个发布任务可能同时发 MY/PH/TH/VN/SG 五个站点，语言各不相同，
  //     单个 language 字段从结构上就不成立；后端 publish_service 也不再校验
  //     （check_language 已不调用），前端拦人等于拿一句假话挡用户；
  //   · allowMismatch：后端的 check_language 不跑，这个开关就没有下游。
  // 站点差异由每个店铺各自的定价模板承担，见 targets[].template。
  const effectiveClaimOnly = false;
  // 负责人 2026-11-05：发布不再需要二次确认勾选，默认视为已确认。
  const [confirmed, setConfirmed] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** 发布结果：非 null 时弹窗切到「结果区」，repriced / warning 常驻可回看 */
  const [result, setResult] = useState<PublishResponse | null>(null);


  // 老师多账号：勾选账号时懒加载店铺。null = 加载中
  const fetchShopsForAccount = async (accId: string): Promise<void> => {
    if (accId in shopsByAccount) return;
    setShopsByAccount((prev) => ({ ...prev, [accId]: null }));
    try {
      const result = await api.get<{ shops: Shop[] }>(`/shops?accountId=${encodeURIComponent(accId)}`);
      setShopsByAccount((prev) => ({ ...prev, [accId]: result.shops ?? [] }));
    } catch {
      setShopsByAccount((prev) => ({ ...prev, [accId]: [] }));
    }
  };

  const toggleAccount = (accId: string, checked: boolean): void => {
    if (checked) {
      setCheckedAccountIds((prev) => [...prev, accId]);
      void fetchShopsForAccount(accId);
    } else {
      setCheckedAccountIds((prev) => prev.filter((id) => id !== accId));
    }
  };

  // 当 checkedAccountIds 变化后，自动加载还没有店铺数据的账号
  useEffect(() => {
    for (const accId of checkedAccountIds) {
      if (!(accId in shopsByAccount)) {
        void fetchShopsForAccount(accId);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkedAccountIds]);

  // 打开时重置为默认店铺
  useEffect(() => {
    if (!open) return;
    setError(null);
    setConfirmed(true);
    // 默认用该账号当前的定价模板（老版 refreshScheduleTemplates 的 result.current）。
    // 不填的话每次发布都得手动选一次，选漏了就是按原价上架。

    setShopId(defaultShopId);
    setResult(null);
    // 老师多账号：打开时默认全选，重置状态，让 useEffect 按需加载
    if (isTeacher && accounts.length > 1) {
      setCheckedAccountIds(accounts.map((a) => a.id));
      setShopsByAccount({});
      setSelectedShopIdsByAccount({});
    } else {
      setCheckedAccountIds([]);
    }
  }, [open, defaultShopId, auth?.accountId, templatesQuery.data?.selected]);

  // 店铺列表到手后校验默认值
  useEffect(() => {
    if (!open || shops.length === 0) return;
    const target = shops.find((shop) => shop.shopId === shopId) ?? shops[0];
    if (target && target.shopId !== shopId) setShopId(target.shopId);
  }, [open, shops, shopId]);


  /** 回源刷新平台回执（结果区与 toast 共用） */
  const refreshReceipts = (): void => {
    refreshLogs.mutate(undefined, {
      onSuccess: (data) =>
        data.degraded
          ? toast.warning("平台暂时不可达，稍后再刷新")
          : toast.success(`已更新 ${data.updated} 条回执`),
      onError: (refreshError) =>
        toast.error("刷新回执失败", { description: apiErrorMessage(refreshError) }),
    });
  };

  const submit = async (): Promise<void> => {
    setError(null);
    if (warehouseIds.length === 0) {
      setError("请选择要发布的商品");
      return;
    }
    if (warehouseIds.length > 200) {
      setError("一次最多发布 200 件商品");
      return;
    }
    if (requiresTemplate && !hasValidTemplate) {
      setError("学员发布必须选择一个可用的定价模板");
      return;
    }
    if (!confirmed) {
      setError("请勾选发布确认");
      return;
    }

    // 统一走后台任务：点发布只创建任务，真正的发布在后台线程里按店铺推进。
    //
    // 为什么不能在前端串行 await（负责人 2026-11-18 反馈「正在提交然后没反应」）：
    //   后端每个店铺都要在妙手重建载体，实测单店 30~60 秒；5 个店铺串行就是
    //   几分钟，而 usePublish 的 timeoutMs 只有 180 秒 —— 前端先超时断开，
    //   用户看到按钮一直停在「正在提交…」，实际后台还在跑。
    //
    // 任务化之后：创建请求只是落一条批次记录（几十毫秒），立刻返回；
    // 进度由批次页轮询 /claim-jobs 显示，还能看每个店铺的成败原因。
    const items = toPublishItems(warehouseIds.map((id) => ({ warehouseId: id })));
    const jobs: Array<{
      accountId: string;
      label: string;
      targets: Array<{ shopId: string; template: string | null }>;
    }> = [];

    if (isTeacher && checkedAccountIds.length > 0) {
      for (const accId of checkedAccountIds) {
        const accShops = shopsByAccount[accId] ?? [];
        const picked = selectedShopIdsByAccount[accId] ?? [];
        // 没勾店铺时回落到该账号第一个店铺（和 UI 上的显示口径一致）
        const shopIds = picked.length > 0 ? picked : [accShops[0]?.shopId];
        const targets = shopIds
          .filter((value): value is string => Boolean(value))
          .map((shopIdValue) => ({
            shopId: shopIdValue,
            // 每个店铺用它自己站点的模板：MYR 模板套到 PH 店铺必错。
            template: templateForSiteName(
              accShops.find((s) => s.shopId === shopIdValue)?.site),
          }));
        if (targets.length) {
          jobs.push({
            accountId: accId,
            label: accounts.find((item) => item.id === accId)?.name ?? accId,
            targets,
          });
        }
      }
    } else {
      if (!shopId) {
        setError("请选择目标店铺");
        return;
      }
      jobs.push({
        accountId: String(auth?.accountId ?? ""),
        label: String(auth?.connectionName ?? ""),
        targets: [{ shopId, template: templateForSiteName(
          (shops.find((shop) => shop.shopId === shopId) ?? shops[0])?.site) }],
      });
    }

    if (jobs.length === 0) {
      setError("没有可发布的店铺");
      return;
    }

    setMultiPublishing(true);
    const created: string[] = [];
    const failed: string[] = [];
    try {
      for (const job of jobs) {
        try {
          const response = await api.post<{ batch: { id: string } }, Record<string, unknown>>(
            "/publish-jobs",
            {
              accountId: job.accountId,
              items,
              targets: job.targets,
              confirmUnverifiedSpecifications: true,
              confirm: true,
            },
            // 创建任务只是落库，正常几十毫秒；给 30 秒足够，且远小于发布本身的耗时。
            { timeoutMs: 30_000 },
          );
          created.push(response.batch.id);
        } catch (jobError) {
          failed.push(`${job.label}：${apiErrorMessage(jobError)}`);
        }
      }
    } finally {
      setMultiPublishing(false);
    }

    if (created.length > 0) {
      const shopTotal = jobs.reduce((sum, job) => sum + job.targets.length, 0);
      toast.success(`已创建 ${created.length} 个发布任务（共 ${shopTotal} 个店铺）`, {
        description: "任务在后台执行，可到「批次」查看每个店铺的进度",
        duration: 8000,
      });
      onJobsCreated?.(created);
      onOpenChange(false);
    }
    if (failed.length > 0) {
      toast.error(`${failed.length} 个任务创建失败`, {
        description: failed.slice(0, 3).join("；"),
        duration: 12000,
      });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size={result ? "lg" : "md"}
      title={result ? "发布结果" : "发布到店铺"}
      description={result ? "结果保留在弹窗里，关闭后可在发布记录里回看。" : ""}
      closeOnOverlay={false}
      footer={
        result ? (
          <>
            <Button
              variant="secondary"
              onClick={() => {
                // 回到表单时把「我确认发布」重新置空，避免沿用上一次的确认态
                setResult(null);
                setConfirmed(true);
                          }}
            >
              继续发布
            </Button>
            <Button
              variant="secondary"
              icon={<RefreshCw />}
              loading={refreshLogs.isPending}
              loadingText="回源中…"
              onClick={refreshReceipts}
            >
              刷新回执
            </Button>
            <Button variant="primary" onClick={() => onOpenChange(false)}>
              关闭
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              icon={<Send />}
              loading={publish.isPending || multiPublishing}
              loadingText="正在提交…"
              disabled={!confirmed
                || (requiresTemplate && !hasValidTemplate)
                || (isTeacher && accounts.length > 1 ? checkedAccountIds.length === 0 : !shopId)}
              onClick={() => { void submit(); }}
            >
              {(() => {
                // 按钮文案按「实际会提交多少条」算，而不是按账号数：
                // 10 个账号各勾 2 个店铺时，说「向 10 个账号发布」会严重低估工作量。
                if (isTeacher && checkedAccountIds.length > 0) {
                  const targets = checkedAccountIds.reduce((sum, accId) => {
                    const picked = selectedShopIdsByAccount[accId] ?? [];
                    if (picked.length > 0) return sum + picked.length;
                    // 没勾店铺时回落到第一个，和 submit 的分支保持一致
                    return sum + ((shopsByAccount[accId]?.[0]?.shopId) ? 1 : 0);
                  }, 0);
                  if (targets === 0) {
                    return effectiveClaimOnly ? "认领到采集箱" : `发布 ${warehouseIds.length} 件`;
                  }
                  return effectiveClaimOnly
                    ? `认领到 ${targets} 个店铺`
                    : `向 ${targets} 个店铺发布 ${warehouseIds.length} 件`;
                }
                return effectiveClaimOnly ? "只认领进采集箱" : `发布 ${warehouseIds.length} 件`;
              })()}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <PublishResultPanel result={result} />
      ) : (
        <div className="flex flex-col gap-4">
          {!connected && (
            <InlineError>
              尚未连接妙手账号，发布前请先
              <button
                type="button"
                className="ml-1 font-medium underline"
                onClick={() => navigate("/unlock", { state: { from: "/publish" } })}
              >
                连接妙手
              </button>
              。
            </InlineError>
          )}

          <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3">
            <div className="flex items-center justify-between">
              <p className="text-[13px] font-medium text-fg-secondary">发布清单</p>
              <Badge variant="neutral">
                <span className="num">{warehouseIds.length}</span> 件
              </Badge>
            </div>
            <p className="mt-1 font-mono text-2xs leading-4 text-fg-muted">
              {warehouseIds.slice(0, 12).join(", ")}
              {warehouseIds.length > 12 ? ` …等 ${warehouseIds.length} 件` : ""}
            </p>
          </div>

          {shopsQuery.isLoading ? (
            <SkeletonRows rows={3} />
          ) : shopsQuery.isError ? (
            <InlineError onRetry={() => void shopsQuery.refetch()}>
              {apiErrorMessage(shopsQuery.error)}
            </InlineError>
          ) : (
            <>
              {/* 老师多账号模式：账号复选 + 各账号店铺选择 */}
              {isTeacher && accounts.length > 1 ? (
                <div>
                  <div className="mb-1 flex items-baseline justify-between gap-1">
                    <div className="flex items-baseline gap-1">
                      <span className="text-sm font-medium text-fg">发布账号</span>

                    </div>
                    <button
                      type="button"
                      className="text-xs text-brand-600 hover:underline"
                      onClick={() => {
                        if (checkedAccountIds.length === accounts.length) {
                          setCheckedAccountIds([]);
                        } else {
                          setCheckedAccountIds(accounts.map((a) => a.id));
                        }
                      }}
                    >
                      {checkedAccountIds.length === accounts.length ? "取消全选" : "全选"}
                    </button>
                  </div>
                  <div className="max-h-56 overflow-y-auto rounded-md border border-border-subtle p-2">
                    <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                    {accounts.map((account) => {
                      const checked = checkedAccountIds.includes(account.id);
                      const accShopsRaw = shopsByAccount[account.id];
                      // null = 加载中，undefined = 还没触发，[] = 已加载无店铺
                      const accShopsLoading = accShopsRaw === null || (checked && accShopsRaw === undefined);
                      const accShops = accShopsRaw ?? [];
                      return (
                        <div key={account.id} className="rounded-md border border-border-subtle bg-canvas-inset/40 p-2">
                          <label className="flex cursor-pointer items-center gap-2 text-sm">
                            <input
                              type="checkbox"
                              className="h-4 w-4 cursor-pointer rounded border-border accent-brand-500"
                              checked={checked}
                              onChange={(e) => toggleAccount(account.id, e.target.checked)}
                            />
                            <span className="truncate font-medium text-fg">{account.name}</span>
                          </label>
                          {checked && accShopsLoading && (
                            <p className="mt-0.5 text-xs text-fg-muted">加载店铺中…</p>
                          )}
                          {checked && !accShopsLoading && accShops.length > 1 && (
                            <div className="mt-2 space-y-1">
                              {accShops.map((shop) => (
                                <label key={shop.shopId} className="flex cursor-pointer items-center gap-2 text-xs">
                                  <input
                                    type="checkbox"
                                    className="h-3 w-3 cursor-pointer rounded border-border accent-brand-500"
                                    checked={(selectedShopIdsByAccount[account.id] ?? []).includes(shop.shopId)}
                                    onChange={(e) => {
                                      setSelectedShopIdsByAccount((s) => {
                                        const prev = s[account.id] ?? [];
                                        if (e.target.checked) {
                                          return {
                                            ...s,
                                            [account.id]: [...prev, shop.shopId],
                                          };
                                        } else {
                                          return {
                                            ...s,
                                            [account.id]: prev.filter((id) => id !== shop.shopId),
                                          };
                                        }
                                      });
                                    }}
                                  />
                                  {/* 必须带站点：同一账号下常有同名的不同国家店铺
                                      （如两个都叫「LJ&shop.」的马来西亚/菲律宾店），
                                      只显示店名根本无法分辨该勾哪个。0.1.23 的下拉
                                      里是「店名 · 站点名」,改复选框时漏掉了这个。 */}
                                  <span className="truncate text-fg-secondary">
                                    {shop.name}
                                    <span className="ml-1 text-fg-muted">
                                      · {shop.siteName || shop.site}
                                    </span>
                                  </span>
                                </label>
                              ))}
                            </div>
                          )}
                          {checked && !accShopsLoading && accShops.length === 1 && (
                            <p className="mt-0.5 truncate text-xs text-fg-secondary">
                              → {accShops[0].name}
                              <span className="ml-1 text-fg-muted">
                                · {accShops[0].siteName || accShops[0].site}
                              </span>
                            </p>
                          )}
                          {checked && !accShopsLoading && accShops.length === 0 && (
                            <p className="mt-0.5 text-xs text-danger-600">暂无店铺（未绑定）</p>
                          )}
                        </div>
                      );
                    })}
                    </div>
                  </div>
                </div>
              ) : (
                <Field label="目标店铺" required hint={shopsQuery.data?.degraded ? "当前展示的是上次缓存的店铺列表" : undefined}>
                  <Select
                    value={shopId ?? ""}
                    onChange={(value) => setShopId(value)}
                    options={shops.map((shop) => ({
                      value: shop.shopId,
                      label: `${shop.name} · ${shop.siteName || shop.site}`,
                    }))}
                    placeholder={shops.length === 0 ? "当前账号下没有店铺" : "选择目标店铺"}
                    disabled={shops.length === 0}
                  />
                </Field>
              )}
            </>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="定价模板"
              required={requiresTemplate}
              hint={
                templatesQuery.isLoading
                  ? "正在读取定价模板…"
                  : templatesQuery.isError
                    ? undefined
                    : usableTemplates.length === 0
                      ? "暂无可用模板，请先在仓库的定价模板中创建并保存模板。"
                      : undefined
              }
            >
              {/*
                一行显示五国模板复选框（负责人 2026-11-18：不要下拉）。
                上方店铺选了哪个国家，这里对应国家的模板才能勾 ——
                没选的国家灰掉，避免「勾了菲律宾却没有菲律宾店铺」这种无效配置。
              */}
              {/* 一行五个，不换行：负责人 2026-11-18 明确「一行显示」 */}
              <div className="flex flex-nowrap items-center justify-between gap-x-2 py-1">
                {templateSites.map((site) => {
                  const enabled = selectedShopSites.includes(site);
                  const checked = enabled && useTemplateSites.includes(site);
                  return (
                    <label
                      key={site}
                      title={enabled
                        ? `按「${templateNameBySite[site]}」模板重算 ${SITE_LABELS[site] || site} 店铺售价`
                        : `上方没有选择${SITE_LABELS[site] || site}的店铺`}
                      className={cn(
                        "flex shrink-0 items-center gap-1 whitespace-nowrap text-xs",
                        enabled
                          ? "cursor-pointer text-fg"
                          : "cursor-not-allowed text-fg-muted opacity-50"
                      )}
                    >
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 shrink-0 cursor-pointer rounded border-border accent-brand-500 disabled:cursor-not-allowed"
                        checked={checked}
                        disabled={!enabled || templatesQuery.isLoading}
                        onChange={(e) => {
                          const on = e.target.checked;
                          setUseTemplateSites((prev) =>
                            on ? [...prev.filter((s) => s !== site), site]
                               : prev.filter((s) => s !== site)
                          );
                          // 记住用户的手动决定，别让默认勾选把它又勾回来
                          if (on) dismissedSitesRef.current.delete(site);
                          else dismissedSitesRef.current.add(site);
                          const name = templateNameBySite[site];
                          if (on && name && auth?.accountId) {
                            selectTemplate.mutate(
                              { name, account: String(auth.accountId) },
                              {
                                onError: (error) => {
                                  toast.warning("模板选择没保存住", {
                                    description: `${apiErrorMessage(error)}。本次发布会用「${name}」，但下次打开可能还是旧的。`,
                                  });
                                },
                              }
                            );
                          }
                        }}
                      />
                      <span>{SITE_LABELS[site] || site}</span>
                    </label>
                  );
                })}
                {templateSites.length === 0 && (
                  <span className="text-xs text-fg-muted">
                    暂无可用模板，请先在仓库的定价模板中创建
                  </span>
                )}
              </div>
              {templatesQuery.isError && (
                <InlineError onRetry={() => void templatesQuery.refetch()}>
                  定价模板读取失败：{apiErrorMessage(templatesQuery.error)}
                </InlineError>
              )}
            </Field>
          </div>

          <Separator />

          {error && <InlineError>{error}</InlineError>}
        </div>
      )}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ *
 * 结果区：repriced 明细表 + warning 告警块（常驻，直到用户关闭弹窗）
 * ------------------------------------------------------------------ */



function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return value;
}

function PublishResultPanel({ result }: { result: PublishResponse }) {
  const repriced: RepricedEntry[] = result.repriced ?? [];
  const failures = result.failures ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* warning：不再只弹 8 秒 toast，这里常驻可回看 */}
      {result.warning && (
        <div className="flex items-start gap-2 rounded-md border border-warning-600/25 bg-warning-50 px-3 py-2.5 text-[13px] leading-5 text-warning-700">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" strokeWidth={2} />
          <div className="min-w-0 flex-1">
            <p className="font-medium">发布提示</p>
            <p className="mt-0.5 break-words">{result.warning}</p>
          </div>
        </div>
      )}

      <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <PackageCheck className="size-4 text-success-600" strokeWidth={2} />
          <p className="text-[13px] font-medium text-fg-secondary">
            {result.publishRequested ? "已提交到平台" : "已处理"}
          </p>
          <Badge variant="neutral">
            批次 <span className="font-mono">{result.batchId || "—"}</span>
          </Badge>
          <Badge variant="info">模式 {result.mode}</Badge>
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <div>
            <p className="text-2xs text-fg-muted">已发布</p>
            <p className="num text-sm font-semibold text-fg">{result.published.length}</p>
          </div>
          <div>
            <p className="text-2xs text-fg-muted">已认领</p>
            <p className="num text-sm font-semibold text-fg">{result.claimed.length}</p>
          </div>
          <div>
            <p className="text-2xs text-fg-muted">失败</p>
            <p
              className={cn(
                "num text-sm font-semibold",
                failures.length > 0 ? "text-danger-600" : "text-fg"
              )}
            >
              {failures.length}
            </p>
          </div>
        </div>
      </div>

      {repriced.length > 0 && (
        <div>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <p className="text-[13px] font-semibold text-fg-secondary">模板改价明细</p>
            <Badge variant="warning">
              <span className="num">{repriced.length}</span> 件被重新定价
            </Badge>
          </div>
          <div className="overflow-hidden rounded-md border border-border-subtle">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="h-8 bg-canvas-inset/70 text-fg-muted">
                  <th className="px-2 text-left font-medium">商品</th>
                  <th className="w-[84px] px-2 text-right font-medium">改前价</th>
                  <th className="w-[84px] px-2 text-right font-medium">改后价</th>
                  <th className="w-[56px] px-2 text-left font-medium">币种</th>
                  <th className="w-[76px] px-2 text-right font-medium">重量</th>
                  <th className="w-[76px] px-2 text-right font-medium">改价 SKU</th>
                </tr>
              </thead>
              <tbody>
                {repriced.map((entry, index) => (
                  <tr
                    key={`${entry.detailId ?? entry.warehouseId ?? "row"}-${index}`}
                    className="border-t border-border-subtle"
                  >
                    <td className="max-w-[180px] truncate px-2 py-1.5 font-mono text-2xs text-fg-secondary">
                      {entry.detailId ?? entry.warehouseId ?? "—"}
                    </td>
                    <td className="num px-2 py-1.5 text-right text-fg-muted line-through">
                      {formatMoney(entry.priceBefore)}
                    </td>
                    <td className="num px-2 py-1.5 text-right font-medium text-fg">
                      {formatMoney(entry.priceAfter)}
                    </td>
                    <td className="px-2 py-1.5 text-fg-secondary">{entry.currency || "—"}</td>
                    <td className="num px-2 py-1.5 text-right text-fg-secondary">
                      {entry.weightKg ? `${entry.weightKg} kg` : "—"}
                    </td>
                    <td className="num px-2 py-1.5 text-right text-fg-secondary">
                      {entry.changed ?? "—"}
                      {entry.skuCount !== null && entry.skuCount !== undefined
                        ? `/${entry.skuCount}`
                        : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-2xs text-fg-muted">
            改前价为模板里取到的样本价；重量来自入库 payload，可能为空。
          </p>
        </div>
      )}

      {failures.length > 0 && (
        <div className="rounded-md border border-danger-600/20 bg-danger-50/60 px-3 py-2.5 text-[13px] leading-5 text-danger-700">
          <p className="font-medium">失败明细（{failures.length} 件）</p>
          <ul className="mt-1 flex flex-col gap-1">
            {failures.map((failure, index) => (
              <li key={`${failure.detailId ?? "fail"}-${index}`} className="break-words">
                <span className="font-mono text-2xs">{failure.detailId ?? "—"}</span>
                {failure.reason ? `：${failure.reason}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {repriced.length === 0 && failures.length === 0 && !result.warning && (
        <p className="text-[13px] leading-5 text-fg-muted">
          没有改价明细，也没有告警。平台回执稍后更新，可点「刷新回执」或到发布记录里查看。
        </p>
      )}
    </div>
  );
}

export default PublishDialog;
