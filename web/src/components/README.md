# 基础层组件索引（design-system §5 落地情况）

> 面向业务页开发：只从这里列出的入口 import。所有尺寸/颜色/动效均取自
> `src/styles/tokens.css`（§2.7）与 `tailwind.config.js`（§2.8），不新增魔法值。

## 导入入口

```ts
import { Button, Input, Dialog, toast } from "@/components/ui";
import { AppShell, PageHeader, PageContent } from "@/components/layout";
import { useToast, useMediaQuery, useHotkey } from "@/hooks";
import { api, ApiError, isApiError } from "@/lib/api";
import { formatPrice, formatCount, formatDate, formatRelative } from "@/lib/utils";
import type { Product, Page, ApiErrorPayload } from "@/lib/types";
```

## components/ui

| 文件 | 导出 | 设计文档 |
|---|---|---|
| `button.tsx` | `Button`, `buttonVariants` | §5.1 / §5.0 / §6.2 |
| `icon-button.tsx` | `IconButton` | §5.1 / §5.0 |
| `input.tsx` | `Field`, `Input`, `Textarea`, `NumberInput`, `InlineInput` | §5.2 |
| `select.tsx` | `Select`, `DropdownSelect`, `SearchInput`, `Kbd` | §5.3 / §5.4 / §5.15 |
| `checkbox.tsx` | `Checkbox`, `Radio`, `RadioGroup`, `Switch` | §5.5 |
| `badge.tsx` | `Badge`, `Tag`, `StatusDot`, `StatusBadge`, `productStatusMeta` | §5.6 / §2.4 |
| `card.tsx` | `Card`, `CardHeader/Title/Description/Content/Footer`, `KpiCard` | §5.7 |
| `skeleton.tsx` | `Skeleton`, `SkeletonText`, `SkeletonCard`, `SkeletonCardGrid`, `SkeletonTable`, `SkeletonRows` | §5.14 |
| `empty-state.tsx` | `EmptyState`, `ErrorState`, `InlineError` | §5.14 |
| `dialog.tsx` | `Dialog`, `ConfirmDialog`, `useDialogState` | §5.12 |
| `drawer.tsx` | `Drawer`, `Sheet` | §5.13 |
| `tooltip.tsx` | `Tooltip` | §5.15 |
| `toast.tsx` | `toast`, `AppToaster`, `TOAST_DURATION` | §5.11 |
| `separator.tsx` | `Separator`, `ScrollArea`, `Progress` | §4.4 / §5.15 |
| `tabs.tsx` | `Tabs`, `Popover`, `InverseSurface` | §5.15 |
| `bulk-action-bar.tsx` | `BulkActionBar` | §5.8 |
| `spinner.tsx` | `Spinner` | §5.0 |

## components/layout

| 文件 | 导出 | 设计文档 |
|---|---|---|
| `TitleBar.tsx` | `TitleBar`, `TitleBarBreadcrumb`, `WindowControlButton`, `useIsMaximized` | §5.10 |
| `Sidebar.tsx` | `Sidebar`, `SidebarItem`, `DEFAULT_NAV` | §5.9 |
| `AppShell.tsx` | `AppShell`, `BrandMark`, `GlobalSearchTrigger` | §7.1 |
| `PageHeader.tsx` | `PageHeader`, `PageContent` | §7.1 |

## 业务页挂载方式

`App.tsx` 里已经有 `<Route path="warehouse" element={<PlaceholderPage />} />`，
把 `element` 换成业务页组件即可，页面内部结构：

```tsx
<PageHeader title="成品仓库" actions={<Button>批量上架</Button>} />
<PageContent>{/* 表格/卡片网格 */}</PageContent>
```

## 约定

- 同屏 primary 按钮 ≤ 1（§5.1）；危险确认用 `ConfirmDialog danger` 且默认聚焦取消（§5.12）。
- 状态色只用 `StatusBadge` / `productStatusMeta`，不自行配色（§2.4）。
- 动效只用 `@/lib/motion` 的 token，位移 ≤ 8px（抽屉 ≤ 24px），进入 ≤ 240ms、退出 ≤ 180ms（§6.3）。
- 接口错误分支只看 `error.code`（api.md §0.5），文案用 `apiErrorMessage(error)`。
