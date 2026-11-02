import { useEffect, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { BrandMark } from "@/components/layout";
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
    <main className="grid min-h-screen place-items-center bg-canvas px-5 py-10">
      <div className="w-full max-w-[440px]">
        <div className="mb-6 flex items-center justify-center gap-2 text-fg">
          <BrandMark className="size-6" />
          <span className="text-[16px] font-semibold tracking-normal">奥皇上品</span>
        </div>
        <SoftwareAccountPanel
          redirectTo={redirectTo}
          onBack={() => undefined}
          allowInitialize={!credentialsStatus.data?.initialized}
          allowRegister
          showBack={false}
        />
        <p className="mt-4 text-center text-[12px] leading-5 text-fg-muted">
          软件账号用于区分操作权限和业务数据，妙手账号请在登录后单独绑定。
        </p>
      </div>
    </main>
  );
}

export default LoginPage;
