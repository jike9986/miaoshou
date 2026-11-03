import { useCallback, useEffect, useMemo, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { BrandMark, TitleBar } from "@/components/layout";
import { SoftwareAccountPanel } from "@/features/auth/UnlockPage";
import { useSoftwareCredentialsStatus } from "@/lib/queries";
import { useOperatorIdentity } from "@/lib/operator";
import { resolveApiBase } from "@/lib/api";

function isLocalApi(): boolean {
  const base = resolveApiBase();
  if (!base) {
    return ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
  }
  try {
    return ["localhost", "127.0.0.1", "::1"].includes(new URL(base).hostname);
  } catch {
    return false;
  }
}

/** 独立的软件账号入口。妙手凭据绑定留在登录后的 /unlock 页面。 */
export function LoginPage() {
  const mainRef = useRef<HTMLElement>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const identity = useOperatorIdentity();
  const localApi = isLocalApi();
  const credentialsStatus = useSoftwareCredentialsStatus(localApi);
  const redirectTo = useMemo(() => {
    const state = location.state as { from?: string } | null;
    const fallback = identity?.role === "member" ? "/warehouse" : "/automation";
    const from = state?.from;
    if (!from || from === "/login") return fallback;
    if (identity?.role === "member" && ["/automation", "/collect", "/collect-box", "/images"].includes(from.split("?")[0])) return fallback;
    return from;
  }, [identity?.role, location.state]);
  const scrollToTop = useCallback(() => {
    mainRef.current?.scrollTo(0, 0);
  }, []);

  useEffect(() => {
    if (identity && !identity.mustChangePassword) {
      navigate(redirectTo, { replace: true });
    }
  }, [identity, navigate, redirectTo]);

  return (
    <div className="flex h-screen min-h-0 flex-col overflow-hidden bg-canvas">
      <TitleBar />
      <main ref={mainRef} className="relative min-h-0 flex-1 overflow-x-hidden overflow-y-auto bg-canvas px-5 py-8 text-fg sm:px-8 sm:py-12 [@media(max-height:520px)]:!py-3">
      <div className="relative mx-auto grid min-h-[calc(100vh-var(--titlebar-h)-6rem)] w-full max-w-[1040px] items-center gap-10 lg:grid-cols-[1fr_440px] lg:gap-20">
        <section className="hidden lg:block">
          <div className="mb-8 flex items-center gap-2.5">
            <span className="grid size-9 place-items-center rounded-xl shadow-2">
              <BrandMark className="size-9" />
            </span>
            <span className="text-[18px] font-semibold tracking-tight text-fg">奥皇上品</span>
          </div>
          <h1 className="text-[30px] font-semibold leading-10 tracking-tight text-fg">
            商品上品工作台
          </h1>
          <p className="mt-3 text-[15px] leading-7 text-fg-secondary">
            从采集箱处理到成品发布，集中完成上品工作。
          </p>
          <dl className="mt-8 space-y-5 border-t border-border-subtle pt-6">
            <div>
              <dt className="text-[13px] font-medium leading-5 text-fg">自动化上品</dt>
              <dd className="mt-1 text-[13px] leading-6 text-fg-muted">处理采集箱商品的标题、图片与价格，再发布。</dd>
            </div>
            <div>
              <dt className="text-[13px] font-medium leading-5 text-fg">成品仓库</dt>
              <dd className="mt-1 text-[13px] leading-6 text-fg-muted">认领已编辑商品，定价后直接发布。</dd>
            </div>
          </dl>
        </section>

        <section className="w-full max-w-[440px] justify-self-center">
          <div className="mb-6 flex items-center justify-center gap-2 lg:hidden [@media(max-height:520px)]:hidden">
            <BrandMark className="size-7" />
            <span className="text-[18px] font-semibold tracking-tight">奥皇上品</span>
          </div>
          <div className="mb-4 px-1 lg:hidden">
            <p className="text-[24px] font-semibold leading-8 tracking-[-0.02em] text-fg">欢迎回来</p>
            <p className="mt-1 text-[13px] leading-5 text-fg-muted [@media(max-height:520px)]:hidden">登录后继续处理你的商品。</p>
          </div>
          <SoftwareAccountPanel
            redirectTo={redirectTo}
            onBack={() => undefined}
            scrollToTop={scrollToTop}
            allowInitialize={localApi && !credentialsStatus.data?.initialized}
            allowRegister
            showBack={false}
          />
          <p className="mt-4 text-center text-[12px] leading-5 text-fg-muted">
            妙手账号请在登录后前往「连接妙手」绑定。
          </p>
        </section>
      </div>
      </main>
    </div>
  );
}

export default LoginPage;
