// features/settings —— 操作员管理（多用户协作的身份与 token）
//
// 这个面板回答三个问题：
//   1. 我现在是谁？（顶部大字，换人要点得着）
//   2. 这台机器上还有谁？（表格：名字 / 是否管理员 / 最后活跃 / 是否启用）
//   3. 怎么把人加进来？（新建 → 一次性 token → 复制给对方）
//
// 两条硬规则：
//   · token 明文只在「新建」和「重置」的响应里出现一次，必须当场用一次性框摊开；
//     重置前要确认「旧 token 会立刻失效」，否则对方会莫名其妙被踢下线。
//   · 非管理员看不到停用 / 重置按钮（不是禁用，是不渲染）——
//     权限不够的按钮摆在那里只会招来一串 403。

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  CheckCircle2,
  KeyRound,
  Pencil,
  ShieldCheck,
  UserPlus,
  Users,
} from "lucide-react";
import { cn, formatRelative } from "@/lib/utils";
import { api, apiErrorMessage } from "@/lib/api";
import type { OperatorsResponse } from "@/lib/types";
import { queryKeys } from "@/lib/queryKeys";
import {
  useCreateOperator,
  useOperators,
  useRenameOperator,
  useRotateOperator,
  useSetRole,
  useToggleOperator,
} from "@/lib/queries";
import {
  clearOperator,
  currentOperator,
  LOCAL_OPERATOR_NAME,
  setOperator,
  useOperatorIdentity,
} from "@/lib/operator";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  EmptyState,
  Field,
  IconButton,
  InlineError,
  Input,
  Select,
  SkeletonRows,
  Separator,
  toast,
  Tooltip,
} from "@/components/ui";
import { OneTimeTokenDialog, ONE_TIME_TOKEN_NOTICE } from "@/features/settings/OneTimeTokenDialog";

/* ------------------------------------------------------------------ *
 * 当前身份卡
 * ------------------------------------------------------------------ */

function IdentityCard({
  onSwitch,
  onCreated,
}: {
  onSwitch: () => void;
  onCreated: () => void;
}) {
  const identity = useOperatorIdentity();
  const operatorsQuery = useOperators();
  const me = useMemo(() => {
    const list = operatorsQuery.data?.operators ?? [];
    if (!identity) return operatorsQuery.data?.current ?? null;
    return list.find((item) => item.id === identity.id) ?? operatorsQuery.data?.current ?? null;
  }, [identity, operatorsQuery.data]);

  const name = identity?.name ?? LOCAL_OPERATOR_NAME;

  return (
    <Card padding="normal">
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-full bg-brand-100 text-[15px] font-semibold text-brand-800">
          {Array.from(name)[0] ?? "本"}
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2">
            <span className="text-[15px] font-semibold leading-[22px] text-fg">
              当前身份：{name}
            </span>
            {me?.admin && (
              <Badge variant="brand" size="sm">
                管理员
              </Badge>
            )}
            {!identity && (
              <Badge variant="neutral" size="sm">
                未设置
              </Badge>
            )}
            {identity && me && !me.enabled && (
              <Badge variant="danger" size="sm">
                已被停用
              </Badge>
            )}
          </p>
          <p className="mt-0.5 text-xs leading-[18px] text-fg-muted">
            {identity
              ? "你的改动会记在这个名字下，冲突时后端按版本号拦住覆盖。"
              : `还没设置操作员：所有改动会记在内置的「${LOCAL_OPERATOR_NAME}」名下，多人共用时谁也分不清是谁改的。`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {identity && (
            <Button
              variant="ghost"
              size="md"
              onClick={() => {
                clearOperator();
                toast.info(`已切换为「${LOCAL_OPERATOR_NAME}」`, {
                  description: "之后的改动会记在本机名下",
                });
              }}
            >
              以本机身份继续
            </Button>
          )}
          <Button variant="secondary" size="md" icon={<Users />} onClick={onSwitch}>
            切换
          </Button>
          <Button variant="primary" size="md" icon={<UserPlus />} onClick={onCreated}>
            新建操作员
          </Button>
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ *
 * 切换身份：粘贴对方给的 token
 *
 * 为什么是粘贴而不是「从列表里选」：
 *   GET /operators 不返回任何 token，所以「切到张三」这件事
 *   只能靠张三把自己的 token 给你。校验方式是拿这串 token 直接打一次
 *   GET /operators —— 响应里的 current 就是它对应的操作员，401 就是无效。
 * ------------------------------------------------------------------ */

function SwitchIdentityDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const client = useQueryClient();

  const submit = (): void => {
    const value = token.trim();
    if (!value) {
      setError("请先粘贴对方给你的 token");
      return;
    }
    setError(null);
    setBusy(true);
    // 显式带上这串 token 探一次：能拿到 current 就说明它有效
    api
      .get<OperatorsResponse>("/operators", { headers: { "X-Operator": value } })
      .then((data) => {
        const who = data.current ?? null;
        setOperator({ token: value, id: who?.id ?? null, name: who?.name ?? "操作员" });
        // 身份一变，后端返回的「我」就不同了：名单与审计都要重取，
        // 否则界面上还挂着上一个人的管理员标记
        void client.invalidateQueries({ queryKey: queryKeys.operators.all });
        void client.invalidateQueries({ queryKey: queryKeys.auditLogs.all });
        if (who && who.enabled === false) {
          toast.warning(`「${who.name}」已被停用`, {
            description: "token 能读到身份，但写操作会被后端拒绝，请联系管理员重新启用",
          });
        } else {
          toast.success(`已切换为「${who?.name ?? "操作员"}」`);
        }
        setToken("");
        onOpenChange(false);
      })
      .catch((err) => {
        setError(
          apiErrorMessage(err) || "这串 token 无效或已被停用，请让对方在「操作员」里重新发一次"
        );
      })
      .finally(() => setBusy(false));
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setToken("");
          setError(null);
        }
        onOpenChange(next);
      }}
      size="md"
      title="切换操作员身份"
      description="把对方给你的 token 粘贴进来，之后的改动就记在他名下。"
      closeOnOverlay={false}
      footer={
        <>
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => {
              setToken("");
              setError(null);
              onOpenChange(false);
            }}
          >
            取消
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            切换
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field
          label="token"
          required
          error={error}
          hint="这串字符由管理员或对方本人一次性发放，服务端只存哈希，丢了只能重置。"
        >
          <Input
            value={token}
            autoFocus
            placeholder="粘贴 token"
            className="font-mono text-xs"
            invalid={Boolean(error)}
            onChange={(event) => {
              setToken(event.currentTarget.value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") submit();
            }}
          />
        </Field>
        <p className="text-2xs leading-4 text-fg-muted">
          不想设置身份也可以直接关掉：后端会把你记到内置的「{LOCAL_OPERATOR_NAME}」名下，
          功能一样能用，只是审计日志里分不清人。
        </p>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ *
 * 新建操作员
 * ------------------------------------------------------------------ */

function CreateOperatorDialog({
  open,
  onOpenChange,
  onToken,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onToken: (token: string, name: string) => void;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<"admin" | "teacher" | "member">("member");
  const [error, setError] = useState<string | null>(null);
  const create = useCreateOperator();

  const submit = (): void => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("请填写操作员名字");
      return;
    }
    if (Array.from(trimmed).length > 32) {
      setError("名字最多 32 个字符");
      return;
    }
    setError(null);
    create.mutate(
      { name: trimmed, role },
      {
        onSuccess: (result) => {
          setName("");
          setRole("member");
          onOpenChange(false);
          // token 只在这里出现一次，立刻交给一次性展示框
          onToken(result.token, result.operator.name);
        },
        onError: (mutationError) => {
          const message = apiErrorMessage(mutationError);
          setError(message);
          toast.error("新建操作员失败", { description: message });
        },
      }
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setName("");
          setRole("member");
          setError(null);
        }
        onOpenChange(next);
      }}
      size="sm"
      title="新建操作员"
      description="创建后会立刻发一串 token，只能看一次，需要当场复制给对方。"
      closeOnOverlay={false}
      footer={
        <>
          <Button
            variant="secondary"
            disabled={create.isPending}
            onClick={() => onOpenChange(false)}
          >
            取消
          </Button>
          <Button variant="primary" loading={create.isPending} onClick={submit}>
            创建
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field
          label="名字"
          required
          error={error}
          hint="用真名或岗位名，审计日志里会直接显示这个名字。"
        >
          <Input
            value={name}
            autoFocus
            maxLength={32}
            placeholder="如：张三"
            invalid={Boolean(error)}
            onChange={(event) => {
              setName(event.currentTarget.value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") submit();
            }}
          />
        </Field>
        <Field label="角色" hint="管理员：能做所有事；老师：能处理商品和发布；成员：只能看仓库和词库。">
          <Select
            value={role}
            onChange={(value) => setRole(value as "admin" | "teacher" | "member")}
            options={[
              { value: "member", label: "成员（只读）" },
              { value: "teacher", label: "老师（处理+发布）" },
              { value: "admin", label: "管理员（全部权限）" },
            ]}
          />
        </Field>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ *
 * 改名
 * ------------------------------------------------------------------ */

function RenameOperatorDialog({
  target,
  onClose,
}: {
  target: { id: string; name: string } | null;
  onClose: () => void;
}) {
  const [name, setName] = useState(target?.name ?? "");
  const [error, setError] = useState<string | null>(null);
  const rename = useRenameOperator();

  // target 变化时重置输入（同一个弹窗复用）
  const [lastId, setLastId] = useState(target?.id ?? null);
  if ((target?.id ?? null) !== lastId) {
    setLastId(target?.id ?? null);
    setName(target?.name ?? "");
    setError(null);
  }

  const submit = (): void => {
    if (!target) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("名字不能为空");
      return;
    }
    setError(null);
    rename.mutate(
      { id: target.id, name: trimmed },
      {
        onSuccess: (result) => {
          toast.success(`已改名为「${result.operator.name}」`);
          // 改的是自己：本地显示名跟着换，否则界面还挂着旧名字
          const me = currentOperator();
          if (me && me.id === result.operator.id) {
            setOperator({ token: me.token, id: me.id, name: result.operator.name });
          }
          onClose();
        },
        onError: (mutationError) => {
          const message = apiErrorMessage(mutationError);
          setError(message);
          toast.error("改名失败", { description: message });
        },
      }
    );
  };

  return (
    <Dialog
      open={target !== null}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      size="sm"
      title="修改操作员名字"
      closeOnOverlay={false}
      footer={
        <>
          <Button variant="secondary" disabled={rename.isPending} onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={rename.isPending} onClick={submit}>
            保存
          </Button>
        </>
      }
    >
      <Field label="名字" required error={error} hint="历史审计日志里的名字不会跟着改（那是当时的快照）。">
        <Input
          value={name}
          autoFocus
          maxLength={32}
          invalid={Boolean(error)}
          onChange={(event) => {
            setName(event.currentTarget.value);
            setError(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
        />
      </Field>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ *
 * 主面板
 * ------------------------------------------------------------------ */

export interface OperatorsPanelProps {
  className?: string;
}

export function OperatorsPanel({ className }: OperatorsPanelProps) {
  const operatorsQuery = useOperators();
  const rotate = useRotateOperator();
  const toggle = useToggleOperator();
  const setRole = useSetRole();
  const identity = useOperatorIdentity();

  const [createOpen, setCreateOpen] = useState(false);
  const [switchOpen, setSwitchOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null);
  const [rotateTarget, setRotateTarget] = useState<{ id: string; name: string } | null>(null);
  const [disableTarget, setDisableTarget] = useState<{ id: string; name: string; enabled: boolean } | null>(
    null
  );
  // 一次性 token：新建 / 重置共用同一个展示框
  const [issuedToken, setIssuedToken] = useState<{
    token: string;
    name: string;
    mode: "created" | "rotated";
  } | null>(null);

  const operators = useMemo(
    () => operatorsQuery.data?.operators ?? [],
    [operatorsQuery.data]
  );
  const me = useMemo(() => {
    const current = operatorsQuery.data?.current ?? null;
    if (!identity) return current;
    return operators.find((item) => item.id === identity.id) ?? current;
  }, [identity, operators, operatorsQuery.data]);

  /**
   * 是否管理员。
   * 后端没回 current（老版本）时按 true 处理：宁可多显示两个按钮，
   * 也不要把唯一的管理入口藏起来让人无路可走。
   */
  const isAdmin = me ? me.admin : true;

  const rotateNow = (): void => {
    const target = rotateTarget;
    if (!target) return;
    rotate.mutate(target.id, {
      onSuccess: (result) => {
        setRotateTarget(null);
        setIssuedToken({ token: result.token, name: result.operator.name, mode: "rotated" });
        // 重置的是自己：本地 token 必须立刻跟着换，否则下一次请求就被 401 拦
        const mine = currentOperator();
        if (mine && mine.id === result.operator.id) {
          setOperator({ token: result.token, id: result.operator.id, name: result.operator.name });
        }
      },
      onError: (error) => toast.error("重置 token 失败", { description: apiErrorMessage(error) }),
    });
  };

  const toggleNow = (): void => {
    const target = disableTarget;
    if (!target) return;
    toggle.mutate(
      { id: target.id, enabled: !target.enabled },
      {
        onSuccess: (result) => {
          toast.success(
            result.operator.enabled
              ? `已启用「${result.operator.name}」`
              : `已停用「${result.operator.name}」`,
            {
              description: result.operator.enabled
                ? "他的 token 现在可以正常使用"
                : "他的 token 立刻失效，历史记录保留",
            }
          );
          setDisableTarget(null);
        },
        onError: (error) => toast.error("操作失败", { description: apiErrorMessage(error) }),
      }
    );
  };

  return (
    <div className={cn("flex flex-col gap-4", className)}>
      <IdentityCard onSwitch={() => setSwitchOpen(true)} onCreated={() => setCreateOpen(true)} />

      <Card padding="none">
        <div className="flex flex-wrap items-center gap-2 px-4 py-3">
          <span className="text-[13px] font-semibold leading-5 text-fg">操作员</span>
          <Badge variant="neutral" size="sm">
            {operators.length} 人
          </Badge>
          <span className="ml-auto flex items-center gap-2">
            <IconButton
              label="刷新操作员列表"
              size="sm"
              variant="ghost"
              icon={<Users className={cn(operatorsQuery.isFetching && "animate-pulse")} />}
              onClick={() => void operatorsQuery.refetch()}
            />
            <Button
              variant="secondary"
              size="sm"
              icon={<UserPlus />}
              onClick={() => setCreateOpen(true)}
            >
              新建操作员
            </Button>
          </span>
        </div>
        <Separator />

        {operatorsQuery.isLoading && <SkeletonRows rows={4} className="px-4 py-3" />}

        {operatorsQuery.isError && (
          <div className="px-4 py-3">
            <InlineError onRetry={() => void operatorsQuery.refetch()}>
              {apiErrorMessage(operatorsQuery.error)}
            </InlineError>
          </div>
        )}

        {!operatorsQuery.isLoading && !operatorsQuery.isError && operators.length === 0 && (
          <EmptyState
            icon={<Users strokeWidth={1.5} />}
            title="还没有操作员"
            description="新建一个操作员，把这串 token 发给同事，之后的改动就能分清是谁做的。"
            action={{ label: "新建操作员", onClick: () => setCreateOpen(true) }}
            minHeight={200}
          />
        )}

        {operators.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="h-9 bg-canvas-inset/60">
                  <th className="border-b border-border px-4 text-left text-xs font-medium text-fg-muted">
                    名字
                  </th>
                  <th className="w-[120px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    角色
                  </th>
                  <th className="w-[148px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    最后活跃
                  </th>
                  <th className="w-[88px] border-b border-border px-3 text-left text-xs font-medium text-fg-muted">
                    状态
                  </th>
                  <th className="border-b border-border px-3 pr-4 text-right text-xs font-medium text-fg-muted">
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {operators.map((item) => {
                  const isSelf = identity?.id === item.id;
                  const isBuiltin = item.builtin === true || item.id === "local";
                  // 非管理员不渲染停用 / 重置（不是灰掉）：权限不够的按钮只会招 403
                  const canManageOthers = isAdmin && !isBuiltin;
                  return (
                    <tr
                      key={item.id}
                      className="border-b border-border-subtle transition-colors duration-fast last:border-0 hover:bg-surface-hover"
                    >
                      <td className="px-4 py-2.5">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="text-[13px] font-medium text-fg">{item.name}</span>
                          {isSelf && (
                            <Badge variant="info" size="sm">
                              我
                            </Badge>
                          )}
                          {isBuiltin && (
                            <Badge variant="neutral" size="sm">
                              内置
                            </Badge>
                          )}
                        </span>
                        <p className="mt-0.5 truncate font-mono text-2xs text-fg-muted">{item.id}</p>
                      </td>
                      <td className="px-3 py-2.5">
                        {item.role === "admin" ? (
                          <Badge variant="brand" icon={<ShieldCheck className="size-3" />}>
                            管理员
                          </Badge>
                        ) : item.role === "teacher" ? (
                          <Badge variant="info" size="sm">
                            老师
                          </Badge>
                        ) : (
                          <Badge variant="neutral" size="sm">
                            成员
                          </Badge>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="num text-xs text-fg-secondary">
                          {item.lastActiveAt ? formatRelative(item.lastActiveAt) : "从未使用"}
                        </span>
                      </td>
                      <td className="px-3 py-2.5">
                        {item.enabled ? (
                          <Badge variant="success" icon={<CheckCircle2 className="size-3" />}>
                            启用
                          </Badge>
                        ) : (
                          <Badge variant="danger" icon={<Ban className="size-3" />}>
                            已停用
                          </Badge>
                        )}
                      </td>
                      <td className="px-3 py-2.5 pr-4">
                        <span className="flex items-center justify-end gap-1">
                          {/* 改名：自己随时能改，别人只有管理员能改 */}
                          {(isSelf || canManageOthers) && !isBuiltin && (
                            <Tooltip content="改名字">
                              <IconButton
                                label={`改名字 ${item.name}`}
                                size="sm"
                                variant="ghost"
                                icon={<Pencil />}
                                onClick={() => setRenameTarget({ id: item.id, name: item.name })}
                              />
                            </Tooltip>
                          )}
                          {canManageOthers && (
                            <>
                              <Tooltip content="改角色">
                                <Select
                                  size="sm"
                                  className="w-[100px]"
                                  value={item.role ?? "member"}
                                  onChange={(value) => {
                                    setRole.mutate(
                                      { id: item.id, role: value as "admin" | "teacher" | "member" },
                                      {
                                        onSuccess: () =>
                                          toast.success(`已把「${item.name}」改为${value === "admin" ? "管理员" : value === "teacher" ? "老师" : "成员"}`),
                                        onError: (error) =>
                                          toast.error("改角色失败", { description: apiErrorMessage(error) }),
                                      }
                                    );
                                  }}
                                  options={[
                                    { value: "member", label: "成员" },
                                    { value: "teacher", label: "老师" },
                                    { value: "admin", label: "管理员" },
                                  ]}
                                />
                              </Tooltip>
                              <Tooltip content="重置 token（旧 token 立刻失效）">
                                <IconButton
                                  label={`重置 ${item.name} 的 token`}
                                  size="sm"
                                  variant="ghost"
                                  icon={<KeyRound />}
                                  onClick={() => setRotateTarget({ id: item.id, name: item.name })}
                                />
                              </Tooltip>
                              <Tooltip content={item.enabled ? "停用这个操作员" : "重新启用"}>
                                <IconButton
                                  label={`${item.enabled ? "停用" : "启用"} ${item.name}`}
                                  size="sm"
                                  variant={item.enabled ? "danger" : "ghost"}
                                  icon={item.enabled ? <Ban /> : <CheckCircle2 />}
                                  onClick={() =>
                                    setDisableTarget({
                                      id: item.id,
                                      name: item.name,
                                      enabled: item.enabled,
                                    })
                                  }
                                />
                              </Tooltip>
                            </>
                          )}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-xs leading-[18px] text-fg-muted">
        提示：没设置操作员时后端把你记到内置的「{LOCAL_OPERATOR_NAME}」名下，功能不受影响；
        多人共用一台机器时才需要每人一个身份，否则冲突与审计都分不清人。
      </p>

      <CreateOperatorDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onToken={(token, name) => setIssuedToken({ token, name, mode: "created" })}
      />

      <SwitchIdentityDialog open={switchOpen} onOpenChange={setSwitchOpen} />

      <RenameOperatorDialog target={renameTarget} onClose={() => setRenameTarget(null)} />

      {/* 重置前的确认：必须说清「旧 token 会立刻失效」 */}
      <ConfirmDialog
        open={rotateTarget !== null}
        onOpenChange={(next) => {
          if (!next && !rotate.isPending) setRotateTarget(null);
        }}
        title={`重置「${rotateTarget?.name ?? ""}」的 token？`}
        description="旧 token 会立刻失效，对方正在用的身份马上掉线，需要你把这串新 token 重新发给他。"
        confirmLabel="重置 token"
        danger
        loading={rotate.isPending}
        onConfirm={rotateNow}
      >
        <p className="text-[13px] leading-5 text-fg-secondary">
          重置后新 token 只显示一次，请当场复制。旧 token 无法恢复，只能再重置一次。
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={disableTarget !== null}
        onOpenChange={(next) => {
          if (!next && !toggle.isPending) setDisableTarget(null);
        }}
        title={
          disableTarget?.enabled
            ? `停用「${disableTarget?.name ?? ""}」？`
            : `重新启用「${disableTarget?.name ?? ""}」？`
        }
        description={
          disableTarget?.enabled
            ? "他的 token 会立刻失效，不能再改任何东西；历史记录和审计日志保留。"
            : "启用后他的 token 立刻恢复可用。"
        }
        confirmLabel={disableTarget?.enabled ? "停用" : "启用"}
        danger={disableTarget?.enabled === true}
        loading={toggle.isPending}
        onConfirm={toggleNow}
      >
        <p className="text-[13px] leading-5 text-fg-secondary">
          {disableTarget?.enabled
            ? "停用不等于删除：随时可以再启用，token 也还是原来那一串。"
            : "如果他手里已经没有那串 token，启用后还要重置一次才能用。"}
        </p>
      </ConfirmDialog>

      {/* 一次性 token 展示框（新建 / 重置共用） */}
      <OneTimeTokenDialog
        open={issuedToken !== null}
        onOpenChange={(next) => {
          if (!next) setIssuedToken(null);
        }}
        token={issuedToken?.token ?? null}
        operatorName={issuedToken?.name}
        description={
          issuedToken?.mode === "rotated"
            ? "旧 token 已经立刻失效，这是新的一串。"
            : undefined
        }
      />

      {/* 兜底提示：万一有人把一次性框截图丢了，这里再提醒一次不可恢复 */}
      <span className="sr-only">{ONE_TIME_TOKEN_NOTICE}</span>
    </div>
  );
}

export default OperatorsPanel;
