// features/auth —— 连接妙手（api.md §1）
// 未连接：全屏引导（AppID / AppSecret，密码框可切换明文，错误就地显示）。
// 已连接：顶部账号名 + 店铺下拉（AccountSwitcher，挂在标题栏右侧）。

import { useEffect, useId, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowRight, Eye, EyeOff, KeyRound, Link2 } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";
import { fadeUp, ease } from "@/lib/motion";
import { cn, formatRelative } from "@/lib/utils";
import { ApiError, apiErrorMessage } from "@/lib/api";
import { useAuthStatus, useConnectAccount, useShops } from "@/lib/queries";
import { useSessionStore } from "@/features/stores";
import { useAuthGuardStore } from "@/features/auth/guard";
import {
  Button,
  Card,
  CardDescription,
  CardTitle,
  DropdownSelect,
  ErrorState,
  Field,
  InlineError,
  Input,
  Skeleton,
  StatusBadge,
  toast,
} from "@/components/ui";
import { PageContent, PageHeader } from "@/components/layout";

/* ------------------------------------------------------------------ *
 * 账号 + 店铺切换（已连接时显示的「账号切换」控件）
 *
 * 左侧账号做成下拉：GET /auth/status 的 accounts 已经有全部已保存账号，
 * 选中即 POST /auth/connect {accountId}，切号后 store 里的店铺会被重置，
 * 下面的店铺下拉会跟着换一批。老版顶部只有一个只读的账号徽章，这里做成可切。
 * ------------------------------------------------------------------ */

export interface AccountSwitcherProps {
  /** 紧凑模式：用在页面工具条里（无外框、字号更小） */
  compact?: boolean;
  className?: string;
}

export function AccountSwitcher({ compact = false, className }: AccountSwitcherProps = {}) {
  const { data: status } = useAuthStatus();
  const connect = useConnectAccount();
  const currentShopId = useSessionStore((state) => state.currentShopId);
  const setCurrentShopId = useSessionStore((state) => state.setCurrentShopId);
  const connected = status?.connected ?? false;

  const { data: shopsData } = useShops(status?.accountId, { enabled: connected });
  const shops = shopsData?.shops ?? [];

  // 首次拿到店铺列表时默认选中第一家有货权的店铺
  useEffect(() => {
    if (!connected) {
      if (currentShopId !== null) setCurrentShopId(null);
      return;
    }
    if (shops.length === 0) return;
    const exists = shops.some((shop) => shop.shopId === currentShopId);
    if (!exists) setCurrentShopId(shops[0].shopId);
  }, [connected, shops, currentShopId, setCurrentShopId]);

  if (!connected) return null;

  const accountOptions = (status?.accounts ?? []).map((account) => ({
    value: account.id,
    label: account.name,
  }));

  const shopOptions = shops.map((shop) => ({
    value: shop.shopId,
    label: shop.name,
    description: shop.siteName || shop.site,
  }));

  return (
    <div className={cn("flex items-center gap-2", className)}>
      {/* 账号切换 */}
      {accountOptions.length > 1 ? (
        <DropdownSelect
          size="sm"
          className={compact ? "w-[104px]" : "w-[148px]"}
          value={status?.accountId ?? null}
          onChange={(accountId) => {
            if (!accountId || accountId === status?.accountId) return;
            const target = (status?.accounts ?? []).find((item) => item.id === accountId);
            setCurrentShopId(null);
            connect.mutate(
              { accountId, save: true, autoScan: false },
              {
                onSuccess: () =>
                  toast.success(`已切换到「${target?.name ?? accountId}」`, {
                    description: "采集箱与店铺已按新账号刷新",
                  }),
                onError: (error) =>
                  toast.error("切换账号失败", { description: apiErrorMessage(error) }),
              }
            );
          }}
          options={accountOptions}
          placeholder="切换账号"
          searchable
          searchPlaceholder="搜索账号名"
          aria-label="切换账号"
          emptyText="没有匹配的账号"
        />
      ) : (
        <span className="flex items-center gap-1.5 text-xs leading-[18px] text-fg-secondary">
          {!compact && (
            <span className="grid size-5 place-items-center rounded-full bg-brand-100 text-[10px] font-medium text-brand-800">
              {Array.from(status?.connectionName ?? "妙")[0]}
            </span>
          )}
          <span className="max-w-[120px] truncate font-medium">
            {status?.connectionName ?? "已连接"}
          </span>
        </span>
      )}

      <span aria-hidden className="h-4 w-px bg-border" />

      {/* 店铺切换 */}
      <DropdownSelect
        size="sm"
        className={compact ? "w-[148px]" : "w-[188px]"}
        value={currentShopId}
        onChange={(shopId) => {
          setCurrentShopId(shopId);
          const shop = shops.find((item) => item.shopId === shopId);
          toast.info(`已切换到「${shop?.name ?? shopId}」`);
        }}
        options={shopOptions}
        placeholder={shops.length === 0 ? "加载店铺…" : "选择店铺"}
        searchable
        searchPlaceholder="搜索店铺名 / 站点"
        aria-label="当前店铺"
        emptyText="没有匹配的店铺"
      />
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 连接页（/unlock）
 * ------------------------------------------------------------------ */

interface FormErrors {
  appId?: string;
  appSecret?: string;
  form?: string;
}

export function UnlockPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const reduced = useReducedMotion();
  const appIdField = useId();
  const secretField = useId();

  const { data: status, isLoading, isError, error, refetch, isFetching } = useAuthStatus();
  const connect = useConnectAccount();
  const markGuest = useAuthGuardStore((state) => state.markGuest);

  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [rememberName, setRememberName] = useState("");
  const [errors, setErrors] = useState<FormErrors>({});

  const connected = status?.connected ?? false;
  const accounts = useMemo(() => status?.accounts ?? [], [status?.accounts]);
  const redirectTo = useMemo(() => {
    const state = location.state as { from?: string } | null;
    return state?.from && state.from !== "/unlock" ? state.from : "/warehouse";
  }, [location.state]);

  // 已连接时自动进入目标页。
  // 但从侧栏主动点「连接妙手」进来（没有 from）时不能弹回 —— 那样用户永远
  // 打不开这一页去换账号，表现就是「点了没反应」。
  useEffect(() => {
    const state = location.state as { from?: string } | null;
    if (connected && state?.from && state.from !== "/unlock") {
      navigate(redirectTo, { replace: true });
    }
  }, [connected, navigate, redirectTo, location.state]);

  const submit = (): void => {
    const next: FormErrors = {};
    const trimmedId = appId.trim();
    const trimmedSecret = appSecret.trim();
    if (!trimmedId) next.appId = "请填写 AppID";
    else if (trimmedId.length > 256) next.appId = "AppID 不能超过 256 个字符";
    if (!trimmedSecret) next.appSecret = "请填写 AppSecret";
    else if (trimmedSecret.length > 512) next.appSecret = "AppSecret 不能超过 512 个字符";
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    const name = rememberName.trim();
    connect.mutate(
      {
        appId: trimmedId,
        appSecret: trimmedSecret,
        name: name ? name : null,
        save: true,
        autoScan: true,
      },
      {
        onSuccess: (result) => {
          setAppSecret("");
          toast.success(`已连接「${result.connectionName ?? trimmedId.slice(-4)}」`, {
            description: `共 ${result.shopsCount} 个店铺，凭据有效`,
          });
          navigate(redirectTo, { replace: true });
        },
        onError: (mutationError) => {
          const message = apiErrorMessage(mutationError);
          setErrors({ form: message });
          const code = mutationError instanceof ApiError ? mutationError.code : null;
          if (code === "UPSTREAM_ERROR") {
            toast.error("妙手拒绝了这组凭据", { description: message });
          } else if (code === "CONFLICT_BUSY") {
            toast.warning("有商品批次正在运行", { description: message });
          } else {
            toast.error("连接失败", { description: message });
          }
        },
      }
    );
  };

  // 已有保存账号时，允许一键用已保存凭据重连
  const reconnect = (accountId: string): void => {
    setErrors({});
    connect.mutate(
      { accountId },
      {
        onSuccess: (result) => {
          toast.success(`已连接「${result.connectionName ?? accountId}」`);
          navigate(redirectTo, { replace: true });
        },
        onError: (mutationError) => {
          setErrors({ form: apiErrorMessage(mutationError) });
        },
      }
    );
  };

  if (isLoading) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center bg-canvas p-8">
        <div className="w-full max-w-[440px] space-y-3">
          <Skeleton className="h-6 w-40" />
          <Skeleton className="h-44 w-full" rounded="rounded-lg" />
          <Skeleton className="h-9 w-full" rounded="rounded-md" />
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center bg-canvas p-8">
        <Card className="w-full max-w-[520px]" padding="normal">
          <ErrorState
            title="读取连接状态失败"
            description={apiErrorMessage(error)}
            errorCode={error instanceof ApiError ? error.code : null}
            onRetry={() => void refetch()}
            retrying={isFetching}
            minHeight={200}
          />
        </Card>
      </div>
    );
  }

  return (
    <>
      <PageHeader
        title="连接妙手"
        breadcrumb={["奥皇上品", "功能", "连接妙手"]}
        description="填写妙手开放平台的 AppID / AppSecret，连接后可拉取店铺、采集箱与类目。"
      />
      <PageContent width="form">
        <motion.div
          variants={reduced ? undefined : fadeUp}
          initial={reduced ? undefined : "hidden"}
          animate={reduced ? undefined : "show"}
          transition={{ duration: 0.18, ease: ease.out }}
          className="w-full"
        >
          <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
            <Card padding="normal">
              <CardTitle>账号凭据</CardTitle>
              <CardDescription>
                凭据只保存在本机数据目录，响应中仅回显后 4 位。AppSecret 永不下行。
              </CardDescription>

              <div className="mt-4 flex flex-col gap-4">
                <Field
                  label="AppID"
                  required
                  htmlFor={appIdField}
                  hint="妙手开放平台 → 授权信息 → AppKey"
                  error={errors.appId ?? null}
                >
                  <Input
                    id={appIdField}
                    value={appId}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="ak_7f3c9a1b2d4e"
                    invalid={Boolean(errors.appId)}
                    icon={<KeyRound />}
                    onChange={(event) => {
                      setAppId(event.currentTarget.value);
                      if (errors.appId) setErrors((prev) => ({ ...prev, appId: undefined }));
                    }}
                  />
                </Field>

                <Field
                  label="AppSecret"
                  required
                  htmlFor={secretField}
                  hint="仅上行校验，不写日志、不回显"
                  error={errors.appSecret ?? null}
                  action={
                    <button
                      type="button"
                      onClick={() => setShowSecret((prev) => !prev)}
                      className="rounded-sm px-1 text-xs font-medium text-brand-700 transition-colors duration-fast hover:bg-brand-50"
                    >
                      {showSecret ? "隐藏明文" : "显示明文"}
                    </button>
                  }
                >
                  <div className="relative flex items-center">
                    <Input
                      id={secretField}
                      type={showSecret ? "text" : "password"}
                      value={appSecret}
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="sk_9c1f0e2a7b6d4f8a"
                      invalid={Boolean(errors.appSecret)}
                      className="pr-10"
                      onChange={(event) => {
                        setAppSecret(event.currentTarget.value);
                        if (errors.appSecret) setErrors((prev) => ({ ...prev, appSecret: undefined }));
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") submit();
                      }}
                    />
                    <button
                      type="button"
                      aria-label={showSecret ? "隐藏 AppSecret" : "显示 AppSecret"}
                      onClick={() => setShowSecret((prev) => !prev)}
                      className="absolute right-1.5 grid size-7 place-items-center rounded-sm text-fg-muted transition-colors duration-fast hover:bg-surface-active hover:text-fg"
                    >
                      {showSecret ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                </Field>

                <Field
                  label="连接名称"
                  hint="选填，1–60 字符；缺省为「妙手 + AppID 后 4 位」"
                >
                  <Input
                    value={rememberName}
                    maxLength={60}
                    placeholder="妙手罗蔓"
                    onChange={(event) => setRememberName(event.currentTarget.value)}
                  />
                </Field>

                {errors.form && <InlineError>{errors.form}</InlineError>}

                <div className="flex items-center gap-2">
                  <Button
                    variant="primary"
                    size="md"
                    loading={connect.isPending}
                    loadingText="正在校验凭据…"
                    icon={<Link2 />}
                    onClick={submit}
                  >
                    连接
                  </Button>
                  <Button
                    variant="ghost"
                    size="md"
                    onClick={() => {
                      markGuest();
                      navigate("/warehouse", { replace: true });
                    }}
                    iconRight={<ArrowRight />}
                  >
                    稍后连接
                  </Button>
                </div>
                <p className="text-xs leading-[18px] text-fg-muted">
                  连接会调用一次 <code className="font-mono">shop_list</code> 校验凭据；选中「稍后连接」可先浏览本机仓库。
                </p>
              </div>
            </Card>

            <div className="flex flex-col gap-4">
              {accounts.length > 0 && (
                <Card padding="normal">
                  <CardTitle className="text-h3">已保存账号</CardTitle>
                  <CardDescription>点击用已保存的凭据重连，无需重新输入。</CardDescription>
                  <ul className="mt-3 flex flex-col gap-1.5">
                    {accounts.map((account) => (
                      <li key={account.id}>
                        <button
                          type="button"
                          disabled={connect.isPending}
                          onClick={() => reconnect(account.id)}
                          className={cn(
                            "flex w-full items-center gap-2 rounded-md border border-border px-2.5 py-2 text-left",
                            "transition-colors duration-fast hover:border-border-strong hover:bg-surface-hover",
                            "disabled:pointer-events-none disabled:opacity-50"
                          )}
                        >
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-[13px] font-medium text-fg">
                              {account.name}
                            </span>
                            <span className="block truncate font-mono text-2xs text-fg-muted">
                              {account.appIdHint ?? "—"}
                              {account.site ? ` · ${account.site}` : ""}
                              {account.lastVerifiedAt
                                ? ` · ${formatRelative(account.lastVerifiedAt)}`
                                : ""}
                            </span>
                          </span>
                          <StatusBadge
                            meta={
                              account.status === "active"
                                ? { label: "有效", tone: "success", dot: "success" }
                                : { label: "需重连", tone: "warning", dot: "warning" }
                            }
                          />
                        </button>
                      </li>
                    ))}
                  </ul>
                </Card>
              )}
            </div>
          </div>
        </motion.div>
      </PageContent>
    </>
  );
}

export default UnlockPage;
