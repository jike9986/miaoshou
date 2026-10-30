// features/auth —— 连接妙手（api.md §1）
// 未连接：全屏引导（AppID / AppSecret，密码框可切换明文，错误就地显示）。
// 已连接：顶部账号名 + 店铺下拉（AccountSwitcher，挂在标题栏右侧）。

import { useEffect, useId, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, KeyRound, Link2, Server } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";
import { fadeUp, ease } from "@/lib/motion";
import { cn } from "@/lib/utils";
import { ApiError, apiErrorMessage, resolveApiBase } from "@/lib/api";
import { getSavedApiBase, saveApiBase } from "@/lib/serverAddress";
import { useAuthStatus, useConnectAccount, useShops, useSoftwareLogin, useSoftwareCredentialsStatus, useChangeSoftwarePassword, useInitializeSoftwareCredentials } from "@/lib/queries";
import { clearOperator, setOperator, useOperatorIdentity } from "@/lib/operator";
import { useSessionStore } from "@/features/stores";
import {
  Button,
  Card,
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
  const identity = useOperatorIdentity();
  const { data: status } = useAuthStatus(Boolean(identity));
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

  const existingIdentity = useOperatorIdentity();
  const { data: status, isLoading, isError, error, refetch, isFetching } = useAuthStatus(Boolean(existingIdentity));
  const credentialsStatus = useSoftwareCredentialsStatus(!existingIdentity);
  const connect = useConnectAccount();

  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [rememberName, setRememberName] = useState("");
  const [errors, setErrors] = useState<FormErrors>({});
  const [accountMode, setAccountMode] = useState(
    existingIdentity?.mustChangePassword ?? !existingIdentity
  );

  const connected = status?.connected ?? false;
  const accounts = useMemo(() => status?.accounts ?? [], [status?.accounts]);
  const redirectTo = useMemo(() => {
    const state = location.state as { from?: string } | null;
    return state?.from && state.from !== "/unlock" ? state.from : "/automation";
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

  if (!existingIdentity) {
    return (
      <>
        <PageHeader title="软件账号登录" />
        <PageContent width="form">
          <ServerAddressControl />
          <SoftwareAccountPanel
            redirectTo={redirectTo}
            onBack={() => undefined}
            allowInitialize={credentialsStatus.data?.initialized === false}
          />
        </PageContent>
      </>
    );
  }

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
      <>
        <PageHeader title="连接妙手" />
        <PageContent width="form">
          <ServerAddressControl />
          <SoftwareAccountPanel redirectTo={redirectTo} onBack={() => undefined} />
          <div className="mt-4 space-y-3">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-44 w-full" rounded="rounded-lg" />
          </div>
        </PageContent>
      </>
    );
  }

  if (isError) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center bg-canvas p-8">
        <div className="w-full max-w-[520px] space-y-4">
          <ServerAddressControl />
          <Card padding="normal">
            <ErrorState
              title="读取连接状态失败"
              description={apiErrorMessage(error)}
              errorCode={error instanceof ApiError ? error.code : null}
              onRetry={() => void refetch()}
              retrying={isFetching}
              minHeight={160}
            />
          </Card>
          <SoftwareAccountPanel redirectTo={redirectTo} onBack={() => undefined} />
        </div>
      </div>
    );
  }

  return (
    <>
      <PageHeader title="连接妙手" />
      <PageContent width="form">
        <ServerAddressControl />
        <div className="mb-4 flex gap-2" role="group" aria-label="登录方式">
          <Button variant={!accountMode ? "primary" : "secondary"} size="sm" onClick={() => setAccountMode(false)}>绑定妙手</Button>
          <Button variant={accountMode ? "primary" : "secondary"} size="sm" onClick={() => setAccountMode(true)}>软件账号登录</Button>
        </div>
        {accountMode ? (
          <SoftwareAccountPanel redirectTo={redirectTo} onBack={() => setAccountMode(false)} />
        ) : (
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

              <div className="mt-4 flex flex-col gap-4">
                <Field
                  label="AppID"
                  required
                  htmlFor={appIdField}
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
                </div>
              </div>
            </Card>

            <div className="flex flex-col gap-4">
              {accounts.length > 0 && (
                <Card padding="normal">
                  <CardTitle className="text-h3">已保存账号</CardTitle>
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
        )}
      </PageContent>
    </>
  );
}

function ServerAddressControl() {
  const [address, setAddress] = useState(() => getSavedApiBase() || resolveApiBase());
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const save = (): void => {
    setError(null);
    try {
      const current = getSavedApiBase();
      const next = saveApiBase(address);
      if (current === next) return;
      setSaving(true);
      clearOperator();
      window.location.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存服务端地址失败");
    }
  };

  return (
    <Card className="mb-4" padding="normal">
      <CardTitle className="text-h3">服务端地址</CardTitle>
      <p className="mt-1 text-[13px] leading-5 text-fg-secondary">
        当前：{getSavedApiBase() || resolveApiBase() || "本机服务"}
      </p>
      <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-end">
        <Field label="HTTPS 服务地址" className="min-w-0 flex-1" error={error}>
          <Input
            type="url"
            value={address}
            autoComplete="url"
            spellCheck={false}
            placeholder="留空使用默认服务器"
            invalid={Boolean(error)}
            onChange={(event) => setAddress(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") save();
            }}
          />
        </Field>
        <Button variant="secondary" icon={<Server />} loading={saving} onClick={save}>
          保存地址
        </Button>
      </div>
    </Card>
  );
}

function SoftwareAccountPanel({ redirectTo, onBack, allowInitialize = false }: {
  redirectTo: string;
  onBack: () => void;
  allowInitialize?: boolean;
}) {
  const usernameId = useId();
  const passwordId = useId();
  const currentId = useId();
  const nextId = useId();
  const identity = useOperatorIdentity();
  const queryClient = useQueryClient();
  const login = useSoftwareLogin();
  const initialize = useInitializeSoftwareCredentials();
  const change = useChangeSoftwarePassword();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [mustChange, setMustChange] = useState(identity?.mustChangePassword ?? false);
  const [error, setError] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(false);
  const [bootstrapUsername, setBootstrapUsername] = useState("");
  const [bootstrapPassword, setBootstrapPassword] = useState("");
  const navigate = useNavigate();

  const submitLogin = () => {
    setError(null);
    login.mutate({ username: username.trim(), password }, {
      onSuccess: (result) => {
        setOperator({ token: result.token, id: result.operator.id, name: result.operator.name, role: result.operator.role, mustChangePassword: result.mustChangePassword });
        setMustChange(result.mustChangePassword);
        setCurrentPassword(password);
        setPassword("");
        if (!result.mustChangePassword) {
          void queryClient.invalidateQueries();
          navigate(redirectTo, { replace: true });
        }
      },
      onError: (reason) => setError(apiErrorMessage(reason)),
    });
  };

  const submitInitialize = () => {
    setError(null);
    if (!bootstrapUsername.trim() || bootstrapPassword.length < 8) {
      setError("请填写用户名，并设置至少 8 个字符的密码");
      return;
    }
    initialize.mutate({ username: bootstrapUsername.trim(), password: bootstrapPassword }, {
      onSuccess: (result) => {
        setOperator({ token: result.token, id: result.operator.id, name: result.operator.name,
          role: result.operator.role, mustChangePassword: result.mustChangePassword });
        void queryClient.invalidateQueries();
        navigate(redirectTo, { replace: true });
      },
      onError: (reason) => setError(apiErrorMessage(reason)),
    });
  };

  const submitPassword = () => {
    setError(null);
    if (newPassword.length < 8) {
      setError("新密码至少需要 8 个字符");
      return;
    }
    change.mutate({ currentPassword, newPassword }, {
      onSuccess: (result) => {
        if (identity) setOperator({ token: result.token, id: identity.id, name: identity.name, role: identity.role, mustChangePassword: result.mustChangePassword });
        void queryClient.invalidateQueries();
        setMustChange(false);
        navigate(redirectTo, { replace: true });
      },
      onError: (reason) => setError(apiErrorMessage(reason)),
    });
  };

  return (
    <Card className="w-full" padding="normal">
      <CardTitle>{allowInitialize && initializing ? "首次设置老师账号" : mustChange ? "设置新密码" : "软件账号登录"}</CardTitle>
      <div className="mt-4 flex flex-col gap-4">
        {allowInitialize && initializing ? <>
          <p className="text-[13px] leading-5 text-fg-secondary">首次接管仅可设置一次。完成后本机令牌失效，后续请使用老师账号登录。</p>
          <Field label="老师用户名" required htmlFor={usernameId}><Input id={usernameId} autoComplete="username" value={bootstrapUsername} onChange={(e) => setBootstrapUsername(e.currentTarget.value)} /></Field>
          <Field label="登录密码" required htmlFor={passwordId}><Input id={passwordId} type="password" autoComplete="new-password" value={bootstrapPassword} onChange={(e) => setBootstrapPassword(e.currentTarget.value)} /></Field>
          <div className="flex flex-wrap gap-2"><Button loading={initialize.isPending} onClick={submitInitialize}>完成首次接管</Button><Button variant="ghost" onClick={() => setInitializing(false)}>返回登录</Button></div>
        </> : mustChange ? <>
          <p className="text-[13px] leading-5 text-fg-secondary">首次登录需要先修改初始密码。</p>
          <Field label="当前密码" required htmlFor={currentId}><Input id={currentId} type="password" autoComplete="current-password" value={currentPassword} onChange={(e) => setCurrentPassword(e.currentTarget.value)} /></Field>
          <Field label="新密码" required htmlFor={nextId}><Input id={nextId} type="password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.currentTarget.value)} /></Field>
          <Button loading={change.isPending} onClick={submitPassword}>保存新密码</Button>
        </> : <>
          <Field label="用户名" required htmlFor={usernameId}><Input id={usernameId} autoComplete="username" value={username} onChange={(e) => setUsername(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === "Enter") submitLogin(); }} /></Field>
          <Field label="密码" required htmlFor={passwordId}><Input id={passwordId} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === "Enter") submitLogin(); }} /></Field>
          <div className="flex flex-wrap gap-2"><Button loading={login.isPending} onClick={submitLogin}>登录</Button>{!allowInitialize && <Button variant="ghost" onClick={onBack}>返回妙手绑定</Button>}</div>
          {allowInitialize && <Button variant="ghost" onClick={() => setInitializing(true)}>首次设置老师账号</Button>}
        </>}
        {error && <InlineError>{error}</InlineError>}
      </div>
    </Card>
  );
}

export default UnlockPage;
