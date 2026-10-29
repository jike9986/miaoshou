// features/carrier —— 载体链接池（api.md §12）
// 跨店发布（mode=carrier）需要用 1688 货源链接重建载体；池子空的时候跨店发布直接失败，
// 所以这里提供手工补链接 / 删链接 / 从已有成品回收。
import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Link2,
  PackageSearch,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { cn, formatCount, formatDate } from "@/lib/utils";
import { apiErrorMessage } from "@/lib/api";
import {
  useAddCarrierLinks,
  useCarrierLinks,
  useRemoveCarrierLinks,
  useSyncSourceLinks,
} from "@/lib/queries";
import { PageContent, PageHeader } from "@/components/layout";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  KpiCard,
  SkeletonTable,
  Textarea,
  toast,
} from "@/components/ui";

/** 后端判定「可用」的口径：连续失败次数 < 3（api.md §12）。 */
const FAILED_LIMIT = 3;

function usableOf(failed: number | undefined): boolean {
  return Number(failed ?? 0) < FAILED_LIMIT;
}

export function CarrierPage({ embedded = false }: { embedded?: boolean } = {}) {
  const listQuery = useCarrierLinks();
  const add = useAddCarrierLinks();
  const remove = useRemoveCarrierLinks();
  const harvest = useSyncSourceLinks();

  const [draft, setDraft] = useState("");
  const [removeTarget, setRemoveTarget] = useState<string | null>(null);

  const data = listQuery.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const usable = data?.usable ?? 0;

  /** 按行/换行切分，顺手去重并只留 http(s)。 */
  const parsedUrls = useMemo(() => {
    const raw = draft
      .split(/[\s,;]+/)
      .map((value) => value.trim())
      .filter((value) => value.startsWith("http"));
    return Array.from(new Set(raw));
  }, [draft]);

  const submit = (): void => {
    if (parsedUrls.length === 0) {
      toast.warning("请先粘贴 1688 商品链接");
      return;
    }
    add.mutate(
      { urls: parsedUrls },
      {
        onSuccess: (result) => {
          setDraft("");
          const parts = [`新增 ${result.added} 条`];
          if (result.skipped > 0) parts.push(`跳过 ${result.skipped} 条重复`);
          if (result.invalid.length > 0) parts.push(`${result.invalid.length} 条不是 1688 链接`);
          toast.success(parts.join("，"), {
            description: result.invalid.length > 0
              ? `被拒的链接：${result.invalid.slice(0, 3).join("、")}`
              : "链接池已更新",
          });
        },
        onError: (error) =>
          toast.error("添加失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const harvestFromWarehouse = (): void => {
    harvest.mutate(
      { all: true, limit: 500 },
      {
        onSuccess: (result) => {
          toast.success(
            result.added > 0
              ? `扫了 ${formatCount(result.scanned)} 件，新增 ${result.added} 条货源`
              : `扫了 ${formatCount(result.scanned)} 件，没有找到新的 1688 货源`,
            { description: "货源来自成品库里已有的 sourceList / oriItemId 字段" }
          );
        },
        onError: (error) =>
          toast.error("回收失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const headerActions = (
    <>
      <IconButton
        label="刷新"
        size="md"
        variant="secondary"
        icon={<RefreshCw className={cn(listQuery.isFetching && "animate-spin")} />}
        onClick={() => void listQuery.refetch()}
      />
      <Button
        variant="secondary"
        icon={<PackageSearch />}
        loading={harvest.isPending}
        onClick={harvestFromWarehouse}
      >
        从仓库回收
      </Button>
    </>
  );

  return (
    <>
      {!embedded && (
      <PageHeader
        title="载体链接池"
        description="跨店发布用它重建载体；池子空了跨店发布会失败"
        actions={headerActions}
      />
      )}

      <PageContent width="table" padding={embedded ? "" : "p-6"} scroll={!embedded}>
        <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
          <KpiCard label="链接总数" value={formatCount(total)} hint="池子里全部链接" />
          <KpiCard
            label="可用"
            value={formatCount(usable)}
            hint={`连续失败少于 ${FAILED_LIMIT} 次`}
          />
          <KpiCard
            label="已退役"
            value={formatCount(Math.max(0, total - usable))}
            hint={`连续失败 ≥ ${FAILED_LIMIT} 次，不再用于重建`}
          />
          <KpiCard
            label="回收上限"
            value={formatCount(500)}
            hint="「从仓库回收」一次扫描的商品数"
          />
        </div>

        {usable === 0 && total > 0 && (
          <div className="flex items-start gap-2 rounded-md border border-warning-600/20 bg-warning-50 px-3 py-2.5 text-sm text-warning-700">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>
              池子里的链接都已被标记退役。跨店发布会报「载体不足」，
              请补新链接或先排查原链接为什么加载失败。
            </span>
          </div>
        )}

        <Card>
          <h3 className="text-sm font-semibold text-fg">添加链接</h3>
          <Field
            hint="只接受 1688 商品链接（列表页 /offer/<id>.html 或 detail.1688.com）；可一次粘贴多条，用换行或空格分隔"
          >
            <Textarea
              value={draft}
              rows={4}
              placeholder={
                "https://detail.1688.com/offer/123456789.html\nhttps://detail.1688.com/offer/987654321.html"
              }
              onChange={(event) => setDraft(event.currentTarget.value)}
            />
          </Field>
          <div className="mt-3 flex items-center gap-2">
            <Button
              icon={<Plus />}
              loading={add.isPending}
              disabled={parsedUrls.length === 0}
              onClick={submit}
            >
              添加 {parsedUrls.length > 0 ? `${parsedUrls.length} 条` : ""}
            </Button>
            {draft.trim() !== "" && parsedUrls.length === 0 && (
              <span className="text-xs text-warning-700">
                没识别出链接，请确认以 http:// 或 https:// 开头
              </span>
            )}
          </div>
        </Card>

        <Card padding="none">
          <div className="flex items-center justify-between px-4 py-3">
            <h3 className="text-sm font-semibold text-fg">池内链接</h3>
            <span className="text-xs text-fg-muted">
              共 {formatCount(total)} 条，可用 {formatCount(usable)} 条
            </span>
          </div>

          {listQuery.isPending ? (
            <SkeletonTable rows={6} columns={["w-[72px]", "flex-1", "w-[160px]", "w-[88px]", "w-8"]} />
          ) : listQuery.isError ? (
            <ErrorState
              title="链接池加载失败"
              description={apiErrorMessage(listQuery.error)}
              onRetry={() => void listQuery.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              icon={<Link2 />}
              title="链接池是空的"
              description="跨店发布需要至少一条可用链接。可以手工粘贴，或点右上角「从仓库回收」。"
            />
          ) : (
            <ul className="divide-y divide-border-subtle">
              {items.map((item) => {
                const ok = usableOf(item.failed);
                return (
                  <li
                    key={item.url}
                    className="flex items-center gap-3 px-4 py-2.5 text-sm"
                  >
                    <Badge variant={ok ? "success" : "danger"}>
                      {ok ? "可用" : "已退役"}
                    </Badge>
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="min-w-0 flex-1 truncate font-mono text-xs text-fg-muted hover:text-fg hover:underline"
                      title={item.url}
                    >
                      {item.url}
                    </a>
                    <span className="shrink-0 text-2xs text-fg-muted">
                      成功 {formatCount(item.ok ?? 0)} · 失败 {formatCount(item.failed ?? 0)}
                    </span>
                    {item.addedAt ? (
                      <span className="shrink-0 text-2xs text-fg-muted">
                        {formatDate(item.addedAt)}
                      </span>
                    ) : null}
                    <IconButton
                      label="删除这条链接"
                      size="sm"
                      variant="ghost"
                      icon={<Trash2 />}
                      onClick={() => setRemoveTarget(item.url)}
                    />
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </PageContent>

      <ConfirmDialog
        open={Boolean(removeTarget)}
        onOpenChange={(open) => !open && setRemoveTarget(null)}
        title="从链接池删除这条链接？"
        description="删除后不会再用于载体重建；已发布出去的草稿不受影响。"
        confirmLabel="删除"
        danger
        loading={remove.isPending}
        onConfirm={() => {
          if (!removeTarget) return;
          remove.mutate(
            { urls: [removeTarget] },
            {
              onSuccess: (result) => {
                setRemoveTarget(null);
                toast.success(`已删除，池子还剩 ${result.total} 条`);
              },
              onError: (error) =>
                toast.error("删除失败", { description: apiErrorMessage(error) }),
            }
          );
        }}
      />
    </>
  );
}
