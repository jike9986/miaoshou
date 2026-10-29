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

import { useState } from "react";
import { Globe, Image, ScrollText, Users } from "lucide-react";
import { PageContent, PageHeader } from "@/components/layout/PageHeader";
import { Tabs, tabId, tabPanelId } from "@/components/ui";
import { operatorDisplayName, useOperatorIdentity } from "@/lib/operator";
import { OperatorsPanel } from "@/features/settings/OperatorsPanel";
import { AuditLogPanel } from "@/features/settings/AuditLogPanel";
import { PublishSettingsPanel } from "@/features/settings/PublishSettingsPanel";
import { ImageServiceCard } from "@/features/automation/ImageServiceCard";

export type SettingsTab = "operators" | "publish" | "image" | "audit";

export interface SettingsPageProps {
  /** 从标题栏身份按钮跳进来时直接定位到某个 Tab */
  initialTab?: SettingsTab;
}

export function SettingsPage({ initialTab = "operators" }: SettingsPageProps) {
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  // 订阅身份：换了人 / 改了名，标题副文案要跟着变
  const identity = useOperatorIdentity();

  return (
    <>
      <PageHeader
        title="设置"
        description={
          identity
            ? `当前身份：${operatorDisplayName()}`
            : `当前身份：${operatorDisplayName()}（未设置，按本机记账）`
        }
      >
        <Tabs
          value={tab}
          onChange={(value) => setTab(value as SettingsTab)}
          idBase="settings"
          items={[
            { value: "operators", label: "操作员", icon: <Users className="size-3.5" /> },
            { value: "publish", label: "发布", icon: <Globe className="size-3.5" /> },
            { value: "image", label: "图片服务", icon: <Image className="size-3.5" /> },
            { value: "audit", label: "审计日志", icon: <ScrollText className="size-3.5" /> },
          ]}
        />
      </PageHeader>

      <PageContent width="table">
        <div
          role="tabpanel"
          id={tabPanelId("settings", tab)}
          aria-labelledby={tabId("settings", tab)}
          className="contents"
        >
          {tab === "operators" && <OperatorsPanel />}
          {tab === "publish" && <PublishSettingsPanel />}
          {tab === "image" && <ImageServiceCard />}
          {tab === "audit" && <AuditLogPanel />}
        </div>
      </PageContent>
    </>
  );
}

export default SettingsPage;
