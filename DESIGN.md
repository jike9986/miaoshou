---
version: 1.0
name: aohuang-desktop
description: "奥皇上品 Windows 桌面生产工具：浅色中性表面、青绿单强调色、14px 正文、4px 间距、商品图与数据优先。以下为语义摘要；完整 Token 以 web/src/styles/tokens.css 为唯一实现。"
colors:
  brand: "#0B7A72"
  on-brand: "#FFFFFF"
  canvas: "#F6F8F9"
  surface: "#FFFFFF"
  surface-inset: "#EFF3F4"
  ink: "#11181C"
  ink-secondary: "#48565F"
  ink-muted: "#64737E"
  border: "#E1E7EB"
  success: "#0B7F57"
  warning: "#B45309"
  danger: "#C0364B"
  info: "#1F6FEB"
typography:
  page-title: { fontFamily: "system-ui, Microsoft YaHei", fontSize: 24px, fontWeight: 600, lineHeight: 32px }
  section-title: { fontFamily: "system-ui, Microsoft YaHei", fontSize: 16px, fontWeight: 600, lineHeight: 24px }
  body: { fontFamily: "system-ui, Microsoft YaHei", fontSize: 14px, fontWeight: 400, lineHeight: 22px }
  body-sm: { fontFamily: "system-ui, Microsoft YaHei", fontSize: 13px, fontWeight: 400, lineHeight: 20px }
  caption: { fontFamily: "system-ui, Microsoft YaHei", fontSize: 12px, fontWeight: 400, lineHeight: 18px }
rounded: { xs: 4px, sm: 6px, md: 8px, lg: 12px, xl: 16px }
spacing: { xxs: 4px, xs: 8px, sm: 12px, md: 16px, lg: 24px, xl: 32px }
---

# 奥皇上品 · DESIGN.md

> 面向设计代理与前端实现的唯一入口。完整数值见 [docs/design-system.md](docs/design-system.md)，实际 Token 以 [web/src/styles/tokens.css](web/src/styles/tokens.css) 与 [web/tailwind.config.js](web/tailwind.config.js) 为准。
>
> 适用：Electron + React + TypeScript + Tailwind 的 Windows 桌面生产工具，不是营销网站。

## 1. 产品气质

**数据优先、商品图优先、界面退后。** 用户每天处理大量商品，设计目标是扫读速度、低误操作率和长时间使用舒适度。

- 中性色承载绝大多数界面；品牌色只表达当前项、主操作、链接、焦点和进度。
- 边框与表面层级优先于阴影；阴影只用于真正浮起的菜单、弹窗和拖拽层。
- 不复制 Apple / Linear / Notion 等营销页的大字号、巨幅留白、渐变或深色展台。
- 不为“看起来高级”增加装饰、玻璃拟态、霓虹、无意义动效或多套品牌色。

## 2. 外部规范的吸收方式

[VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md) 是 84 份公开网站 DESIGN.md 的第三方整理，内容是公开可见 CSS 与页面模式的快照，不是各公司的官方内部规范，也不保证适用于产品 UI。

本项目只吸收跨产品成立的规则：

| 参考 | 吸收 | 不吸收 |
|---|---|---|
| [Linear](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/linear.app/DESIGN.md) | 克制品牌色、细边框、表面层级、紧凑组件 | 近黑营销画布、紫色品牌、超大展示字 |
| [Notion](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/notion/DESIGN.md) | 4px 间距体系、清晰组件状态、内容层级 | 营销 Hero、彩色故事卡、品牌插画 |
| [Figma](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/figma/DESIGN.md) | 黑白骨架、明确焦点、工具属性优先 | 大面积彩色区块、超大编辑字体 |
| [Apple](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/apple/DESIGN.md) | 内容/图片优先、交互色克制、触屏大热区 | 每屏一张产品海报、胶囊按钮体系 |
| [Airbnb](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/airbnb/DESIGN.md) | 图片完整呈现、图片优先的内容卡 | 圆角营销卡、珊瑚色品牌体系 |

来源边界与逐项研究见 [docs/design-standards-source-review.md](docs/design-standards-source-review.md)。

## 3. 设计 Token

### 颜色

组件只使用语义 Token，不写页面级十六进制颜色：

- 画布：`canvas`、`canvas-inset`、`sidebar`
- 表面：`surface`、`surface-hover`、`surface-active`、`elevated`
- 文字：`fg`、`fg-secondary`、`fg-muted`、`fg-disabled`、`fg-inverse`
- 边框：`border`、`border-subtle`、`border-strong`
- 品牌：`brand-50`…`brand-950`
- 状态：`success`、`warning`、`danger`、`info`

规则：

1. 正文使用 `fg` 或 `fg-secondary`；辅助信息使用 `fg-muted`。
2. `fg-disabled` 只用于禁用态，不能承担仍需阅读的信息。
3. 状态色不能单独承载含义，必须同时有文字、图标或形状。
4. 卡片叠卡片时，内层使用 `canvas-inset`，不再叠加白底和阴影。
5. 禁用不存在的旧类名：`fg-subtle`、`border-line`、`surface-sunken`、`surface-2` 及未定义的状态色阶。

### 字体

- 系统字体栈：Windows 优先 Segoe UI / Microsoft YaHei，不引入网络字体阻塞启动。
- 基准正文：14/22；次级正文：13/20；辅助说明：12/18。
- 11px 只用于次要元数据，不用于关键操作、错误原因或需要连续阅读的内容。
- 页面标题：24/32；区块标题：16/24；商品标题：14/20。
- 数量、价格、耗时和进度使用 `.num`，避免数字宽度变化造成布局跳动。

### 间距、圆角、层级

- 间距以 4px 为基准，常用 4 / 8 / 12 / 16 / 24。
- 控件圆角 8px，卡片 12px，浮层 16px；同一区域不混用无意义的圆角。
- 图标默认 16px，导航 18px；只用 lucide，保持相同 strokeWidth。
- 内容卡片默认白底 + 1px 边框；浮层才使用 `shadow-2/3`。

## 4. 组件契约

所有交互组件必须覆盖：默认、悬停、按下、键盘焦点、禁用、加载、错误（适用时）。

### 按钮与链接

- 主操作每屏最多一个；危险操作使用 `danger` 并确认影响对象和数量。
- 禁用按钮需要能解释原因；必要时把说明放在父元素 `title` 或就近提示中。
- 可点击文字必须看起来可点击，并有真实行为；禁止无响应的装饰性链接。
- 图标按钮必须有可访问名称。

### 表单

- 可见标签必须通过 `htmlFor/id` 或包裹关系关联到控件。
- 错误在字段附近展示，并使用 `aria-invalid` / `role=alert`；不仅靠红色。
- 数字输入有单位、范围与步进；无效输入不静默变成另一个值。
- 原生 Select 优先；自绘下拉必须具备方向键、Enter、Escape、焦点恢复和选中语义。

### 卡片与列表

- 商品图使用固定比例容器，避免加载时布局偏移；失败时有占位。
- 商品标题固定两行，数量与状态位置稳定。
- 卡片点击区必须有可见键盘焦点；选择框不能只在鼠标悬停时才能被键盘发现。
- 加载、空、错误、部分失败是不同状态，不能互相冒充。

### 弹窗与抽屉

- 打开后焦点进入，Tab 不逃逸，Escape 可关闭，关闭后焦点返回触发器。
- 关闭后必须立即移除可阻挡页面的遮罩；退出动画不能把透明弹窗留在 DOM 中。
- 表单弹窗避免误点遮罩丢失输入；危险确认默认聚焦取消。

## 5. 桌面布局

- 默认基准 1440×900；主框架由 40px 标题栏、232px 侧栏和内容区组成。
- 只有明确的内容容器滚动，页面标题和主要操作保持可见。
- 收起侧栏只保留必要导航图标与展开按钮，不重复品牌 Logo。
- 自动化页按“采集箱 → 处理设置 → 状态/摘要”排列；批量设置必须在开始处理前可读到。
- 100/页等高密度场景允许列表内部滚动，不允许截断分页数字、主要设置或任务状态。
- 窗口变窄时优先换行和内部滚动，不缩小文字、不隐藏关键动作。

## 6. 动效

- 动效只解释因果、层级和内容更新；常用 120–240ms。
- 不添加装饰性循环动画；加载与真实运行状态除外。
- 支持 `prefers-reduced-motion`。
- 开关、筛选、分页和摘要更新不能引起无关卡片高度或位置跳动。

## 7. 可访问性与内容

- 键盘可完成所有鼠标任务；焦点环清晰可见。
- 桌面鼠标热区至少 28×28px，触屏目标 44×44px，相邻目标留有间距。
- 正文和关键辅助文字满足 WCAG AA 对比度；品牌色不是降低对比度的理由。
- 文案直接说结果和下一步：失败必须说明原因与可执行动作。
- 不伪造进度、店铺、任务状态或成功提示。

## 8. 禁止事项

- 复制其他产品的品牌色、字体或营销布局。
- 在业务组件写新的色值、阴影、z-index 或动效时长。
- 使用 Tailwind 中不存在的“看起来像 Token”的类名。
- 用 `outline-none` 去掉焦点却不提供替代焦点样式。
- 把禁用、加载、空数据都做成同一种灰色卡片。
- 为对齐截图添加无功能按钮、假数据或静态假进度。

## 9. 代理交付检查

修改界面前先读本文件与相关组件；修改后必须：

```bash
pnpm --filter web design:check
pnpm --filter web typecheck
pnpm --filter web lint
pnpm --filter web build
```

并验证至少一种窗口尺寸、键盘焦点、加载/空/错误状态及真实数据下的视觉结果。自动检查只覆盖可静态证明的问题，不能代替 CDP 或人工视觉验收。
