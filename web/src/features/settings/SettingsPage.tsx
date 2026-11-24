// features/settings —— 设置页（操作员 / 定价模板 / 审计日志 / 违规词库 / 关于）
//
// 为什么挂在 /settings 而不是新开一个侧边栏入口：
//   左侧导航是「做事的四个板块」，设置不是一件「活」；多一个常驻导航项
//   只会让每天要点几十次的列表变长。入口放在标题栏右侧的身份按钮上 ——
//   它本来就该显示「现在是谁」，点一下就能换人 / 看审计，语义正好对上。
//
// 2026-11-23 按负责人要求精简：
//   · 「处理配置」整块删掉 —— 发布总开关和图片服务都在别处能配，
//     这里再放一份只是让人多点一次，且配置项与自动化页重复。
//   · 「定价模板」对**所有角色**开放（含学员）：负责人要求学员也能自己
//     改模板。后端按操作员隔离存储，学员改的是自己那份私有库，
//     不会影响老师定的共享基准、也不会影响别的学员。
//   · 「账号与权限」里原来「学员账号」和「兼容身份维护」两块内容几乎重叠，
//     合并成一块。

import { Calculator, Info, ScrollText, ShieldBan, Users } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { PageContent, PageHeader } from "@/components/layout/PageHeader";
import { Tabs, tabId, tabPanelId } from "@/components/ui";
import { useOperatorIdentity } from "@/lib/operator";
import { useOperators } from "@/lib/queries";
import { OperatorsPanel } from "@/features/settings/OperatorsPanel";
import { AuditLogPanel } from "@/features/settings/AuditLogPanel";
import { PricingTemplatePanel } from "@/features/settings/PricingTemplatePanel";
import { VersionPanel } from "@/features/settings/VersionPanel";
import { LocalCachePanel } from "@/features/settings/LocalCachePanel";
import { WordsPage } from "@/features/words/WordsPage";
import { SHOW_ACCOUNT_UI } from "@/lib/featureFlags";

export type SettingsTab =
  | "operators"
  | "pricing"
  | "audit"
  | "words"
  | "about";

export interface SettingsPageProps {
  /** 从标题栏身份按钮跳进来时直接定位到某个 Tab */
  initialTab?: SettingsTab;
}

const SETTINGS_TABS: SettingsTab[] = [
  "operators",
  "pricing",
  "audit",
  "words",
  "about",
];

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

  // 定价模板对所有角色开放（负责人 2026-11-23：学员也要能自己改）。
  // 处理配置已删除，所以这里不再有它的分支。
  const allowPricing = true;
  const activeTab: SettingsTab = (() => {
    if (tab === "pricing" && allowPricing) return "pricing";
    if (tab === "words") return "words";
    if (tab === "about") return "about";
    if (tab === "operators" && SHOW_ACCOUNT_UI) return "operators";
    if (tab === "audit" && canManage) return "audit";
    // 请求的 Tab 不可见时回到一个当前身份一定能看的
    if (SHOW_ACCOUNT_UI) return "operators";
    return canManage ? "audit" : "about";
  })();

  const settingsTabs = [
    ...(SHOW_ACCOUNT_UI
      ? [{ value: "operators", label: canManage ? "账号与权限" : "账号", icon: <Users className="size-3.5" /> }]
      : []),
    // 定价模板：所有人可见。学员用的是自己那份私有模板。
    { value: "pricing", label: "定价模板", icon: <Calculator className="size-3.5" /> },
    ...(canManage
      ? [{ value: "audit", label: "审计日志", icon: <ScrollText className="size-3.5" /> }]
      : []),
    { value: "words", label: "违规词库", icon: <ShieldBan className="size-3.5" /> },
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
          {activeTab === "pricing" && <PricingTemplatePanel />}
          {activeTab === "audit" && canManage && <AuditLogPanel />}
          {activeTab === "words" && <WordsPage embedded />}
          {activeTab === "about" && (
            <div className="flex flex-col gap-4">
              <VersionPanel />
              <LocalCachePanel />
            </div>
          )}
        </div>
      </PageContent>
    </>
  );
}

export default SettingsPage;