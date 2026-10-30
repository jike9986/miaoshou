// features/settings —— 设置页（操作员 / 审计日志）
//
// 为什么挂在 /settings 而不是新开一个侧边栏入口：
//   左侧导航是「做事的四个板块」，设置不是一件「活」；多一个常驻导航项
//   只会让每天要点几十次的列表变长。入口放在标题栏右侧的身份按钮上 ——
//   它本来就该显示「现在是谁」，点一下就能换人 / 看审计，语义正好对上。
//
// 两个面板按 Tab 分区：
//   · 操作员：当前身份 + 名单 + 一次性 token（管理动作都在这里）
//   · 审计日志：谁在什么时候动了什么（排查用）

import { Info, SlidersHorizontal, ScrollText, Users } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { PageContent, PageHeader } from "@/components/layout/PageHeader";
import { Tabs, tabId, tabPanelId } from "@/components/ui";
import { useOperatorIdentity } from "@/lib/operator";
import { useOperators } from "@/lib/queries";
import { OperatorsPanel } from "@/features/settings/OperatorsPanel";
import { AuditLogPanel } from "@/features/settings/AuditLogPanel";
import { PublishSettingsPanel } from "@/features/settings/PublishSettingsPanel";
import { ImageServiceCard } from "@/features/automation/ImageServiceCard";
import { VersionPanel } from "@/features/settings/VersionPanel";
import { SHOW_ACCOUNT_UI } from "@/lib/featureFlags";

export type SettingsTab = "operators" | "processing" | "audit" | "about";

export interface SettingsPageProps {
  /** 从标题栏身份按钮跳进来时直接定位到某个 Tab */
  initialTab?: SettingsTab;
}

const SETTINGS_TABS: SettingsTab[] = ["operators", "processing", "audit", "about"];

export function SettingsPage({ initialTab = SHOW_ACCOUNT_UI ? "operators" : "about" }: SettingsPageProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const tab = SETTINGS_TABS.includes(requestedTab as SettingsTab)
    ? (requestedTab as SettingsTab)
    : initialTab;
  const identity = useOperatorIdentity();
  const operatorsQuery = useOperators();
  const role = operatorsQuery.data?.current?.role ?? identity?.role;
  const canManage =
    role === "teacher" || role === "admin" || operatorsQuery.data?.current?.admin === true;
  const activeTab = SHOW_ACCOUNT_UI
    ? canManage || tab === "operators" || tab === "about" ? tab : "operators"
    : canManage && (tab === "processing" || tab === "audit") ? tab : "about";
  const settingsTabs = [
    ...(SHOW_ACCOUNT_UI
      ? [{ value: "operators", label: canManage ? "账号与权限" : "账号", icon: <Users className="size-3.5" /> }]
      : []),
    ...(canManage
      ? [
          { value: "processing", label: "处理配置", icon: <SlidersHorizontal className="size-3.5" /> },
          { value: "audit", label: "审计日志", icon: <ScrollText className="size-3.5" /> },
        ]
      : []),
    { value: "about", label: "关于与更新", icon: <Info className="size-3.5" /> },
  ] as const;

  return (
    <>
      <PageHeader
        title="设置"
      >
        <Tabs
          value={activeTab}
          onChange={(value) => {
            setSearchParams((current) => {
              const next = new URLSearchParams(current);
              next.set("tab", value);
              return next;
            }, { replace: true });
          }}
          idBase="settings"
          items={[...settingsTabs]}
        />
      </PageHeader>

      <PageContent width="table">
        <div
          role="tabpanel"
          id={tabPanelId("settings", activeTab)}
          aria-labelledby={tabId("settings", activeTab)}
          className="contents"
        >
          {SHOW_ACCOUNT_UI && activeTab === "operators" && <OperatorsPanel />}
          {activeTab === "processing" && canManage && (
            <div className="flex flex-col gap-4">
              <PublishSettingsPanel />
              <ImageServiceCard />
            </div>
          )}
          {activeTab === "audit" && canManage && <AuditLogPanel />}
          {activeTab === "about" && <VersionPanel />}
        </div>
      </PageContent>
    </>
  );
}

export default SettingsPage;
