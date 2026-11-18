# HANDOFF — 多店铺复选框选择（全账号放开）

> 本轮主题：`8cbe89b` 放开「多店铺勾选」的角色限制，并修复复选框状态更新。
> 仓库：`D:\缓存文件夹\codex工作台\p006\miaoshou`（注意：`docs/HANDOFF.md` 里写的 `E:\codex工作台\p006\tk-desktop` 是旧路径，已失效）
> 本文所有数字都附可复跑命令；命令跑不出同样结果 = 文档过期。

---

## 一、本轮交付项

| # | 交付项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 代码提交 `8cbe89b` | 已完成 | `git show --stat 8cbe89b` |
| 2 | 推送到 `origin/master` | 已完成 | HEAD = origin/master = `9e4c9e5`，ahead/behind = 0/0 |
| 3 | `npm run build` 出前端产物 | 已完成 | `web/dist/assets/index-BlcwlKgP.js` 515022 B |
| 4 | 产物复制到 `release/win-unpacked/resources/web/` | 已完成 | `diff -r web/dist release/win-unpacked/resources/web` 无差异 |
| 5 | 清空 Electron 缓存 `%LOCALAPPDATA%\aohuang-desktop` | 已完成 | 目录 11:05 重建，11:20 有写入 |
| 6 | 交接文档 | 本文 | — |

复核命令：

```bash
cd "D:/缓存文件夹/codex工作台/p006/miaoshou"
git log --oneline -6
git rev-list --left-right --count origin/master...HEAD      # → 0  0
git show --numstat --format='' 8cbe89b                      # → launch.json 11/0, PublishDialog 16/17, tsbuildinfo 1/1
diff -r web/dist release/win-unpacked/resources/web && echo IDENTICAL
stat -c '%s %n' web/dist/assets/index-BlcwlKgP.js           # → 515022
```

### 提交内容（3 个文件，+28 / -18）

```
.claude/launch.json                        | 11 ++++++++++   ← 混入的调试配置
web/src/features/publish/PublishDialog.tsx | 33 ++++----     ← 本轮实质改动
web/tsconfig.tsbuildinfo                   |  2 +-           ← 混入的构建缓存
```

`web/src/features/publish/PublishDialog.tsx` 三处改动：

1. **角色限制移除**（第 63 行）
   ```ts
   // 旧：const isTeacher = identity?.role !== "member" && accounts.length > 1;
   // 新：
   const isTeacher = accounts.length > 1;   // 多账号模式：只要有多个账号就显示多账号选择器
   ```
2. **导入清理**：删掉 `useOperatorIdentity`（`@/lib/operator`），该文件内已无其它用途。
3. **店铺复选框状态更新改函数式**（第 478–493 行）：`prev` 从闭包外层取值改为在 `setSelectedShopIdsByAccount(s => …)` 内部取，避免连续点击多个店铺时基于旧快照覆盖。

多账号模式下每个账号独立的多选店铺列表在 470–499 行；账号级懒加载店铺在 108–137 行。

---

## 二、验证结论

### 已确认通过

| 项 | 证据 |
|---|---|
| TypeScript 通过 | `cd web && npx tsc --noEmit` → exit 0 |
| 产物与部署目录逐字节一致 | `diff -r` 无输出；`index-BlcwlKgP.js` md5 = `512b351c81cfb0396d2d891851b3252e` 两侧相同 |
| `index.html` 也同步 | md5 `da3cfb75cd1c73d68b92c6ce373579cb` 两侧相同，引用的正是 `index-BlcwlKgP.js` |
| 打包后逻辑真的是「无角色门控」 | 产物中门控编译为 `u&&c.length>1`（`node -e` 切片查看 `发布账号` 上文），不再是 `role!=="member"` |
| 新文案已进产物 | `老师模式：每个账号独立发布`、`加载店铺中`、`暂无店铺（未绑定）` 均在 bundle 内 |

### 产物体积口径

| 指标 | 值 |
|---|---|
| 原始 | 515,022 B（502.9 KiB / 515.0 kB） |
| gzip -9 | 149,360 B |

⚠️ 原交付说明里写的「484.31 KB」与当前实际文件不符。两种口径（kB / KiB / gzip）都对不上，判断是更早一次构建的读数，或者抄了别的产物。以 `stat` 为准。

---

## 三、问题分析（必须知道的三件事）

### 3.1 打包形态：正式客户端**只连远程**，不分发本地后端

这点容易误判成「包坏了」。事实：

- `app/electron-builder.yml` 的 `extraResources` 只有 `../web/dist → web`，注释明确写「正式客户端通过 HTTPS 访问服务器，不分发本地后端或共享客户端令牌」。
- `main.js:312` `REMOTE_API_BASE = "https://aohuang888.com/miaoshou-platform"`；`useRemoteServer()` 在 `app.isPackaged` 时直接返回 `true`（`main.js:314-325`），本地后端只在 dev 或 `AOHUANG_LOCAL_BACKEND=1` 时起。
- `scripts/verify-desktop-package.mjs:28` 反向断言：`resources/server` 存在就是失败。

所以 `release/win-unpacked/resources/` 只有 `app.asar` + `web/` 是**设计如此**，不是漏打后端。

### 3.2 `release/win-unpacked` 是「半手工拼的」，不是 electron-builder 完整产物

发现的事实：

- `resources/app.asar` 内 `package.json` 版本 = **0.1.24**，而 `app/package.json` = 0.1.25 → `node scripts/verify-desktop-package.mjs` 直接挂：`Packaged app version is stale: '0.1.24' !== '0.1.25'`。
- asar 里的 `dist/main.js` 与当前 `app/dist/main.js` **完全相同** → 代码是新的，只有版本串是旧的。
- `win-unpacked/` 根目录还躺着 `main.js`、`preload.js`、`sessionVault.js`、`updateInstaller.js`、`package.json`（749 B，即 `app/package.json` 的副本）。electron-builder 不会把 `main.js` 放在这一层（它在 asar 里），这些是手工复制留下的。运行时以 asar + 其内 `package.json` 为准，根目录这堆是噪音，但会误导后来人。
- 结论：本轮只做了「替换 `resources/web/`」。要出一个**能发**的包，必须重跑 `electron-builder --win --dir`（或 `scripts/build.ps1`）。

### 3.3 版本号五处不一致，`build.ps1` 第一步就挂

`cd server && python -X utf8 scripts/check_version.py`：

```
package.json（根 / electron-builder）        0.1.25
web/package.json（前端）                      0.1.25
app/package.json（Electron 壳）              0.1.25
server/core/constants.py（APP_VERSION 兜底）  0.1.25
installer/setup.iss（安装包文件名与显示版本）  0.1.23  ← 不一致
```

改法：`installer/setup.iss:6` `#define MyAppVersion "0.1.23"` → `"0.1.25"`。
不修这条，`scripts/build.ps1` 的「1/5 版本与静态检查」会 throw，整条打包链走不下去。

---

## 四、遗留问题 / 待办

按优先级排序。

### P0 — 会影响功能正确性

**P0-1 只勾 1 个账号时，发布会用错账号**
多账号 fan-out 分支的进入条件是 `checkedAccountIds.length > 1`（第 223 行）。用户在全选列表里**只勾 1 个**账号时，代码落到单账号分支（第 270–285 行），那里用的是 `shopId`（来自 `useShops(auth.accountId)`，即**当前连接账号**的店铺），`accountId` 根本不传。
后果：勾了账号 B、但只勾一个 → 实际发到当前账号 A 的某个店铺。
修法建议：分支条件改为 `isTeacher && checkedAccountIds.length >= 1`，或在 `length === 1` 时把 `accountId: checkedAccountIds[0]` 与对应 `shopsByAccount[checkedAccountIds[0]]` 一起带进单账号分支。

**P0-2 前端仍在拦语言不匹配，后端已经不管了**
`ff07096`「disable language check on publish」只改了后端：`server/services/publish_service.py:1108-1112` 的 `check_language` 调用被注释掉。
但前端 `PublishDialog.tsx:217` 仍然 `if (mismatch && !allowMismatch) { setError(…); return; }`，第 589 行还会渲染「默认会被后端拒绝（422 SITE_LANGUAGE_MISMATCH）」的警告块。
后果：跨站发布被前端挡住，用户看到的理由（后端会拒绝）已是假话。
**另注**：`language` 与 `exchangeRate` 两个 state 在提交体里根本没传（第 238–247、276–285 行两处都没带），mismatch 判定纯靠本地默认值算出，属于死逻辑。要么补上 `language` 传参并放开拦截，要么把整块 UI + 校验删掉。

**P0-3 `release/win-unpacked` 不可交付**
见 3.2、3.3。需要：修 `setup.iss` 版本 → `pnpm build` → `cd app && pnpm exec electron-builder --win --dir` → `node scripts/verify-desktop-package.mjs` 通过 → 再出安装包。当前 `release/` 里最新的安装程序还是 `奥皇上品-0.1.23-Setup.exe`，`latest.yml` 停在 0.1.21，自动更新链路没动。

### P1 — 一致性与可维护性

**P1-1 定时发布页没跟上这次的改动**
`web/src/features/schedules/SchedulesPage.tsx:698` 仍是 `const isTeacher = identity?.role !== "member" && accounts.length > 1;`，与 PublishDialog 的新口径不一致：同一个学员账号，在发布弹窗里能看到多账号选择器，在定时任务里看不到。
且该页每账号只能选**一个**店铺（`Select`，第 1009 行 `selectedShopByAccount`），本轮「多店铺勾选」能力没有覆盖到定时任务。若需求含定时任务，这块是缺口。

**P1-2 提交里混入构建噪音**
`8cbe89b` 带进了 `.claude/launch.json`（本机 web-dev 调试配置）和 `web/tsconfig.tsbuildinfo`。
后者更麻烦：`.gitignore:69` 已写 `*.tsbuildinfo`，但该文件从 `d4d1df8`（首次提交）起就被跟踪，gitignore 对已跟踪文件无效，所以每次构建都会产生 diff。
清理：
```bash
git rm --cached web/tsconfig.tsbuildinfo
git rm --cached .claude/launch.json      # 若确认只是本机调试用
git commit -m "chore: untrack build cache and local debug config"
```

**P1-3 根目录还躺着一份过期的 `main.js`**
仓库根的 `main.js`（21803 B，最后一次改动 `c6f2e94` 2026-11-17）与 `app/dist/main.js`（21595 B）**内容不同**。真正的 Electron 入口是 `app/src/main.ts` → `app/dist/main.js`。根目录这份是被跟踪的历史遗留（`git ls-files` 里确有 `main.js`），容易被误改。建议确认无引用后 `git rm`。

### P2 — 代码质量

**P2-1 `e.target.checked` 读得仍然偏晚**
第 478–493 行的「修复」把 `prev` 移进了 updater，但 `e.target.checked` 依旧在 updater 内部读。updater 是在 React 渲染阶段执行的，不是事件回调里立即执行。虽然 React 18 不再池化事件、`target` 指向的 DOM 节点也还在，但严格说应该在回调开头先取值：
```ts
onChange={(e) => {
  const next = e.target.checked;   // 先落值
  setSelectedShopIdsByAccount((s) => { … 用 next … });
}}
```

**P2-2 多账号 fan-out 用同一个 `template` 打所有账号**
第 242 行 `template: template || null` 是全局单值。而单账号路径会把「该账号上次选的模板」作为默认值带出（第 149 行）。多账号场景下各账号可能适用不同模板，目前无法分别设置。

**P2-3 多账号 fan-out 的失败信息只显示第一条**
第 262 行 `toast.error(`${failMsgs.length} 个店铺发布失败`, { description: failMsgs[0] })` —— 只透出 `failMsgs[0]`，其余失败原因丢失。建议在结果区列出「账号 × 店铺 → 失败原因」全量清单。

---

## 五、测试清单（回归用）

前提：需要至少一个「多账号」的 operator 账号（`auth.accounts.length > 1`），且其中至少一个账号绑定了 ≥ 2 个店铺。

### A. 角色放开

- [ ] **A1** 用 `role = "member"` 的账号登录，若其可见账号 > 1，打开发布弹窗 → 应出现「发布账号」多选卡片区（本轮修复点）
- [ ] **A2** 用 `role = "teacher"` 的账号登录 → 行为与修复前一致，无回归
- [ ] **A3** 单账号用户（`accounts.length === 1`）→ 仍是「目标店铺」下拉，不是多选卡片

### B. 店铺多选

- [ ] **B1** 展开某账号（有 3 个店铺）→ 3 个复选框全部未勾
- [ ] **B2** 快速连点第 1、第 2 个店铺 → 两个都变勾选，无「后点覆盖先点」现象（本轮修复点）
- [ ] **B3** 再点掉第 1 个 → 只剩第 2 个勾选
- [ ] **B4** 取消勾选整个账号 → 该账号卡片折叠，店铺复选框消失；重新勾上 → 店铺列表回来且选中态已重置
- [ ] **B5** 某账号只有 1 个店铺 → 显示 `→ 店铺名` 文本，不显示复选框
- [ ] **B6** 某账号 0 店铺 → 显示红字「暂无店铺（未绑定）」
- [ ] **B7** 账号刚从别的账号切过来 → 短暂显示「加载店铺中…」，随后出结果（不卡死）

### C. 发布行为

- [ ] **C1** 勾 2 个账号，各勾 1–2 个店铺 → 按钮文案为「向 2 个账号发布 N 件」→ 点击后每个「账号 × 店铺」都产生一条发布记录
- [ ] **C2** 勾 2 个账号但都不勾店铺 → 按设计回落到各账号第一个店铺，需确认这是期望行为
- [ ] **C3** 勾 1 个账号（多账号场景）→ **重点验证 P0-1**：发布是否落到勾选的那个账号，而不是当前连接账号
- [ ] **C4** 部分店铺失败 → toast 显示失败条数，确认失败明细是否足够定位（见 P2-3）
- [ ] **C5** 一批 200 件以上 → 仍被「一次最多发布 200 件」挡住

### D. 打包产物

- [ ] **D1** `python -X utf8 scripts/check_version.py` → 五处全部 0.1.25，exit 0
- [ ] **D2** `node scripts/verify-desktop-package.mjs` → 无 assertion 失败（当前挂在 asar 版本 0.1.24）
- [ ] **D3** `release/win-unpacked/AohuangShangpin.exe --no-sandbox` 起窗口，DevTools → Sources 确认加载的是 `index-BlcwlKgP.js`
- [ ] **D4** 打包版连的是 `https://aohuang888.com/miaoshou-platform`（Network 面板核对），不是 127.0.0.1
- [ ] **D5** 清空 `%LOCALAPPDATA%\aohuang-desktop` 后启动 → 回到登录页，登录后 `operator-session.v1.json` 重建

---

## 六、后续调查方向

1. **多账号扇出的幂等性**：`usePublish` 每次 mutate 都生成新的 `idempotencyKey("pub")`（`web/src/lib/queries.ts:110-116`）。同一批商品发到 N 个店铺 = N 个独立 key，后端 `publish_keys` 按「商品 × 目标店铺」去重（`publish_service.py` 内注释），所以不会互相挡。但若用户连点两次「发布」，第二批是全新 key，**重复上架不会被拦** —— 需要在后端或前端加批次级去重。
2. **`accountId` 权限边界**：`require_account(db, accountId)` 对 `member` 会走 `has_account_access` 校验（`account_service.py:133-176`），teacher/admin 全局放行。本轮把前端门控放给所有角色后，`member` 若被绑定了多个账号，理论上可以一次向多个账号发布 —— 需确认这是产品意图，还是应该在后端加限制。
3. **店铺列表缓存**：`fetchShopsForAccount` 用 `if (accId in shopsByAccount) return;` 做一次性缓存，一个弹窗生命周期内只拉一次。若发布过程中对方账号新增了店铺，需要关窗重开才能看到。是否需要 `refetch`？
4. **定时发布对齐**：见 P1-1。要把「多账号 + 多店铺」做成统一能力，建议抽一个 `AccountShopPicker` 组件，两页共用，避免第 3 次出现同样的 role 门控漂移。
5. **`confirm` 三重保险**：当前 `confirmed` 默认 `true`（第 94 行，2026-11-05 负责人决定取消二次确认），`submit` 里的 `if (!confirmed)` 已成死代码。若后续要恢复确认，记得同时恢复 UI 勾选框。

---

## 七、快速索引

```
web/src/features/publish/PublishDialog.tsx    本轮改动主体
  :48    accounts prop 类型
  :63    isTeacher = accounts.length > 1        ← 角色放开
  :108-137 账号级店铺懒加载
  :140-158 打开弹窗时的状态重置（多账号默认全选）
  :217   语言不匹配拦截（已成死逻辑，见 P0-2）
  :223   多账号扇出入口（length > 1，见 P0-1）
  :470-499 店铺多选复选框（本轮修 onChange）
  :512-522 单账号「目标店铺」下拉（向后兼容）

web/src/features/schedules/SchedulesPage.tsx   同类功能未同步（P1-1）
  :698   role 门控仍在
  :1009  每账号单选店铺

app/electron-builder.yml                       打包配置：只有 web/dist，无本地后端
main.js (根，过期副本) / app/dist/main.js (真实入口)
  :312   REMOTE_API_BASE
  :314-325 useRemoteServer()
server/services/publish_service.py
  :1106-1112  语言检查已注释
scripts/check_version.py                       五处版本一致性（当前失败）
scripts/verify-desktop-package.mjs             产物校验（当前失败：asar 0.1.24）
installer/setup.iss:6                          MyAppVersion 0.1.23 ← 待改 0.1.25
docs/HANDOFF.md                                旧版交接（2026-10-27，路径已失效）
```

---

## 八、0.1.25 发版记录（2026-11-18 下午）

### 为什么桌面端一直识别不到 0.1.25

根因不在客户端，在**服务端更新清单**：

    $ curl https://aohuang888.com/miaoshou-downloads/version.json
    { "version": "0.1.23", ... }                       ← 停在 0.1.23

    $ curl -o /dev/null -w '%{http_code}' .../AohuangShangpin-0.1.24-Setup.exe
    404
    $ curl -o /dev/null -w '%{http_code}' .../AohuangShangpin-0.1.25-Setup.exe
    404

客户端判定链：`GET /system/version` → `fetch_manifest()` 拉上面这份 JSON → `is_newer(latest, current)`。
线上 latest 是 0.1.23，本机 current 也是 0.1.23 → 相等 → `updateAvailable: false`。
**0.1.24 / 0.1.25 的安装包从来没有生成、也没有上传过。**

顺带确认：本机 `D:\软件\奥皇上品` 里装的其实也是 **0.1.24**（asar 的 package.json 写的就是 0.1.24），
前端 bundle 里 `h-3 w-3`（店铺复选框的类名）根本不存在 → 所以界面上多店铺账号只有一个下拉单选。

### 本次做完的事

| 步骤 | 结果 |
|---|---|
| `installer/setup.iss` 版本 0.1.23 → 0.1.25 | `check_version.py` 五处一致，exit 0 |
| 修 `scripts/verify-desktop-package.mjs` 的 mock | 补 `setVisualZoomLevelLimits() {}`，脚本原本抛 TypeError 而非报真实产物问题 |
| 修 `PublishDialog.tsx` 扇出条件 | `length > 1` → `> 0`，只勾 1 个账号也走多店铺分支 |
| `pnpm build` | 通过，产物 `index-BGivIiLi.js` 484.78 kB |
| `electron-builder --win --dir` | 通过 |
| `node scripts/verify-desktop-package.mjs` | **通过**：`Desktop package verified: v0.1.25, 8 current web files, HTTPS software login, no shared token or local backend` |
| `electron-builder --win --publish never` | 生成 `release/奥皇上品-0.1.25-Setup.exe`（71,439,893 B） |
| 上传安装包 + 重写 version.json | sha256 `345144f69f19322bac2e84887a4a067502f531a06730956eb99b7caa2770937e`，远端校验一致 |
| 后端部署 | `publish_service.py` + `core/constants.py` 上传，服务 active，health 返回 `{"ok":true,"version":"0.1.25","db":"ok"}` |
| 本机安装目录同步 0.1.25 | 已备份到 `D:\软件\奥皇上品.bak-0.1.24-20261118`（264 MB），再覆盖 |

### 公网实测

    version.json          200，内容已是 0.1.25
    AohuangShangpin-0.1.25-Setup.exe   200，Range 请求返回 206（支持断点续传）

### 后端判定复算（用项目自己的函数）

    manifest latest = 0.1.25
      current=0.1.23  updateAvailable=True
      current=0.1.24  updateAvailable=True
      current=0.1.25  updateAvailable=False
      current=0.1.26  updateAvailable=False

即 0.1.24 的客户端下次检查更新时**一定会**识别到 0.1.25。

### 0.1.24 → 0.1.25 前端产物功能差异（实测 grep）

| 特征 | 0.1.24 | 0.1.25 |
|---|---|---|
| 店铺复选框 `h-3 w-3` | ✗ | ✓ |
| 「多店铺账号可勾选」提示 | ✗ | ✓ |
| 「向 N 个店铺发布」按钮文案 | ✗ | ✓ |
| 账号/店铺失败明细 | ✓ | ✓ |

---

## 九、本次核验中动过的文件

- 恢复过一次仓库根 `package.json`：核验 asar 版本时 `npx asar extract-file` 把包内 `package.json`（`{"name":"app",...}`）写到了当前目录，覆盖了根 `package.json`。已用 `git checkout -- package.json` 还原（现在 1105 B，version 0.1.25，`git status` 干净）。
- 除上述还原外，本轮**未修改任何源文件**，也未提交任何改动。
