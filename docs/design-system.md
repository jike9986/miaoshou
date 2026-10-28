# 奥皇上品 · 桌面端设计系统

> 版本 v1.0 · 适用于 `tk-desktop/web`（React 18 + TypeScript + Tailwind 3.4 + shadcn/ui + Framer Motion 11）
> 定位：电商运营人员的**生产工具**。每天处理数百个商品，界面的核心指标是「扫读速度 / 误操作率 / 长时间使用的眼睛疲劳度」。
> 本文所有数值均为最终实现值，可直接落到 `tokens.css` / `tailwind.config.js` / 组件 `cva` 变体中。

**目录**

| 章节 | 内容 | 产出物 |
|---|---|---|
| [0](#0-落地基线) | 落地基线、与现有脚手架的对齐 | 需改动清单 |
| [1](#1-设计原则) | 设计原则（5 条） | — |
| [2](#2-颜色系统) | 颜色系统、CSS 变量、Tailwind 映射 | `tokens.css`、`tailwind.config.js` |
| [3](#3-排版) | 字体栈、字号阶梯、数字排版 | `fontSize` 覆写 |
| [4](#4-间距圆角阴影) | 间距 / 圆角 / 阴影 / 层级 | `spacing`、`borderRadius`、`boxShadow` |
| [5](#5-组件规格) | 组件规格（class 级）：按钮 / 输入 / 卡片 / 表格 / 导航 / 标题栏 / Toast / 弹窗 / 抽屉 / 空状态 | `components/ui/*` |
| [6](#6-动效规范) | 动效规范、克制动效清单 | `lib/motion.ts` |
| [7](#7-布局规范) | 布局、栅格、断点、窗口 | `components/layout/*` |
| [8](#8-实施与验收清单) | 落地顺序与验收清单 | — |

---

## 0. 落地基线

| 项 | 值 |
|---|---|
| 设计基准分辨率 | 1440 × 900（Windows 100% 缩放） |
| 基准字号 | 14px（桌面密度，不是 Web 营销页的 16px） |
| 基准间距 | 4px |
| 主题策略 | 浅色为唯一主主题，深色以 `.dark` 类扩展，默认不暴露切换入口 |
| 配色策略 | 中性色占屏 ≥ 92%，品牌色 ≤ 6%，状态色 ≤ 2% |
| 圆角策略 | 控件 8px / 卡片 12px / 浮层 16px / 徽标 4px，全局最多 3 种圆角同时出现在同一区域 |
| 图标 | lucide-react，统一 `size-4`（16px）；导航 18px、空状态 32px、徽标内 12px |
| 禁止 | 装饰性渐变（仅骨架屏与图片蒙层允许）、霓虹色、大面积玻璃拟态、装饰性动画 |

需要新增 / 覆盖的文件：

```
web/src/styles/tokens.css        # 新建：全部 CSS 变量 + 基础层 + 工具类（第 2.7 节）
web/src/lib/motion.ts            # 新建：动效 token（第 6.1 节）
web/tailwind.config.js           # 覆盖现存文件：变量 → 原子类映射（第 2.8 节）
web/src/components/ui/*          # 新建：shadcn 组件按第 5 节规格实现
web/src/components/layout/*      # 新建：TitleBar / Sidebar / AppShell / PageHeader
```

### 0.1 与现有脚手架的对齐（必须一并改掉）

当前 `tk-desktop` 已有可运行的 Electron 壳（`app/src/main.ts` 无边框窗口 + 4 个 IPC）与 Tailwind 骨架（`web/tailwind.config.js`），但颜色体系是 `hsl()` 的另一套命名。落地本文时有 5 处冲突，按下表处理，**不要两套 token 并存**：

| 位置 | 现状 | 改为 | 原因 |
|---|---|---|---|
| `web/tailwind.config.js` | 颜色用 `hsl(var(--canvas))` | `rgb(var(--canvas) / <alpha-value>)` | 变量改用 `R G B` 空格分隔，才能支持 `/50` 透明度修饰符 |
| `web/tailwind.config.js` | 色名 `line / ink / ok / warn / bad` | `border / fg / success / warning / danger` | 与 shadcn 命名习惯一致，减少心智负担 |
| `web/tailwind.config.js` | `borderRadius.xl=12 lg=10 md=8 sm=6` | `xs=4 sm=6 md=8 lg=12 xl=16 2xl=20` | 原映射把 `lg` 定为 10px，与卡片 12px 规格冲突 |
| `app/src/main.ts` | `minWidth: 1120`, `backgroundColor: "#f6f8f8"` | `minWidth: 1180`；`backgroundColor: "#F6F8F9"` | 1120 时扣掉侧栏与内边距只剩 840px，装不下表格最小列宽（860px）会横滚；底色与 `--canvas` 一致，避免启动闪色 |
| `app/src/main.ts` | `width: 1440, height: 920` | 保留（设计基准 1440×900，多出的 20px 由标题栏吃掉） | 不改窗口默认尺寸 |

窗口 IPC 已就绪（`app/src/preload.ts` 暴露 `window.desktop.{minimize,toggleMaximize,close,isMaximized,platform}`），标题栏按钮直接调用，无需改动主进程：

```tsx
window.desktop.minimize();
window.desktop.toggleMaximize();
window.desktop.close();
```

窗口按钮的「最大化/还原」图标需跟随状态，订阅方式：

```tsx
// 打开时取一次 + 监听 resize（Electron 最大化会触发）
useEffect(() => {
  const sync = () => window.desktop.isMaximized().then(setMaximized);
  sync();
  window.addEventListener("resize", sync);
  return () => window.removeEventListener("resize", sync);
}, []);
```

---

## 1. 设计原则

**1. 数据优先，界面退后。**
任何装饰都不能压过商品图和数字。页面里最大的字号只给「价格」和「页面标题」，其余一律 ≤ 16px。彩色只用于**动作**和**状态**，不用于分区。

**2. 一屏解决，少跳转。**
高频操作（改价、改库存、改状态、批量上架）在列表页就地完成，用抽屉/浮层承载，不跳新页面。用户完成一次任务的平均点击次数 ≤ 3。

**3. 状态永远可见。**
每个商品、每个任务都有明确状态（待上架 / 上架中 / 已上架 / 已下架 / 失败），用同一套徽标样式；失败必须带原因与「重试」，绝不让用户猜。

**4. 克制的动效。**
动效只服务于三件事：**因果**（点开的东西从点击处长出来）、**层级**（浮层从下方浮起）、**更新**（列表项进出）。不做滚动视差、不做装饰性循环动画、不做数字滚动。

**5. 密度可调，但不牺牲呼吸。**
表格行高、卡片间距按「紧凑 / 标准」两档控制；无论如何，可点击元素之间的间隙 ≥ 4px，点击热区 ≥ 28×28px（触屏 44×44px）；正文行高恒为字号的 1.55–1.6 倍。

---

## 2. 颜色系统

### 2.1 品牌色（青绿，基于现有 #087C72 优化）

原色 `#087C72` = HSL(175°, 88%, 26%)，对白字对比度 **5.08:1**（WCAG AA 通过，但压线），偏暗、饱和度过高，大面积按钮排在一起会显「脏」，且在 11px 徽标文字上余量不足。

优化方向：**饱和度 88% → 83%（−5 个百分点），明度 26% 保持不变，色相 175° → 176°**，得到 `#0B7A72` = HSL(176°, 83%, 26%)，对白字对比度 **5.20:1**。肉眼几乎看不出换了颜色（品牌连续性保住），但主色与白字的余量从 5.08 提到 5.20，11px 徽标文字与 hover 梯度都更稳。改动只发生在饱和度与 1° 色相上，属于「可感知但说不出哪里变了」的安全调整。

| Token | Hex | 用途 |
|---|---|---|
| `--brand-50` | `#EFF8F7` | 选中行底色、标签浅底 |
| `--brand-100` | `#D7EFEC` | 悬停浅底、进度条轨道 |
| `--brand-200` | `#AFDFD9` | 分隔强调、图表次要系列 |
| `--brand-300` | `#7BC8C0` | 深色模式下的浅色文字 |
| `--brand-400` | `#45ABA1` | 深色模式主色候选 |
| `--brand-500` | `#1E8F84` | 图表主系列、链接悬停 |
| **`--brand-600`** | **`#0B7A72`** | **主按钮底色、品牌主色、链接** |
| `--brand-700` | `#096159` | 主按钮 hover |
| `--brand-800` | `#084F49` | 主按钮 active |
| `--brand-900` | `#0A403C` | 深色文字强调 |
| `--brand-950` | `#032724` | 深色模式画布叠加 |

> 主色 `#0B7A72` 只允许出现在：主按钮、当前导航项、选中态、链接、焦点环、进度。**不要**用它做大面积背景或标题色。

### 2.2 中性色阶梯（冷灰，色相 200°，彩度 3–6%）

> 这是**调色参考**，不是要声明的变量：`tokens.css` 只声明 2.3 节的语义 Token，中性色通过语义 Token 间接使用（避免组件里出现 `bg-neutral-100` 这种「知道色号但不知道用途」的写法）。
> 对应关系（新加层级时照此起语义名）：`0/25/50 → surface / surface-hover`，`100 → sidebar / surface-active`，`200 → border`，`300 → border-strong`，`400 → text-disabled`，`600 → text-muted`，`700 → text-secondary`，`900 → inverse-surface`。

| Token | Hex | 用途 |
|---|---|---|
| `--neutral-0` | `#FFFFFF` | 卡片、表格、面板 |
| `--neutral-25` | `#FCFCFD` | 输入框内高光 |
| `--neutral-50` | `#F8FAFB` | 行 hover、卡片 hover |
| `--neutral-100` | `#F1F4F6` | 侧边栏、标题栏、控件 active |
| `--neutral-150` | `#E9EEF1` | 分割线（浅） |
| `--neutral-200` | `#E1E7EB` | 默认边框 |
| `--neutral-300` | `#CBD5DB` | 悬停边框、强分割 |
| `--neutral-400` | `#A7B4BD` | 禁用文字、占位图标 |
| `--neutral-500` | `#7D8C97` | 装饰性图标 |
| `--neutral-600` | `#64737E` | muted 文字 |
| `--neutral-700` | `#48565F` | 次级文字 |
| `--neutral-800` | `#2A343C` | 深色标题（备用） |
| `--neutral-900` | `#191F25` | 反色浮层底色 |
| `--neutral-950` | `#0E1317` | 深色模式画布 |

### 2.3 语义 Token（浅色主题，唯一实现来源）

| CSS 变量 | Hex | 对比度（对 #FFF） | 用途 |
|---|---|---|---|
| `--canvas` | `#F6F8F9` | — | 应用内容区底色 |
| `--canvas-inset` | `#EFF3F4` | — | 凹槽区、分组底、表头底、空状态图标底 |
| `--sidebar` | `#F1F4F6` | — | 左侧边栏 + 标题栏 |
| `--surface` | `#FFFFFF` | — | 卡片、表格、面板、输入框 |
| `--surface-hover` | `#F8FAFB` | — | 卡片/行 hover |
| `--surface-active` | `#F1F4F6` | — | 控件按下、ghost hover |
| `--elevated` | `#FFFFFF` | — | 弹窗、下拉、抽屉、Toast（配 shadow-3） |
| `--overlay` | `rgba(12,20,24,0.45)` | — | 模态遮罩（唯一带 alpha 的颜色变量，配 2px 背景模糊） |
| `--border-subtle` | `#EDF1F3` | — | 表格行线、卡片内分割 |
| `--border` | `#E1E7EB` | — | 控件/卡片默认边框 |
| `--border-strong` | `#CBD5DB` | — | hover 边框、强调分割 |
| `--text-primary` | `#11181C` | **17.9:1** | 标题、正文、表格主信息、价格 |
| `--text-secondary` | `#48565F` | **7.6:1** | 次级正文、导航文字、表头 |
| `--text-muted` | `#64737E` | **4.9:1** | 辅助说明、时间、SKU、占位符 |
| `--text-disabled` | `#A7B4BD` | **2.1:1** | 仅禁用态（不承担信息） |
| `--text-inverse` | `#FFFFFF` | — | 深底反白文字 |
| `--inverse-surface` | `#191F25` | — | 反色浮层底：Tooltip、批量操作栏 |
| `--ring` | `rgb(11 122 114 / 0.32)` | — | 焦点环（变量存 `11 122 114`） |
| `--ring-danger` | `rgb(192 54 75 / 0.30)` | — | 危险动作焦点环（变量存 `192 54 75`） |

**使用规则**
- 正文永远用 `--text-primary` 或 `--text-secondary`；`--text-muted` 仅用于 ≥12px 的辅助信息。
- 卡片叠卡片时，内层用 `--canvas-inset` 而不是再加白底 + 阴影。
- 1px 边框优先于阴影表达层级；阴影只给真正浮起的层。

### 2.4 状态色

| 语义 | Token | Hex | 浅底 | 深字 | 用途 |
|---|---|---|---|---|---|
| 成功 | `--success-500` | `#12996A` | `#EAF7F0` | `#086646` | 上架成功、任务完成 |
| 成功 | `--success-600` | `#0B7F57` | `#D2EEDF` | `#086646` | 成功图标/文字（5.0:1，白底） |
| 警告 | `--warning-500` | `#D97706` | `#FFF8EB` | `#92400E` | 审核中、库存偏低 |
| 警告 | `--warning-600` | `#B45309` | `#FEEDD0` | `#92400E` | 警告文字（5.0:1，白底） |
| 危险 | `--danger-500` | `#D24258` | `#FDF2F4` | `#A22A3E` | 删除、失败 |
| 危险 | `--danger-600` | `#C0364B` | `#FAE0E4` | `#A22A3E` | 危险按钮（5.4:1，白底） |
| 信息 | `--info-500` | `#3B82F6` | `#EFF6FF` | `#1A5BBF` | 同步中、提示 |
| 信息 | `--info-600` | `#1F6FEB` | `#DBEAFE` | `#1A5BBF` | 信息文字（4.64:1） |

状态色**只用于**：徽标、图标、左侧 2px 指示条、输入框错误边框。**不用**于整块背景（浅底除外，且必须是 `*-50`）。

业务状态映射（全应用统一，不允许各页面自定义）：

| 业务状态 | 徽标色 | 点色 |
|---|---|---|
| 待上架 | 中性（`--canvas-inset` 底 / `--text-secondary` 字） | `#A7B4BD` |
| 上架中 / 审核中 | 警告浅底 | `#D97706`（呼吸动画，仅此一处允许循环） |
| 已上架 / 在售 | 成功浅底 | `#12996A` |
| 已下架 / 草稿 | 中性 | `#7D8C97` |
| 失败 / 驳回 | 危险浅底 | `#D24258` |
| 同步中 | 信息浅底 | `#3B82F6`（点旋转） |

### 2.5 图表色（顺序取用，不跳色）

`#0B7A72` → `#1F6FEB` → `#B45309` → `#7C5CD6` → `#0B7F57` → `#C0364B` → `#0E7490` → `#64737E`

- 对应变量 `--chart-1` … `--chart-8`（已写入 `:root` 与 `tailwind.config.js` 的 `colors.chart`）；图表里用 `text-chart-1` / `stroke-chart-1`，或直接读 `rgb(var(--chart-1))`。
- 折线/柱状按顺序取前 N 个颜色，**不跳色、不重复**；超过 8 个系列时合并为「其他」（`#64737E`）。
- 深色模式下同一组色值仍可辨认（已按 4.5:1 对 `#131A1E` 校核），无需另设一套。

### 2.6 深色扩展（`.dark`，先留接口，v1 不开放切换）

| 变量 | 值 |
|---|---|
| `--canvas` | `#0C1013` |
| `--canvas-inset` | `#090C0E` |
| `--sidebar` | `#10161A` |
| `--surface` | `#131A1E` |
| `--surface-hover` | `#192227` |
| `--surface-active` | `#1F2A30` |
| `--elevated` | `#1A2226` |
| `--border-subtle` | `#1E272C` |
| `--border` | `#2A353B` |
| `--border-strong` | `#3B4950` |
| `--text-primary` | `#E7EDF0` |
| `--text-secondary` | `#A9B8C1` |
| `--text-muted` | `#7E8F99` |
| `--text-disabled` | `#4C5B64` |
| `--text-inverse` | `#06201D` |
| `--inverse-surface` | `#263037`（CSS 变量写 `38 48 55`） |
| `--brand-600` | `#38BFB0`（深色下作为浅色块，按钮文字用 `--text-inverse` = `#06201D`） |
| `--overlay` | `rgba(0,0,0,0.62)` |
| `--shadow-1` | `0 1px 2px rgba(0,0,0,0.40)` |
| `--shadow-2` | `0 6px 16px -4px rgba(0,0,0,0.52)` |
| `--shadow-3` | `0 20px 48px -12px rgba(0,0,0,0.68)` |

### 2.7 `tokens.css`（完整，直接可用）

```css
/* web/src/styles/tokens.css */
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :root {
    /* ========== 背景层级 ========== */
    --canvas: 246 248 249;        /* #F6F8F9 */
    --canvas-inset: 239 243 244;  /* #EFF3F4 */
    --sidebar: 241 244 246;       /* #F1F4F6 */
    --surface: 255 255 255;       /* #FFFFFF */
    --surface-hover: 248 250 251; /* #F8FAFB */
    --surface-active: 241 244 246;/* #F1F4F6 */
    --elevated: 255 255 255;      /* #FFFFFF */
    --overlay: rgba(12, 20, 24, 0.45); /* 唯一带 alpha 的变量：遮罩不做透明度修饰 */

    /* ========== 边框 ========== */
    --border-subtle: 237 241 243; /* #EDF1F3 */
    --border: 225 231 235;        /* #E1E7EB */
    --border-strong: 203 213 219; /* #CBD5DB */

    /* ========== 文字 ========== */
    --text-primary: 17 24 28;     /* #11181C  17.9:1 */
    --text-secondary: 72 86 95;   /* #48565F   7.6:1 */
    --text-muted: 100 115 126;    /* #64737E   4.9:1 */
    --text-disabled: 167 180 189; /* #A7B4BD   2.1:1 */
    --text-inverse: 255 255 255;  /* #FFFFFF */
    /* 反色浮层底（Tooltip / 批量操作栏 / 深色提示条） */
    --inverse-surface: 25 31 37;  /* #191F25 */

    /* ========== 品牌 ========== */
    --brand-50: 239 248 247;   /* #EFF8F7 */
    --brand-100: 215 239 236;  /* #D7EFEC */
    --brand-200: 175 223 217;  /* #AFDFD9 */
    --brand-300: 123 200 192;  /* #7BC8C0 */
    --brand-400: 69 171 161;   /* #45ABA1 */
    --brand-500: 30 143 132;   /* #1E8F84 */
    --brand-600: 11 122 114;   /* #0B7A72  主色 */
    --brand-700: 9 97 89;      /* #096159 */
    --brand-800: 8 79 73;      /* #084F49 */
    --brand-900: 10 64 60;     /* #0A403C */
    --brand-950: 3 39 36;      /* #032724 */

    /* ========== 状态 ========== */
    --success-50: 234 247 240;  /* #EAF7F0 */
    --success-100: 210 238 223; /* #D2EEDF */
    --success-500: 18 153 106;  /* #12996A */
    --success-600: 11 127 87;   /* #0B7F57 */
    --success-700: 8 102 70;    /* #086646 */

    --warning-50: 255 248 235;  /* #FFF8EB */
    --warning-100: 254 237 208; /* #FEEDD0 */
    --warning-500: 217 119 6;   /* #D97706 */
    --warning-600: 180 83 9;    /* #B45309 */
    --warning-700: 146 64 14;   /* #92400E */

    --danger-50: 253 242 244;   /* #FDF2F4 */
    --danger-100: 250 224 228;  /* #FAE0E4 */
    --danger-500: 210 66 88;    /* #D24258 */
    --danger-600: 192 54 75;    /* #C0364B */
    --danger-700: 162 42 62;    /* #A22A3E */

    --info-50: 239 246 255;     /* #EFF6FF */
    --info-100: 219 234 254;    /* #DBEAFE */
    --info-500: 59 130 246;     /* #3B82F6 */
    --info-600: 31 111 235;     /* #1F6FEB */
    --info-700: 26 91 191;      /* #1A5BBF */

    /* ========== 图表 ========== */
    --chart-1: 11 122 114;
    --chart-2: 31 111 235;
    --chart-3: 180 83 9;
    --chart-4: 124 92 214;
    --chart-5: 11 127 87;
    --chart-6: 192 54 75;
    --chart-7: 14 116 144;
    --chart-8: 100 115 126;

    /* ========== 焦点环 ========== */
    --ring: 11 122 114;           /* 用法：rgb(var(--ring) / 0.32) */
    --ring-danger: 192 54 75;     /* 用法：rgb(var(--ring-danger) / 0.30) */

    /* ========== 圆角 ========== */
    --radius-xs: 4px;
    --radius-sm: 6px;
    --radius-md: 8px;
    --radius-lg: 12px;
    --radius-xl: 16px;
    --radius-2xl: 20px;

    /* ========== 阴影 ========== */
    --shadow-1: 0 1px 2px rgba(16, 24, 32, 0.06), 0 1px 1px rgba(16, 24, 32, 0.04);
    --shadow-2: 0 4px 12px -2px rgba(16, 24, 32, 0.10), 0 2px 4px -2px rgba(16, 24, 32, 0.06);
    --shadow-3: 0 16px 40px -8px rgba(16, 24, 32, 0.18), 0 4px 12px -4px rgba(16, 24, 32, 0.08);
    --shadow-focus: 0 0 0 2px #FFFFFF, 0 0 0 4px rgba(11, 122, 114, 0.32);

    /* ========== 动效 ========== */
    --dur-instant: 90ms;
    --dur-fast: 120ms;
    --dur-exit: 140ms;
    --dur-card: 160ms;
    --dur-base: 180ms;
    --dur-image: 200ms;
    --dur-medium: 240ms;
    --dur-slow: 320ms;
    --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
    --ease-in: cubic-bezier(0.4, 0, 1, 1);
    --ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);
    --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);

    /* ========== 层级 ========== */
    --z-sticky: 10;
    --z-titlebar: 30;
    --z-dropdown: 40;
    --z-overlay: 50;
    --z-modal: 60;
    --z-toast: 70;
    --z-tooltip: 80;

    /* ========== 布局 ========== */
    --titlebar-h: 40px;
    --sidebar-w: 232px;
    --sidebar-w-collapsed: 64px;
    --content-pad: 24px;
    --content-max: 1600px;
    --form-max: 880px;

    /* ========== 字体 ========== */
    --font-sans: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei",
      "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC",
      "Noto Sans CJK SC", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Mono",
      Consolas, monospace;

    color-scheme: light;
  }

  .dark {
    --canvas: 12 16 19;
    --canvas-inset: 9 12 14;
    --sidebar: 16 22 26;
    --surface: 19 26 30;
    --surface-hover: 25 34 39;
    --surface-active: 31 42 48;
    --elevated: 26 34 38;
    --overlay: rgba(0, 0, 0, 0.62);

    --border-subtle: 30 39 44;
    --border: 42 53 59;
    --border-strong: 59 73 80;

    --text-primary: 231 237 240;
    --text-secondary: 169 184 193;
    --text-muted: 126 143 153;
    --text-disabled: 76 91 100;
    --text-inverse: 6 32 29;
    --inverse-surface: 38 48 55;  /* 深色下的反色浮层：比 elevated 再亮一档 */

    --brand-600: 56 191 176;  /* #38BFB0 */
    --brand-700: 76 208 194;  /* 深色下 hover 变亮 */
    --brand-800: 98 222 209;
    --brand-50: 12 40 38;

    --shadow-1: 0 1px 2px rgba(0, 0, 0, 0.40);
    --shadow-2: 0 6px 16px -4px rgba(0, 0, 0, 0.52);
    --shadow-3: 0 20px 48px -12px rgba(0, 0, 0, 0.68);
    --shadow-focus: 0 0 0 2px #131A1E, 0 0 0 4px rgba(56, 191, 176, 0.40);

    color-scheme: dark;
  }

  * {
    border-color: rgb(var(--border));
  }

  html,
  body,
  #root {
    height: 100%;
    overflow: hidden; /* 桌面应用：滚动只发生在内容区 */
  }

  body {
    background-color: rgb(var(--canvas));
    color: rgb(var(--text-primary));
    font-family: var(--font-sans);
    font-size: 14px;
    line-height: 22px;
    /* Windows 下不要用 -webkit-font-smoothing: antialiased，会让中文字重变细 */
    font-synthesis: none;
    text-rendering: optimizeSpeed;
  }

  ::selection {
    background: rgb(var(--brand-600) / 0.16);
    color: rgb(var(--text-primary));
  }

  /* 滚动条：12px 轨道，8px 滑块，悬停加深 */
  * {
    scrollbar-width: thin;
    scrollbar-color: rgb(var(--border-strong)) transparent;
  }
  ::-webkit-scrollbar {
    width: 12px;
    height: 12px;
  }
  ::-webkit-scrollbar-track {
    background: transparent;
  }
  ::-webkit-scrollbar-thumb {
    background-color: rgb(var(--border-strong));
    border: 3px solid transparent;
    background-clip: content-box;
    border-radius: 9999px;
  }
  ::-webkit-scrollbar-thumb:hover {
    background-color: rgb(var(--text-disabled));
    background-clip: content-box;
  }
  ::-webkit-scrollbar-corner {
    background: transparent;
  }
}

@layer utilities {
  .drag-region {
    -webkit-app-region: drag;
    user-select: none;
  }
  .no-drag {
    -webkit-app-region: no-drag;
  }
  .num {
    font-variant-numeric: tabular-nums;
    font-feature-settings: "tnum" 1, "zero" 1;
  }
  /* 下拉/Popover 打开：不引入 tailwindcss-animate 也能用 */
  .anim-pop-in {
    animation: pop-in var(--dur-fast) var(--ease-out) both;
  }
  .anim-fade-in {
    animation: fade-in var(--dur-base) var(--ease-out) both;
  }
  .anim-fade-out {
    animation: fade-out var(--dur-fast) var(--ease-in) both;
  }
  /* 骨架屏：统一入口，颜色取自 Token */
  .skeleton {
    border-radius: 3px;
    background-image: linear-gradient(
      90deg,
      rgb(var(--canvas-inset)) 25%,
      rgb(var(--surface-hover)) 37%,
      rgb(var(--canvas-inset)) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.6s linear infinite;
  }
}

@keyframes pop-in {
  from { opacity: 0; transform: scale(0.97) translateY(-2px); }
  to   { opacity: 1; transform: scale(1) translateY(0); }
}
@keyframes fade-in {
  from { opacity: 0; }
  to   { opacity: 1; }
}
@keyframes fade-out {
  from { opacity: 1; }
  to   { opacity: 0; }
}

/* 骨架屏微光 */
@keyframes shimmer {
  0%   { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
/* 仅「审核中」状态点允许的循环动画 */
@keyframes status-breathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.45; transform: scale(0.86); }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

### 2.8 `tailwind.config.js` 映射（覆盖现有文件）

> 现有文件是 ESM 的 `.js`（`export default` + `package.json` 无 `"type": "commonjs"` 冲突），因此保持 `.js` 而非改成 `.ts`，避免动构建链路。

```js
// web/tailwind.config.js —— 整体替换
/** @type {import('tailwindcss').Config} */

const rgb = (v) => `rgb(var(${v}) / <alpha-value>)`;

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: { DEFAULT: rgb("--canvas"), inset: rgb("--canvas-inset") },
        sidebar: rgb("--sidebar"),
        surface: {
          DEFAULT: rgb("--surface"),
          hover: rgb("--surface-hover"),
          active: rgb("--surface-active"),
        },
        elevated: rgb("--elevated"),
        inverse: rgb("--inverse-surface"),
        /* 遮罩：变量自带 alpha，直接透传 */
        overlay: "var(--overlay)",
        border: {
          DEFAULT: rgb("--border"),
          subtle: rgb("--border-subtle"),
          strong: rgb("--border-strong"),
        },
        /* 文字：text-fg / text-fg-secondary / text-fg-muted ... */
        fg: {
          DEFAULT: rgb("--text-primary"),
          secondary: rgb("--text-secondary"),
          muted: rgb("--text-muted"),
          disabled: rgb("--text-disabled"),
          inverse: rgb("--text-inverse"),
        },
        brand: {
          50: rgb("--brand-50"),
          100: rgb("--brand-100"),
          200: rgb("--brand-200"),
          300: rgb("--brand-300"),
          400: rgb("--brand-400"),
          500: rgb("--brand-500"),
          600: rgb("--brand-600"),
          700: rgb("--brand-700"),
          800: rgb("--brand-800"),
          900: rgb("--brand-900"),
          950: rgb("--brand-950"),
        },
        success: {
          50: rgb("--success-50"),
          100: rgb("--success-100"),
          500: rgb("--success-500"),
          600: rgb("--success-600"),
          700: rgb("--success-700"),
        },
        warning: {
          50: rgb("--warning-50"),
          100: rgb("--warning-100"),
          500: rgb("--warning-500"),
          600: rgb("--warning-600"),
          700: rgb("--warning-700"),
        },
        danger: {
          50: rgb("--danger-50"),
          100: rgb("--danger-100"),
          500: rgb("--danger-500"),
          600: rgb("--danger-600"),
          700: rgb("--danger-700"),
        },
        info: {
          50: rgb("--info-50"),
          100: rgb("--info-100"),
          500: rgb("--info-500"),
          600: rgb("--info-600"),
          700: rgb("--info-700"),
        },
        chart: {
          1: rgb("--chart-1"),
          2: rgb("--chart-2"),
          3: rgb("--chart-3"),
          4: rgb("--chart-4"),
          5: rgb("--chart-5"),
          6: rgb("--chart-6"),
          7: rgb("--chart-7"),
          8: rgb("--chart-8"),
        },
      },
      fontFamily: {
        sans: [
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Microsoft YaHei",
          "PingFang SC",
          "Hiragino Sans GB",
          "Source Han Sans SC",
          "Noto Sans CJK SC",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "JetBrains Mono",
          "Cascadia Mono",
          "Consolas",
          "monospace",
        ],
      },
      fontSize: {
        display: ["32px", { lineHeight: "40px", fontWeight: "700", letterSpacing: "-0.02em" }],
        h1: ["24px", { lineHeight: "32px", fontWeight: "600", letterSpacing: "-0.01em" }],
        title: ["18px", { lineHeight: "26px", fontWeight: "600" }],
        h2: ["20px", { lineHeight: "28px", fontWeight: "600", letterSpacing: "-0.005em" }],
        h3: ["16px", { lineHeight: "24px", fontWeight: "600" }],
        /* 覆盖 Tailwind 默认：sm 13/20，base 14/22 */
        base: ["14px", { lineHeight: "22px" }],
        sm: ["13px", { lineHeight: "20px" }],
        xs: ["12px", { lineHeight: "18px" }],
        "2xs": ["11px", { lineHeight: "16px", letterSpacing: "0.02em" }],
        /* 数字专用：与 .num 搭配 */
        "num-lg": ["28px", { lineHeight: "36px", fontWeight: "600", letterSpacing: "-0.01em" }],
      },
      /* 下列间距与 Tailwind 默认刻度完全一致，写出来只为锁定契约；
         若将来 Tailwind 改默认值，这里会把它钉回 4px 基准 */
      spacing: {
        px: "1px",
        0.5: "2px",
        1: "4px",
        1.5: "6px",
        2: "8px",
        2.5: "10px",
        3: "12px",
        3.5: "14px",
        4: "16px",
        5: "20px",
        6: "24px",
        7: "28px",
        8: "32px",
        10: "40px",
        12: "48px",
        16: "64px",
        20: "80px",
      },
      borderRadius: {
        xs: "4px",
        sm: "6px",
        md: "8px",
        lg: "12px",
        xl: "16px",
        "2xl": "20px",
      },
      /* 本文用到的透明度修饰符（/32、/45、/22…）不在 Tailwind 默认刻度里，
         必须在此扩展，否则 bg-brand-600/32、bg-white/92 会被丢弃 */
      opacity: {
        2: "0.02",
        4: "0.04",
        6: "0.06",
        8: "0.08",
        12: "0.12",
        15: "0.15",
        16: "0.16",
        18: "0.18",
        22: "0.22",
        24: "0.24",
        26: "0.26",
        32: "0.32",
        36: "0.36",
        45: "0.45",
        55: "0.55",
        65: "0.65",
        85: "0.85",
        92: "0.92",
      },
      boxShadow: {
        1: "var(--shadow-1)",
        2: "var(--shadow-2)",
        3: "var(--shadow-3)",
        focus: "var(--shadow-focus)",
      },
      screens: {
        xs: "375px",
        xs2: "414px",
        sm: "640px",
        md: "768px",
        lg: "1024px",
        xl: "1280px",
        "2xl": "1536px",
        "3xl": "1680px",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.16, 1, 0.3, 1)",
        in: "cubic-bezier(0.4, 0, 1, 1)",
        "in-out": "cubic-bezier(0.4, 0, 0.2, 1)",
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      transitionDuration: {
        instant: "90ms",
        fast: "120ms",
        exit: "140ms",
        card: "160ms",
        base: "180ms",
        image: "200ms",
        medium: "240ms",
        slow: "320ms",
      },
      keyframes: {
        shimmer: {
          "0%": { backgroundPosition: "100% 50%" },
          "100%": { backgroundPosition: "0% 50%" },
        },
        "status-breathe": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.45", transform: "scale(0.86)" },
        },
        spin: {
          to: { transform: "rotate(360deg)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s linear infinite",
        "status-breathe": "status-breathe 1.8s ease-in-out infinite",
        spin: "spin 0.6s linear infinite",
      },
    },
  },
  // ESM 下不能用 require()。若需要 tailwindcss-animate，先
  // pnpm add -D tailwindcss-animate，再改为：
  //   import animate from "tailwindcss-animate";
  //   plugins: [animate],
  plugins: [],
};
```

---

## 3. 排版

### 3.1 字体栈

```css
--font-sans: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei",
             "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC",
             "Noto Sans CJK SC", sans-serif;
--font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Mono",
             Consolas, monospace;
```

- Windows 优先命中 `Microsoft YaHei UI`（由 `Microsoft YaHei` 自动回退），macOS 命中 `PingFang SC`。
- **不要**引入 Web 字体做正文（首屏闪字 + 包体增大）；仅在需要品牌标题时按需子集化。
- 中英文混排：不额外加空格，不手动断词，交给浏览器。
- 全局 `font-synthesis: none`，禁止合成粗体/斜体。

### 3.2 字号阶梯

| Token | 字号 / 行高 | 字重 | 字距 | 用途 | Tailwind |
|---|---|---|---|---|---|
| `display` | 32 / 40 | 700 | -0.02em | 数据看板大数字、引导页标题 | `text-display` |
| `h1` | 24 / 32 | 600 | -0.01em | 页面主标题（极少用） | `text-h1` |
| `h2` | 20 / 28 | 600 | -0.005em | 区块标题、弹窗大标题 | `text-h2` |
| `title` | 18 / 26 | 600 | 0 | **页面头标题**（所有业务页默认） | `text-title` |
| `h3` | 16 / 24 | 600 | 0 | 卡片标题、弹窗标题、抽屉标题、商品卡价格 | `text-h3` |
| `body` | 14 / 22 | 400 | 0 | 正文、表格内容、表单值 | `text-base` |
| `body-strong` | 14 / 22 | 500 | 0 | 表格主列、导航项、按钮 | `text-base font-medium` |
| `sm` | 13 / 20 | 400 | 0 | 卡片标题、卡片描述、次级信息 | `text-sm` |
| `label` | 13 / 18 | 500 | 0 | 表单标签 | `text-sm font-medium leading-[18px]` |
| `xs` | 12 / 18 | 400 | 0 | 时间、辅助说明、Toast 描述 | `text-xs` |
| `2xs` | 11 / 16 | 500 | 0.02em | 徽标、SKU、分组标题、极密表格 | `text-2xs` |
| `num-lg` | 28 / 36 | 600 | -0.01em | KPI 大数字、合计金额 | `text-num-lg` |
| `num-md` | 16 / 24 | 600 | 0 | 商品卡价格 | `text-h3 font-semibold` |
| `num-sm` | 13 / 20 | 500 | 0 | 表格内数字、库存 | `text-sm font-medium` |

> 所有 `num-*` 用法必须同时加 `.num` 工具类（等宽数字）；`num-lg` / `num-md` / `num-sm` 三个 Token 只约束字号行高字重，不负责等宽。

> **Tailwind 刻度已本地化**：`text-sm` = 13/20、`text-base` = 14/22（见 2.8 节 `fontSize` 覆写）。因此组件类里直接写 `text-sm` / `text-base` 即符合本表；需要 15px 这类介于刻度之间的字号时，先加 Token（例：`title-sm: 15px/22`），不要写 `text-[15px]`。

**字号上限**：除 `display` / `num-lg` / `h1`，任何文字不超过 24px。同一页面最多同时出现 5 种字号（典型组合：18 标题 / 16 小标题 / 14 正文 / 13 辅助 / 11 徽标）。

### 3.3 数字排版（价格是核心信息）

- 所有价格、库存、销量、百分比、时间戳：`font-variant-numeric: tabular-nums`（工具类 `.num`），保证列对齐。
- 价格统一格式：货币符号与数字之间无空格，符号降一档字号。符号由店铺区域决定，不要硬编码 `¥`：
  ```tsx
  <span className="num text-h3 font-semibold text-fg">
    <span className="mr-0.5 text-xs font-medium">{currency}</span>129.00
  </span>
  ```
  区域 → 符号：`MY → RM`、`TH → ฿`、`ID → Rp`、`VN → ₫`、`PH → ₱`、`SG → S$`、`US/GB → $ / £`。
  金额本身按区域习惯格式化（`Intl.NumberFormat(locale, { minimumFractionDigits: 2 })`），小数位数不同不额外处理。
- 价格区间（`price_min ≠ price_max`，商品模型的常见情况）：显示 `RM12.90 – RM35.00`，中间用 en dash 两侧各 1 个半角空格；只有价差 > 0 时才显示区间，相同时只显示单值。
- 划原价（改价对比）：`text-xs text-fg-muted line-through num`，与现价基线对齐、间距 6px。
- 大数字（≥ 10000）用千分位；金额保留 2 位小数（`Rp`/`₫` 按当地习惯取 0 位），库存整数。
- 涨跌用 `+/-` 符号 + 状态色，不只靠颜色（色盲可用）：`+12.4%` 配 `ArrowUpRight` 14px。

### 3.4 文本规则

| 场景 | 规则 |
|---|---|
| 商品标题 | 13/20（`text-sm font-medium`），`line-clamp-2`，固定 2 行高度（40px），不因标题长度改变卡片高度 |
| SKU / ID | 11/16（`text-2xs font-mono text-fg-muted`），单行 `truncate` |
| 表格单元格 | 单行 `truncate`，超过时 `title` 属性 + 悬停 300ms Tooltip |
| 长描述 | `line-clamp-3`，配「展开」文字按钮 |
| 空值 | 统一显示 `—`（em dash），不用「无 / null / -」 |
| 数字单位 | 中文与数字间不加空格：「12 件」写成「12件」，价格除外 |

---

## 4. 间距、圆角、阴影

### 4.1 间距阶梯（4px 基准）

| Token | 值 | 典型用途 |
|---|---|---|
| `0.5` | 2px | 图标与文字微调、徽标内边距 |
| `1` | 4px | 图标与文字间距（紧凑）、标签组内间隙 |
| `1.5` | 6px | 徽标内边距、行内元素间隙 |
| `2` | 8px | 按钮组间隙、图标按钮内边距、表单行内间隙 |
| `2.5` | 10px | 导航项左右内边距 |
| `3` | 12px | 表格单元格水平内边距、卡片内边距（紧凑） |
| `3.5` | 14px | 输入框水平内边距 |
| `4` | 16px | 卡片内边距、卡片间距、表单字段间距 |
| `5` | 20px | 区块内间距 |
| `6` | 24px | 内容区外边距、抽屉内边距、弹窗内边距 |
| `8` | 32px | 大区块分隔、页面顶部留白 |
| `10` | 40px | 空状态上下留白 |
| `12` | 48px | 引导页区块 |
| `16` | 64px | 极少数大留白 |
| `20` | 80px | 引导页 |

**垂直节奏**：表单字段之间 16px；字段标签与控件之间 6px；分组之间 24px；页面 header 与内容之间 16px。

### 4.2 圆角

| Token | 值 | 用于 |
|---|---|---|
| `xs` | 4px | 徽标、小标签、内嵌色块、复选框 |
| `sm` | 6px | 小按钮（sm）、下拉项、键盘按键提示 |
| `md` | 8px | **默认**：按钮（md）、输入框、下拉触发器、导航项 |
| `lg` | 12px | 卡片、表格容器、Toast、提示条 |
| `xl` | 16px | 弹窗、抽屉、大面积面板 |
| `2xl` | 20px | 仅引导页 |
| `full` | 9999px | 头像、状态点、进度条、开关 |

规则：**同心圆角**——内层圆角 = 外层圆角 − 内边距（如 12px 卡片 + 12px 内边距，内部图片圆角取 0，用 `overflow-hidden` 裁切）。

### 4.3 阴影（3 档 + 焦点环）

| Token | 值 | 用于 |
|---|---|---|
| `shadow-1` | `0 1px 2px rgba(16,24,32,.06), 0 1px 1px rgba(16,24,32,.04)` | 卡片默认、激活的导航项、吸顶表头（滚动后） |
| `shadow-2` | `0 4px 12px -2px rgba(16,24,32,.10), 0 2px 4px -2px rgba(16,24,32,.06)` | 卡片 hover、下拉面板、Popover |
| `shadow-3` | `0 16px 40px -8px rgba(16,24,32,.18), 0 4px 12px -4px rgba(16,24,32,.08)` | 弹窗、抽屉、Toast |
| `shadow-focus` | `0 0 0 2px #FFF, 0 0 0 4px rgba(11,122,114,.32)` | 键盘焦点环（等价 `ring-2 ring-brand-600/32 ring-offset-2`） |

规则：**同一屏内同时可见的阴影层级不超过 2 档**；静态元素不加阴影；不要在 `shadow-1` 的卡片上再叠 `shadow-2` 的内层卡片。

### 4.4 边框与分割线

- 默认边框 `1px solid var(--border)`；表格行线用 `--border-subtle`。
- 禁止左侧彩色粗边（>2px）装饰卡片；状态指示条统一 2px。
- 分割线：水平 `h-px bg-border-subtle`；垂直 `w-px bg-border-subtle`；两侧留 0（通栏）而非缩进。

### 4.5 层级

`sticky 10 → titlebar 30 → dropdown 40 → overlay 50 → modal 60 → toast 70 → tooltip 80`

> Tailwind 3 的 `z-` 默认刻度只有 `0/10/20/30/40/50`，本文的 60/70/80 **必须写成任意值** `z-[60]` / `z-[70]` / `z-[80]`，否则类名不生成、浮层会被遮罩盖住。已在 `tokens.css` 中定义 `--z-*` 变量，也可用 `z-[var(--z-modal)]`。
> 层级只在需要跨越堆叠上下文时声明；同一区域内的元素不要靠 `z-index` 互相压盖，改用 DOM 顺序。

---

## 5. 组件规格

### 5.0 通用状态约定

| 状态 | 处理 |
|---|---|
| hover | 只改 `background-color` / `border-color`，**不改尺寸**；时长 120ms |
| active | `scale(0.98)` + 底色加深一档；90ms |
| focus-visible | `ring-2 ring-brand-600/32 ring-offset-2 ring-offset-surface`；**只在键盘操作时出现**（`:focus-visible`） |
| disabled | `opacity: 0.5` + `pointer-events: none`；主按钮额外把底色降到 `brand-600/45` |
| loading | 左侧 spinner 14px（`size-3.5 rounded-full border-2 border-brand-600/25 border-t-brand-600 animate-spin`，600ms linear），文字不变，宽度不变（用 `min-w` 锁死） |
| 点击热区 | 图标按钮最小 28×28，触屏 44×44（用 `::after` 扩热区，不放大视觉） |

### 5.1 按钮

```tsx
// web/src/components/ui/button.tsx
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn"; // clsx + tailwind-merge；仓库还没有就新建：
// export const cn = (...i: ClassValue[]) => twMerge(clsx(i));

export const buttonVariants = cva(
  [
    "inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-medium",
    "transition-[background-color,border-color,color,box-shadow,transform]",
    "duration-fast ease-in-out active:scale-[0.98] active:duration-instant",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600/32",
    "focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
    "disabled:pointer-events-none disabled:opacity-50",
    "[&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        /* 图标尺寸随尺寸档位变化，见 size 变体里的 [&_svg]:size-* */
        primary: [
          "bg-brand-600 text-white shadow-1",
          "hover:bg-brand-700 active:bg-brand-800",
          "disabled:bg-brand-600/45 disabled:shadow-none",
        ].join(" "),
        secondary: [
          "border border-border bg-surface text-fg shadow-1",
          "hover:border-border-strong hover:bg-surface-hover",
          "active:bg-surface-active",
        ].join(" "),
        ghost: [
          "bg-transparent text-fg-secondary",
          "hover:bg-surface-active hover:text-fg",
          "active:bg-border-subtle",
        ].join(" "),
        danger: [
          "bg-danger-600 text-white shadow-1",
          "hover:bg-danger-700 active:bg-danger-700",
          "focus-visible:ring-danger-600/30",
          "disabled:bg-danger-600/45 disabled:shadow-none",
        ].join(" "),
      },
      size: {
        /* 高度沿用 Tailwind 的 h-7=28 / h-9=36 / h-11=44 */
        sm: "h-7 rounded-sm px-2.5 text-sm gap-1.5 [&_svg]:size-3.5",
        md: "h-9 rounded-md px-3.5 text-base gap-2 [&_svg]:size-4",
        lg: "h-11 rounded-[10px] px-5 text-base gap-2 [&_svg]:size-[18px]",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading, disabled, children, ...props }, ref) => (
    <button
      ref={ref}
      type="button"
      aria-busy={loading || undefined}
      disabled={disabled || loading}
      className={cn(
        buttonVariants({ variant, size }),
        /* 只在 loading 时锁宽，避免按钮从"保存"跳到"保存中..."引起布局抖动；
           常规状态不锁宽，否则 sm 尺寸的行内按钮会被撑到 72px */
        loading && "min-w-[96px]",
        className
      )}
      {...props}
    >
      {loading && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
      {children}
    </button>
  )
);
Button.displayName = "Button";
```

**各状态色值（浅色）**

| 变体 | 默认 | hover | active | disabled | 文字 |
|---|---|---|---|---|---|
| primary | `#0B7A72` | `#096159` | `#084F49` | `#0B7A72` 45% | `#FFFFFF` |
| secondary | `#FFFFFF` + `#E1E7EB` | `#F8FAFB` + `#CBD5DB` | `#F1F4F6` | 50% | `#11181C` |
| ghost | 透明 | `#F1F4F6` | `#EDF1F3` | 50% | `#48565F` → hover `#11181C` |
| danger | `#C0364B` | `#A22A3E` | `#A22A3E` | 50% | `#FFFFFF` |

**尺寸**

| 尺寸 | 高度 | 水平内边距 | 字号/行高（Tailwind） | 圆角 | 图标 | 典型场景 |
|---|---|---|---|---|---|---|
| `sm` | 28px（`h-7`） | 10px（`px-2.5`） | 13/20（`text-sm`） | 6px（`rounded-sm`） | `size-3.5`（14px） | 表格行内操作、卡片角标按钮 |
| `md` | 36px（`h-9`） | 14px（`px-3.5`） | 14/22（`text-base`） | 8px（`rounded-md`） | `size-4`（16px） | **默认**：表单提交、工具栏 |
| `lg` | 44px（`h-11`） | 20px（`px-5`） | 14/22（`text-base`） | 10px（`rounded-[10px]`） | `size-[18px]` | 引导页、登录、抽屉底部主操作 |

**按钮组**：`gap-2`；主次并列时主按钮在右（Windows 习惯）或按平台 `dialog` 顺序统一为「取消 / 确认」，确认在右。整组 `justify-end`。

**图标按钮**：`size-7`（28px）/ `size-9`（36px），`rounded-md`，`grid place-items-center`，图标 `size-4`（16px），不加边框，hover 用 `bg-surface-active`。必须带 `aria-label`，并用 `<Tooltip>` 包裹（折叠侧栏除外，那里用 Tooltip 承担导航名）。

**不允许**：按钮内放两个以上图标；文字换行；同屏出现两个 primary 按钮。

### 5.2 输入框 / 文本域 / 数字输入

**基础类**

```
h-9 w-full rounded-md border border-border bg-surface px-3.5 text-base text-fg
placeholder:text-fg-muted
shadow-[inset_0_1px_0_rgba(16,24,32,0.02)]
transition-[border-color,box-shadow] duration-fast ease-in-out
hover:border-border-strong
focus:border-brand-600 focus:outline-none focus:ring-[3px] focus:ring-brand-600/[0.18]
disabled:cursor-not-allowed disabled:bg-surface-active disabled:text-fg-disabled
aria-[invalid=true]:border-danger-600 aria-[invalid=true]:ring-danger-600/[0.15]
aria-[invalid=true]:focus:ring-danger-600/[0.15]
```

| 属性 | 值 |
|---|---|
| 高度 | 36px（`md`）/ 28px（`sm`，表格内联编辑）/ 44px（`lg`） |
| 内边距 | 左右 14px；带图标时左 36px（图标 `size-4`，`left-3` 居中：12 + 16 = 28，文字从 36 开始） |
| 字号 | 14/22（`text-base`）；纯数字加 `.num` |
| 占位符 | `#64737E`，不额外降透明度 |
| 错误态 | 边框 `#C0364B` + 3px `rgba(192,54,75,.15)` 光环（聚焦时保持同色，不叠加品牌色）+ 下方 12px 错误文案（6px 间距，`text-danger-600`） |
| 帮助文案 | 12/18（`text-xs`），`text-fg-muted`，控件下方 6px |
| 标签 | 13/18，500（`text-sm font-medium leading-[18px]`），`text-fg-secondary`，控件上方 6px；必填在标签后加 `*`（`text-danger-600`） |

**文本域**：`min-h-[88px] py-2.5 resize-y`，右下角显示字数 `12/200`（`text-xs text-fg-muted`，超限变 `text-danger-600`）。

**数字输入（价格/库存）**：单位前缀/后缀用 `relative` + 绝对定位承载，输入框 `pl-8`（货币符号）或 `pr-10`（「件」）；`inputMode="decimal"`；聚焦时 `select()` 全选；↑↓ 步进（价格 0.01 / 库存 1）；`onBlur` 时格式化回 2 位小数。货币符号来自商品所属区域，见 3.3 节。

**内联编辑（表格单元格）**：默认渲染为文本，单击变输入框（高度 28px，无边框，`bg-surface` + `ring-1 ring-brand-600`），Enter 提交、Esc 取消、失焦提交。

### 5.3 下拉选择（Select）

**触发器**：复用输入框类，加 `flex items-center justify-between pr-2.5`，右侧 `ChevronDown` 16px `text-fg-muted`，展开时旋转 180°（140ms）。多选时在触发器内渲染徽标 + `+N`。

**面板**（不依赖 `tailwindcss-animate`，直接挂 tokens.css 里的 `.anim-pop-in`）：

```
z-[40] min-w-[var(--radix-select-trigger-width)] max-h-[280px] overflow-y-auto
rounded-md border border-border bg-elevated p-1 shadow-2
anim-pop-in
```

> Radix 的 `Content` 在打开时才挂载（未开 `forceMount`），所以 `.anim-pop-in` 直接放在 class 上即可，不需要 `data-[state=open]:` 前缀——自定义工具类加变体在 Tailwind 3 下行为不稳定，别赌。

**选项**：

```
relative flex h-8 cursor-default select-none items-center rounded-sm pl-2.5 pr-8
text-sm text-fg outline-none
data-[highlighted]:bg-surface-active
data-[state=checked]:font-medium data-[state=checked]:text-brand-700
disabled:pointer-events-none disabled:opacity-50
```

选中项右侧 `Check`（`size-4 text-brand-600`），绝对定位 `right-2`。分组标题 11/16 500（`text-2xs font-medium text-fg-muted`），`px-2.5 py-1.5`。

**搜索型下拉（商品类目、店铺）**：面板顶部固定搜索框（h-8，无外边框，底部分割线），选项高亮匹配片段用 `<mark class="bg-brand-100 text-brand-900 rounded-xs px-0.5">`。

### 5.4 搜索框

```
相对容器 h-9
Search 图标：absolute left-3 size-4 text-fg-muted
input：h-9 w-full rounded-md border border-border bg-surface pl-9 pr-16 text-base
聚焦时图标变 text-brand-600
右侧快捷键提示：absolute right-2 top-1/2 -translate-y-1/2
  hidden group-focus-within:hidden  → 聚焦后隐藏
```

快捷键提示：`kbd` 高 20px，`px-1.5 rounded-xs border border-border bg-canvas-inset text-2xs text-fg-muted num`，显示 `Ctrl K`（Windows）/ `⌘K`（macOS）。

- 输入即搜，防抖 **250ms**；清空按钮（`X`，`size-3.5`）在有值时出现，位于快捷键提示的位置（两者互斥）。
- 全局搜索触发 `Ctrl/Cmd + K` 打开命令面板（`max-w-[560px]`，顶部输入，下方分组结果，↑↓ 选择，Enter 进入，Esc 关闭）。
- 表格筛选型搜索宽度 240px，页面级搜索宽度 320px（`w-80`）。
- 无结果时**不弹下拉**，而是在列表区展示「搜索无结果」空状态（避免遮住表格）。

### 5.5 复选框 / 单选 / 开关

| 控件 | 尺寸 | 规格 |
|---|---|---|
| Checkbox | 16×16 | `rounded-xs border-[1.5px] border-border-strong bg-surface`；选中 `bg-brand-600 border-brand-600` + 白色 `Check`（`size-3` stroke 3）；不确定态 `Minus`；`focus-visible:ring-2 ring-brand-600/32 ring-offset-1` |
| Radio | 16×16 | 圆形 `border-[1.5px]`；未选 `border-border-strong`，选中 `border-brand-600` + 内点 6px `bg-brand-600`；焦点环同上 |
| Switch | 36×20 | 轨道 `rounded-full`；关 `bg-border-strong`，开 `bg-brand-600`；滑块 16px 白色 + `shadow-1`，`translate-x-0 → translate-x-4`，160ms `ease-out` |
| 标签点击 | — | 整个 label 可点，label 与控件间距 8px，label `text-sm` |

行内多选：`gap-6`；纵向列表：`gap-3`。禁用态统一 `opacity-50 cursor-not-allowed`。

### 5.6 标签 / 徽标 / 状态点

**徽标（Badge）**

```
inline-flex h-5 items-center gap-1 rounded-xs px-1.5
text-2xs font-medium whitespace-nowrap
```

| 变体 | 底 | 字 | Token |
|---|---|---|---|
| neutral | `#EFF3F4` | `#48565F` | `bg-canvas-inset text-fg-secondary` |
| brand | `#EFF8F7` | `#096159` | `bg-brand-50 text-brand-700` |
| success | `#EAF7F0` | `#086646` | `bg-success-50 text-success-700` |
| warning | `#FFF8EB` | `#92400E` | `bg-warning-50 text-warning-700` |
| danger | `#FDF2F4` | `#A22A3E` | `bg-danger-50 text-danger-700` |
| info | `#EFF6FF` | `#1A5BBF` | `bg-info-50 text-info-700` |

**状态点**：6px 圆（`size-1.5 rounded-full`）+ 徽标，`gap-1.5`；「审核中」点使用 `animate-status-breathe`（1.8s ease-in-out infinite，全应用唯一允许的循环动画之一——另一个是骨架屏）。

**可移除标签**：徽标 + 右侧 `X`（`size-3`），`hover:bg-fg/5`，点击移除（同时支持 Backspace 键）。

### 5.7 卡片

**通用卡片**

```
rounded-lg border border-border bg-surface shadow-1
transition-[transform,box-shadow,border-color] duration-card ease-out
hover（仅可点击卡片）：-translate-y-0.5 shadow-2 border-border-strong
focus-within：ring-2 ring-brand-600/25
```

内边距：16px（标准）/ 12px（紧凑）。卡片内标题 `text-h3`；描述 `text-sm text-fg-muted`；卡片间距 16px。

**商品卡（核心组件）**

```
容器：group relative flex flex-col overflow-hidden rounded-lg border bg-surface
      border-border shadow-1
      transition-[transform,box-shadow,border-color] duration-card ease-out
      hover:-translate-y-0.5 hover:border-border-strong hover:shadow-2
      data-[selected=true]:border-brand-600 data-[selected=true]:ring-1
      data-[selected=true]:ring-brand-600
```

| 区域 | 规格 |
|---|---|
| 图片区 | **比例固定 1:1**（`aspect-square`）。TikTok 商品主图规范是 1:1 且白底/场景图混用，统一裁切才能让网格纵向对齐；`object-cover` 居中裁切，不用 `contain`（会出现不齐的留白）。容器 `w-full overflow-hidden bg-canvas-inset`；`img`：`size-full object-cover transition-transform duration-image ease-out group-hover:scale-[1.02]`；`loading="lazy"`；`decoding="async"`；`draggable={false}`。详情页大图可用 **3:4**（`aspect-[3/4]`，最大宽 480px），但列表/网格一律 1:1 |
| 图片角标 | 左上：状态徽标（`absolute left-2 top-2`，带 `bg-white/92 backdrop-blur-[2px]`）；右上：勾选框（`absolute right-2 top-2 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 data-[state=checked]:opacity-100`，120ms）。注意 Radix Checkbox 的状态属性是 `data-state="checked"`，**不是** `data-checked` |
| 图片蒙层 | 底部 56px 线性渐变 `linear-gradient(to top, rgba(12,20,24,.55), transparent)`，仅当图片上叠加操作按钮时启用 |
| 内容区 | `flex flex-1 flex-col gap-2 p-3` |
| 标题 | `text-sm font-medium text-fg` + `line-clamp-2 min-h-[40px]`（恒占 2 行，保证网格对齐） |
| SKU | `text-2xs font-mono text-fg-muted truncate` |
| 价格行 | `flex items-baseline gap-1.5`：用 `<PriceRange>` 统一渲染；单值 `num text-h3 font-semibold`，区间 `num text-h3 font-semibold` + 中间 en dash，禁用价 `num text-xs text-fg-muted line-through` 只在「已下架/改价中」时出现 |
| 元信息行 | `flex items-center gap-3 text-xs text-fg-muted`：销量（`ShoppingBag` 12px）、库存（`Boxes` 12px）；数字用 `.num` |
| 底部操作 | `flex items-center justify-between border-t border-border-subtle px-3 py-2`；左侧状态/更新时间，右侧图标按钮组（`size-7`，`opacity-0 group-hover:opacity-100 group-focus-within:opacity-100`，120ms） |
| 选中态 | `border-brand-600` + 1px 品牌环 + 背景 `bg-brand-50/40` |
| 禁用/失败态 | 整卡 `opacity-60`，失败原因以 `text-xs text-danger-600` 文案占位到标题下方，不弹窗 |

卡片固定最小高度：图片宽 + 132px（内容区 16 + 标题 40 + SKU 16 + 价格 24 + 元信息 16 + 间距 3×8 = 128，+4 冗余）；标题恒占 2 行，保证网格对齐。

**商品卡 TSX 骨架（可直接粘贴）**

```tsx
export function ProductCard({ product, selected, onToggle }: ProductCardProps) {
  return (
    <article
      className={cn(
        "group relative flex flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-1",
        "transition-[transform,box-shadow,border-color] duration-card ease-out",
        "hover:-translate-y-0.5 hover:border-border-strong hover:shadow-2",
        "focus-within:ring-2 focus-within:ring-brand-600/25",
        selected && "border-brand-600 ring-1 ring-brand-600"
      )}
      data-selected={selected}
    >
      <div className="relative aspect-square w-full overflow-hidden bg-canvas-inset">
        <img
          src={product.cover}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          className="size-full object-cover transition-transform duration-image ease-out group-hover:scale-[1.02]"
        />
        <StatusBadge status={product.status} className="absolute left-2 top-2 bg-white/92 backdrop-blur-[2px]" />
        <Checkbox
          checked={selected}
          onCheckedChange={() => onToggle(product.id)}
          aria-label={`选择 ${product.title}`}
          className="absolute right-2 top-2 opacity-0 transition-opacity duration-fast group-hover:opacity-100 group-focus-within:opacity-100 data-[state=checked]:opacity-100"
        />
      </div>

      <div className="flex flex-1 flex-col gap-2 p-3">
        <h3 className="line-clamp-2 min-h-[40px] text-sm font-medium text-fg">{product.title}</h3>
        <p className="truncate font-mono text-2xs text-fg-muted">{product.detailId}</p>
        <div className="flex items-baseline gap-1.5">
          <PriceRange min={product.priceMin} max={product.priceMax} currency={product.currency} />
        </div>
        <div className="flex items-center gap-3 text-xs text-fg-muted">
          <span className="flex items-center gap-1"><ShoppingBag className="size-3" />{product.skuCount}</span>
          <span className="flex items-center gap-1"><Boxes className="size-3" />{product.stock}</span>
        </div>
      </div>

      <footer className="flex items-center justify-between border-t border-border-subtle px-3 py-2">
        <span className="text-xs text-fg-muted">{product.updatedAt}</span>
        <div className="flex items-center gap-1 opacity-0 transition-opacity duration-fast group-hover:opacity-100 group-focus-within:opacity-100">
          <IconButton icon={Pencil} label="编辑" />
          <IconButton icon={MoreHorizontal} label="更多" />
        </div>
      </footer>
    </article>
  );
}
```

> 底部操作按钮整卡 hover 才出现，键盘 Tab 到卡片内任意元素（`group-focus-within`）时也会出现——这是「克制动效」与「可用性」的唯一折中，不允许去掉 focus 分支。

**KPI 指标卡**

```
rounded-lg border border-border bg-surface p-4
label：text-sm text-fg-muted
value：text-num-lg num text-fg mt-1
delta：text-xs num mt-2 flex items-center gap-1（涨 text-success-600 / 跌 text-danger-600，配 ArrowUpRight / ArrowDownRight `size-3`）
```
四列网格 `grid gap-4 grid-cols-2 xl:grid-cols-4`。

### 5.8 数据表格

**结构**

```
容器：rounded-lg border border-border bg-surface overflow-hidden
滚动容器：overflow-auto，max-h = calc(100vh - 标题栏 - 页头 - 分页)
table：w-full border-collapse table-fixed（列宽显式声明，避免跳动）
```

**表头**

```
sticky top-0 z-[10] h-10 bg-canvas-inset/85 backdrop-blur-[2px]
border-b border-border
th：px-3 text-left text-xs font-medium text-fg-muted
第一个 th：pl-4；最后一个：pr-4
可排序列：cursor-pointer select-none hover:text-fg
  排序图标 ArrowUpDown 14px opacity-0 hover 时 opacity-60
  激活列：ArrowUp/ArrowDown 14px text-brand-600，opacity-100
```

> 表头底色必须 ≥85% 不透明：`/70` 时下方滚动内容会透出，文字与数字叠在一起无法扫读。

**行**

| 密度 | 行高 | 内边距 | 场景 |
|---|---|---|---|
| 紧凑 | 44px | `px-3`，首列 `pl-4` | 商品列表、订单列表（默认） |
| 标准 | 56px | `px-3` | 带两行内容（图 + 标题/SKU） |

```
tr：border-b border-border-subtle last:border-0
    transition-colors duration-fast
    hover:bg-surface-hover
    data-[state=selected]:bg-brand-50
    focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset
    focus-visible:ring-brand-600/32
td：px-3 text-base text-fg align-middle
数值列：text-right num
```

**商品表列定义**

| 列 | 宽度 | 对齐 | 内容 |
|---|---|---|---|
| 勾选 | 40px | 居中 | Checkbox；表头为全选（含不确定态） |
| 商品 | 自适应（min 280px） | 左 | 40×40 缩略图（`rounded-sm`）+ 标题（`text-sm font-medium`，单行截断）+ SKU（`text-2xs font-mono text-fg-muted`） |
| 价格 | 120px | 右 | `<PriceRange>`；区间在行内用 en dash 连接，第二行放 `text-2xs text-fg-muted` 的成本/利润（可选） |
| 库存 | 88px | 右 | `num`；≤10 时文字变 `text-warning-600`（`#B45309`） |
| 状态 | 104px | 左 | 徽标 + 点 |
| 更新时间 | 132px | 左 | `text-xs num text-fg-muted`，格式 `MM-DD HH:mm` |
| 操作 | 96px | 右 | 2 个图标按钮（编辑 / 更多），默认 `opacity-60`，行 hover 变 100% |

**合计列宽 860px**（40+280+120+88+104+132+96），这是最小窗口不出现横向滚动的下限；新增列必须同步调整其它列或把低优先级列收进「列设置」。

**批量操作栏**（选中 ≥1 时出现）

```
fixed bottom-6 left-1/2 -translate-x-1/2 z-[40]
flex h-12 items-center gap-3 rounded-lg bg-inverse px-3 text-white shadow-3
进入：y +8 → 0，opacity 0 → 1，200ms ease-out
```
内容：`已选 12 项`（`text-sm`）+ 竖分割线（`bg-white/15`）+ 操作按钮（`text-white/90 hover:bg-white/10 h-8 px-2.5 rounded-sm`）+ 关闭。禁止超过 5 个操作，超出收进「更多」。

**分页**：右下角，`h-8`，`共 1,284 条` + 每页条数下拉（20/50/100）+ 上一页/下一页图标按钮 + 页码（当前页 `bg-canvas-inset font-medium`）。距表格 12px。

**状态**

| 状态 | 表现 |
|---|---|
| 首次加载 | 8 行骨架（行高 44px），不做 spinner |
| 分页/排序加载 | 表体 `opacity-60 pointer-events-none`，右上角 16px spinner，不换骨架 |
| 空 | 表头保留，表体替换为空状态（高度 280px） |
| 错误 | 表头保留，表体替换为错误状态 + 重试 |
| 行 hover 延迟 | 无延迟，直接变色（120ms） |

### 5.9 侧边栏导航

```
宽度 232px（折叠 64px），高度 100%，背景 var(--sidebar)
border-r border-border
flex flex-col
过渡由父级 .app-shell 的 grid-template-columns 承担（240ms ease-out），
侧边栏自身不写 width 过渡，避免与栅格动画打架
```

| 区域 | 规格 |
|---|---|
| 顶部品牌区 | 高 48px，`px-3`，图标 `size-5`（20px）+ 名称 `text-base font-semibold`（折叠时只留图标居中）；下方 `pt-2` |
| 导航区 | `flex-1 overflow-y-auto px-2 py-2` |
| 分组标题 | `text-2xs font-medium uppercase tracking-[.04em] text-fg-muted`，`px-2.5`，`mt-4 mb-1.5`；折叠时隐藏，改为 1px 分割线 |
| 导航项 | 高 36px，`px-2.5 rounded-md gap-2.5 text-sm font-medium`；图标 `size-[18px]` |
| 导航项-默认 | `text-fg-secondary`，图标 `text-fg-muted` |
| 导航项-hover | `bg-canvas-inset text-fg`，图标 `text-fg-secondary`，100ms |
| 导航项-当前 | `bg-surface text-brand-700 shadow-1`，图标 `text-brand-600`；左侧 2px 指示条（`absolute left-0 top-1/2 -translate-y-1/2 h-4 w-[2px] rounded-full bg-brand-600`） |
| 徽标 | 右侧 `ml-auto`，`text-2xs num rounded-xs bg-canvas-inset px-1.5 text-fg-muted`；当前项徽标 `bg-brand-100 text-brand-800`；>99 显示 `99+` |
| 折叠态 | 项变 40×36 居中，图标保持 `size-[18px]`；悬停 400ms 后出 Tooltip（右侧 8px，`bg-inverse text-white text-xs px-2 py-1 rounded-sm shadow-2`） |
| 底部账户区 | `h-14 border-t border-border px-2`；头像 24px 圆形 + 名称 `text-sm font-medium` + 店铺名 `text-2xs text-fg-muted`；右侧 `MoreHorizontal` `size-4` |
| 折叠切换 | 底部或顶部 `PanelLeft` 图标按钮 28×28 |

导航项间距 2px；一级导航最多 7 项，超出归入「更多」。

### 5.10 自定义标题栏（Electron 无边框窗口）

窗口配置（`app/src/main.ts` 已实现，见第 0.1 节）：`frame: false`、`titleBarStyle: "hidden"`，Windows 下自绘按钮，不启用 `titleBarOverlay`。

```
高度 40px，宽度 100%，背景 var(--sidebar)
border-b border-border
flex items-center
-webkit-app-region: drag（整条）
z-index 30（写在 CSS，不要用 z-30 与页面头 z-[10] 混用）
```

| 区域 | 规格 |
|---|---|
| 左侧品牌 | `pl-3`，`gap-2`，图标 `size-4`，文字 `text-sm font-medium text-fg-secondary`，不可选中 |
| 中部（可选） | 全局搜索入口 `h-7 w-[360px] rounded-sm border border-border bg-surface/70 text-xs`，居中绝对定位；也可放当前页面面包屑（`text-xs text-fg-muted`，分隔符 `/`） |
| 右侧窗口按钮 | 3 个，每个 **46×40px**，`-webkit-app-region: no-drag`，`grid place-items-center` |
| 拖拽区 | 除按钮外全部可拖；双击标题栏切换最大化：`onDoubleClick={() => window.desktop.toggleMaximize()}` |
| 用户选择 | 标题栏文字 `user-select: none`；窗口按钮图标 `pointer-events: none` |

**窗口按钮规格**

| 按钮 | 图标 | 尺寸 | 默认 | hover | active | 说明 |
|---|---|---|---|---|---|---|
| 最小化 | `Minus` | `size-3`（12px），`strokeWidth={1.5}` | `text-fg-secondary` | `bg-canvas-inset` | `bg-border-subtle` | — |
| 最大化 | `Square` | `size-3`，`strokeWidth={1.5}` | 同上 | 同上 | 同上 | 非最大化时显示 |
| 还原 | `Copy` | `size-3`，`strokeWidth={1.5}` | 同上 | 同上 | 同上 | 最大化时显示（状态来自 `window.desktop.isMaximized()`） |
| 关闭 | `X` | `size-3`，`strokeWidth={1.5}` | 同上 | `bg-danger-600 text-white` | `bg-danger-700 text-white` | 唯一允许的红色 hover 区 |

> lucide 默认 `strokeWidth={2}`，10px 下会糊成一团，12px + 1.5 才是 Windows 标题栏的观感；不要用 `size-2.5`。

- 圆角：按钮本身 **0**（贴边，符合 Windows 11 习惯），不设 `rounded`。
- 焦点环：`focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600/40`（关闭按钮用 `ring-danger-600/40`）。
- 图标垂直居中；按钮区域与标题栏同高，不留 1px 缝隙。
- 窗口处于最大化时，最大化按钮换 `Copy`（还原）图标，且不显示窗口外边圆角。
- macOS（未来）：标题栏 44px，左侧红黄绿 12px 圆点（间距 8px，`pl-4`），不显示右侧按钮。
- 禁止：在标题栏放彩色渐变、放大量操作按钮（最多 1 个全局入口 + 窗口控制）。

### 5.11 Toast（sonner）

```tsx
<Toaster
  position="bottom-right"
  offset={16}
  gap={8}
  visibleToasts={3}
  duration={4000}
  closeButton
  toastOptions={{
    className:
      "w-[360px] rounded-lg border border-border bg-elevated p-3 pl-3.5 shadow-3 " +
      "text-sm text-fg gap-2.5 items-start",
  }}
/>
```

| 项 | 规格 |
|---|---|
| 宽度 | 360px（最大 `calc(100vw - 32px)`） |
| 内边距 | 12px 14px |
| 圆角 | 12px |
| 图标 | 16px，置于首行，`mt-0.5`；成功 `#0B7F57`、警告 `#B45309`、错误 `#C0364B`、信息 `#1F6FEB` |
| 标题 | `text-sm font-medium text-fg` |
| 描述 | `text-xs text-fg-muted`，`mt-0.5`，最多 2 行 |
| 关闭 | 24×24 ghost，`opacity-0 group-hover:opacity-100` |
| 动作 | 文字按钮 `text-xs font-medium text-brand-700`，最多 1 个 |
| 进入 | `opacity 0→1, y 8→0, scale .98→1`，220ms `ease-out` |
| 退出 | `opacity 1→0, y 0→4`，140ms `ease-in` |
| 停留 | 成功 3000ms / 信息 4000ms / 错误 6000ms；hover 暂停计时 |
| 堆叠 | 最多 3 条，新的在下方，其余上移 8px |

规则：**同一次用户动作只出一条 Toast**（批量操作汇总为一条：「已上架 12 个商品，2 个失败」+「查看详情」）。

### 5.12 弹窗（Dialog）

```
遮罩：fixed inset-0 z-[50] bg-overlay backdrop-blur-[2px]
      （--overlay 自带 alpha，不要再挂 /45；也不要用 bg-[rgb(var(--overlay))]/45
        ——任意值里的 rgb(var(...)) 不是合法颜色函数，会静默失效）
      （a11y：遮罩本身不加 tabIndex，点击/滚动穿透由 Radix Dialog 处理）
内容：fixed left-1/2 top-1/2 z-[60] -translate-x-1/2 -translate-y-1/2
      w-[calc(100vw-48px)] rounded-xl border border-border bg-elevated shadow-3
      max-h-[85vh] flex flex-col
```

| 尺寸 | 宽度 | 场景 |
|---|---|---|
| sm | 400px | 二次确认、重命名 |
| md | 480px | **默认**：表单、设置 |
| lg | 640px | 双列表单、批量编辑 |
| xl | 880px | 复杂配置（极少） |

| 区域 | 规格 |
|---|---|
| 头部 | `px-6 pt-5 pb-0`；标题 `text-h3`；描述 `text-sm text-fg-muted`，`mt-1.5`；关闭按钮右上 `absolute right-3 top-3 size-7` |
| 内容 | `px-6 py-4 overflow-y-auto`；表单字段间距 16px |
| 底部 | `px-6 py-4 border-t border-border-subtle flex justify-end gap-2`；主操作在右 |
| 危险确认 | 主按钮用 `danger` 变体；标题下必须写清影响对象与数量 |
| 焦点 | 打开时聚焦第一个可交互元素（危险确认默认聚焦「取消」）；Esc 关闭；焦点锁定在弹窗内 |
| 点击遮罩 | 表单类弹窗不关闭（防误触丢数据），确认类允许关闭 |

**动效**：遮罩 `opacity 0→1` 160ms；内容 `opacity 0→1, scale .96→1, y 4→0` 200ms `ease-out`；关闭 140ms `ease-in`（`scale .98`）。不做出弹簧回弹。

### 5.13 抽屉（Drawer / Sheet）

用于商品详情、编辑、批量操作，从右侧滑出：

```
遮罩：同 Dialog
面板：fixed right-0 top-0 z-[60] h-full bg-elevated border-l border-border
      shadow-3 rounded-l-xl
```

| 尺寸 | 宽度 |
|---|---|
| sm | 400px |
| md | **480px**（默认） |
| lg | 640px |
| xl | 800px |

| 区域 | 规格 |
|---|---|
| 头部 | 高 56px，`px-6 border-b border-border`，标题 `text-h3` + 右侧关闭 |
| 内容 | `p-6 overflow-y-auto`，`flex-1` |
| 底部 | 高 64px，`px-6 border-t border-border flex justify-end gap-2`（固定不滚动） |
| 进入 | `x 24→0` + `opacity 0→1`，240ms `ease-out` |
| 退出 | `x 0→16` + `opacity 1→0`，180ms `ease-in` |
| 宽度切换 | 不动画（直接切换），避免布局抖动 |

抽屉打开时**不**压缩主内容宽度（不推开布局），只覆盖。

### 5.14 空状态 / 骨架屏 / 错误状态

**空状态**

```
flex min-h-[280px] flex-col items-center justify-center px-6 py-10 text-center
图标容器：size-[88px] rounded-2xl bg-canvas-inset grid place-items-center
图标：size-8 text-fg-disabled，strokeWidth={1.5}
标题：mt-4 text-h3 font-semibold text-fg
描述：mt-1.5 max-w-[320px] text-sm text-fg-muted
主操作：mt-5（primary 或 secondary，按是否有下一步决定）
次要操作：文字按钮，mt-2，text-brand-700
```

| 场景 | 图标 | 标题 | 描述 | 操作 |
|---|---|---|---|---|
| 无商品 | `PackageOpen` | 还没有商品 | 绑定店铺后同步，或手动添加第一个商品。 | 同步商品（primary）/ 手动添加（ghost） |
| 搜索无结果 | `SearchX` | 没有匹配的商品 | 换个关键词，或清空筛选条件。 | 清空筛选（secondary） |
| 筛选后为空 | `FilterX` | 当前筛选下没有商品 | 已选 3 个筛选条件。 | 清空筛选（secondary） |
| 无任务 | `ListChecks` | 暂无上架任务 | 选好商品后点击「批量上架」。 | 去选商品（primary） |
| 无权限 | `Lock` | 没有访问权限 | 联系管理员为当前账号开通「商品管理」。 | — |
| 加载失败 | `AlertTriangle` | 加载失败 | 网络异常，请稍后重试。 | 重试（primary） |

空状态**不用**插画、不用大面积渐变，最多一个线性图标；文案一律「一句事实 + 一句下一步」，不用感叹号。

**骨架屏**

```
基础块：.skeleton（见 2.7 节工具类，颜色取自 --canvas-inset / --surface-hover）
高度：文本行 12px，缩略图 40px，标题 16px
尺寸：动画时长 1.6s linear infinite（全局唯一的持续循环动画，与状态点并列）
```

- 文本行：高 12px，圆角 3px，宽度 60% / 80% / 40% 交错。
- 商品卡骨架：图片区 `aspect-square rounded-lg` + 2 行文字 + 1 行价格（宽 40%）。
- 表格骨架：8 行，每行 44px，列宽与真实表一致（避免加载完成跳动）。
- 骨架数量 = 首屏可见数量 + 2；只在首次加载用，切页/刷新用半透明 + spinner。
- 骨架总时长超过 3s 时，在骨架上方显示「正在加载，请稍候…」（`text-xs text-fg-muted`）。
- 骨架必须包在 `aria-hidden="true"` 的容器里，同时给外层加 `aria-busy="true"`，避免读屏器把占位块念出来。

**错误状态**

行内错误条：

```
flex items-start gap-2 rounded-md border border-danger-600/20 bg-danger-50
px-3 py-2.5 text-sm text-danger-700
AlertCircle size-4 mt-0.5 shrink-0
右侧：重试（ghost 文字按钮，text-danger-700）
```

区域级错误（替换列表/卡片区）：

```
min-h-[280px] 居中布局，同空状态
图标容器 bg-danger-50，图标 AlertTriangle size-8 text-danger-600
标题「加载失败」（text-h3 font-semibold）
描述：一句人话 + 错误码（`text-xs font-mono text-fg-muted`，如 ERR_NETWORK_500）
操作：重试（primary）/ 查看日志（ghost）
```

三种错误降级的分工（不允许混用）：

| 层级 | 组件 | 用于 |
|---|---|---|
| 字段级 | 输入框 `aria-invalid` + 下方红字 | 校验失败（价格 ≤ 0、标题为空） |
| 行内条 | 上文错误条 | 单个操作失败、局部接口失败（保存失败、同步失败） |
| 区域级 | 替换整个列表/卡片区 | 首屏接口失败，没有内容可展示 |
| Toast | sonner | 只报操作结果，不承载需要用户阅读的详情 |

字段级错误：见 5.2。**Toast 绝不作为唯一的错误出口**——离开页面就看不见了。

### 5.15 其它基础件

| 组件 | 规格 |
|---|---|
| Tabs | 高 36px，项 `px-3 text-sm`，未选 `text-fg-secondary hover:text-fg`，选中 `text-fg font-medium` + 底部 2px `bg-brand-600`（Framer 的 `layoutId="tab-underline"`，200ms `ease-out`）；分隔线 1px `border-subtle` |
| Tooltip | `bg-inverse text-white text-xs leading-4 px-2 py-1 rounded-sm shadow-2`，最大宽 240px，延迟 300ms（表格内 400ms），进入 120ms；内容永远不是唯一信息来源 |
| Popover | `rounded-md border border-border bg-elevated p-3 shadow-2`；用 `.anim-pop-in`（140ms） |
| 进度条 | 高 6px，`rounded-full bg-canvas-inset`；填充 `bg-brand-600`，宽度过渡 `duration-medium ease-out`；不确定态：`w-1/3` 的填充块在轨道内左右平移（1.2s `ease-in-out` 循环），仅此一处允许进度类循环 |
| 分割线 | `h-px bg-border-subtle`；垂直 `w-px bg-border-subtle` |
| 键盘按键 | `h-5 min-w-5 px-1 rounded-xs border border-border bg-canvas-inset text-2xs text-fg-muted num grid place-items-center` |
| 头像 | 24/32/40px 圆形，`bg-brand-100 text-brand-800` 首字，或图片 `object-cover` |
| 骨架 | 见 5.14；组件内统一用 `<Skeleton className="h-3 w-[60%]" />` 封装 |

---

## 6. 动效规范

### 6.1 时长与缓动

| Token | 值 | 用于 |
|---|---|---|
| `instant` | 90ms | 按钮按下、勾选、开关 |
| `fast` | 120ms | hover 变色、图标按钮显隐 |
| `exit` | 140ms | 统一退场时长（弹窗/下拉/Toast/批量栏） |
| `card` | 160ms | 卡片 hover（位移 + 阴影 + 边框三属性同步） |
| `base` | 180ms | 列表项进出、Tab 内容淡入、下拉关闭 |
| `image` | 200ms | 卡片图片缩放（比容器慢 40ms，产生「图在框里轻微推近」的层次） |
| `medium` | 240ms | 抽屉、命令面板、批量操作栏 |
| `slow` | 320ms | 仅页面级切换（少用） |
| `ease-out` | `cubic-bezier(0.16, 1, 0.3, 1)` | **默认进入**：元素出现、展开、滑入 |
| `ease-in` | `cubic-bezier(0.4, 0, 1, 1)` | 退出、收起 |
| `ease-in-out` | `cubic-bezier(0.4, 0, 0.2, 1)` | 颜色/透明度过渡、位置移动 |
| `ease-spring` | `cubic-bezier(0.34, 1.56, 0.64, 1)` | **仅** Popover/Tooltip 的 `scale`，过冲 ≤ 4% |

原则：**进入慢于退出**（退出时长 = 进入 × 0.7）；移动距离 ≤ 8px（抽屉 ≤ 24px）；同时动画属性 ≤ 3 个。

```ts
// web/src/lib/motion.ts
import { useReducedMotion } from "framer-motion";
import type { Transition, Variants } from "framer-motion";

/** 显式元组类型，否则 as const 的 readonly 元组不能赋给 Framer 的 Easing */
type Bezier = [number, number, number, number];

export const ease: Record<"out" | "in" | "inOut" | "spring", Bezier> = {
  out: [0.16, 1, 0.3, 1],
  in: [0.4, 0, 1, 1],
  inOut: [0.4, 0, 0.2, 1],
  spring: [0.34, 1.56, 0.64, 1],
};

export const dur = {
  instant: 0.09,
  fast: 0.12,
  exit: 0.14,
  card: 0.16,
  base: 0.18,
  image: 0.2,
  medium: 0.24,
  slow: 0.32,
} as const;

export const t = {
  hover: { duration: dur.fast, ease: ease.inOut } satisfies Transition,
  press: { duration: dur.instant, ease: ease.inOut } satisfies Transition,
  enter: { duration: dur.base, ease: ease.out } satisfies Transition,
  exit: { duration: dur.exit, ease: ease.in } satisfies Transition,
  overlay: { duration: dur.medium, ease: ease.out } satisfies Transition,
};

export const listItem: Variants = {
  hidden: { opacity: 0, y: 6 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { duration: dur.base, ease: ease.out, delay: Math.min(i, 8) * 0.024 },
  }),
  exit: { opacity: 0, transition: { duration: dur.exit, ease: ease.in } },
};

export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 4 },
  show: { opacity: 1, y: 0, transition: t.enter },
  exit: { opacity: 0, transition: t.exit },
};

export const scaleIn: Variants = {
  hidden: { opacity: 0, scale: 0.96, y: 4 },
  show: { opacity: 1, scale: 1, y: 0, transition: { duration: 0.2, ease: ease.out } },
  exit: { opacity: 0, scale: 0.98, transition: t.exit },
};

export const drawerRight: Variants = {
  hidden: { x: 24, opacity: 0 },
  show: { x: 0, opacity: 1, transition: t.overlay },
  exit: { x: 16, opacity: 0, transition: { duration: 0.18, ease: ease.in } },
};

export const toastIn: Variants = {
  hidden: { opacity: 0, y: 8, scale: 0.98 },
  show: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.22, ease: ease.out } },
  exit: { opacity: 0, y: 4, transition: { duration: dur.exit, ease: ease.in } },
};

/**
 * 无障碍降级：把任意带位移的变体转成纯淡入。
 * 用法：const v = useMotionSafe(listItem);
 *
 * 说明：tokens.css 里的 prefers-reduced-motion 只关得掉 CSS 动画/过渡，
 * Framer Motion 走的是 JS 内联样式，必须在这里单独处理。
 */
const MOTION_PROPS = new Set(["x", "y", "scale", "rotate", "translateX", "translateY"]);

export function useMotionSafe(variants: Variants): Variants {
  const reduce = useReducedMotion();
  if (!reduce) return variants;

  const strip = (v: unknown) => {
    if (typeof v !== "object" || v === null) return v;
    return Object.fromEntries(
      Object.entries(v as Record<string, unknown>).filter(([key]) => !MOTION_PROPS.has(key)),
    );
  };

  return Object.fromEntries(
    Object.entries(variants).map(([key, value]) => [key, strip(value)]),
  ) as Variants;
}
```

> `useReducedMotion()` 返回 `boolean | null`（首帧为 `null`），上面的 `if (!reduce)` 把 `null` 当作「不需要降级」，避免首帧闪一下。

### 6.2 逐场景规格

| 场景 | 属性 | 时长 | 缓动 | 备注 |
|---|---|---|---|---|
| 按钮 hover | `background-color`, `border-color` | 120ms | in-out | 不改尺寸、不加阴影 |
| 按钮按下 | `transform: scale(0.98)` | 90ms | in-out | 松开回弹同 90ms；禁用时不动 |
| 输入框聚焦 | `border-color`, `box-shadow` | 120ms | in-out | 光环从 0 → 3px，不做扩散动画 |
| 复选框勾选 | `background-color` + 对勾 `scale .6→1` | 90ms / 120ms | in-out / out | 对勾只在选中瞬间动 |
| 卡片 hover | `translateY(-2px)`, `box-shadow 1→2`, `border-color` | 160ms | out | 仅「可点击」卡片；列表里 200 张卡也只动当前一张 |
| 卡片图片 hover | `transform: scale(1.02)` | 200ms | out | 必须在 `overflow-hidden` 容器内 |
| 卡片操作按钮 | `opacity 0→1` | 120ms | in-out | 无位移，避免误触 |
| 列表项进场 | `opacity 0→1`, `y 6→0` | 180ms | out | 前 8 项 stagger 24ms，其后 0 |
| 列表项删除 | `opacity 1→0`, `height→0` | 160ms | in | 用 `AnimatePresence` + `layout`；批量删除不逐项动画 |
| 列表重排 | `layout` 位置 | 200ms | out | 仅拖拽排序启用；筛选切换不做 |
| 表格行 hover | `background-color` | 120ms | in-out | 无位移、无缩放 |
| 批量操作栏 | `y 8→0`, `opacity`, `scale .98→1` | 200ms | out | 退出 140ms |
| 页面切换 | `opacity 0→1`, `y 8→0` | 220ms | out | **无退场动画**（直接替换），路由切换体感最快 |
| 目录/分区进入 | `opacity`, `y 4` | 180ms | out | 只做一次，不重复触发 |
| 下拉打开 | `opacity`, `scale .97→1`, `y -2→0` | 140ms | out | 关闭 100ms（下拉是唯一短于 exit 的退场，因为它离点击点最近） |
| 弹窗 | 遮罩 `opacity`；内容 `scale .96→1` + `y 4→0` | 160 / 200ms | in-out / out | 关闭 140ms |
| 抽屉 | `x 24→0`, `opacity` | 240ms | out | 关闭 180ms |
| Toast | `y 8→0`, `opacity`, `scale .98→1` | 220ms | out | 关闭 140ms；多条依次 40ms 错峰 |
| 侧边栏折叠 | `width` | 240ms | out | 文字 `opacity` 100ms 先隐、后显 |
| Tab 指示器 | `layoutId` 位移 | 200ms | out | 仅下划线移动，内容淡入 120ms |
| 数字更新 | 不做滚动，直接替换 | 0ms | — | 可用 240ms 的背景高亮淡出提示变化 |
| 骨架屏 | 背景位移 | 1600ms | linear | 唯一的长循环（除审核中状态点） |

### 6.3 必须克制的动效（明确禁止）

1. **不给表格行加位移/缩放**——高密度扫读时任何位移都会造成错行。
2. **不做数字滚动/翻牌**——价格滚动会让人误读，且每次刷新都在动。
3. **不做背景/边框的循环呼吸**（审核中状态点除外）。
4. **不做卡片 3D 倾斜、鼠标跟随光斑、渐变流动**。
5. **不做页面级视差与滚动动画**。
6. **不给静态信息加 hover 动效**（如纯文本、标签、图标）。
7. **不在同一屏同时运行超过 2 组动画**；批量操作时只保留一个进度指示。
8. **过渡距离不超过 8px**（抽屉/侧栏除外），不做过冲弹跳（`ease-spring` 仅限 `scale` ≤ 4%）。
9. **不做 blur 值过渡**（性能差且廉价感强）。
10. **列表 100 项以上不做 stagger**，直接整块淡入。

### 6.4 无障碍

- 全局响应 `prefers-reduced-motion: reduce`：CSS 过渡/动画时长压到 1ms（见 2.7 节 tokens.css 末尾的 media query）。
- Framer Motion 走的是 JS 内联样式，CSS 媒体查询管不到，必须用 6.1 节的 `useMotionSafe()` / `useReducedMotion()` 把 `x/y/scale` 剥掉，只留 `opacity`。
- 动画不得阻塞交互：任何进入动画期间元素即可点击（不要给浮层挂 `pointer-events-none` 等待动画完成）。
- 动效之外的基础可及性（同步纳入验收）：所有图标按钮有 `aria-label`；选中态不只靠颜色（徽标 + 文字）；表格勾选框用 `aria-checked="mixed"` 表达不确定态。

---

## 7. 布局规范

### 7.1 主框架

```
┌───────────────────────────────────────────────┐
│ 自定义标题栏  40px（可拖拽）                    │
├──────────────┬────────────────────────────────┤
│ 侧边栏        │ 页面头 56px（sticky）           │
│ 232px        ├────────────────────────────────┤
│              │ 内容区 padding 24px             │
│              │ （唯一滚动容器）                 │
└──────────────┴────────────────────────────────┘
```

```css
/* 直接写进 tokens.css 的 @layer components，与 grid 布局一一对应 */
.app-shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  grid-template-rows: var(--titlebar-h) minmax(0, 1fr);
  grid-template-areas:
    "titlebar titlebar"
    "sidebar  main";
  height: 100vh;
  overflow: hidden;
  transition: grid-template-columns var(--dur-medium) var(--ease-out);
}
.app-shell[data-sidebar="collapsed"] {
  grid-template-columns: var(--sidebar-w-collapsed) minmax(0, 1fr);
}
.app-titlebar { grid-area: titlebar; min-width: 0; }
.app-sidebar  { grid-area: sidebar; min-width: 0; overflow: hidden; }
.app-main     { grid-area: main; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }
.app-header   { height: 56px; flex: none; }
.app-content  { flex: 1; min-height: 0; overflow-y: auto; padding: var(--content-pad); scrollbar-gutter: stable; }
/* 宽度约束放在内层 wrapper 上，避免直接子元素各自居中 */
.app-content-inner { display: flex; flex-direction: column; gap: 16px;
  max-width: var(--content-max); margin-inline: auto; }
/* 表单页不居中、不占满 */
.app-content[data-width="form"] .app-content-inner { max-width: var(--form-max); margin-inline: 0; }
```

| 区域 | 尺寸 |
|---|---|
| 标题栏 | 40px（macOS 44px） |
| 侧边栏 | 232px（折叠 64px，切换按钮在底部） |
| 页面头 | 56px，`px-6`，`border-b border-border`，`bg-canvas/85 backdrop-blur-[2px]`，`sticky top-0 z-[10]` |
| 页面头标题 | `text-title`；可选面包屑 `text-xs text-fg-muted` 在标题上方（`mb-1`） |
| 页面头操作 | 右侧，`gap-2`，主按钮 md |
| 内容区 | `padding: 24px`，`gap: 16px` |
| 内容最大宽度 | 表格页 100%（上限 1600px 居中）；表单页 880px 左对齐（不居中，避免视线跳跃） |
| 滚动 | 仅 `.app-content` 滚动；侧边栏独立滚动；标题栏与页面头固定 |
| 最小窗口 | 1180 × 720（`app/src/main.ts` 的 `minWidth` 需同步改为 1180）：内容区 1180 − 232 − 48 = **900px**，减去滚动条 12px 后 888px，仍大于商品表最小列宽 860px，不出现横向滚动 |
| 滚动条 | 内容区 `scrollbar-gutter: stable`，避免出现/消失时内容左右跳动 |

### 7.2 栅格与常用排布

| 场景 | 排布 |
|---|---|
| KPI 区 | `grid gap-4 grid-cols-2 xl:grid-cols-4` |
| 商品卡网格 | `grid gap-4` + 列数见下表（用 `grid-template-columns: repeat(auto-fill, minmax(<min>, 1fr))` 实现，不写死断点类） |
| 表单 | 单列，字段 `gap-4`；容器 ≥ 640px 时可两列（`grid-cols-2 gap-x-6 gap-y-4`），长文本字段跨两列 |
| 详情页 | 主区 `1fr` + 侧栏 320px，`gap-6`（<1024 时侧栏落到主区下方） |
| 工具栏 | `flex items-center gap-2 flex-wrap`，搜索在左、筛选居中、主操作在最右 |
| 表格 + 筛选 | 筛选栏 48px 高，位于表格上方，`mb-4` |

**商品卡列数**（`auto-fill` + `minmax`，按内容区实际宽度自动落到下表）

| 内容区宽度 | 列数 | `minmax` 取值 |
|---|---|---|
| < 640 | 2 | `148px` |
| 640–1023 | 2 | `240px` |
| 1024–1279 | 3 | `220px` |
| 1280–1599 | 4 | `216px` |
| 1600–1919 | 5 | `224px` |
| ≥ 1920 | 6 | `232px` |

```css
.product-grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fill, minmax(216px, 1fr));
}
@media (min-width: 1600px) { .product-grid { grid-template-columns: repeat(auto-fill, minmax(224px, 1fr)); } }
@media (min-width: 1920px) { .product-grid { grid-template-columns: repeat(auto-fill, minmax(232px, 1fr)); } }
```

### 7.3 响应式断点

| 断点 | 宽度 | 布局变化 |
|---|---|---|
| `sm` | ≥ 640 | 内容 padding 20px；KPI 2 列 |
| `md` | ≥ 768 | 内容 padding 24px；商品卡 2 列 |
| `lg` | ≥ 1024 | 商品卡 3 列；表格显示全部列 |
| `xl` | ≥ 1280 | **桌面主场景**：侧边栏完整 232px；商品卡 4 列；KPI 4 列 |
| `2xl` | ≥ 1536 | 商品卡 5 列 |
| `3xl` | ≥ 1680 | 商品卡 6 列；内容区居中，最大 1600px |
| `max-lg` | < 1024 | 侧边栏自动折叠为 64px（可手动展开为浮层） |
| `max-md` | < 768 | 侧边栏变抽屉（宽 280px，遮罩）；标题栏 44px；内容 padding 16px |

> Tailwind 的 `max-lg:` 需要 v3.2+，当前 `tailwindcss@^3.4.13` 满足；`3xl` 是本项目自定义断点（见 2.8 节 `screens`），不要在其它地方用 `min-[1680px]:` 代替。

**未来手机端（375+，同一套 Token，只换布局）**

| 断点 | 宽度 | 独有变化 |
|---|---|---|
| `xs` | ≥ 375 | 单列；内容 padding 16px；标题栏 44px |
| `xs2` | ≥ 414 | 单列；卡片内边距 14px |
| `< sm`（`max-sm`，< 640） | 手机布局 | 侧边栏 → 底部 Tab（56px + `env(safe-area-inset-bottom)`）；表格 → 卡片列表；抽屉全屏（`w-full`，顶部 44px） |

- 底部 Tab 最多 5 项（图标 `size-[22px]` + 文字 `text-2xs`），当前项 `text-brand-700`，图标填充色改为 `fill-brand-600/12`。
- 表格转卡片：左侧 48×48 图 + 右侧标题 2 行（`text-sm`）+ 价格（`text-h3 font-semibold num`）+ 状态徽标；操作收进行尾「更多」（44×44 热区）。
- 触控目标 ≥ 44×44（小于 44 的图标按钮用 `::after` 扩热区）；卡片间距 12px；列表行高 64px。
- 表单：标签置于控件上方，控件高 44px（`lg`），按钮整宽（`w-full`），主操作吸底（`sticky bottom-0` + 上边框 + 安全区内边距）。
- 字号阶梯沿用桌面，颜色、圆角、阴影、动效全部复用，不新增 Token。
- 手机端不显示自定义标题栏窗口按钮（由系统状态栏承担）。

### 7.4 窗口

| 项 | 值 |
|---|---|
| 默认尺寸 | 1440 × 920（`app/src/main.ts` 现值；设计按 1440×900 推导，高度多出 20px 归内容区） |
| 最小尺寸 | 1180 × 720 |
| 记忆 | 记住上次尺寸与最大化状态（`window-state` 存到 `app.getPath("userData")`） |
| 高 DPI | 125% / 150% 缩放下布局不破；断点按 CSS 像素判断 |
| 全屏 | 允许（F11），全屏时隐藏标题栏，内容区高度 +40px |
| 背景色 | `#F6F8F9`（与 `--canvas` 一致，防启动白闪） |
| 圆角 | 窗口本身不设圆角（Windows 11 由系统 DWM 处理） |

---

## 8. 实施与验收清单

### 8.1 文件落地顺序

0. `web/src/lib/cn.ts`（若不存在）：`export const cn = (...i: ClassValue[]) => twMerge(clsx(i))`；`clsx` 与 `tailwind-merge` 已在 `package.json` 中。
1. `web/src/styles/tokens.css`（第 2.7 节全文）→ 在 `main.tsx` 引入（它是唯一的 `@tailwind` 入口，不要在别处重复引入）。
2. `web/tailwind.config.js`（第 2.8 节）→ 整体替换现有主题；现有 `hsl(var(--canvas))` 一类映射必须删除，不允许两套 token 并存。
3. `web/src/lib/motion.ts`（第 6.1 节）。
4. `web/src/components/ui/` 按第 5 节逐个覆写 shadcn 组件：`button` → `input` → `select` → `checkbox` → `badge` → `card` → `table` → `dialog` → `sheet` → `tooltip` → `tabs`。
5. `web/src/components/layout/`：`TitleBar` → `Sidebar` → `AppShell` → `PageHeader`。
6. 业务组件：`ProductCard` → `ProductTable` → `EmptyState` / `Skeleton` / `ErrorState`。

### 8.2 验收清单

**Token 与配置**
- [ ] `tokens.css` 已引入，`:root` 内**除 `--overlay` 外**所有颜色变量均为 `R G B` 三元组（空格分隔）；`--overlay` 直接存 `rgba()`，用 `bg-overlay` 而非 `bg-overlay/45`。
- [ ] `tailwind.config.js` 已整体替换；仓库内 `grep -rn "hsl(var(--" web/` 结果为空。
- [ ] 全站颜色只从语义 Token 取：`grep -rnE "#[0-9a-fA-F]{6}" web/src/components` 仅命中图片蒙层与图表色。
- [ ] 每个用到的透明度修饰符都在 `theme.extend.opacity` 中（`/32`、`/45`、`/22`、`/92`…）。
- [ ] 浮层层级全部为 `z-[50]/z-[60]/z-[70]/z-[80]`，没有 `z-60` 这类不存在的类名。

**可读性与一致性**
- [ ] 正文对比度 ≥ 4.5:1；`--text-muted` 不用于 < 12px 或长段落。
- [ ] 同屏 primary 按钮 ≤ 1 个；同屏字号 ≤ 5 种（标题 18 / 小标题 16 / 正文 14 / 辅助 13 / 徽标 11）；同屏圆角 ≤ 3 种。
- [ ] 所有可点击元素有 `:focus-visible` 环，Tab 顺序符合视觉顺序。
- [ ] 表格：表头吸顶（底色 ≥85% 不透明）、数值右对齐、等宽数字、`table-fixed` 固定列宽不抖动。
- [ ] 商品卡标题恒 2 行，网格卡片高度一致（图片区 `aspect-square`）。

**窗口与布局**
- [ ] 标题栏 40px，三个窗口按钮 46×40，关闭 hover 为 `#C0364B`；按钮区 `-webkit-app-region: no-drag` 生效（能点不能拖）。
- [ ] 双击标题栏空白处切换最大化；最大化按钮图标随 `isMaximized()` 变为还原图标。
- [ ] 侧边栏 232px / 折叠 64px，当前项有 2px 品牌色指示条，折叠态 Tooltip 400ms 后出现。
- [ ] 内容区只有 `.app-content` 滚动，页面头与标题栏不跟着滚。
- [ ] 最小窗口 1180×720 下无横向溢出（表格内部滚动除外）。

**状态完整性**
- [ ] 商品列表 / 商品卡网格 / 任务列表三处均实现：空状态、骨架屏、错误状态 + 重试。
- [ ] 首次加载用骨架，切页/刷新用半透明 + spinner，失败有行内错误与区域级错误两种降级。
- [ ] 批量操作只出一条汇总 Toast，且带「查看详情」。
- [ ] 输入框聚焦环 3px、错误环 3px 且颜色分别为 `brand-600/18`、`danger-600/15`。

**动效**
- [ ] 进入 ≤ 240ms、退出 ≤ 180ms；列表项位移 ≤ 6px；页面切换无退场；同屏动画组 ≤ 2。
- [ ] 表格行无位移/缩放；数字不滚动；无装饰性循环动画（仅「审核中」状态点与骨架屏两处）。
- [ ] `prefers-reduced-motion` 下 Framer 位移变体已剥离（`useMotionSafe`），功能完整。
- [ ] `duration-*` / `ease-*` 全部取自 2.8 节扩展刻度，无 `duration-[...]` 任意值。

---

**维护约定**：任何新颜色、新字号、新圆角、新时长必须先加 Token 再使用；PR 中禁止出现魔法数值（`text-[17px]`、`#0a7`、`rounded-[13px]`、`duration-[350ms]`）。
