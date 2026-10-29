// features/words —— 违规词库（api.md §10）
// 搜索 + 单条添加 + 批量粘贴 + 批量删除 + 清空。不再分「删除/复核」类：命中的词一律删除。

import { useMemo, useRef, useState } from "react";
import {
  Plus,
  SearchX,
  Trash2,
  Upload,
} from "lucide-react";
import { cn, formatCount } from "@/lib/utils";
import { api, ApiError, apiErrorMessage } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LIMITS } from "@/lib/constants";
import { useMutateWords, useViolationWords } from "@/lib/queries";
import { useWordsStore } from "@/features/stores";
import { Pagination, TextAction, useDebouncedValue } from "@/features/shared";
import { PageContent, PageHeader } from "@/components/layout";
import {
  Badge,
  BulkActionBar,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  InlineError,
  SearchInput,
  Select,
  SkeletonRows,
  Switch,
  Textarea,
  toast,
} from "@/components/ui";

const PAGE_SIZE_OPTIONS = [100, 200, 500];

export function WordsPage() {
  const store = useWordsStore();
  const [searchDraft, setSearchDraft] = useState(store.searchDraft);
  const debounced = useDebouncedValue(searchDraft, 300);
  const q = debounced.trim();

  // 词库不再分类：界面不暴露「删除/复核」选择，新增与导入一律按「删除」处理
  const listQuery = useViolationWords({
    page: store.page,
    pageSize: store.pageSize,
    action: null,
    q,
  });
  const mutate = useMutateWords();
  const queryClient = useQueryClient();
  /** 真启停：只翻 enabled 位，不删词（与后端 PATCH /violation-words/enabled 对应）。 */
  const toggleEnabled = useMutation({
    mutationFn: (body: { enabled: boolean; words: string[] }) =>
      api.patch<{ updated: number; enabled: boolean; missing: string[] }>(
        "/violation-words/enabled",
        body
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.violationWords.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.auth.all });
    },
  });

  const [addOpen, setAddOpen] = useState(false);
  const [singleWord, setSingleWord] = useState("");
  const [addError, setAddError] = useState<string | null>(null);

  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [importReplace, setImportReplace] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);

  const data = listQuery.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  const selected = store.selectedWords;

  const pageWords = items.map((item) => item.word);
  const allSelected = pageWords.length > 0 && pageWords.every((word) => selected.includes(word));

  const parsedImportWords = useMemo(
    () =>
      Array.from(
        new Set(
          importText
            .split(/[\n,，;；\t]+/)
            .map((word) => word.trim())
            .filter((word) => word.length > 0 && word.length <= 64)
        )
      ),
    [importText]
  );

  const submitSingle = (): void => {
    const word = singleWord.trim();
    if (!word) {
      setAddError("请填写要屏蔽的词");
      return;
    }
    if (Array.from(word).length > 64) {
      setAddError("词不能超过 64 个字符");
      return;
    }
    setAddError(null);
    mutate.mutate(
      { action: "add", word, category: "remove" },
      {
        onSuccess: () => {
          toast.success(`已添加「${word}」`);
          setSingleWord("");
          setAddOpen(false);
        },
        onError: (error) => {
          const code = error instanceof ApiError ? error.code : null;
          setAddError(
            code === "DUPLICATE_WORD"
              ? "这个词已经在词库里了"
              : code === "WORD_CONFLICT"
                ? "同一个词不能同时出现在「删除」与「复核」分类"
                : apiErrorMessage(error)
          );
        },
      }
    );
  };

  const submitImport = (): void => {
    if (parsedImportWords.length === 0) {
      setImportError("没有解析到有效词（每行一个，或用逗号/分号分隔）");
      return;
    }
    if (parsedImportWords.length > LIMITS.wordsPageSize.max) {
      setImportError(`一次最多导入 ${LIMITS.wordsPageSize.max} 个词`);
      return;
    }
    setImportError(null);
    mutate.mutate(
      {
        action: "import",
        words: parsedImportWords,
        category: "remove",
        mode: importReplace ? "replace" : "append",
      },
      {
        onSuccess: (result) => {
          // 被跳过的词要列出来：运营需要知道哪些没进去（重复/超长/放不下），
          // 只说「跳过 N 个」他们没法核对，只能整批重来。
          const skipped = result.skipped ?? [];
          const notes: string[] = [];
          if (skipped.length > 0) {
            const head = skipped.slice(0, 5).join("、");
            notes.push(
              skipped.length > 5
                ? `跳过 ${skipped.length} 个：${head} 等`
                : `跳过 ${skipped.length} 个：${head}`
            );
          }
          if (result.truncated) {
            notes.push(`单次最多处理 ${result.limit ?? 500} 个，剩余请再导入一次`);
          }
          toast.success(`已导入 ${result.added} 个词`, {
            description: notes.length > 0 ? notes.join("；") : undefined,
          });
          setImportText("");
          setImportOpen(false);
        },
        onError: (error) => {
          const code = error instanceof ApiError ? error.code : null;
          setImportError(
            code === "WORD_LIMIT_EXCEEDED"
              ? `删除类词条已达上限 ${LIMITS.wordsTotal} 条`
              : apiErrorMessage(error)
          );
        },
      }
    );
  };

  const submitDelete = (): void => {
    mutate.mutate(
      { action: "remove-many", words: selected },
      {
        onSuccess: (result) => {
          const missing = result.skipped ?? [];
          toast.success(`已删除 ${result.removed} 个词`, {
            description:
              missing.length > 0
                ? `${missing.length} 个词不存在：${missing.slice(0, 5).join("、")}`
                : undefined,
          });
          store.clearWords();
          setDeleteOpen(false);
        },
        onError: (error) => toast.error("删除失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const submitClear = (): void => {
    // 词库不再分「删除/复核」类（界面上命中的词一律删除），清空按删除类执行——
    // 历史复核类词条若有残留不在本页清空范围内。
    mutate.mutate(
      { action: "clear", category: "remove", confirm: true },
      {
        onSuccess: (result) => {
          toast.success(`已清空 ${result.removed} 个词`);
          store.clearWords();
          setClearOpen(false);
        },
        onError: (error) => toast.error("清空失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  const headerActions = (
    <>
      <Button variant="secondary" icon={<Upload />} onClick={() => setImportOpen(true)}>
        批量导入
      </Button>
      <Button variant="primary" icon={<Plus />} onClick={() => setAddOpen(true)}>
        添加违规词
      </Button>
    </>
  );

  return (
    <>
      <PageHeader
        title="违规词库"
        actions={headerActions}
      />

      <PageContent width="table">
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-[220px] flex-1">
            <SearchInput
              value={searchDraft}
              onChange={(value) => {
                setSearchDraft(value);
                store.setSearchDraft(value);
                store.setQ(value.trim());
              }}
              debounceMs={300}
              placeholder="搜索词面"
              width="md"
              shortcut={null}
            />
          </div>

          <Select
            size="md"
            aria-label="每页条数"
            className="w-[132px]"
            value={String(store.pageSize)}
            onChange={(value) => store.setPageSize(Number(value))}
            options={PAGE_SIZE_OPTIONS.map((size) => ({
              value: String(size),
              label: `每页 ${size}`,
            }))}
          />

          <IconButton
            label="删除已选"
            size="md"
            variant="danger"
            icon={<Trash2 />}
            disabled={selected.length === 0}
            onClick={() => setDeleteOpen(true)}
          />
        </div>

        {listQuery.isLoading && (
          <SkeletonRows rows={6} />
        )}

        {listQuery.isError && (
          <Card>
            <ErrorState
              title="词库加载失败"
              description={apiErrorMessage(listQuery.error)}
              errorCode={listQuery.error instanceof ApiError ? listQuery.error.code : null}
              onRetry={() => void listQuery.refetch()}
              retrying={listQuery.isFetching}
            />
          </Card>
        )}

        {!listQuery.isLoading && !listQuery.isError && items.length === 0 && (
          <Card>
            <EmptyState
              icon={<SearchX strokeWidth={1.5} />}
              title={q ? "没有匹配的词" : "词库是空的"}
              description={
                q
                  ? "换一个关键词，或切换到「全部」分类再找。"
                  : "添加违规词后，处理商品时标题里的这些词会被自动清理。"
              }
              action={
                q
                  ? {
                      label: "清除搜索",
                      onClick: () => {
                        setSearchDraft("");
                        store.setQ("");
                      },
                    }
                  : { label: "添加违规词", onClick: () => setAddOpen(true) }
              }
            />
          </Card>
        )}

        {!listQuery.isLoading && !listQuery.isError && items.length > 0 && (
          <>
            {/* 全选/清空操作条 */}
            <div className="flex items-center gap-2">
              <Checkbox
                checked={allSelected}
                indeterminate={!allSelected && selected.length > 0}
                onChange={() => {
                  if (allSelected) store.clearWords();
                  else store.setWords(pageWords);
                }}
                aria-label="全选本页"
                label="全选"
              />
              {selected.length > 0 && (
                <span className="text-xs text-fg-muted">已选 {selected.length} 个</span>
              )}
            </div>

            {/* 标签流式布局：一词一个胶囊，多个一行排满。
                一词一行的表格在几十上百个词时全是空白，翻页翻到手酸。 */}
            <div className="flex flex-wrap gap-2">
              {items.map((item) => {
                const checked = selected.includes(item.word);
                return (
                  <span
                    key={item.id}
                    className={cn(
                      "group inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[13px] transition-colors duration-fast",
                      checked
                        ? "border-brand-600/60 bg-brand-50"
                        : item.enabled
                          ? "border-border bg-surface hover:border-border-strong"
                          : "border-border-subtle bg-canvas-inset/50 text-fg-muted line-through"
                    )}
                  >
                    <Checkbox
                      checked={checked}
                      onChange={() => store.toggleWord(item.word)}
                      aria-label={`选择 ${item.word}`}
                      size="sm"
                    />
                    <span className="font-medium">{item.word}</span>
                    <span className="text-2xs text-fg-muted">
                      {item.source === "seed" ? "内置" : item.source === "import" ? "导入" : "手动"}
                    </span>
                    <Switch
                      checked={item.enabled}
                      aria-label={`${item.enabled ? "停用" : "启用"} ${item.word}`}
                      disabled={toggleEnabled.isPending}
                      onChange={(checked) => {
                        toggleEnabled.mutate(
                          { enabled: checked, words: [item.word] },
                          {
                            onSuccess: () =>
                              toast.success(
                                checked ? `已启用「${item.word}」` : `已停用「${item.word}」`
                              ),
                            onError: (error) =>
                              toast.error(checked ? "启用失败" : "停用失败", {
                                description: apiErrorMessage(error),
                              }),
                          }
                        );
                      }}
                    />
                    <button
                      type="button"
                      title={`删除 ${item.word}`}
                      className="ml-0.5 rounded-full p-0.5 text-fg-muted opacity-0 transition-opacity hover:text-danger-600 group-hover:opacity-100"
                      onClick={() => {
                        store.setWords([item.word]);
                        setDeleteOpen(true);
                      }}
                    >
                      <Trash2 className="size-3" strokeWidth={1.75} />
                    </button>
                  </span>
                );
              })}
            </div>
          </>
        )}

        <Pagination
          page={store.page}
          pageSize={store.pageSize}
          total={total}
          hasMore={data?.hasMore}
          pageSizeOptions={PAGE_SIZE_OPTIONS.map((size) => ({ value: size, label: String(size) }))}
          onPageChange={store.setPage}
          onPageSizeChange={store.setPageSize}
          disabled={listQuery.isFetching}
          extra={
            <span className="flex items-center gap-2">
              {q && (
                <TextAction
                  onClick={() => {
                    setSearchDraft("");
                    store.setQ("");
                  }}
                >
                  清除搜索
                </TextAction>
              )}
              <TextAction onClick={() => setClearOpen(true)} className="text-danger-600">
                清空词库
              </TextAction>
            </span>
          }
        />
      </PageContent>

      <BulkActionBar
        count={selected.length}
        total={total}
        unit="个词"
        onClear={store.clearWords}
        actions={[
          {
            label: "删除已选",
            icon: <Trash2 className="size-3.5" />,
            danger: true,
            onClick: () => setDeleteOpen(true),
          },
        ]}
      />

      {/* 单条添加 */}
      <Dialog
        open={addOpen}
        onOpenChange={(open) => {
          setAddOpen(open);
          if (!open) {
            setSingleWord("");
            setAddError(null);
          }
        }}
        size="sm"
        title="添加违规词"
        description="处理商品标题时命中的词会按分类处理。"
        closeOnOverlay={false}
        footer={
          <>
            <Button variant="secondary" onClick={() => setAddOpen(false)}>
              取消
            </Button>
            <Button variant="primary" loading={mutate.isPending} onClick={submitSingle}>
              添加
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Field label="违规词" required error={addError} hint="1–64 个字符，不能含控制字符">
            <Textarea
              value={singleWord}
              rows={2}
              maxLength={64}
              showCount
              autoFocus
              placeholder="如：全网最低价"
              invalid={Boolean(addError)}
              onChange={(event) => {
                setSingleWord(event.currentTarget.value);
                setAddError(null);
              }}
            />
          </Field>
        </div>
      </Dialog>

      {/* 批量导入 */}
      <Dialog
        open={importOpen}
        onOpenChange={(open) => {
          setImportOpen(open);
          if (!open) {
            setImportText("");
            setImportError(null);
          }
        }}
        size="md"
        title="批量导入违规词"
        description="每行一个，或用逗号 / 分号 / 制表符分隔；重复与超长的项会自动跳过。"
        closeOnOverlay={false}
        footer={
          <>
            <Button variant="secondary" onClick={() => setImportOpen(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={mutate.isPending}
              disabled={parsedImportWords.length === 0}
              onClick={submitImport}
            >
              导入 {parsedImportWords.length > 0 ? parsedImportWords.length : ""} 个词
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Field
            label="词列表"
            required
            error={importError}
            hint={`最多 ${LIMITS.wordsPageSize.max} 个词；已解析 ${parsedImportWords.length} 个`}
          >
            {/* 老版能直接导入 .txt/.csv（title-words-file）。几百个词手敲不现实，
                这里保留同样的入口：读文件塞进 textarea，再走同一套解析。 */}
            <div className="mb-1.5 flex items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                icon={<Upload />}
                onClick={() => fileInputRef.current?.click()}
              >
                导入文件
              </Button>
              <span className="text-2xs text-fg-muted">支持 .txt / .csv，每行一个词或逗号分隔</span>
              <input
                ref={fileInputRef}
                type="file"
                accept=".txt,.csv,text/plain,text/csv"
                className="hidden"
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  // 选同一个文件两次也要能触发 change
                  event.currentTarget.value = "";
                  if (!file) return;
                  const reader = new FileReader();
                  reader.onload = () => {
                    const text = String(reader.result ?? "");
                    setImportText((prev) => (prev.trim() ? `${prev}
${text}` : text));
                    setImportError(null);
                  };
                  reader.onerror = () => setImportError("读取文件失败，请确认是文本文件");
                  reader.readAsText(file, "utf-8");
                }}
              />
            </div>
            <Textarea
              value={importText}
              rows={8}
              autoFocus
              placeholder={"秒杀\n限时特价\ndrop shipping"}
              invalid={Boolean(importError)}
              onChange={(event) => {
                setImportText(event.currentTarget.value);
                setImportError(null);
              }}
            />
          </Field>

          <div className="grid gap-3">
            <Field label="导入模式" hint="替换会先清空现有全部词条">
              <Select
                value={importReplace ? "replace" : "append"}
                onChange={(value) => setImportReplace(value === "replace")}
                options={[
                  { value: "append", label: "追加（保留现有词）" },
                  { value: "replace", label: "替换（先清空现有词）" },
                ]}
              />
            </Field>
          </div>

          {importReplace && (
            <InlineError className="border-warning-600/20 bg-warning-50 text-warning-700">
              替换模式会删除全部现有词条，导入前请确认已导出备份。
            </InlineError>
          )}

          {parsedImportWords.length > 0 && (
            <div className="rounded-md border border-border-subtle bg-canvas-inset/60 p-3">
              <p className="text-xs font-medium text-fg-secondary">预览（前 30 个）</p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {parsedImportWords.slice(0, 30).map((word) => (
                  <Badge key={word} variant="neutral">
                    {word}
                  </Badge>
                ))}
                {parsedImportWords.length > 30 && (
                  <Badge variant="info">…等 {parsedImportWords.length} 个</Badge>
                )}
              </div>
            </div>
          )}
        </div>
      </Dialog>

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`删除已选的 ${selected.length} 个词？`}
        description="删除后这些词不再参与标题清理，需重新添加才能恢复。"
        confirmLabel={`删除 ${selected.length} 个`}
        danger
        loading={mutate.isPending}
        onConfirm={submitDelete}
      >
        <p className="line-clamp-3 text-[13px] leading-5 text-fg-secondary">
          {selected.slice(0, 12).join("、")}
          {selected.length > 12 ? ` …等 ${selected.length} 个` : ""}
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={clearOpen}
        onOpenChange={setClearOpen}
        title="清空全部词条？"
        description={`会删除约 ${formatCount(total)} 个词，不可恢复。`}
        confirmLabel="确认清空"
        danger
        loading={mutate.isPending}
        onConfirm={submitClear}
      >
        <p className="text-[13px] leading-5 text-fg-secondary">
          建议先用「批量导入」的记录确认可恢复来源。内置词条删除后需调用 seed 接口才能恢复。
        </p>
      </ConfirmDialog>
    </>
  );
}

export default WordsPage;
