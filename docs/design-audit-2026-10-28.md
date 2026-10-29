# 设计规范审计与内置治理：2026-10-28

> 对象：`tk-desktop` 当前前端。方法：先评审 [awesome-design-md 来源](./design-standards-source-review.md)，再静态审计 `web/src`，最后修复高置信问题并接入可执行检查。

## 1. 结论

- **P0：未发现**阻断级设计问题。
- **P1/P2：共 12 项高置信问题**，本次全部有对应修复。
- awesome-design-md 不是厂商官方规范；它适合提供 **DESIGN.md 机读格式、Token 组织、Do/Don't 护栏和 lint 思路**，不能把营销页色值、字体和布局直接搬进桌面生产工具。
- 本项目新增根级 [../DESIGN.md](../DESIGN.md)，作为代理和实现共用的唯一设计入口；`pnpm --filter web design:check` 已加入 `web.check`。

## 2. 本次吸收的规范

| 来源 | 吸收到本仓的规则 | 刻意不吸收 |
|---|---|---|
| Linear | 中性色承载界面、克制单一强调色、表面层级 + 细边框、组件状态完整 | 深色营销画布、紫色品牌、超大展示字 |
| Notion | 浅色多级灰阶、4px 间距、辅助信息低干扰 | 营销 Hero、彩色故事卡、品牌插画 |
| Figma | 黑白骨架、明确焦点、工具属性优先 | 彩色营销区块、编辑品牌字体 |
| Apple | 内容/图片优先、交互色克制、触控热区 | 每屏海报式布局、胶囊按钮体系 |
| Airbnb | 图片完整呈现、图片优先内容卡 | 圆角营销卡、珊瑚色品牌体系 |
| Raycast/Google Stitch | YAML frontmatter、Do/Don't 护栏、Token lint 思路 | dark-only 启动器风格、插画内高饱和色 |

详细来源边界和 84 项索引见 [design-standards-source-review.md](./design-standards-source-review.md)。

## 3. 内置治理

### 文档层

- 新增 [../DESIGN.md](../DESIGN.md)：YAML 机读摘要 + 产品气质 + Token/组件/布局/动效/可访问性契约 + 禁止事项。
- 保留 [design-system.md](./design-system.md)：继续作为完整数值与组件规格的详细手册。
- 新增 [design-standards-source-review.md](./design-standards-source-review.md)：84 项来源、官方规范对照和适用性分级。

### 执行层

- 新增 `web/scripts/design-check.mjs`。
- `web/package.json` 新增 `design:check`，并让 `check` 顺序执行类型、Lint、设计护栏。
- 当前检查覆盖：
  - 业务 TSX 禁止硬编码十六进制颜色；
  - 禁止已删除或不存在的 Token（如 `fg-subtle`、`border-line`、`surface-sunken`、未定义状态色阶）；
  - 禁止 `bg-overlay/xx` 这种 Tailwind 无法生成的遮罩写法；
  - `focus-visible:outline-none` 附近必须有替代焦点环；
  - 根级 DESIGN.md、详细设计系统、Token 文件和 reduced-motion 护栏必须存在。

## 4. 问题与修复

| # | 优先级 | 问题 | 处理 |
|---:|---|---|---|
| 1 | P1 | `bg-overlay/45` 不会生成有效遮罩，Dialog/Drawer/窄屏导航可能透明 | 改为直接使用自带透明度的 `bg-overlay`；商品图角标改用 `bg-inverse/55` |
| 2 | P1 | ConfirmDialog「默认聚焦取消」实际未生效 | 支持 `[data-dialog-cancel]` 初始焦点，取消按钮显式标记 |
| 3 | P1 | 窄屏导航抽屉无模态语义、Esc 和焦点锁定 | 增加 `role=dialog`、`aria-modal`、焦点陷阱、Esc 关闭、关闭后焦点恢复 |
| 4 | P1 | DropdownSelect 非搜索模式键盘无法选择 | 打开后聚焦 listbox；支持 ↑↓/Home/End/Enter/Esc；增加 active descendant 与焦点恢复 |
| 5 | P1 | automation 表单标签未关联控件 | 语言、主图/SKU/详情数量、重量、长宽高均补齐 `htmlFor/id` |
| 6 | P1 | automation 窄屏仍锁死 320px 右栏 | `<lg` 改单列整页滚动，卡片内部强制高度只在桌面模式启用 |
| 7 | P1 | 批处理逐件刷 Toast，再追加汇总 | 移除逐件 Toast，统一为一次结果汇总；明细保留在处理状态面板 |
| 8 | P2 | Tabs 不支持方向键、Home/End、roving tabindex | 已实现左右方向键循环、Home/End 与自动激活 |
| 9 | P2 | 存在不存在的 Token 类，样式静默丢失 | 修复 `text-fg-subtle`、`warning-200/300/800`、`danger-200`、`border-line`、`surface-sunken`、`surface-2`；由 design-check 防回归 |
| 10 | P2 | SkeletonRows 对读屏器静默 | 增加 `role=status`、`aria-busy`、可配置 label 和 sr-only 文案 |
| 11 | P2 | Tooltip 与触发器没有可访问关系 | 增加稳定 tooltip id，并在展开时通过 `aria-describedby` 关联 |
| 12 | P2 | 交互态绕过 Token，按钮密度与文档不一致 | 侧栏/标题栏/徽标/模板列表改用 `surface-hover/active`；按钮 md/lg 统一 14px 正文 |

## 5. 本次实测验收

### 命令

- `pnpm check`：web typecheck / ESLint / design-check 与 app typecheck / ESLint 全部通过；保留 24 条既有 fast-refresh / exhaustive-deps 警告，0 error。
- `pnpm --filter web build`：Vite 生产构建通过，2053 modules。
- `cd server && python -m pytest tests -q`：165 项通过。

### CDP（真实 Electron，1440×900 与 900×700）

- 弹窗遮罩计算值为 `rgba(12, 20, 24, 0.45)`，Esc 后 DOM 立即移除，无透明残留层。
- 危险确认弹窗打开后 `document.activeElement` 为「取消」，Esc 只关闭确认层，未触发删除。
- automation 8 个关键控件均有 `label[for]` 关联，缺失数为 0。
- Tabs 使用 ArrowRight 后从「商品列表」切到「批次」，roving tabindex 由 `[0,-1]` 变为 `[-1,0]`，焦点跟随选中项。
- 900×700 下 automation 不再显示固定 320px 右栏，整页可滚动；窄屏导航为 `role=dialog aria-modal=true aria-label=导航`，焦点进入抽屉，Esc 可关闭。
- 截图证据：[默认 1440×900](./screenshots/design-audit-2026-10-28-default.png)、[窄屏 900×700](./screenshots/design-audit-2026-10-28-narrow.png)。

## 6. 仍保留的差距

1. `Tabs` 已具备键盘模型，但组件本身不拥有面板，因此还没有统一的 `aria-controls/tabpanel id` 绑定；需要在调用方补面板 id，或后续拆出 `TabPanels`。
2. `design:check` 是静态护栏，只证明“可机查的违规不存在”；对比度、真实键盘路径和视觉层级仍需 CDP/人工验收。本次只完成核心页面与核心交互验收。
3. `@google/design.md` 外部 lint 本次只做来源研究，未作为依赖固定；当前采用零依赖脚本，避免 CI 被临时 npx 下载阻断。
4. 现有深色 Token 存在，但产品当前以浅色为主；深色视觉仍需专门截图和对比度验收。

## 7. 验收基线

修复后要求以下命令全部通过：

```bash
pnpm --filter web typecheck
pnpm --filter web lint
pnpm --filter web design:check
pnpm --filter web build
```

视觉验收至少覆盖：默认宽度、窄屏单列、弹窗遮罩、确认弹窗初始焦点、DropdownSelect 键盘选择、Tabs 方向键、Skeleton/Tooltip 可访问语义。
