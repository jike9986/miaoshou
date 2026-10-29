// design-system §5.11 Toaster 挂载点 · §7.1 主框架 · §8.1 路由骨架
// 业务路由：/automation /warehouse /schedules /words /unlock，另有 /settings（操作员与审计日志）。
// 与老版一致，采集箱和图片翻译没有独立页面。
// 未连接妙手时自动跳 /unlock（features/auth/guard），用户选择「稍后连接」后不再强跳。

import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useRef } from "react";
import {
  CalendarClock,
  KeyRound,
  PackageSearch,
  Settings,
  ShieldBan,
  Zap,
} from "lucide-react";
import { AppShell, BrandMark } from "@/components/layout/AppShell";
import type { SidebarNavGroup } from "@/components/layout/Sidebar";
import { PageContent, PageHeader } from "@/components/layout/PageHeader";
import { AppToaster, toast } from "@/components/ui/toast";
import { EmptyState } from "@/components/ui/empty-state";
import { UnlockPage } from "@/features/auth/UnlockPage";
import { useAutoUnlock, useAuthGuardStore } from "@/features/auth/guard";
import { WarehousePage } from "@/features/warehouse/WarehousePage";
import { AutomationPage } from "@/features/automation/AutomationPage";
import { SchedulesPage } from "@/features/schedules/SchedulesPage";
import { WordsPage } from "@/features/words/WordsPage";
import { SettingsPage } from "@/features/settings/SettingsPage";
import { useAuthStatus, useVersion } from "@/lib/queries";
import { hasRuntimeToken } from "@/lib/bootstrap";
import { operatorDisplayName, useOperatorIdentity } from "@/lib/operator";

/* ------------------------------------------------------------------ *
 * 左侧导航（与路由一一对应）
 * ------------------------------------------------------------------ */

// 四项板块，与老版侧边栏一致：自动化 / 违规词库 / 成品仓库 / 定时发布，加「连接妙手」。
// 采集箱是自动化页商品列表的数据源，图片翻译是处理设置里的开关 —— 都不是独立页面。
const NAV: SidebarNavGroup[] = [
  {
    title: "功能",
    items: [
      { to: "/automation", label: "自动化上品", icon: Zap },
      { to: "/warehouse", label: "成品仓库", icon: PackageSearch },
      { to: "/schedules", label: "定时发布", icon: CalendarClock },
      { to: "/words", label: "违规词库", icon: ShieldBan },
      { to: "/unlock", label: "连接妙手", icon: KeyRound },
    ],
  },
  {
    // 设置是一件「正经入口」，不该只藏在标题栏一个像用户名的按钮后面。
    // 放进侧边栏「系统」组：看得见、点得到、说得清里面是什么。
    title: "系统",
    items: [{ to: "/settings", label: "设置", icon: Settings }],
  },
];

// 标题栏中部不再显示面包屑（老版顶部只有 logo + 右侧账号与开始处理），
// 页面标题交给各自 PageHeader。这里保留表结构仅供将来需要时启用。

/* ------------------------------------------------------------------ *
 * 壳：账号区 + 顶部店铺下拉 + 自动跳转
 * ------------------------------------------------------------------ */

function Shell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { data: status } = useAuthStatus();
  const guest = useAuthGuardStore((state) => state.guest);
  const operatorIdentity = useOperatorIdentity();
  const versionQuery = useVersion(true, hasRuntimeToken());
  const notifiedVersion = useRef<string | null>(null);
  useAutoUnlock();

  useEffect(() => {
    const version = versionQuery.data;
    if (!version?.ok || !version.updateAvailable || !version.latest
      || notifiedVersion.current === version.latest) return;
    notifiedVersion.current = version.latest;
    toast.info(`发现新版本 ${version.latest}`, {
      description: "更新详情和安装操作可在设置中查看。",
      action: { label: "打开设置", onClick: () => navigate("/settings") },
      duration: 8000,
    });
  }, [versionQuery.data, navigate]);

  useEffect(() => {
    if (operatorIdentity?.mustChangePassword && location.pathname !== "/unlock") {
      navigate("/unlock", { replace: true, state: { from: location.pathname } });
    }
  }, [operatorIdentity?.mustChangePassword, location.pathname, navigate]);

  const connected = status?.connected ?? false;
  // 操作员名：已设置显示名字，未设置标注「本机（未设置）」——
  // 多人共用一台机器时，左下角随时能看到「现在是谁在操作」。
  const operatorLabel = operatorIdentity
    ? operatorDisplayName()
    : `${operatorDisplayName()}（未设置）`;
  const roleLabel = operatorIdentity?.role === "teacher" || operatorIdentity?.role === "admin"
    ? "老师"
    : operatorIdentity ? "学员" : "未登录";

  return (
    <>
      <TokenNotice />
      <AppShell
        nav={NAV}
        brand={{ icon: <BrandMark className="size-4" />, name: "奥皇上品" }}
        account={{
          name: operatorLabel,
          detail: `${roleLabel} · 妙手${connected ? "已连接" : "未连接"}${guest ? " · 游客模式" : ""}`,
        }}
        onAccountClick={() => navigate("/settings")}
        // 标题栏不再放账号/店铺切换：账号和店铺已经挪到「开始处理」旁边，
        // 这里再放一份就是两套重复控件，只会占掉标题栏高度。
        // 也不传 routeMeta：标题栏中部不显示「功能 / 板块名」面包屑。
        // 老版顶部只有 logo，页面标题由 PageHeader 负责。
      />
    </>
  );
}

/* ------------------------------------------------------------------ *
 * 404 / 令牌缺失
 * ------------------------------------------------------------------ */

function NotFoundPage() {
  const navigate = useNavigate();
  return (
    <>
      <PageHeader title="页面不存在" />
      <PageContent width="form">
        <EmptyState
          title="没有找到这个页面"
          description="地址可能已变更，请从左侧导航重新进入。"
          action={{ label: "返回仓库", onClick: () => navigate("/warehouse", { replace: true }) }}
          minHeight={320}
        />
      </PageContent>
    </>
  );
}

/** 浏览器直开（无 preload 注入 token）时的兜底提示，不是错误，只是说明 */
function TokenNotice() {
  if (hasRuntimeToken()) return null;
  return (
    <div className="fixed inset-x-0 top-0 z-[70] border-b border-warning-600/20 bg-warning-50 px-6 py-2 text-center text-[13px] leading-5 text-warning-700">
      未拿到本地访问令牌：桌面端由 Electron 注入；浏览器预览请确认后端已在 8812 端口启动后刷新页面。
    </div>
  );
}

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/" element={<Shell />}>
          <Route index element={<Navigate to="/automation" replace />} />
          <Route path="automation" element={<AutomationPage />} />
          <Route path="warehouse" element={<WarehousePage />} />
          <Route path="schedules" element={<SchedulesPage />} />
          <Route path="words" element={<WordsPage />} />
          <Route path="words/*" element={<WordsPage />} />
          <Route path="unlock" element={<UnlockPage />} />
          {/* 设置页：只从标题栏的身份按钮进（不在左侧导航里占位） */}
          <Route path="settings" element={<SettingsPage />} />

          {/* 旧地址兼容：老书签/外部链接不再有独立页面，统一回到新板块 */}
          {/* 采集箱 / 图片翻译 不是页面：前者是自动化页的数据源，
              后者是处理设置里的开关。旧地址统一回自动化页。 */}
          <Route path="collect" element={<Navigate to="/automation" replace />} />
          <Route path="collect-box" element={<Navigate to="/automation" replace />} />
          <Route path="images" element={<Navigate to="/automation" replace />} />
          <Route path="carrier" element={<Navigate to="/warehouse" replace />} />
          <Route path="publish" element={<Navigate to="/schedules" replace />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
      <AppToaster position="bottom-right" />
    </>
  );
}
