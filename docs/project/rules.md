# 项目规范

> 本文是强制约束，不可因"方便"而绕过。每次修改代码前先读相关章节。

---

## 0. 文档及时更新（最高优先级）

**用户说的所有有用信息，当次对话里就要写进文档。不只是"规范"——任何业务背景、偏好、限制、临时决定、发现的问题都算。对话结束前检查一遍有没有漏。**

| 用户说的内容 | 更新目标文档 |
|---|---|
| 新 bug / 已修复问题 | `docs/features/{功能}/tasks.md` |
| 新业务规则/约束/偏好 | `docs/project/rules.md` + `docs/features/{功能}/spec.md` |
| 需求变更 / 新验收标准 | `docs/features/{功能}/requirements.md` |
| 部署/环境信息 | `CLAUDE.md` 和/或 `DEPLOY.md` |
| 技术方案变更 | `docs/features/{功能}/spec.md` |
| 临时决定/背景说明/特殊情况 | `docs/features/{功能}/spec.md` 或 `docs/project/rules.md` |

不更新 = 下次对话丢失 = 等于白说。

---

## 1. 版本管理

### 版本号格式
`0.MINOR.PATCH` — 修 bug 加 PATCH，新功能加 MINOR，不用 MAJOR。

### 五处必须同步
```
package.json                     "version": "0.1.xx"
web/package.json                 "version": "0.1.xx"
app/package.json                 "version": "0.1.xx"
server/core/constants.py         return "0.1.xx"（实际读根 package.json，此为兜底）
installer/setup.iss              #define MyAppVersion "0.1.xx"
```

> 第 5 处是 2026-11-18 补的：`installer/setup.iss` 的 `MyAppVersion` 决定安装包文件名和
> 控制面板里显示的版本，漏改它 `scripts/build.ps1` 第一步就 throw。`server/core/constants.py`
> 的 `APP_VERSION = _package_version()` 优先读仓库根 `package.json`，只有打包后读不到才用字面量。

验证命令（一条命令查全部五处，不一致直接非零退出）：
```bash
python -X utf8 scripts/check_version.py      # exit 0 = 五处一致
```
```bash
grep '"version"' package.json web/package.json app/package.json
grep 'return "0\.' server/core/constants.py
grep 'MyAppVersion' installer/setup.iss
```

### 打包事实（勿把正常行为当 bug）
- 正式客户端 **只连远程** `https://aohuang888.com/miaoshou-platform`（`app/src/main.ts` 的 `REMOTE_API_BASE`），`app.isPackaged` 时 `useRemoteServer()` 恒为 true。
- `app/electron-builder.yml` 的 `extraResources` 只有 `../web/dist → web`，**不分发本地后端**。`release/win-unpacked/resources/` 下没有 `server/` 是设计如此，`scripts/verify-desktop-package.mjs` 也是反向断言。
- 本地后端只在 dev 或 `AOHUANG_LOCAL_BACKEND=1` 时启动。
- 只替换 `release/win-unpacked/resources/web/` 等于「换了前端」，不是完成打包。`app.asar` 版本会停在旧值，`node scripts/verify-desktop-package.mjs` 会报 `Packaged app version is stale`。完整打包走 `scripts/build.ps1` 或 `cd app && pnpm exec electron-builder --win --dir`。

---

## 2. 代码规范

### Python（server/）
- 所有时间戳用 `now_ts()`（Unix 秒，`REAL`），不用 `datetime`
- 并发写商品必须调 `revision_guard(db, product, client_revision)`
- 编辑商品任意字段后，若 `product.automation_result` 存在且 `draft_verified_revision is not None`，立即同步：
  ```python
  product.draft_verified_revision = int(product.revision or 0)
  ```
- 错误用 `raise errors.AppError("CODE", "中文说明", status=4xx)`，不要裸 `raise HTTPException`
- 新增 API 路由必须在 `docs/project/api-changelog.md` 记录

### TypeScript（web/）
- 服务端状态全用 TanStack Query，不用 `useState` 存服务器数据
- 修改后调 `queryClient.invalidateQueries({ queryKey: queryKeys.xxx.all })`，不要手动刷页面
- 组件只用 Tailwind 类名，不写 inline style
- 新查询键在 `src/api/queryKeys.ts` 统一注册

### 禁止事项
- 不往代码里硬编码密钥、密码、API key
- 不直接 `git reset --hard` 或 `rm -rf`（除非用户明确要求）
- 不在 Electron 渲染进程里直连数据库
- 不绕过 `revision_guard` 修改 `product.revision`

---

## 3. 分支与提交

- 主分支：`main`
- 修 bug：`fix/简短描述`
- 新功能：`feat/简短描述`
- 提交消息末尾加：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

## 4. 构建与部署

### 前端构建
```bash
# 必须在 bash（不是 PowerShell）
cd /d/缓存文件夹/codex工作台/p006/miaoshou
pnpm build
```

产物在 `release/win-unpacked/`。

### 桌面端部署（本地）
```bash
cmd /c scripts\\do_deploy.bat
```

脚本会：停止旧进程 → 覆盖 exe + app.asar + resources/web → 完成。用户手动重启应用。

### 后端部署（服务器）
```bash
python scripts/deploy_server.py
```

脚本会：SSH 上传 4 个 Python 文件 → `systemctl restart aohuang-listing-v2` → 验证 `active`。

SSH 凭据：`C:\Users\Administrator\Desktop\雨云.txt`（最后一行是密码）。

### 完整发版顺序
1. 四处同步版本号
2. `pnpm build`（构建前端+Electron）
3. `python scripts/deploy_server.py`（部署后端）
4. `cmd /c scripts\\do_deploy.bat`（部署桌面端）

---

## 5. 数据库

- 生产：PostgreSQL 14，`127.0.0.1:5432/miaoshou_listing_v2`（服务器本地）
- 开发：SQLite WAL，`%LOCALAPPDATA%\aohuang-desktop\app.db`
- 迁移用 Alembic，见 `docs/migrations.md`
- 不要在迁移脚本里写业务逻辑

---

## 6. 外部依赖

| 服务 | 用途 | 限制 |
|---|---|---|
| 妙手开放平台 | 商品读写、发布 | 账号级 1.5s 节流，HMAC-SHA256 签名 |
| 图片翻译 API | 图片文字翻译 | 费用上限 10 元/批次，异步任务 |
| DeepSeek API | 颜色规范化翻译 | key 在服务器环境变量 |
| aohuang888.com | 更新包分发 | nginx 反代，Let's Encrypt 证书 |

---

## 服务器选型：瓶颈在线路，不在 CPU / 内存（2026-11-20 实测）

负责人反馈「别人下载更新软件很慢」。实测结论：**跟 CPU、内存、磁盘全都无关，
是出口线路质量差**。

### 实测数据（生产 38.95.75.47 / 香港 AS979 NetLab Global）

| 指标 | 数值 | 判定 |
|---|---|---|
| CPU 负载 | load 0.12 / 4 核（AMD EPYC 7A23） | 空闲 |
| **3 并发下载时 CPU** | **98% idle，0% steal** | **完全不是瓶颈** |
| 内存 | 8 GB 用 1.07 GB，6.1 GB 是 buff/cache | 富余 |
| 磁盘 | 97 GB 用 41 GB（43%） | 富余 |
| IO pressure | avg10=3.5% | 空闲 |
| TCP 拥塞控制 | BBR + fq（已开） | 已调优 |
| nginx 静态下载限速 | 无（limit_req 只用在 aohuang-ai 的登录/注册） | 无限制 |

**关键指标：TCP 重传率 14.9%**

```
TcpOutSegs          32,440,467
TcpRetransSegs       4,835,126   → 14.9%
TcpExtTCPLostRetransmit 731,525
```

正常链路 < 1%，跨境优质线路 1–3%，**14.9% 属于线路质量不合格**。

### 同机同时段对照（排除用户端带宽问题）

| 目标 | 速度 |
|---|---|
| 自家服务器 38.95.75.47 | 200 KB/s ~ 1.4 MB/s（波动剧烈） |
| Cloudflare 香港节点 | **3 ~ 9 MB/s（稳定）** |
| 阿里云国内镜像 | 3 KB/s（跨境回程几乎不通） |
| 服务器本机回环 | 13 MB/s（证明服务器出口网卡没问题） |

同一台客户端、同一时段，**Cloudflare 快 5–10 倍** → 带宽和客户端都没问题，
是这台服务器的跨境出口线路差。

### 单连接 vs 多连接

```
单连接下载 74 MB 安装包：205 秒，363 KB/s
4 连接并发合计：        1.7 MB/s
```

单连接被限死，多连接才跑得起来 —— 说明瓶颈在**单条 TCP 流的传输效率**
（高 RTT × 高重传 = BBR 也救不回来），不是总带宽不够。

Electron 自动更新是**单连接下载**，所以用户感觉特别慢。

### 换服务器该看什么

**不看 CPU 核数，不看内存大小，看这三样：**

1. **回程线路**：必须是 CN2 GIA / 三网优化（联通 9929 / 移动 CMI）。
   「香港」「BGP」这种泛称不够 —— AS979 就是普通国际线路，
   去程可以但回程差。
2. **重传率**：上机第一件事跑 `nstat -az | grep TcpRetransSegs`，
   超过 3% 就退。这个数比任何测速脚本都准。
3. **带宽是否独享**：共享 100Mbps 在高峰期会烂到没法用。

**CPU / 内存按现有用量给够了**：4 核 8 GB 现在用不到 15%，
同规格就够，加钱堆配置不会有任何改善。

### 另一条更省的路：安装包走 CDN

安装包是**纯静态文件**，跟业务逻辑无关。Cloudflare 免费版就能缓存，
实测 3–9 MB/s。**不需要换服务器**，把 `miaoshou-downloads/` 挂到
Cloudflare 后面即可 —— 这是成本最低、见效最快的一步。

> 注意：图片站（`/tk-product-automation/`）不能挂 CDN，
> 因为翻译和采集侧在大陆，走 CDN 反而更慢。详见
> `docs/features/warehouse/image-zone-promotion-2026-11-19.md`。

---

## 清理 Inno 注册表前必须保留 InstallLocation（2026-11-20 事故）

「应用和功能」里出现两套「奥皇上品」时，清理残留项的**前提是先确认不会破坏
`UsePreviousAppDir` 的判断依据**。

`installer/setup.iss` 用 `UsePreviousAppDir=yes` 决定升级时装回哪个目录，
它读的就是 `HKCU\...\Uninstall\{8F3A2C41-7B5E-4D9A-9C1F-2E6B8D4A7F30}_is1`
里的 `InstallLocation`。

**我把它整个删掉后，下一次安装会装到默认的
`%LOCALAPPDATA%\Programs\奥皇上品`，而旧目录 `D:\软件\奥皇上品` 还在 ——
变成两份，且桌面快捷方式指向新目录，用户以为「旧版没清掉」。**

正确做法：

```powershell
# 1. 先看 InstallLocation 实际值
Get-ItemProperty "HKCU:\...\Uninstall\{8F3A2C41-...}_is1" | Select InstallLocation

# 2. 如果该键是「已失效的旧版本」（卸载器文件不存在），删掉它
#    然后**立刻用真实安装目录重建**，而不是留空
New-Item -Path $key -Force
New-ItemProperty $key -Name InstallLocation -Value "D:\软件\奥皇上品\"
New-ItemProperty $key -Name DisplayVersion  -Value "<当前版本>"
New-ItemProperty $key -Name UninstallString -Value '"D:\软件\奥皇上品\unins000.exe"'
```

判断依据：`UninstallString` 里那个卸载器文件是否存在。
存在 = 这套是真的，不要删；不存在 = 空壳，可以删，但删完要重建
`InstallLocation`，否则下次安装会换目录。

---

## deploy_server.py 的文件清单不含 carrier_service（2026-11-20 实测）

改完 `server/services/carrier_service.py` 跑 `python scripts/deploy_server.py`，
脚本会打 `Done.` 和 `Service status: active`，**但那个文件没被上传**。

发现方式：改完架构后跑端到端测试，`source_override` 事件只触发 1 次（应 3 次），
且事件里带 `index=None` —— 那是旧代码才有的字段。查证：

```
远端 source_override 出现次数: 1     ← 旧代码
远端 round_targets 次数: 7           ← 手动上传后的新代码特征
```

**所以：改 `carrier_service.py` 后，必须单独比对 sha256 确认真的上去了。**

```python
import hashlib, sys; sys.path.insert(0,'.')
import _dsh_remote as R
R.put(r"D:\...\miaoshou\server\services\carrier_service.py",
      "/opt/miaoshou-listing-v2/server/services/carrier_service.py")
local = hashlib.sha256(open("miaoshou/server/services/carrier_service.py","rb").read()).hexdigest()
c,o,_ = R.run("sha256sum /opt/miaoshou-listing-v2/server/services/carrier_service.py", 60)
print("一致:", local == o.split()[0].strip())
```

> 这条不能靠「脚本打 Done 了就算上了」判断。部署脚本该修 —— 它的
> `FILES` 清单需要把 `services/*.py` 全量纳入，而不是只列一部分。

---

## 涉及「批量 × 逐件」的逻辑必须先写「每件不同」的测试（2026-11-20 教训）

上一版「载体货源链接覆盖」基于「复制会继承当前链接」的假设就写了代码，
没有验证「一批 N 件共用一个 `common_id`」这个场景。结果是：

- 循环外改一次链接 → 这一批 N 件**全部指向同一条链接**
- 负责人的原问题（一批 50 个不同商品，采购分不清哪件对哪条）**一点没解决**
- 而且更难发现：新入仓商品确实有了链接，只是全批共用一条

正确做法：写一个假 client，它每次复制都**记下当时的链接快照**，
然后断言「N 件副本的链接各不相同」。见
`tests/test_carrier_source_per_item.py::test_every_copy_gets_its_own_link`。

这类 bug 只有这种测试能抓出来 —— 单件测试全部会过。

---

## Pacer 常量改了必须同步改 MIN（2026-11-20 修过的真 bug）

`services/miaoshou.py` 的 `Pacer.reward()` 逻辑是：

```python
if self._ok_streak >= 50 and self.interval > UPSTREAM_PACE_MIN_SECONDS:
    self.interval = max(UPSTREAM_PACE_MIN_SECONDS, self.interval - 0.05)
```

原配置 `UPSTREAM_PACE_MIN_SECONDS = 0.90` 与 `UPSTREAM_PACE_SECONDS = 0.90`
**相等**，于是 `interval > MIN` 永远为假 —— **自适应降速从未生效过**。

后果：一旦 `penalize()` 因为偶发限流把间隔抬到 1.5s，就要连续成功
50 次 × 每次降 0.05s = **1000 次请求**才能爬回 0.90 —— 实际等于永久变慢。

**规则：MIN 必须严格小于默认值，否则 reward 形同虚设。**
现在 `MIN = 0.55`，且 `tests/test_pacer_and_rate_limit.py` 里
`test_min_is_lower_than_default` + `test_reward_can_actually_lower_interval`
会盯住这条。

---

## 妙手限流的真实文案是「请求太频繁」（2026-11-20 压测抓到）

`_is_rate_limit` 原来的关键词表是：

```python
"ratelimit" / "rate_limit" / "qps" / "频率" / "限流" / "too many"
```

但妙手实际返回的是：

```
妙手接口失败 [fail]：请求太频繁，请稍后重试
```

「频」和「繁」一个都不在关键词表里 —— 于是**限流被当成业务错误**：
不退避、不抬 Pacer 间隔，重试也不会好。

已补：`太频繁` / `过于频繁` / `frequent` / `throttl` / `rate limit`（空格变体）。

同时确认不能误判：`下游服务响应超时`、`下游服务繁忙`、`商品不存在`、
`stock 不能大于99999` 都不能被判成限流（会让退避策略走错分支）。
测试见 `test_does_not_misclassify_gateway_errors_as_rate_limit`。

---

## 关于「节流是不是白等」的实测结论（2026-11-20）

负责人问「故意等的 0.9 秒真的需要这么久吗」。实测三组（同载体、同 6 条链接）：

```
interval=0.90  总 6.16s   客户端睡 4.16s   上游 2.00s   6/6 成功
interval=0.00  总 5.46s   客户端睡 0.00s   上游 5.46s   6/6 成功
interval=0.35  总 6.07s   客户端睡 0.58s   上游 5.49s   6/6 成功
```

**关掉节流只快 12%，因为上游自己慢了 2.7 倍** —— 妙手在服务端排队。
客户端不睡就得在等响应时睡，所以「故意白等」不成立。

**结论：不要为了提速去动 `UPSTREAM_PACE_SECONDS`。** 真要提速，改变的是
「减少上游请求次数」（例如这次加的链式复用 ossMd5，一批 50 件少读 49 次详情），
而不是缩短间隔。

另外单独测过纯 edit 路径：0.20 也能 6/6 成功且略快，但那是单一接口场景；
混合请求（列表+详情+edit+复制）没有验证过，不动主常量。
