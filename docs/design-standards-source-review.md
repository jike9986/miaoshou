# awesome-design-md 来源评审：设计规范索引与 tk-desktop 适用性

> 研究日期：2026-10-28 · 执行：独立研究子任务
> 一手来源仓库：<https://github.com/VoltAgent/awesome-design-md>
> 本地取证副本：`E:\codex工作台\p006\_awesome-design-md-research`（`git clone --depth 1`，commit `fcc70284932276d085f01ef9f83edde90b1258cb`，2026-10-28；只读，未修改）
> 标注约定：**[索引]** = awesome-design-md 索引/README 陈述的事实；**[原规范]** = 各站点 DESIGN.md / 原规范自身陈述的事实；**[适用性]** = 本文对 tk-desktop（Electron + React 18 + Tailwind 3.4 + shadcn/ui + Framer Motion 11 桌面生产工具）的判断，属观点而非来源事实。

---

## 1. 索引事实：这个仓库收录的是什么 **[索引]**

来源：<https://github.com/VoltAgent/awesome-design-md>（README.md，本地取证行 11–46、85–248）

1. **它收录的不是大厂官方设计规范，而是"DESIGN.md 分析文件"**。官方定义：DESIGN.md 是 Google Stitch 提出的概念——"A plain-text design system document that AI agents read to generate consistent UI"（README 行 35–44）。规范原文：<https://stitch.withgoogle.com/docs/design-md/overview/> 与格式规范 <https://stitch.withgoogle.com/docs/design-md/specification/>。
2. **收录规模**：README 徽章标注 84 份；本地取证 `design-md/` 目录实测 84 个子目录、84 份 DESIGN.md（与徽章一致）。README Collection 章节列出 82 条；`slack`、`spotify` 两份未出现在索引列表（slack 在本地副本存在但索引未列，spotify 同理——[适用性] 引用这两份时以目录为准）。
3. **每份 DESIGN.md 的固定结构**（README 行 203–217，[索引]）：

   | # | 章节 | 捕获内容 |
   |---|---|---|
   | 1 | Visual Theme & Atmosphere | 氛围、密度、设计哲学 |
   | 2 | Color Palette & Roles | 语义名 + hex + 功能角色 |
   | 3 | Typography Rules | 字体族、完整层级表 |
   | 4 | Component Stylings | 按钮/卡片/输入/导航含状态 |
   | 5 | Layout Principles | 间距阶梯、栅格、留白哲学 |
   | 6 | Depth & Elevation | 阴影系统、表面层级 |
   | 7 | Do's and Don'ts | 设计护栏与反模式 |
   | 8 | Responsive Behavior | 断点、触达目标、折叠策略 |
   | 9 | Agent Prompt Guide | 快速取色、可直接用的提示词 |

4. **授权与性质**（README 行 244–248，[索引]）：MIT License；"extracted design tokens represent publicly visible CSS values"，不主张拥有任何站点视觉标识。即这些是**第三方对公开站点 CSS 的逆向提取**，不是厂商官方规范文件。
5. **格式变体**（本地取证）：大多数 DESIGN.md 采用 YAML frontmatter（`colors:`/`typography:`/`rounded:`/`spacing:`/`components:` 结构化 token + 正文散文），少数（spotify 等）为纯散文章节体。frontmatter 内使用 `{colors.xxx}` 引用语法。

## 2. 收录清单与原文链接 **[索引]**

README Collection（行 85–200）按 9 类收录。原文链接均为 `getdesign.md/<slug>/design-md`；GitHub 侧对应 `design-md/<slug>/DESIGN.md`。

### AI & LLM 平台（13）
Claude <https://getdesign.md/claude/design-md> · Cohere <https://getdesign.md/cohere/design-md> · ElevenLabs <https://getdesign.md/elevenlabs/design-md> · Jobright <https://getdesign.md/jobright/design-md> · Minimax <https://getdesign.md/minimax/design-md> · Mistral AI <https://getdesign.md/mistral.ai/design-md> · Ollama <https://getdesign.md/ollama/design-md> · OpenCode <https://getdesign.md/opencode.ai/design-md> · Replicate <https://getdesign.md/replicate/design-md> · Runway <https://getdesign.md/runwayml/design-md> · Together AI <https://getdesign.md/together.ai/design-md> · VoltAgent <https://getdesign.md/voltagent/design-md> · xAI <https://getdesign.md/x.ai/design-md>

### 开发者工具与 IDE（7）
Cursor <https://getdesign.md/cursor/design-md> · Expo <https://getdesign.md/expo/design-md> · Lovable <https://getdesign.md/lovable/design-md> · Raycast <https://getdesign.md/raycast/design-md> · Superhuman <https://getdesign.md/superhuman/design-md> · Vercel <https://getdesign.md/vercel/design-md> · Warp <https://getdesign.md/warp/design-md>

### 后端、数据库与 DevOps（9）
ClickHouse <https://getdesign.md/clickhouse/design-md> · Composio <https://getdesign.md/composio/design-md> · HashiCorp <https://getdesign.md/hashicorp/design-md> · MongoDB <https://getdesign.md/mongodb/design-md> · Oracle <https://getdesign.md/oracle/design-md> · PostHog <https://getdesign.md/posthog/design-md> · Sanity <https://getdesign.md/sanity/design-md> · Sentry <https://getdesign.md/sentry/design-md> · Supabase <https://getdesign.md/supabase/design-md>

### 生产力与 SaaS（8）
Cal.com <https://getdesign.md/cal/design-md> · Intercom <https://getdesign.md/intercom/design-md> · Linear <https://getdesign.md/linear.app/design-md> · Mintlify <https://getdesign.md/mintlify/design-md> · Notion <https://getdesign.md/notion/design-md> · Resend <https://getdesign.md/resend/design-md> · SAP <https://getdesign.md/sap/design-md> · Zapier <https://getdesign.md/zapier/design-md>

### 设计与创意工具（6）
Airtable <https://getdesign.md/airtable/design-md> · Clay <https://getdesign.md/clay/design-md> · Figma <https://getdesign.md/figma/design-md> · Framer <https://getdesign.md/framer/design-md> · Miro <https://getdesign.md/miro/design-md> · Webflow <https://getdesign.md/webflow/design-md>

### 金融与加密（9）
Binance <https://getdesign.md/binance/design-md> · Coinbase <https://getdesign.md/coinbase/design-md> · Kraken <https://getdesign.md/kraken/design-md> · Mastercard <https://getdesign.md/mastercard/design-md> · Mercury <https://getdesign.md/mercury/design-md> · NerdWallet <https://getdesign.md/nerdwallet/design-md> · Revolut <https://getdesign.md/revolut/design-md> · Stripe <https://getdesign.md/stripe/design-md> · Wise <https://getdesign.md/wise/design-md>

### 电商与零售（6）
Airbnb <https://getdesign.md/airbnb/design-md> · Meta <https://getdesign.md/meta/design-md> · mydealz <https://getdesign.md/mydealz/design-md> · Nike <https://getdesign.md/nike/design-md> · Shopify <https://getdesign.md/shopify/design-md> · Starbucks <https://getdesign.md/starbucks/design-md>

### 媒体与消费电子（15）
1win <https://getdesign.md/1win/design-md> · Al Jazeera <https://getdesign.md/aljazeera/design-md> · Apple <https://getdesign.md/apple/design-md> · DJI <https://getdesign.md/dji/design-md> · HP <https://getdesign.md/hp/design-md> · IBM <https://getdesign.md/ibm/design-md> · NVIDIA <https://getdesign.md/nvidia/design-md> · Oura <https://getdesign.md/oura/design-md> · Pinterest <https://getdesign.md/pinterest/design-md> · PlayStation <https://getdesign.md/playstation/design-md> · SpaceX <https://getdesign.md/spacex/design-md> · Spotify <https://getdesign.md/spotify/design-md>（未列索引，目录存在）· The Verge <https://getdesign.md/theverge/design-md> · Uber <https://getdesign.md/uber/design-md> · Vodafone <https://getdesign.md/vodafone/design-md> · WIRED <https://getdesign.md/wired/design-md>

### 汽车（7）
BMW / BMW M / Bugatti / Ferrari / Lamborghini / Renault / Tesla（<https://getdesign.md/bmw/design-md> 等）

### 复古 Web（2）
Dell 1996 <https://getdesign.md/dell-1996/design-md> · Nintendo 2001 <https://getdesign.md/nintendo-2001/design-md>

**官方厂商设计系统对照**（[原规范]，用于区分"官方规范"与"第三方提取"）：
IBM Carbon <https://carbondesignsystem.com> · Apple HIG <https://developer.apple.com/design/> · Shopify Polaris <https://polaris.shopify.com> · Vercel Geist <https://vercel.com/geist>。**[适用性]** awesome-design-md 的 IBM 条目自述"faithful to Carbon Design System"（本地取证 `design-md/ibm/DESIGN.md` frontmatter），Vercel/Cursor/Warp/Superhuman/Slack 条目自述为 "inspired interpretation"——即部分条目连"逆向提取"都不是，只是风格仿写，采信时要降权。

## 3. 与桌面生产工具强相关的规范：可执行要点

以下逐条引用本地取证副本中的原文要点。**[原规范]** 全部提取自营销站点而非各产品 App 本体（Raycast 条目 Known Gaps 明确："The marketing site is documented; the in-product app surface is its own design system"）。

### 3.1 Linear — <https://getdesign.md/linear.app/design-md> · 本地 `design-md/linear.app/DESIGN.md`（548 行）

**[原规范]**
- 色彩：单彩色强调——薰衣草蓝 `#5e6ad2` 仅用于品牌标、焦点环、少量 CTA，"never decoratively"（frontmatter description）；语义色仅保留 success `#27a644`。
- 表面层级：`canvas #010102 → surface-1 #0f1011 → … → surface-4 #191a1b` 四级阶梯 + 1px hairline 边框（`#23252a/#34343a/#3e3e44`），"carries hierarchy without shadow"（行 271）。
- 排版：12 级层级表；负字距随字号递减（80px 时 −3.0px，body 16px 时 −0.05px，行 34–97）。
- 间距：4/8/12/16/24/32/48px 七级 + section 96px（行 121–129）。
- 圆角：xs 4 → pill 八级；卡片用 12px、永不用 pill（行 273）。
- 组件全部带 default/hover/pressed 状态变体（行 131–255）。

**[适用性] ★★★★★ 最高优先级参考。** tk-desktop 现有 [design-system.md](design-system.md) 与其同构：中性色占屏 ≥92%、品牌色 ≤6%、hairline 边框承层级、4px 基准间距、控件 8px/卡片 12px 圆角——几乎逐条吻合。可直接借鉴：①"四级表面阶梯 + hairline 替代投影"的深色扩展方案（本仓深色是 `.dark` 扩展，缺正式 token 表）；②负字距阶梯（本仓 display 层级未定义 letterSpacing 数值）；③组件状态变体以独立条目定义（对应 cva 变体写法）。

### 3.2 Raycast — <https://getdesign.md/raycast/design-md> · 本地 `design-md/raycast/DESIGN.md`（669 行）

**[原规范]**
-  elevation 完全由表面色阶梯构建，"never from drop shadows"（行 603、612）。
- 白色 pill 是唯一主 CTA；饱和强调色（黄/红/绿/蓝）只许出现在插画内，"never on chrome buttons or text"（行 608、614）。
- Inter 必须开 `font-feature-settings: "calt","kern","liga","ss03"`（行 604）。
- 卡片内边距克制在 16–24px，"Don't pad cards with 32px+"（行 617）。
- 交互元素 ≥36px 触控目标（行 634）；断点表 320→1920px 七级（行 621–631）。
- Iteration Guide：改 DESIGN.md 后跑 `npx @google/design.md lint DESIGN.md`，检查 broken-ref / contrast-ratio / orphaned-tokens（行 656）。

**[适用性] ★★★★☆。** 36px 控件高度、"surface 阶梯替代阴影"、"强调色不进 chrome"三条可直接写进本仓规范；`@google/design.md` lint 工具可用于校验我们自己的 DESIGN/design-system 文档 token 引用完整性。但其系统是 dark-only、桌面启动器语境，浅色生产工具需自行反转。

### 3.3 Notion — <https://getdesign.md/notion/design-md> · 本地 `design-md/notion/DESIGN.md`（821 行）

**[原规范]**：暖白 canvas `#ffffff` + 表面 `#f6f5f4/#fafaf9`；hairline `#e5e3df`；正文墨色 `#1a1a1a/#37352f/#5d5b54/#787671` 五级灰阶；语义色 success `#1aae39` / warning `#dd5b00` / error `#e03131`；pastel 卡片 tint 八色仅用于功能卡片（frontmatter colors 段）。

**[适用性] ★★★★☆。** 本仓是浅色唯一主主题（[design-system.md](design-system.md) §0），Notion 是全清单中浅色工作区语境最贴近的：五级灰阶文字、hairline 边框、暖白表面可直接对标本仓 `--canvas/--fg` 系列做校准参考。其彩色 pastel tint 体系对"商品状态标签"配色有参考价值，但注意这些是营销页用法。

### 3.4 IBM（Carbon）— <https://getdesign.md/ibm/design-md> · 本地 `design-md/ibm/DESIGN.md`

**[原规范]**：白表面 + 炭黑文字 + 单一 IBM Blue `#0f62fe`；圆角克制在 0–4px；卡片细边框无投影；IBM Plex Sans 300 字重做大标题（frontmatter description）。

**[适用性] ★★★★☆。** 企业级高密度工具的最佳锚点，且 Carbon 有真正的官方规范站 <https://carbondesignsystem.com>（含数据表格、表单密度规范——正是电商运营工具的核心场景）。[适用性] 建议：本仓表格/表单密度问题查 Carbon 官方文档，而非这份营销页提取。

### 3.5 Cursor / Vercel / Warp / Superhuman — 开发者工具组

**[原规范]** Cursor（`design-md/cursor/DESIGN.md`）：暖米白 `#f7f7f4` 编辑页画布 + 近黑暖墨 `#26251e` + 单一 Cursor Orange `#f54e00` 只做 CTA 和 wordmark；卡片 hairline 无阴影；80px 节距；JetBrains Mono 用于全部代码面。Vercel/Warp/Superhuman 自述为 "inspired interpretation"（风格仿写，非提取）。

**[适用性] ★★★☆☆。** Cursor 的"暖白画布 + 暖墨正文 + 单强调色"是本仓浅色主题可直接参考的配色气质；等宽字体用于数据/编码列（本仓商品 ID、货号列）可借其 "JetBrains Mono on every code surface" 做法。标注 inspired 的三份降权使用。

### 3.6 Spotify — 本地 `design-md/spotify/DESIGN.md`（246 行，索引未列出）

**[原规范]**：近黑沉浸 `#121212–#1f1f1f`；"UI recedes into shadow so content can glow"；Spotify Green `#1ed760` "never decorative, always functional"；pill 按钮 500–9999px、圆形控件 50%；重投影 `rgba(0,0,0,0.5) 0 8px 24px`（§1–2）。

**[适用性] ★★★☆☆。** 其"UI 无色、内容（商品图）即色彩"哲学与电商工具天然契合——本仓大量商品图场景可借"界面保持非彩色，让商品图提供色彩"这条。pill/重投影/全大写按钮与本仓"生产工具克制"定位冲突，不采。

### 3.7 Apple / Figma / Stripe / Shopify / Airbnb 等

**[适用性] ★★☆☆☆ 低优先级。** 均为营销/消费语境（大展示字、全幅摄影、装饰渐变），与本仓 §0 禁止项（装饰渐变、大面积玻璃拟态、装饰动画）直接冲突。仅 Apple HIG（官方 <https://developer.apple.com/design/>）的触控目标/层级原则可作背景阅读；Shopify 对应的官方 Polaris <https://polaris.shopify.com> 是电商后台规范，若要做商家后台类产品值得单独研究，但 awesome-design-md 的 Shopify 条目是"dark-first cinematic"营销页提取，不适用于运营工具。

## 4. 对 tk-desktop 的结论 **[适用性]**

1. **性质判断**：awesome-design-md 是"营销站点 CSS 逆向提取 + AI 可读化"的索引，价值在于**结构化 token 的组织方式**（YAML frontmatter + `{ref}` 引用 + 9 章节 + Do/Don't 护栏 + lint 工具链），而非具体色值。具体色值均来自营销页，不能直接搬进桌面生产工具。
2. **最值得借的三件事**：
   - **文档格式**：把本仓 [design-system.md](design-system.md) 的散文 token 表增补一份 YAML frontmatter 版本（Linear 式），让 AI agent 与 lint 工具可机读校验；
   - **护栏写法**：每套规范带 Do/Don't 清单（Raycast 式），本仓 §0 已有禁止项，可扩成成对护栏；
   - **lint 闭环**：`npx @google/design.md lint`（Raycast DESIGN.md 行 656 引用）检查 token 断引用与对比度。
3. **风格参考排序**：Linear（暗色扩展 + 克制 accent）≈ Notion（浅色灰阶）> IBM Carbon（查官方站）> Raycast（表面阶梯/触控目标）> Cursor（暖白气质 + 等宽数据列）> Spotify（内容即色彩）。其余全部视为营销风格参考，不进生产 UI。
4. **未改动声明**：本研究未修改 tk-desktop 内任何其他文件，未提交 git；取证副本位于仓外 `p006/_awesome-design-md-research`。

---

### 引用来源汇总

- awesome-design-md 索引：<https://github.com/VoltAgent/awesome-design-md>（commit `fcc7028`，本地取证 `p006/_awesome-design-md-research`）
- DESIGN.md 概念与格式规范（Google Stitch）：<https://stitch.withgoogle.com/docs/design-md/overview/> · <https://stitch.withgoogle.com/docs/design-md/specification/>
- 各站点 DESIGN.md 原文：`https://getdesign.md/<slug>/design-md`（逐条见 §2）
- 厂商官方设计系统：IBM Carbon <https://carbondesignsystem.com> · Apple HIG <https://developer.apple.com/design/> · Shopify Polaris <https://polaris.shopify.com> · Vercel Geist <https://vercel.com/geist>
- 本项目上下文：[docs/design-system.md](design-system.md) §0（Electron 壳 + React 18 + Tailwind 3.4 + shadcn/ui 浅色生产工具定位）
