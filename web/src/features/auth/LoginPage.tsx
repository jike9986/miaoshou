import { useEffect, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowRight, Layers3, ShieldCheck, Sparkles } from "lucide-react";
import { BrandMark, TitleBar } from "@/components/layout";
import { SoftwareAccountPanel } from "@/features/auth/UnlockPage";
import { useSoftwareCredentialsStatus } from "@/lib/queries";
import { useOperatorIdentity } from "@/lib/operator";

/** 独立的软件账号入口。妙手凭据绑定留在登录后的 /unlock 页面。 */
export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const identity = useOperatorIdentity();
  const credentialsStatus = useSoftwareCredentialsStatus(true);
  const redirectTo = useMemo(() => {
    const state = location.state as { from?: string } | null;
    return state?.from && state.from !== "/login" ? state.from : "/automation";
  }, [location.state]);

  useEffect(() => {
    if (identity && !identity.mustChangePassword) {
      navigate(redirectTo, { replace: true });
    }
  }, [identity, navigate, redirectTo]);

  return (
    <div className="flex h-screen min-h-[720px] flex-col overflow-hidden bg-canvas">
      <TitleBar />
      <main className="relative min-h-0 flex-1 overflow-auto bg-canvas px-5 py-8 text-fg sm:px-8 sm:py-12">
      <div aria-hidden className="pointer-events-none absolute -left-28 -top-32 size-[420px] rounded-full bg-brand-100/55 blur-3xl" />
      <div aria-hidden className="pointer-events-none absolute -bottom-48 -right-20 size-[520px] rounded-full bg-info-50/65 blur-3xl" />
      <div className="relative mx-auto grid min-h-[calc(100vh-var(--titlebar-h)-6rem)] w-full max-w-[1040px] items-center gap-10 lg:grid-cols-[1fr_440px] lg:gap-20">
        <section className="hidden lg:block">
          <div className="mb-10 flex items-center gap-2.5">
            <span className="grid size-9 place-items-center rounded-xl bg-brand-600 text-white shadow-2">
              <BrandMark className="size-6 brightness-0 invert" />
            </span>
            <span className="text-[18px] font-semibold tracking-tight text-fg">奥皇上品</span>
          </div>
          <p className="mb-3 flex items-center gap-2 text-[13px] font-medium text-brand-700">
            <Sparkles className="size-4" aria-hidden /> 你的上品工作台
          </p>
          <h1 className="max-w-[560px] text-[40px] font-semibold leading-[1.16] tracking-[-0.035em] text-fg">
            从采集、编辑到发布，<br />每一步都清楚可控。
          </h1>
          <p className="mt-5 max-w-[500px] text-[15px] leading-7 text-fg-secondary">
            登录软件账号后，自动化上品和成品仓库会按你的账号保存数据、任务和发布记录。
          </p>
          <div className="mt-9 grid max-w-[520px] gap-3">
            {[
              { icon: Layers3, title: "流程分开，结果相连", text: "采集箱处理完成后，再按需加入成品仓库。" },
              { icon: ShieldCheck, title: "账号数据独立保存", text: "每个账号只看到自己的操作记录和发布状态。" },
              { icon: ArrowRight, title: "状态反馈更清晰", text: "任务进度、失败原因和待处理项集中展示。" },
            ].map(({ icon: Icon, title, text }) => (
              <div key={title} className="flex items-start gap-3 rounded-xl border border-border-subtle bg-surface/70 px-4 py-3 shadow-1 backdrop-blur-sm">
                <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg bg-brand-50 text-brand-700">
                  <Icon className="size-4" strokeWidth={1.8} aria-hidden />
                </span>
                <div className="min-w-0">
                  <p className="text-[13px] font-semibold leading-5 text-fg">{title}</p>
                  <p className="mt-0.5 text-[12px] leading-5 text-fg-muted">{text}</p>
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="w-full max-w-[440px] justify-self-center">
          <div className="mb-6 flex items-center justify-center gap-2 lg:hidden">
            <BrandMark className="size-7" />
            <span className="text-[18px] font-semibold tracking-tight">奥皇上品</span>
          </div>
          <div className="mb-4 px-1 lg:hidden">
            <p className="text-[24px] font-semibold leading-8 tracking-[-0.02em] text-fg">欢迎回来</p>
            <p className="mt-1 text-[13px] leading-5 text-fg-muted">登录后继续处理你的商品。</p>
          </div>
          <SoftwareAccountPanel
            redirectTo={redirectTo}
            onBack={() => undefined}
            allowInitialize={!credentialsStatus.data?.initialized}
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
