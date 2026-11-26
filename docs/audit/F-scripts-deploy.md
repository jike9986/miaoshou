# 审计 F：脚本、部署与发布链

> 审计范围：`scripts/`（44 文件）、`tools/`（34 文件）、`server/scripts/`（17 文件）、
> `server/ops/`（3 文件）、全仓敏感信息。
> 基线：`master` @ `4c9f32d`（2026-11-26），版本 0.1.54。
> 只做检测诊断，未修改任何源码。
>
> **第一版（v1）在 2026-11-26 被 Lead 实测推翻一条 P0 结论，已修正为 v2。**
> 修正记录见下；每个 P0 现在都标注了证据等级：
> `【已实测】` = 在真实生产服务器 `38.95.75.47` 上跑命令验证过；
> `【代码推断】` = 只读源码得出，未在运行态验证。

---

## 审计修正记录（v1 → v2）

### 被推翻的结论

**v1 原文**（F-P0-1）：

> 部署清单是硬编码的 21 条白名单，`server/` 下 157 个非测试文件里 **136 个永远
> 不会被上传**。而且已经实际漂移：`services/claim_job_service.py` 是「认领卡死一天
> 不能重认领」的真根因修复（`da2279e`），**不在清单里**，`python scripts/deploy_server.py`
> 跑完显示成功、服务器跑的还是旧代码。

**为什么错**：这是纯代码推断，我把「清单里没有」直接等同于「服务器上没有」。
两者不是一回事 —— 文件可以通过别的途径上去。

**实测证伪**：把生产运行目录（`api` + `services` + `models` + `core` + `schemas` + `db`
+ `main.py` + `run_server.py`）与本地仓库逐文件比对 sha256（换行统一为 LF）：

```
① 生产运行目录逐文件 sha256（换行已统一为 LF）
======================================================================
  本地 112 / 远端 112
  内容不同 0 / 仅本地 0 / 仅远端 0
  ✅ 生产运行目录与本地仓库完全一致
```

**112 个文件全部一致，差异为 0。** 所以「服务器跑的是旧代码」这个断言不成立 ——
`claim_job_service.py`、`image_upload.py`、`miaoshou.py`、`core/utils.py`、
`readback_check.py`、`source_link.py`、`carrier_service.py` 等全部与本地一致。

**真实部署途径**（v1 漏掉的）：生产 `/opt/miaoshou-listing-v2/` 下有一整套
`release-runner.py` + `stage-*/` + `source-*.tar.gz` + `release-*.json` 的
**整目录部署流水线**（2026-11-03 建的，共 8 次发布记录）。它做的是
`tar -xzf source-<release>.tar.gz` → 校验 `manifest.json` 里的逐文件 sha256 →
`compileall` → 起临时 QA 库跑 `alembic upgrade` → 比对行数 → 再切换。这个流水线
**比 `deploy_server.py` 严谨得多**，而且它包含全部文件。

另外从生产文件 mtime 可以看出两种途径并存的痕迹：

```
2026-11-21 14:56  core/utils.py              ← 单独传的（多数）
2026-11-21 14:56  services/source_link.py    ← 单独传的
2026-11-24 23:50  services/image_upload.py   ← 单独传的
2026-11-25 10:39  services/claim_job_service.py ← 单独传的
2026-11-25 20:07  ← 21 个文件同一时刻，正是 deploy_server.py 的 FILES 条数
```

`2026-11-25 20:07` 那一批实测 `find -newermt "2026-11-25 19:59" | wc -l` = **21**，
与 `deploy_server.py` 清单条数完全吻合。所以那个脚本是**部分增量工具**，
不是唯一部署途径，它的窄清单在设计上是合理的。

### 被修正的结论拆分

| v1 编号 | v1 结论 | v2 处置 |
|---|---|---|
| F-P0-1（前半） | 136/157 永不上传 → 服务器跑旧代码 | **删除**（实测证伪，见上） |
| F-P0-1（后半） | 清单硬编码，`428eb6b`/`da2279e` 说明机制脆弱 | **降级为 F-P1-10**。风险陈述改为「将来新增文件默认不会被 `deploy_server.py` 带上」，不是「现在已经陈旧」 |
| — | — | **新增 F-P0-1a**：`root` cron 执行 world-writable 脚本 + 运行目录 777（实测新发现，比原结论严重） |
| — | — | **新增 F-P0-1b**：生产 `server/` 残留 108 个本地已不存在的文件（Lead 提出，已实测复核） |

### 我自己复核时踩到的坑（第二条独立教训）

比对过程中我一度算出 `services/source_link.py` 不一致：

```
本地 5a896420… 26179B   远端 ？  25591B
```

差 **588 字节**。看起来像代码漂移。实际是 **CRLF vs LF**：本地文件有 588 行
以 CRLF 结尾，换成 LF 每行少 1 字节，588 行正好差 588 字节。文本 diff = 0 行。

> **教训：比对服务器与本地代码必须统一换行符，否则 CRLF/LF 差异会伪装成代码漂移。**
> 正确做法见 `scripts/compare_server_code.py:34` —— 它早就是这么写的：
> `data = path.read_bytes().replace(b"\r\n", b"\n")`。
> 本次复核脚本 `docs/audit/_verify_f_deploy.py` 沿用了同一口径。

### 验证脚本

`docs/audit/_verify_f_deploy.py` —— 三项检查的只读实现，可复跑：

```bash
python -X utf8 docs/audit/_verify_f_deploy.py
```

---

## 结论摘要

这条链上有 **5 个 P0**（编号已按修正记录重排），证据等级分布如下：

| 编号 | 标题 | 证据等级 |
|---|---|---|
| F-P0-1a | `root` cron 执行 world-writable 脚本 + 运行目录 777 | **已实测** |
| F-P0-1b | 生产 `server/` 残留 108 个本地不存在的文件 | **已实测** |
| F-P0-2 | 仓库跟踪的 `.captcha_dom.txt` 含真实 1688 会话 token | **已实测**（`git ls-files` + 文件内容） |
| F-P0-3 | `build.ps1` 在文档给的命令下跑不起来 | **已实测**（`bash scripts/build.ps1` → exit 2） |
| F-P0-4 | 版本号从构建到发布全程无交叉校验 | **代码推断** |
| F-P0-5 | `deploy_server.py` 部署后自检等于零，恒 exit 0 | **已实测**（`exec_command` 三元组语义 + `channel.recv_exit_status()` 验证；源码无 `SystemExit`） |

一句话：**这条链的真实问题不在「漏传文件」，在「生产环境没有边界」** ——
低权限账户能改 root cron 要执行的脚本，108 个死文件混在运行目录里，
`.bak` 旧版备份躺在包目录下，而部署脚本永远说成功。

---

## P0 问题（凭据泄漏 / 生产事故风险）

### F-P0-1a 生产 `root` cron 执行 world-writable 脚本，运行目录全 777

> **证据等级：已实测**（在 `38.95.75.47` 上跑 `runuser` 验证可写性）

这是本次复核新发现的，比 v1 的「漏传文件」严重一个数量级。

- **证据 1：运行目录全部 `drwxrwxrwx`**

  ```
  $ stat -c '%A %U:%G %n' /opt/miaoshou-listing-v2/server{,/api,/services,/models,/core,/schemas,/db,/scripts}
  drwxr-xr-x root:root /opt/miaoshou-listing-v2/server
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/api
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/services
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/models
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/core
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/schemas
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/db
  drwxrwxrwx root:root /opt/miaoshou-listing-v2/server/scripts
  ```

  实测 world-writable 文件 **265 个**，其中运行代码：`api` 24 / `services` 59 /
  `models` 21 / `core` 6 / `schemas` 3 / `db` 2。

- **证据 2：`/etc/cron.d/aohuang-img-migrate` 以 root 执行**

  ```
  0 2 * * * root cd /opt/miaoshou-listing-v2/server && /opt/miaoshou-listing-v2/venv/bin/python3 scripts/migrate_autotemp.py >> /var/log/aohuang-img-migrate.log 2>&1 && /opt/miaoshou-listing-v2/venv/bin/python3 scripts/image_zones.py --clean >> /var/log/aohuang-img-migrate.log 2>&1
  ```

- **证据 3：被 cron 执行的 `image_zones.py` 是 `666`，且低权限账户实测可写**

  ```
  $ stat -c '%A %U:%G %n' .../scripts/migrate_autotemp.py .../scripts/image_zones.py
  -rwxr-xr-x root:root .../scripts/migrate_autotemp.py
  -rw-rw-rw- root:root .../scripts/image_zones.py     ← 666

  $ runuser -u www-data -- test -w .../scripts/image_zones.py && echo YES || echo NO
  YES www-data 可写
  ```

  `www-data` 是 nginx worker 用户（`/etc/nginx/nginx.conf`: `user www-data;`，
  `ps` 实测 `www-data nginx` 在跑）。

  同一条链上还有：

  ```
  $ runuser -u www-data -- test -w .../server/main.py && echo YES
  YES www-data 可写 main.py
  $ runuser -u www-data -- test -w .../server/services && echo YES
  YES www-data 可写 services/
  ```

- **完整攻击路径（本机已验证每一环可达）**：

  ```
  nginx worker (www-data) 被拿下 / 任意文件写入漏洞
    → 覆盖 /opt/miaoshou-listing-v2/server/scripts/image_zones.py（666）
    → 02:00 root cron 执行它
    → root 权限任意代码执行
  → 或者更直接：覆盖 services/*.py（目录 777）
    → systemctl restart 后 uvicorn 以 miaoshou-listing-v2 用户加载（提权到服务账户）
    → 覆盖 main.py（实测可写）→ 下次重启即执行
  ```

  注意 `image_zones.py` 的**内容与仓库版本一致**（只是 `# noqa: UP031` 注释差异，
  内容无实质差异），但**权限**让它成为 root 提权跳板。这与内容无关。

- **性质**：根因（目录权限与执行主体不匹配 —— 一个被 root 执行的脚本，
  其可写主体不该包含非特权账户；这不是「加个 chmod」能一次性解决的，
  因为产生 777 的行为（上传方式）会再次把它改回去）

- **影响**：
  - 生产服务器存在 **www-data → root 的本地提权路径**。
  - `image_upload` 走 SFTP 往这台机器传图（`server/services/image_upload.py:45`
    `DEFAULT_SFTP_HOST = "38.95.75.47"`），图片走的是同一个 SSH 账号体系 ——
    文件落盘权限会持续被上传行为重新污染。
  - 排查时 `grep` 会先命中 cron 脚本而不是仓库源码。

- **根因修复方案**（按顺序，缺一不可）：
  1. 权限收敛：
     ```bash
     chmod -R go-w /opt/miaoshou-listing-v2/server
     find /opt/miaoshou-listing-v2/server -type d -exec chmod 755 {} \;
     find /opt/miaoshou-listing-v2/server -type f -exec chmod 644 {} \;
     chmod 755 /opt/miaoshou-listing-v2/server/scripts/migrate_autotemp.py
     ```
  2. **把 cron 脚本移出服务目录**（这才是根因）：
     `/opt/miaoshou-listing-v2/ops/migrate_autotemp.py`，`root:root 700`，
     cron 从那里执行。服务目录里不该有 root 执行的东西。
  3. `image_zones.py --clean` 从 cron 里去掉 —— 日志显示它每天都是
     「跳过原因：未配置本地图床目录（AOHUANG_IMAGE_LOCAL_DIR），无法清理」，
     从来没真删过。一个永远 no-op 的 root cron 只贡献攻击面，不贡献功能。
  4. 上传后自愈：`deploy_server.py` 上传完直接
     `find <dir> -type f -exec chmod 644 {} \;`，不依赖人记得。

---

### F-P0-1b 生产 `server/` 残留 108 个本地已不存在的文件，含完整旧版备份目录

> **证据等级：已实测**（`docs/audit/_verify_f_deploy.py` 检查项 ②）

- **证据**：远端 333 个 `.py`，仅远端有 **108** 个（本地 `<` 仓库 383 个文件）。

```
② 生产 server/ 下本地已不存在的残留文件
======================================================================
  本地 383 / 远端 333 / 仅远端（残留）108

  根目录残留（3）:
     _run_once.py
     fix_converted_image_refs.py
     optimize_product_images.py

  scripts/ 残留（103）:
     scripts/_audit_live_claim_data.py
     scripts/_inspect_existing_claims.py
     scripts/_run_claim_test.py
     scripts/analyze_images.py
     scripts/audit_claimed.py
     ... （共 103 个，含 diag_* / check_* / clean_* / drop_* / reset_* / fix_* 系列）

  其他残留（2）:
     .bak-20261118122340/constants.py
     .bak-20261118122340/publish_service.py
```

- **`scripts/` 里有大量破坏性命名**（生产上，root 可执行）：

  ```
  $ ls .../scripts/ | grep -E '^(drop|reset|clean|fix|restore|dedup)'
  clean_colors.py  clean_gallery.py  clean_notes.py  clean_prefix.py
  clean_stale_publogs.py  dedup_images.py  drop_dead_images.py
  fix_8images.py  fix_badkeys.py  fix_by_recollect.py  fix_delivery.py
  fix_dupcolor2.py  fix_final2.py  fix_final_two.py  fix_images.py
  fix_last2.py  fix_last43.py  fix_last56.py  fix_manual2.py
  fix_propname.py  fix_propname2.py  fix_required.py
  reset_claims.py  reset_common_box.py  restore_colors.py  restore_price.py
  ```

  生产 `scripts/` 实测 **119** 个 `.py`，本地仓库只有 **16** 个。

- **`.bak-20261118122340/` 的内容**

  ```
  $ ls -la .../server/.bak-20261118122340/
  -rw-r--r-- 1 root root  5344 Nov 18 12:23 constants.py
  -rw-r--r-- 1 root root 84902 Nov 18 12:23 publish_service.py

  $ grep 'return "0\.' .../.bak-20261118122340/constants.py
  30:    return "0.1.24"          ← 旧版 0.1.24

  $ grep 'return "0\.' .../core/constants.py
  30:    return "0.1.54"          ← 线上 0.1.54
  ```

- **对 Lead 提出的「sys.path 歧义会加载到旧版」这一假设，实测不成立**：

  ```
  $ python -c "import importlib.util as u; u.find_spec('.bak-20261118122340')"
  ImportError: no package specified for '.bak-20261118122340'

  $ python -c "import sys; print([p for p in sys.path if 'bak' in p] or '无')"
  无 .bak 在 sys.path

  $ python -c "import services.publish_service as p; print(p.__file__)"
  /opt/miaoshou-listing-v2/server/services/publish_service.py      ← 加载的是正确的

  $ python -c "import main; print(main.__file__)"
  /opt/miaoshou-listing-v2/server/main.py
  ```

  原因：目录名以 `.` 开头且含 `-`，**不是合法 Python 标识符，无法被 import**，
  也不在 `sys.path` 里。所以「加载旧版代码」这条路是堵死的。

  同理「`ruff`/`mypy` 在服务器上会扫到它」也不成立：生产 venv 里
  `No module named ruff`（实测），且 ruff 默认跳过 dot 目录。

  **但它仍有实际危害**（换了形态）：
  - `grep -rn "某个函数名" /opt/miaoshou-listing-v2/server` 会命中
    `publish_service.py` 的旧版本（84902 字节 vs 线上 95036 字节，差 10134 字节），
    排查时会看到两份不同实现、且不知道该信哪个。
  - `tar -czf` 备份 / 事故取证时会把旧版一起打进去，污染证据。

- **性质**：根因（部署只是「往上加文件」，从来没有「删除」这一步；
  `release-runner.py` 的 stage 解包也是叠加语义）

- **影响**：
  - 生产 `scripts/` 有 27 个 `drop_*` / `reset_*` / `clean_*` / `restore_*` 命名的
    脚本，全部 root 可执行、且 `scripts/` 目录是 777。误执行任意一个都可能改生产数据。
  - 排查成本：`grep` 结果里新旧代码混在一起。
  - 事故取证时 tar 包内容不干净。

- **根因修复方案**：
  1. 一次性清理（先列出、再删，保留 `.bak` 到 `/var/backups/`）：
     ```bash
     # 1. .bak 目录移走，不要留在服务目录
     mv /opt/miaoshou-listing-v2/server/.bak-20261118122340 \
        /var/backups/miaoshou-listing-v2/
     # 2. 用仓库清单反查，删掉本地不存在的 scripts
     cd /opt/miaoshou-listing-v2/server
     for f in scripts/*.py; do
       [ -f "本地仓库/$f" ] || echo "STALE: $f"   # 先只看
     done
     ```
  2. **让部署具备删除能力**：`deploy_server.py` 加一步「远端存在但本地不存在
     → 移到 `_stale/`（不直接删）」。这是根因修复 —— 只加不删的部署必然累积。
  3. 把 `_verify_f_deploy.py` 的检查项 ② 接进 `verify_release.py` 或 CI，
     让「残留数」变成可见指标而不是靠人偶然发现。

---

### F-P0-2 仓库跟踪的 `.captcha_dom.txt` 含真实 1688 会话 token

> **证据等级：已实测**（`git ls-files` 确认跟踪 + 文件内容确认）

- **证据**：`.captcha_dom.txt`（957,592 字节，`git ls-files` 确认已跟踪）

  ```
  TOKENSTR": "a8d820f8cd1e317d496b14ece58ff528",
  ```

  同文件还有真实的 1688 页面结构、`cookie2=` 字段名、`CookieUserInfo` 等。
  这是 2026-11-24 抓 1688 图搜时的 DOM 转储，**不是测试夹具**。

  同类被跟踪的抓包残留（17 个，全部 `git ls-files` 确认）：

  ```
  .c1_home.png  .c2_search.png  .c3_page.html  .captcha_dom.txt
  .captcha_page.png  .diag.png  .img5_a.png  .img5_b.png
  .img6_1.png  .img6_2.png  .kw_data.json  .kw_page.html
  .kw_resp_0.json  .login.err  .ssh_tun.err  .tun.err  .upload_page.png
  ```

  `.gitignore` 对它们**全部无效**（`git check-ignore` 逐个返回空）：

  ```
  .captcha_dom.txt  NOT IGNORED
  .tun.err          NOT IGNORED
  .kw_page.html     NOT IGNORED
  ...
  ```

  `.gitignore:94` 只排了 `tools/source_match/.captcha_dom.txt`（子目录版），
  根目录这 17 个一个没排。

- **性质**：根因（`.gitignore` 按路径枚举而不是按类型排除，且忽略了根目录）

- **影响**：
  - 真实 1688 会话 token 已进入 git 历史。`git rm --cached` 只能停止未来泄漏，
    **历史里那份永久存在**。
  - `.tun.err` 里还带着本机路径 + 用户名（`D:\缓存文件夹\...`、
    `C:\Users\Administrator\...`），属信息泄漏。
  - 任何人 clone 仓库即拿到一份可用性未知的会话凭证。

- **根因修复方案**：
  1. `git rm --cached` 这 17 个文件，加 `.gitignore` 规则：`/.*.png`、`/.*.html`、
     `/.*.json`、`/.*.err`、`/.*.txt`（配合已有的白名单例外 `.editorconfig` 等）。
  2. 该 token 按「已泄漏」处置：确认 1688 侧会话是否仍有效，失效则无需轮换，
     有效则主动登出。
  3. 加 CI 闸门：`scripts/check_no_artifacts.py` 扫根目录未忽略的 `.*` 文件，
     有则 exit 1（与 `check_version.py` 同性质，进 `version-consistency` job）。

---

### F-P0-3 `build.ps1` 在文档给的命令下跑不起来（两种 shell 都踩坑）

> **证据等级：已实测**（`bash scripts/build.ps1` → `exit=2`，见 F-P0-3 证据块）

- **证据**：`scripts/build.ps1:6-7`

  ```powershell
  param([switch]$SkipChecks)
  ```

  `DEPLOY.md:82`：

  ```bash
  bash scripts/build.ps1        # 校验 → build → electron-builder --win nsis → 校验安装包
  ```

  `CLAUDE.md:79-83`：

  ```bash
  # 在 bash（Git Bash）中运行，不要用 PowerShell
  cd /d/缓存文件夹/codex工作台/p006/miaoshou
  pnpm build
  cmd /c scripts\\do_deploy.bat
  ```

  **实测**：

  ```
  $ bash scripts/build.ps1
  scripts/build.ps1: line 6: syntax error near unexpected token `[switch]$SkipChecks'
  scripts/build.ps1: line 6: `param([switch]$SkipChecks)'
  真实 exit=2
  ```

  `docs/HANDOFF-2026-11-25.md:211` 写的是唯一正确的那条：

  ```bash
  pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
  ```

  同一份 handoff 里还留着反例（`:202-203`）：

  > **注意**：`build.ps1` 用 PowerShell 5（`powershell -File`）会报一堆
  > `Unexpected token` 解析错误，必须用 `pwsh`。

- **性质**：跷跷板（三份文档给出三种互相矛盾的调用法：`bash` / `powershell -File`
  / `pwsh -File`，改一处必然让另两处变成错的；且没有一处是**可执行的**唯一入口）

- **影响**：
  - 按 `CLAUDE.md` 走的开发者会在第一步就撞墙。撞墙后的自然反应是「那我手工
    `pnpm build` 吧」—— 于是**跳过了 `check_version.py` 和 `verify-desktop-package.mjs`
    两道闸门**，产出的包版本可能和 `package.json` 不一致。
  - 与 F-P0-4 直接串联：绕过校验 → 版本号漂移 → 发出错误版本。

- **根因修复方案**：脚本本身改成双兼容入口，而不是靠文档记：
  - 保留 `build.ps1` 但加一个 `build.sh` 薄壳：
    ```bash
    #!/usr/bin/env bash
    set -euo pipefail
    exec pwsh -NoProfile -ExecutionPolicy Bypass -File "$(dirname "$0")/build.ps1" "$@"
    ```
  - `CLAUDE.md` / `DEPLOY.md` / `HANDOFF` 三处统一改成 `bash scripts/build.sh`。
  - CI 加一条 `bash scripts/build.sh --help` 的冒烟（验证入口可解析）。

---

### F-P0-4 版本号从构建到发布全程无交叉校验，可发出「标着新版本号的旧包」

> **证据等级：代码推断**（`build.ps1` / `publish_release.py` / `verify_release.py`
> 三处源码里都没有版本号交叉校验；未构造实际的错包发版来验证）

- **证据**：

  `scripts/build.ps1:44-49`（产物校验只看文件存不存在）：

  ```powershell
  $version = (Get-Content -LiteralPath app\package.json -Raw | ConvertFrom-Json).version
  $installer = Join-Path $root "release\奥皇上品-$version-Setup.exe"
  if (-not (Test-Path -LiteralPath $installer)) { throw "安装包产物不存在：$installer" }
  ```

  **没有**任何「安装包的 mtime 晚于本次构建开始时间」的判定。
  `release/` 里 0.1.44~0.1.54 十一个包全躺着，只要版本号没变，
  `Test-Path` 对上一轮残留的包同样返回 true。

  `scripts/publish_release.py:68-70`（版本号来自命令行，不与 `package.json` 比对）：

  ```python
  installer = Path(sys.argv[1])
  version = sys.argv[2]
  notes = sys.argv[3] if len(sys.argv) > 3 else f"v{version}"
  ```

  `grep -n "check_version\|package.json" scripts/publish_release.py` → 无输出。

  `scripts/verify_release.py:88-128`（只做「清单 sha == 实际 sha」的自洽校验）：

  ```python
  expected_sha = str(manifest.get("sha256") or "").strip().lower()
  ...
  actual, size = hash_url(manifest["url"])
  ok_manifest_bytes = actual == expected_sha and size == expected_size
  ...
  match = sha_fixed == expected_sha and size_fixed == expected_size
  ```

  **这是一条闭环**：清单说什么就校验什么。如果 `version.json` 和固定链接
  **同时**指向 0.1.44，`ok_manifest_bytes=True`、`match=True`、全绿通过。
  2026-11-26 事故里固定链接是坏的，所以这次拦住了；但**下次如果是
  `publish_release.py` 拿错包**（比如手工传了 `release/奥皇上品-0.1.44-Setup.exe`
  却写 `0.1.54`），三道闸门一道都不会响。

  `grep -n "package.json\|check_version" scripts/verify_release.py` → 无输出。

- **性质**：根因（「版本号从哪来」这件事在整条链上没有任何单一真相源，
  每个脚本各信各的输入）

- **影响**：
  - 可发出「文件名 0.1.55 / `app.asar` 里 0.1.55 / 实际字节是 0.1.54」的包，
    自动更新会判定「有新版本」→ 下载 → 装完还是 0.1.54 → **角标永不消失**。
    这正是 `check_version.py:5-8` 自己描述的故障形态。
  - 无法从「`verify_release.py` 通过」推断「发出去的版本是对的」。

- **根因修复方案**：把 `package.json` 设为唯一真相源，三个脚本全部改为**读**它而不是**收**它：
  1. `build.ps1:44` 加一条：构建开始前记录 `$buildStart = Get-Date`，
     产物校验加 `if ((Get-Item $installer).LastWriteTime -lt $buildStart) { throw "安装包是上一轮的残留" }`。
  2. `publish_release.py` 的 `version` 参数改为可选，缺省从
     `json.loads(Path("package.json").read_text())["version"]` 取；传了就必须相等，否则 `SystemExit`。
  3. `verify_release.py` 第 0 步：`assert manifest["version"] == package_json["version"]`。
     这条能独立拦住「清单指向旧版本」这一类，不需要网络。

---

### F-P0-5 `deploy_server.py` 部署后自检等于零（失败退出 0）

> **证据等级：已实测**
> （`exec_command` 三元组语义已在 `38.95.75.47` 上验证：
> `len(return) == 3`、`r[1]` 是 `ChannelFile`、`r[1].channel.recv_exit_status()` 可用；
> 源码里无 `SystemExit` / `exit()` —— `grep` 全文仅 `main()` 定义与调用）

- **证据**：`scripts/deploy_server.py:60-75`

  ```python
  print("Restarting service...")
  _, stdout, stderr = c.exec_command("systemctl restart aohuang-listing-v2", timeout=30)
  out = stdout.read().decode().strip()
  err = stderr.read().decode().strip()
  if out:
      print("stdout:", out)
  if err:
      print("stderr:", err)

  # Verify service is running
  _, stdout, _ = c.exec_command("systemctl is-active aohuang-listing-v2", timeout=10)
  status = stdout.read().decode().strip()
  print(f"Service status: {status}")

  c.close()
  print("Done.")
  ```

  三个具体缺口：

  1. `exec_command` 的返回元组是 `(stdin, stdout, stderr)`，**没有 `exit_status`** ——
     代码用 `_` 丢掉的是 `stdin`，`systemctl restart` 的退出码**从未被读取**。

     实测该语义（本机连生产验证）：

     ```
     $ python -c "...r = c.exec_command(\"sh -c 'exit 7'\"); ..."
     exec_command 返回类型: tuple 长度: 3
     解包 _, stdout, stderr 后第2项类型: ChannelFile 第3项: ChannelStderrFile
     channel.recv_exit_status() = 7
     ```

     退出码只能从 `stdout.channel.recv_exit_status()` 取，代码里从未调用。

  2. `systemctl is-active` 只 `print`，**没有 `if status != "active": sys.exit(1)`**。
  3. `main()` 结尾无 `return`，`sys.exit()` 恒为 0。

  对比同仓库的 `publish_release.py:99-102`（有校验）：

  ```python
  remote_size = run(f"stat -c %s {remote_exe}").strip()
  if remote_size != str(size):
      c.close()
      raise SystemExit(f"上传后大小不符：本地 {size}，服务器 {remote_size}")
  ```

  `deploy_server.py` **没有对应的 stat/sha 校验**（`grep -n "sha\|hash\|md5\|stat"` → 无输出）。

- **性质**：根因（部署脚本没有「成功」的定义，只打印不判定）

- **影响**：
  - 服务启动失败（比如上传的文件有语法错误）时脚本仍然是 exit 0。
    `428eb6b` 的 commit message 自己写了「这种假成功比报错更难查」，
    但**脚本本身的退出码仍然是假成功**。
  - **它是 F-P1-10（清单硬编码）至今没人发现的原因**：即使某次真漏了文件，
    这个脚本也会照常说 `Done.`、`Service status: active` —— 因为服务确实活着，
    只是跑的是旧代码。没有任何信号会提示「代码没换」。
  - 与 F-P0-1b 同源：只加不删 + 无差异检测 = 生产目录持续漂移而无人知晓。

- **根因修复方案**：加 sha256 落地校验（注意换行统一，见修正记录）：

  ```python
  # 上传后：逐文件落地校验
  for local_rel, remote_rel in FILES:
      local_sha = hashlib.sha256(
          (LOCAL_BASE / local_rel).read_bytes().replace(b"\r\n", b"\n")
      ).hexdigest()
      _, out, _ = c.exec_command(f"sha256sum {REMOTE_BASE}/{remote_rel}", timeout=30)
      remote_sha = out.read().decode().split()[0]
      if local_sha != remote_sha:
          raise SystemExit(f"落地校验失败: {remote_rel}")

  # 重启后：真读退出码
  _, out, _ = c.exec_command("systemctl restart aohuang-listing-v2", timeout=30)
  rc = out.channel.recv_exit_status()
  if rc != 0:
      raise SystemExit(f"restart 失败 rc={rc}")

  _, out, _ = c.exec_command("systemctl is-active aohuang-listing-v2", timeout=10)
  if out.read().decode().strip() != "active":
      raise SystemExit("服务未能启动，部署失败")

  # 版本自检（这才是「代码真的换了」的唯一证据）
  _, out, _ = c.exec_command(
      "curl -s http://127.0.0.1:8813/api/v1/system/health", timeout=30)
  served = json.loads(out.read().decode()).get("version")
  local_ver = json.loads((LOCAL_BASE.parent / "package.json").read_text("utf-8"))["version"]
  if served != local_ver:
      raise SystemExit(f"服务端版本 {served} != package.json {local_ver}")
  ```

  注意最后一条：`systemctl is-active` 说 active 不等于「新代码在跑」。
  只有 `/api/v1/system/health` 返回的 `version` 才是运行态证据。**这是本条的关键**。

---

## P1 问题

### F-P1-10 `deploy_server.py` 清单是 21 条硬编码（已从 P0 降级）

> **证据等级：已实测**（清单 21 条与 2026-11-25 20:07 那批文件数吻合）

**v1 曾把这条定为 P0，理由是「漏传导致服务器跑旧代码」。该理由已被实测证伪
（见上方修正记录）。降级为 P1，风险重新表述为「将来新增文件默认不会被带上」。**

- **证据**：`scripts/deploy_server.py:11-37` 是纯硬编码，无 `rglob`/`glob`/`os.walk`：

  ```python
  FILES = [
      ("services/automation_run_service.py", "services/automation_run_service.py"),
      ("services/color_service.py",          "services/color_service.py"),
      ...
      ("main.py",                             "main.py"),
  ]
  ```

  实测 `find ... -newermt "2026-11-25 19:59" | wc -l` = **21**，与清单条数一致
  —— 证明它按清单工作，是**部分增量工具**，不是唯一部署途径。

- **仍然成立的风险**（机制层面，非事故层面）：

  1. 新增文件默认不会被带上。`428eb6b` 的 commit message 自己写过：

     ```
     漏掉的话 `python scripts/deploy_server.py` 会「成功」但服务器跑的还是
     旧逻辑，看起来部署过了、实际没生效 —— 这种假成功比报错更难查。
     ```

     这次是人工补上了 3 个文件。下次没人记得补，就会重演 —— 只是这次
     有 F-P0-5（恒 exit 0）会把假成功继续保持沉默。

  2. 两个部署途径（`release-runner.py` 整目录 / `deploy_server.py` 增量）
     没有文档说明谁该在什么时候用。`CLAUDE.md:71-75` 只写了后者：

     ```
     **后端部署**（上传 Python 文件 + 重启服务）：
     python scripts/deploy_server.py
     ```

     而实际上 `release-runner.py` 才是有 sha256 校验、QA 库试跑、行数比对的
     那条。文档推荐的是弱的那条。

- **性质**：根因（「哪些文件要部署」这件事有两个真相源，且文档指向弱的那个）

- **影响**：目前**无实际事故**（实测 112/112 一致）。风险是下一次新增文件时静默漏传。

- **根因修复方案**：
  1. 清单改按目录推导（这样新增文件自动被带上）：
     ```python
     UPLOAD_DIRS = ("api", "services", "models", "core", "schemas", "db")
     FILES = [(p.relative_to(LOCAL_BASE).as_posix(),) * 2
              for d in UPLOAD_DIRS for p in (LOCAL_BASE / d).rglob("*.py")
              if "__pycache__" not in p.parts] + [("main.py", "main.py")]
     ```
  2. 上传后逐文件 sha256 比对（`compare_server_code.py:22-36` 的 `local_hashes()`
     已经是现成实现，直接复用）。
  3. 文档合并两条途径：`CLAUDE.md` 补齐 `release-runner.py` 的使用条件，
     或明确废弃其中一条。

---

### F-P1-1 固定链接校验只覆盖 2/5 个主机名（同一个盲区换了个位置）

> **证据等级：代码推断**（`verify_release.py:39-50` 的列表只有 2 个主机名；未在运行时验证另外 3 个主机名当前是否真的可达/一致）

- **证据**：`scripts/verify_release.py:39-50`

  ```python
  MANIFEST_HOSTS = [
      "https://aohuang888.com/miaoshou-downloads/version.json",
      "https://aohuang888.xyz/miaoshou-downloads/version.json",
  ]
  PRIMARY_FIXED_LINK = f"https://aohuang888.com{FIXED_LINK_PATH}"
  SECONDARY_FIXED_LINK = f"https://aohuang888.xyz{FIXED_LINK_PATH}"
  ```

  但 `docs/project/fixed-download-link-2026-11-26.md:126-130` 自己列出的是 5 个：

  ```
  aohuang888.com      → 302 → AohuangShangpin-0.1.54-daa2c7bf-Setup.exe
  www.aohuang888.com  → 302 → 同上
  aohuang888.cn       → 302 → 同上
  www.aohuang888.cn   → 302 → 同上
  aohuang888.xyz      → 200（CF 缓存的旧字节，待 purge）
  ```

  `www.aohuang888.com` / `aohuang888.cn` / `www.aohuang888.cn` **三个主机名
  从未被任何脚本校验过**。

- **性质**：补丁叠补丁（事故报告把范围定成「主域 + .xyz」，脚本忠实实现了这两条，
  而文档里实测过的另外三个主机名被漏掉了 —— 修的是症状，不是「枚举所有入口」这个动作）

- **影响**：`.cn` 域名和 `www.` 前缀都是被广告出去的形态。如果哪天有人在
  `.cn` 上单独配了一条 nginx 或者某条 CF 规则，校验照样全绿。
  这与「六个版本没发现」的成因**完全同构**：不是某一处坏了，是校验面没有穷举。

- **根因修复方案**：主机名列表从一个地方（`release_fixed_link.py`）派生，
  并加一条「文档里列出的主机名必须全部在校验列表里」的测试：
  ```python
  # release_fixed_link.py
  PUBLIC_HOSTS = ("aohuang888.com", "www.aohuang888.com",
                  "aohuang888.cn", "www.aohuang888.cn", "aohuang888.xyz")
  ```
  `server/tests/test_release_fixed_link.py` 里断言 `set(PUBLIC_HOSTS) == set(verify 的列表)`。

---

### F-P1-2 `backup-db.ps1` 备的是 SQLite，生产库是 PostgreSQL

> **证据等级：已实测**（生产 `DEPLOY.md:29` 指向 PostgreSQL 14；`backup-db.ps1` 只 import sqlite3，且 `server/ops/backup-miaoshou-listing-v2.sh` 用 pg_dump —— 两条路径都已核对）

- **证据**：`scripts/backup-db.ps1:69-73`

  ```powershell
  $DbFile = Join-Path $DataDir "app.db"
  if (-not (Test-Path $DbFile)) {
      Write-Bad "数据库不存在：$DbFile"
      exit 2
  }
  ```

  `scripts/backup-db.ps1:107-133`（Python 内联脚本，只 `import sqlite3`）：

  ```python
  con = sqlite3.connect(src, timeout=60.0)
  con.execute("PRAGMA busy_timeout=60000")
  con.execute("VACUUM INTO ?", (dest,))
  ```

  但 `DEPLOY.md:29`：

  ```
  ├── PostgreSQL 14  127.0.0.1:5432/miaoshou_listing_v2
  ```

  **同一个仓库里只有 `server/ops/backup-miaoshou-listing-v2.sh` 是对的**：

  ```bash
  pg_dump --format=custom --dbname=miaoshou_listing_v2 --file="$next"
  pg_restore --list "$next" >/dev/null
  ```

  而这个脚本**没有任何部署自动化**：`grep -rn "backup-miaoshou-listing-v2\|var/backups"`
  只命中 systemd unit 自己引用自己，`deploy_server.py` 的清单里没有它。

- **性质**：跷跷板（两份备份脚本，一份对生产无效、一份没被安装；跑哪份取决于
  谁知道，没有判据）

- **影响**：
  - 开发者按 `CLAUDE.md` 的本地优先习惯跑 `backup-db.ps1`，会得到「备份成功」
    的绿色输出，但它备的是本机一个可能不存在或早已废弃的 SQLite 文件。
  - 生产 PostgreSQL 的备份**只靠一条 systemd timer 在用**，从仓库到运行态的
    安装步骤没有被固化。换机器重建时这一步会丢。

- **根因修复方案**：
  1. `backup-db.ps1` 头部加显式断言：检测到 `AOHUANG_DATABASE_URL` 是 postgres 时
     `Write-Bad "生产是 PostgreSQL，请用 server/ops/backup-miaoshou-listing-v2.sh"; exit 2`。
  2. 把 `server/ops/` 三个文件纳入 `deploy_server.py`（修完 F-P0-1 后自动覆盖），
     并加一步 `systemctl daemon-reload && systemctl enable --now aohuang-listing-v2-backup.timer`。

---

### F-P1-3 没有任何恢复演练；备份只覆盖「最新 + 上一份」

> **证据等级：代码推断 + 已实测**（脚本只有 latest/previous 两份是源码事实；生产 `/var/backups/miaoshou-listing-v2/` 未逐份验证可恢复性）

- **证据**：`server/ops/backup-miaoshou-listing-v2.sh:5-19`

  ```bash
  backup_dir=/var/backups/miaoshou-listing-v2
  next="$backup_dir/next.dump"
  latest="$backup_dir/latest.dump"
  previous="$backup_dir/previous.dump"
  ...
  if [[ -f "$latest" ]]; then
      cp -f -- "$latest" "$previous"
  fi
  mv -f -- "$next" "$latest"
  ```

  **只有两份**。如果 `latest.dump` 是坏的（`pg_dump` 中途失败但写出了一个
  可被 `pg_restore --list` 接受的截断文件），下一次运行会把它 `cp` 成
  `previous.dump`，**两份同时坏掉**。

  唯一的恢复验证记录在 `docs/production-server-2026-10-30.md:33-36`：

  ```
  The latest dump was restored into a temporary isolated database and verified
  at 4 accounts, 355 products, 4 operators, and 1 size-chart bookmark.
  ```

  这是 2026-10-30 一次性的人工动作，**没有脚本**，`grep -rn "恢复演练\|restore drill"`
  无任何自动化实现。

- **性质**：根因（备份的「可用性」没有任何周期性证据，只有一次历史记录）

- **影响**：备份存在 ≠ 能恢复。当前状态是「不知道能不能恢复」，
  而生产有 355 件商品 + 全量 operator 凭据。

- **根因修复方案**：
  1. 备份脚本加 `pg_restore --list` 之外的**真恢复验证**：
     ```bash
     tmpdb="restore_check_$$"
     createdb "$tmpdb"
     pg_restore --dbname="$tmpdb" "$next" && \
       psql -tAc "select count(*) from products" "$tmpdb"
     dropdb "$tmpdb"
     ```
     失败就不 `mv`（保留 `next.dump` 供取证）。
  2. 保留策略改成按日期命名（`app-YYYYMMDD.dump`）+ 保留 14 份，
     与 `backup-db.ps1:99` 的 `$stamp` 做法对齐。

---

### F-P1-4 价格逻辑三处重复实现，已经漂移

> **证据等级：代码推断**（三处实现逐行比对得出；未构造 floor 边界用例跑实际差异）

审计要求第 7 条：`scripts/claim_fast.py` 的价格逻辑是否与
`server/services/carrier_service.py` 一致。

**结论：不一致，已漂移 3 处。**

| # | 位置 | 代码 | 漂移点 |
|---|---|---|---|
| 1 | `server/scripts/claim_fast.py:147-150` | `if rate is None or rate <= 0: continue` | 汇率取不到时**直接跳过该 SKU**（价格不动） |
|  | `server/services/carrier_service.py:195-200` | `else: target = (current * (quoted_cny / purchase_d)).quantize(...)` | 汇率取不到时**按比例推算**，仍会改价 |
| 2 | `server/scripts/claim_fast.py:153-154` | `target = (floor + Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)` | 用了 `ROUND_HALF_UP` |
|  | `server/services/carrier_service.py:204` | `target = (floor + Decimal("0.01")).quantize(Decimal("0.01"))` | **没传 rounding**，走 `Decimal` 默认的 `ROUND_HALF_EVEN`（银行家舍入） |
| 3 | `server/scripts/claim_fast.py:212` | `client.category_tree("MY")` | 站点硬编码 `MY` |
|  | `server/scripts/claim_fast.py:286` | `site = str(product.payload.get("site") or "MY") if product.payload else "MY"` | 缺省硬编码 `MY` |

第 3 处直接复现了 `7b5c675` 修的那个 bug 的形状：

> `7b5c675` fix(claim): 成本线正则不再写死 MYR —— 其他国家认领全失败的真实根因

修了 `carrier_service` 的正则，`claim_fast.py` 里 `"MY"` 还在。
（第 116-117 行的正则本身已对齐，是 `site` 的来源没对齐。）

第 2 处的实际差异：`floor=225.50` 时 `+0.01 = 225.51`，两者一样；
但 `floor=225.505` 时 `ROUND_HALF_UP` → `225.52`，`ROUND_HALF_EVEN` → `225.50`
—— 抬到成本线**以下**，妙手继续报错，重试 12 次于 `MAX_COST_FLOOR_RETRIES` 耗尽。

- **性质**：根因（同一个业务口径有三份实现：`carrier_service`、
  `collect_box_service:1419-1424`、`claim_fast.py`）

- **影响**：生产走 `carrier_service`（`api/claim_jobs.py` 路径）和
  `collect_box_service`（认领到采集箱路径），两者已对齐；`claim_fast.py`
  是**运维手工跑**的批量脚本，漂移后「手工跑出来的结果 ≠ 接口跑出来的结果」，
  排查时会被误导。

- **根因修复方案**：`claim_fast.py` 不再自己算价，改为调
  `carrier_service._raise_price_to_reported_floor`（它已经是「拿 payload + 异常
  → 改 payload」的纯函数形态，直接可复用；`tools/source_match/verify_bump_v2.py:9`
  就是这么用的）。删除 `claim_fast.py:97-165` 整段。

---

### F-P1-5 `scripts/regression.py` 直连真实生产库写入并删除

> **证据等级：代码推断**（`regression.py` 无 `AOHUANG_DATA_DIR` 隔离是源码事实；未在连接生产库的环境下实跑验证写入）

- **证据**：`scripts/regression.py:47-52`（**没有**设 `AOHUANG_DATA_DIR`）

  ```python
  db = SessionLocal()
  _operator, token = operator_service.create(
      db, f"build-regression-{uuid.uuid4().hex[:8]}", role=operator_service.ROLE_TEACHER)
  db.commit()
  ```

  对比同目录的 `scripts/_audit_verify.py:11-18`（**有**隔离）：

  ```python
  REAL = Path(os.environ["LOCALAPPDATA"]) / "aohuang-desktop" / "app.db"
  TMP = Path(tempfile.mkdtemp(prefix="audit-"))
  shutil.copy2(REAL, TMP / "app.db")
  os.environ["AOHUANG_DATA_DIR"] = str(TMP)
  ```

  `grep -n "AOHUANG_DATA_DIR" scripts/regression.py` → 只在 `:361` 作为
  **异常消息的字符串匹配**出现，不生效。

  真实写入点：`scripts/regression.py:160`（`"save": True`）+ `:173-177`（`db.delete(row)`）
  + `:428`（`db.delete(test_account)`）。

- **性质**：跷跷板（`_audit_verify*.py` 四个脚本都做了临时库隔离，
  `regression.py` 没做；跑哪个决定生产库安不安全）

- **影响**：在开发机连生产库的环境下跑一次回归，会往 `operators` 表塞一个
  `build-regression-xxxx` 账号，往 `products` 写一行再删。中途 Ctrl+C
  会留下脏数据。

- **根因修复方案**：把 `_audit_verify.py:9-18` 那段隔离逻辑抽成
  `scripts/_test_env.py:isolate_data_dir(prefix)`，五个回归脚本统一 import。
  再在 `regression.py` 头部 `assert "aohuang-desktop" not in os.environ["AOHUANG_DATA_DIR"]`。

---

### F-P1-6 `latest.yml` 是死配置，但文档还在教人维护它

> **证据等级：已实测**（`release/latest.yml` 停在 0.1.21；`grep` 确认无脚本读写；`electron-updater` 未被依赖）

- **证据**：`DEPLOY.md:115`

  ```
  6. `latest.yml` 是客户端下载链接的软链接辅助文件，同步更新
  ```

  实际状态：

  ```
  $ cat release/latest.yml
  version: 0.1.21
  files:
    - url: 奥皇上品-0.1.21-Setup.exe
  ...
  releaseDate: '2026-11-14T11:18:15.000Z'
  ```

  停在 **0.1.21**（当前 0.1.54）。`grep -rn "latest.yml" scripts/ server/ app/src/`
  → **无任何脚本读写它**。

  且客户端**不依赖 electron-updater**：

  ```
  $ grep -rn "electron-updater\|autoUpdater" app/src/ app/package.json web/src
  app/src/updateDownload.ts:186:// 更新器就是 electron-updater 那一套契约：
  ```

  只有一行注释提到「那一套契约」，实际是自己实现的 `updateDownload.ts` +
  `version.json` 链路。

- **性质**：补丁叠补丁（Inno → NSIS 迁移时（`b9c8fc3`/`04f9063`）`latest.yml`
  的维护责任没跟着去掉，文档留了一句空话）

- **影响**：任何人都可能照着 `DEPLOY.md:115` 去手工更新 `latest.yml`，
  以为在做一件有用的事。更糟的是「以为更新了它就算发完」，而真正决定
  更新链路的是 `version.json` + snippet。

- **根因修复方案**：`release/` 已在 `.gitignore:10` 里，`latest.yml` 是构建残留。
  直接删 `DEPLOY.md:115` 这条，并在 `docs/project/auto-update-nsis-2026-11-23.md`
  里补一句「NSIS 迁移后不再生成/使用 latest.yml，更新链路唯一真相是
  `version.json` + `scripts/release_fixed_link.py` 的 snippet」。

---

### F-P1-7 `do_deploy.bat` / `deploy_local.bat` 无退出码检查，且路径已失效

> **证据等级：已实测**（`deploy_local.bat:3-4` 的 `D:\huan_cache` 与 `make-shortcut.ps1:6` 的 `E:\codex工作台` 实测均不存在；缺 errorlevel 检查是源码事实）

- **证据**：`scripts/do_deploy.bat:9-19`

  ```bat
  copy /Y "%SRC%\AohuangShangpin.exe" "%DST%\AohuangShangpin.exe"
  echo [1/3] AohuangShangpin.exe updated

  copy /Y "%SRC%\resources\app.asar" "%DST%\resources\app.asar"
  echo [2/3] app.asar updated

  rmdir /S /Q "%DST%\resources\web"
  xcopy /E /I /Y "%SRC%\resources\web" "%DST%\resources\web"
  echo [3/3] resources\web updated
  ```

  - 三个 `copy` / `xcopy` **都没有 `if errorlevel 1 goto :fail`**。
    `echo [...updated]` 无条件执行 —— 复制失败也打印 updated。
  - `rmdir /S /Q` **先删后拷**，如果 `xcopy` 失败（磁盘满、文件被占用），
    `resources\web` 被删掉了且没拷回来 → **界面直接白屏**。

  `scripts/deploy_local.bat:3-4`（路径已不存在）：

  ```bat
  set "SRC=D:\huan_cache\miaoshou\release\win-unpacked"
  set "DST=D:\software\aohuang"
  ```

  实测两个目录都不存在。

  `scripts/make-shortcut.ps1:6`：

  ```powershell
  $dir = "E:\codex工作台\p006\tk-desktop\release\win-unpacked"
  ```

  实测不存在。

- **性质**：根因（三份部署脚本各写各的路径，没有单一真相源；
  且全部缺少失败中断）

- **影响**：`CLAUDE.md:82` 明确教的是 `cmd /c scripts\\do_deploy.bat`，
  这是**主推路径**。它删完 `resources\web` 后如果 `xcopy` 失败，
  用户下次打开客户端是白屏，且没有任何错误信息。

- **根因修复方案**：
  1. `do_deploy.bat` 加 `setlocal enabledelayedexpansion` + 每步
     `if errorlevel 1 (echo [FAIL] ... & exit /b 1)`。
  2. `rmdir` 改成先 `move "%DST%\resources\web" "%DST%\resources\web.old"`，
     `xcopy` 成功后再删 `.old`（失败可回滚）。
  3. 三份脚本的 `SRC`/`DST` 改从 `scripts/deploy_server.py` 同级的
     单一配置读，或直接删掉 `deploy_local.bat` / `make-shortcut.ps1`
     这两个已失效的。

---

### F-P1-8 `tools/source_match/` 有 3 个脚本会 UPDATE 生产表

> **证据等级：代码推断**（`apply_on_server.sh` 无 dry-run 闸门是源码事实；未实跑）

- **证据**：`tools/source_match/apply_on_server.sh:1-5`

  ```bash
  set -a
  . /etc/aohuang-listing-v2.env
  set +a
  export PYTHONPATH=/opt/miaoshou-listing-v2/server
  /opt/miaoshou-listing-v2/venv/bin/python <<'PY'
  ```

  `:37-48`（直接 `update products`，无 dry-run 闸门、无 `--apply` 参数）：

  ```sql
  update products
  set payload = (coalesce(payload::jsonb, '{}'::jsonb)
                 || jsonb_build_object('sourceUrl', ..., 'sourceItemId', ...))::json,
      revision = coalesce(revision, 0) + 1,
      updated_at = extract(epoch from now())::double precision
  where id = :pid
  ```

  对比 `tools/source_match/apply_matches.py:5`（有闸门）：

  ```
  · 默认 dry-run，必须显式 --write 才落库
  ```

  但 `apply_on_server.sh` **没有**。它读 `/tmp/to_write.json` 就无条件写。
  如果那个文件是上一轮的残留（比如手工改过、或者上一轮中断），
  会写错商品。

- **性质**：跷跷板（同一件事两份实现，`apply_matches.py` 有闸门、
  `apply_on_server.sh` 没有；在服务器上顺手跑 `*.sh` 是最自然的动作）

- **影响**：`products.payload` 被写错 → 商品货源链接指向错误 offer →
  发布出去的商品挂错货。且 `revision + 1` 之后 `draft_verified_revision`
  不同步的话商品会从自动化结果页消失（正是 `CLAUDE.md:91-100` 第 4 条）。

- **根因修复方案**：`apply_on_server.sh` 头部加
  ```bash
  if [ "${1:-}" != "--write" ]; then
      echo "[dry-run] 加 --write 才写库"; exit 0
  fi
  ```
  并且 `to_write.json` 的生成也走 `apply_matches.py`，不手工编辑。

---

### F-P1-9 `_meas/` 根目录有 94 个文件被跟踪，其中 3 个 JSON 是运行快照

> **证据等级：已实测**（`git ls-files` 计数 + `git check-ignore` 逐项确认返回空）

- **证据**：

  ```
  $ git ls-files | grep -c "^_meas/"
  94
  ```

  `.gitignore:50-54` 只排了子目录：

  ```
  # --- 探针（server/_meas/）的结果快照 ----------------------------------------
  server/_meas/*.json
  server/_meas/_out/
  ```

  **根目录 `_meas/` 的三个 JSON 全部被跟踪**（`git check-ignore` 返回空）：

  ```
  _meas/probe_concurrency.json     NOT IGNORED
  _meas/probe_network_fs.json      NOT IGNORED
  _meas/probe_share.json           NOT IGNORED
  ```

  内容示例（`_meas/probe_share.json:1-5`）：

  ```json
  {
   "total": 30,
   "pass": 30,
   "fail": 0,
   "generatedAt": "2026-10-28 20:31:37",
   ```

  这是「某次跑探针的产物」，每次跑内容都不同 —— 正是 `.gitignore:51-52`
  注释里说要避免的那种 diff 噪音，只是规则写错了目录层级。

- **性质**：根因（`.gitignore` 的路径前缀写成了 `server/_meas/`，
  而探针实际跑了两个目录；漏了一个）

- **影响**：仓库里 94 个一次性探针脚本 + 3 个快照，`git log` 被
  `_meas/probe_covui5.js` 这类文件占满（见 `da2279e` 一次删了 396 行探针）。

- **根因修复方案**：`.gitignore` 改成 `_meas/*.json` + `**/_meas/_out/`
  （匹配任意层级），并 `git rm --cached _meas/*.json`。

---

## P2 问题

### F-P2-1 `publish_release.py` 有命令注入面

- **证据**：`scripts/publish_release.py:99` / `:106` / `:119` / `:141`

  ```python
  remote_size = run(f"stat -c %s {remote_exe}").strip()
  run(f"ln -sf {remote_exe} {REMOTE_DIR}/latest-Setup.exe")
  run(f"cat > {REMOTE_DIR}/version.json << 'ENDJSON'\n{content}\nENDJSON")
  print(run(f"ls -lh {remote_exe}"))
  ```

  `version = sys.argv[2]`（`:69`）未做字符白名单校验，直接进 `remote_exe`
  → 进 `exec_command`。传 `0.1.54; rm -rf /` 会执行。
  同类：`scripts/compare_server_code.py:58` 的 `repr(script)` 已经做了转义
  （用的是 `repr` 而非 `f"..."`），是对的。

- **性质**：根因（同一个仓库里两种写法并存，`compare_server_code.py` 是对的）

- **影响**：低 —— 只有本机能跑。但 `deploy_server.py` / `publish_release.py`
  是「按文档跑」的脚本，输入不该被信任。

- **根因修复方案**：`version` 加 `re.fullmatch(r"\d+\.\d+\.\d+", version)` 断言
  （`check_version.py:33` 的 `SEMVER_RE` 现成可复用）。

---

### F-P2-2 `claim_fast.py` 引用了已删除的常量，跑不起来

- **证据**：`server/scripts/claim_fast.py:104-105`（docstring）

  ```
  1. 拿货价 × CLAIM_PRICE_BUMP(1.18) → 微微上挑；
  ```

  `:145` 用的是 `carrier_service.CLAIM_PRICE_BUMP`，这个存在。
  但 `tools/source_match/verify_deployed.py:18-19` 检查的是另一批：

  ```python
  gone = [n for n in ("CLAIM_PRICE_BUMP_RATIO", "CLAIM_PRICE_MAX_BUMP",
                      "_PRICE_BASE_KEY", "_PRICE_CAPPED_KEY")
          if hasattr(carrier_service, n)]
  ```

  说明历史上有过至少 4 个同义常量名。`claim_fast.py` 是这段演化的化石层，
  与 `carrier_service` 的当前形态已经不同步（详见 F-P1-4）。

- **性质**：补丁叠补丁

- **影响**：`claim_fast.py` 实际不可运行（`pick_common` 在公共箱为空时
  `raise RuntimeError`，`:62`），但它的存在让人以为「有批量认领工具」。

- **根因修复方案**：删除 `server/scripts/claim_fast.py`（`api/claim_jobs.py` +
  `claim_job_service.py` 已覆盖该能力），或在头部加
  `raise SystemExit("已废弃，用 api/v1/claim-jobs")`。

---

### F-P2-3 `_audit_verify*.py` 四个脚本临时目录不清理

- **证据**：`scripts/_audit_verify.py:12-13`

  ```python
  TMP = Path(tempfile.mkdtemp(prefix="audit-"))
  shutil.copy2(REAL, TMP / "app.db")
  ```

  四个脚本（`_audit_verify.py` / `2` / `3` / `4`）**都没有 `finally: shutil.rmtree(TMP)`**。
  对比 `server/scripts/db.py:161-163`（有清理）：

  ```python
  finally:
      if not args.real:
          shutil.rmtree(data_dir, ignore_errors=True)
  ```

- **性质**：根因

- **影响**：每次跑审计往 `%TEMP%` 里留一份完整 `app.db` 副本（含
  `secret.key` 加密的 AppSecret）。`_audit_verify.py:14-16` 明确 copy 了 `secret.key`。
  系统临时目录不清 → 密钥副本长期驻留。

- **根因修复方案**：`try/finally` 包住 `main()`，`finally` 里 `shutil.rmtree(TMP, ignore_errors=True)`。
  或者统一用 `tempfile.TemporaryDirectory()` 上下文管理器。

---

### F-P2-4 `build.ps1` 的 `-SkipChecks` 无审计痕迹

- **证据**：`scripts/build.ps1:6` / `:19-25`

  ```powershell
  param([switch]$SkipChecks)
  ...
  if (-not $SkipChecks) {
    Write-Host "1/4 版本与静态检查..." -ForegroundColor Cyan
    python -X utf8 scripts\check_version.py
    if ($LASTEXITCODE -ne 0) { throw "版本号不一致" }
  }
  ```

- **性质**：根因

- **影响**：跳过时无任何标记。发出去的包和正常包无法区分。
  与 F-P0-4 串联后，「绕过校验发版」是可复现的路径。

- **根因修复方案**：`if ($SkipChecks) { Write-Warning "已跳过版本/静态检查，此产物不可发布" }`，
  并且 `publish_release.py` 检查 `release/` 里有没有 `.skipchecks` 标记文件，有则拒绝发布。

---

### F-P2-5 缓存目录已正确忽略（无问题，记录为对照）

- **证据**（`git check-ignore` 逐项验证全部命中）：

  ```
  __pycache__        .gitignore:17:__pycache__
  .ruff_cache        .gitignore:22:.ruff_cache
  server/.pytest_cache  .gitignore:21:.pytest_cache
  _build_tmp         .gitignore:67:_build_tmp/
  server/__pycache__    .gitignore:17:__pycache__
  scripts/__pycache__   .gitignore:17:__pycache__
  ```

  `git ls-files | grep -E "__pycache__|_build_tmp|pytest_cache|ruff_cache"` → 无输出。
  这一项是干净的。**唯一的例外是 `_meas/*.json`（F-P1-9）**。

---

## 用户可见入口校验覆盖面

枚举「用户实际会拿到安装包的入口」，逐个判定：

| # | 入口 | 有无校验 | 校验方式 | 盲区 |
|---|---|---|---|---|
| 1 | `version.json` 里的 `url`（带哈希） | ✅ 有 | `verify_release.py:99-107` 下载完整文件算 sha256 + size | 只校验「清单说的」和「实际下的」一致；**不校验清单版本号是否等于 `package.json`**（F-P0-4） |
| 2 | `aohuang888.com/miaoshou-downloads/latest-Setup.exe` | ✅ 有（硬闸门） | `verify_release.py:46,116-128` | 无 |
| 3 | `aohuang888.xyz/miaoshou-downloads/latest-Setup.exe` | ⚠️ 警告项 | `verify_release.py:50,116` `blocking=False` | 默认**不阻塞发布**，STALE 也能发出去 |
| 4 | `www.aohuang888.com/miaoshou-downloads/latest-Setup.exe` | ❌ **无** | — | 文档 `fixed-download-link-2026-11-26.md:127` 实测过，脚本未覆盖 |
| 5 | `aohuang888.cn/miaoshou-downloads/latest-Setup.exe` | ❌ **无** | — | 同上 `:128` |
| 6 | `www.aohuang888.cn/miaoshou-downloads/latest-Setup.exe` | ❌ **无** | — | 同上 `:129` |
| 7 | `aohuang888.xyz/miaoshou-downloads/version.json` | ✅ 有 | `verify_release.py:41,75-86` 与 .com 逐字段比对 | 只比这两个域名，`.cn` 的清单未查 |
| 8 | `nginx` snippet（`miaoshou-latest-redirect.conf`） | ⚠️ 部分 | `publish_release.py:130-134` 跑 `nginx -t` | 只验语法，**不验跳转目标是否等于当前版本**；`parse_snippet_target()` 写好了但只在测试里用（`release_fixed_link.py:82-90`，生产代码零调用） |
| 9 | `latest.yml` | ❌ **无** | — | 停在 0.1.21，无脚本维护，但 `DEPLOY.md:115` 教人维护（F-P1-6） |
| 10 | 客户端侧 `ALLOWED_HOSTS` 白名单 | ✅ 有 | `app/src/updateDownload.ts:10-16` + `verify-desktop-package.mjs` 打包校验 | 白名单含 `.xyz`，与 #3 的「警告项」不匹配 |
| 11 | `release/` 里的历史包（0.1.44~0.1.54 共 11 个） | ❌ **无** | — | 任何人 `scp` 错一个包，全链路过绿（F-P0-4） |

**关键结论**：上次事故的教训是「校验没覆盖到的入口等于没校验」。
这次修复把入口从 1 个扩到 3 个（#1 #2 #3），但**入口总数是 11 个**，
仍有 5 个完全无校验（#4 #5 #6 #9 #11）、2 个部分校验（#3 #8）。
**同一个盲区换了个位置。**

最关键的是 #8 和 #11：

- #8：`parse_snippet_target()` 这个函数**已经写好了**（`release_fixed_link.py:82-90`），
  测试也覆盖了（`server/tests/test_release_fixed_link.py:123`），
  但 `verify_release.py` **从来没调用它**。它能让校验从「字节对」升级到
  「跳转目标对」，成本是 0 行新代码。
- #11：`release/` 里 11 个包共存，`build.ps1:44-46` 的 `Test-Path` 无法
  区分「这次的」和「上次的」。

### 运行态（不在「下载入口」范围，但同样决定用户拿到什么）

这张表只管「用户点下载拿到什么文件」。还有一类入口决定「用户装完之后跑什么代码」，
本次复核补上：

| # | 入口 | 有无校验 | 校验方式 | 盲区 |
|---|---|---|---|---|
| 12 | 生产 `server/` 运行代码是否等于仓库 | ⚠️ 有但靠手动 | `scripts/compare_server_code.py`（sha256，已统一换行）；`release-runner.py` 的 `manifest.json` 逐文件 sha256 | **不在任何自动闸门里**。`deploy_server.py`（文档推荐的那条）不调用它 |
| 13 | 服务端实际运行版本 | ⚠️ 有但靠手动 | `GET /api/v1/system/health` 返回 `version`（`docs/HANDOFF-2026-11-25.md:242-247`） | `deploy_server.py` 不查；只在 handoff 文档里当「部署自检」教人手工 curl |
| 14 | 生产 `scripts/` 里被 root cron 执行的脚本 | ❌ **无** | — | `migrate_autotemp.py` 本地仓库根本没有；`image_zones.py` 是 666（F-P0-1a） |
| 15 | 生产目录权限 | ❌ **无** | — | 运行目录 777、265 个 world-writable 文件，无任何检查（F-P0-1a） |
| 16 | 生产是否存在本地已删的残留文件 | ❌ **无** | — | 108 个残留，无任何检查（F-P0-1b） |

**#12 / #13 是这次复核最重要的补白**：2026-11-26 事故的教训是「校验没覆盖到的
入口等于没校验」，但那句话当时只被理解成「下载链接」。同样的逻辑适用于
「生产上跑的代码是不是仓库里那份」—— 这条今天**只靠人记得跑
`compare_server_code.py`**，没有任何闸门，和固定链接事故之前的处境一模一样。

---

## 硬编码凭据清单

| 文件:行号 | 类型 | 是否真凭据 | 处置 |
|---|---|---|---|
| `.captcha_dom.txt`（根目录，957 KB） | 1688 会话 `TOKENSTR` = `a8d820f8…` | **是**（真实抓包） | `git rm --cached`；确认 1688 侧会话是否仍有效；按已泄漏处置 |
| `scripts/deploy_server.py:7` | 密码文件**路径** `C:\Users\Administrator\Desktop\雨云.txt` | 否（是路径） | 改为 `AOHUANG_SSH_PASSWORD_FILE` 环境变量，缺省保留 |
| `scripts/publish_release.py:47` | 同上 | 否 | 同上 |
| `server/services/image_upload.py:88` | 同上（`Path.home() / "Desktop" / "雨云.txt"`） | 否 | 同上（已有 `IMAGE_SFTP_CREDENTIAL_FILE` 覆盖，见 `:86-89`） |
| `tools/source_match/tunnel.py:24` | 同上 | 否 | 同上 |
| `CLAUDE.md:16` | 密码文件路径（明文写进规范） | 否 | 改为指向环境变量说明 |
| `docs/project/rules.md:120` | 同上 | 否 | 同上 |
| `docs/HANDOFF-2026-11-19.md:27` | 同上 | 否 | 同上 |
| `server/tests/test_email_smtp_transport.py:21` | `mail_password="test-only-password"` | 否（占位符） | 保留 |
| `server/tests/test_operator_login.py:329` | `password = "timing-pass-456"` | 否（测试夹具） | 保留 |
| `server/tests/test_password_reset.py:580` | `mail_password="private-password"` | 否 | 保留 |
| `server/tests/test_database_configuration.py:30,58,137` | `postgresql://user:password@…` | 否（测试夹具） | 保留 |
| `server/tests/test_share_service_database.py:65,111` | `private-user:private-password@…` | 否 | 保留 |
| `scripts/inject-token.mjs:19,23-27` | 客户端令牌，**从环境变量/忽略文件读** | 否（设计正确） | 保留 |
| `scripts/verify-desktop-package.mjs:188` | `AOHUANG_REMOTE_CLIENT_TOKEN: "legacy-test-token-must-be-ignored"` | 否（断言用） | 保留 |
| `tools/source_match/.1688-cookie.txt`（1,621 字节） | 1688 `cookie2=` 真实 cookie | **是** | ✅ **已正确忽略**（`.gitignore:79`），无需处理 |
| `DEPLOY.md:165` | `postgresql://miaoshou_listing_v2:<密码>@…` | 否（占位符） | 保留 |

**硬编码服务器 IP `38.95.75.47`（5 处）**：
`scripts/deploy_server.py:5`、`scripts/publish_release.py:45`、
`scripts/compare_server_code.py:58`、`server/services/image_upload.py:45`、
`tools/source_match/tunnel.py:22`。属运维信息，非凭据，但同样应从
`/etc/aohuang-listing-v2.env` 或 `~/.ssh/config` 派生。

**真凭据总数：1 个文件**（`.captcha_dom.txt`）。
**密码文件路径硬编码：4 处代码 + 3 处文档**。

---

## 建议的回归测试

按「新增的闸门必须能独立失败」原则，每条测试都应能在**只回滚修复**的情况下变红。

### T0 生产环境边界（对应 F-P0-1a / F-P0-1b，新增）

这两条不能做成单元测试（要连生产），做成一个 **CI 定时任务 + 发布前置检查**：

```python
# docs/audit/_verify_f_deploy.py 已经是可用的检查 ②③ 实现
# 接进 scripts/verify_release.py 作为第 5 步（或独立 cron）
def test_production_has_no_world_writable_runtime_files():
    """root cron 执行的脚本不能是 666/777；运行目录不能是 777。"""
    out = ssh("stat -c '%A %U:%G %n' /opt/miaoshou-listing-v2/server/{api,services,scripts}")
    for line in out.splitlines():
        assert not line.startswith("drwxrwxrwx"), f"目录 777: {line}"

def test_low_priv_user_cannot_write_cron_script():
    """www-data 不能写被 root cron 执行的任何脚本。"""
    rc = ssh_rc("runuser -u www-data -- test -w "
                "/opt/miaoshou-listing-v2/server/scripts/image_zones.py")
    assert rc != 0, "www-data 可写 root cron 脚本 → 提权路径"

def test_production_has_no_stale_files():
    """生产 server/ 下不应存在本地仓库没有的 .py。"""
    local = {p.relative_to(LOCAL_SERVER).as_posix() for p in LOCAL_SERVER.rglob("*.py")}
    remote = set(ssh("cd /opt/miaoshou-listing-v2/server && "
                     "find . -name '*.py' -not -path '*__pycache__*' | sed 's|^\\./||'").splitlines())
    assert remote - local == set(), f"生产有残留: {sorted(remote - local)[:10]}"

def test_production_runtime_matches_repo():
    """生产运行目录 112 个文件逐个 sha256 必须等于仓库（换行统一为 LF）。"""
    # 直接复用 docs/audit/_verify_f_deploy.py 的 local_hashes() + check1()
    ...
```

### T1 部署清单完整性（对应 F-P1-10，已从 T1 降级不改编号）

```python
# server/tests/test_deploy_manifest.py
def test_every_server_module_is_in_deploy_manifest():
    """server/ 下每个非测试 .py 必须在 deploy_server.FILES 里。
    新加一个 service 忘了进清单 → 这条红。"""
    from scripts.deploy_server import FILES
    listed = {remote for _local, remote in FILES}
    actual = {p.relative_to(SERVER).as_posix()
              for p in SERVER.rglob("*.py")
              if not {"tests", "_meas", "__pycache__"} & set(p.parts)}
    assert actual - listed == set()
```

> 注意：这条测试**是在 v1 的错误结论下写的**。现在它的价值变成「防止将来新增
> 文件漏传」，不是「证明现在已经陈旧」—— 后者已被实测证伪。保留是因为
> 机制风险仍在（F-P1-10）。

### T2 版本号单一真相源（对应 F-P0-4）

```python
def test_publish_release_refuses_version_mismatch():
    """命令行版本号与 package.json 不一致时必须 SystemExit。"""
    with pytest.raises(SystemExit):
        publish_release.main(["release/x-0.1.44-Setup.exe", "0.1.54"])
```

### T3 固定链接主机名穷举（对应 F-P1-1）

```python
def test_verify_release_covers_every_public_host():
    """文档里实测过的每个主机名都必须在 verify_release 的校验列表里。"""
    from scripts import verify_release, release_fixed_link
    covered = {u.split("/")[2] for u in verify_release.MANIFEST_HOSTS}
    covered |= {u.split("/")[2] for u in
                (verify_release.PRIMARY_FIXED_LINK, verify_release.SECONDARY_FIXED_LINK)}
    assert set(release_fixed_link.PUBLIC_HOSTS) <= covered
```

### T4 snippet 跳转目标一致性（对应用户可见入口 #8）

```python
def test_deployed_snippet_points_at_manifest_version():
    """parse_snippet_target() 已经写好但生产零调用 —— 这条把它接上。"""
    snippet = redirect_snippet(artifact_url("0.1.54", V154_SHA))
    manifest = {"version": "0.1.54", "sha256": V154_SHA}
    assert parse_snippet_target(snippet) == artifact_url(
        manifest["version"], manifest["sha256"])
    # 反向：清单升版、snippet 没跟着升 → 必须不等
    assert parse_snippet_target(snippet) != artifact_url("0.1.55", V154_SHA)
```

### T5 build.ps1 入口可执行（对应 F-P0-3）

```yaml
# .github/workflows/ci.yml，version-consistency job 里加
- name: build 脚本入口可解析
  shell: pwsh
  run: |
    # 只解析不执行（-NoProfile -WhatIf 不适用于 param 块，用语法解析）
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
      "scripts/build.ps1", [ref]$null, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "build.ps1 语法错误: $errors" }
```

### T6 价格口径三处一致（对应 F-P1-4）

```python
@pytest.mark.parametrize("floor,expected", [
    ("225.505", "225.52"),   # ROUND_HALF_UP
    ("225.50",  "225.51"),
])
def test_cost_floor_rounding_is_consistent(floor, expected):
    """carrier_service 与 claim_fast 对同一 floor 必须给同一个数。
    当前 carrier_service:204 没传 rounding（走 ROUND_HALF_EVEN）→ 会红。"""
    body = {"site": "MY", "skuMap": {";a;": {
        "originPrice": "23", "price": "1.00", "isDelete": "0"}}}
    carrier_service._raise_price_to_reported_floor(
        body, make_err(f"无法覆盖产品基础成本 {floor}MYR"))
    assert next(iter(body["skuMap"].values()))["price"] == expected
```

### T7 仓库卫生闸门（对应 F-P0-2 / F-P1-9）

```python
# scripts/check_no_artifacts.py
"""根目录未忽略的 .* 抓包残留 + _meas/*.json 快照，有则 exit 1。"""
ALLOWED = {".editorconfig", ".gitignore", ".npmrc",
           ".prettierrc.json", ".prettierignore", ".github", ".claude"}
tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
bad = [p for p in tracked
       if (p.startswith(".") and p.split("/")[0] not in ALLOWED)
       or p.endswith(("probe_concurrency.json", "probe_share.json",
                      "probe_network_fs.json"))]
if bad:
    print("\n".join(f"  仓库不该跟踪: {p}" for p in bad)); sys.exit(1)
```

### T8 备份可恢复（对应 F-P1-3）

```bash
# server/tests/test_backup_restore.sh（CI 里用 postgres service 跑）
pg_dump --format=custom --dbname=test_db --file=/tmp/t.dump
createdb restore_check
pg_restore --dbname=restore_check /tmp/t.dump
psql -tAc "select count(*) from products" restore_check | grep -qx "355"
dropdb restore_check
```

---

## 附：审计用到的原始数据

### 生产服务器实测（2026-11-26 复核，只读）

全部由 `python -X utf8 docs/audit/_verify_f_deploy.py` 复现。

```
① 生产运行目录逐文件 sha256（api+services+models+core+schemas+db+main+run_server）
  本地 112 / 远端 112
  内容不同 0 / 仅本地 0 / 仅远端 0
  ✅ 生产运行目录与本地仓库完全一致

② 生产 server/ 残留
  本地 383 / 远端 333 / 仅远端（残留）108
  根目录残留 3: _run_once.py, fix_converted_image_refs.py, optimize_product_images.py
  scripts/ 残留 103
  其他 2: .bak-20261118122340/constants.py, .bak-20261118122340/publish_service.py

③ 权限
  drwxrwxrwx root:root  .../server/{api,services,models,core,schemas,db,scripts}
  -rw-rw-rw- root:root  .../scripts/image_zones.py          ← 被 root cron 执行
  -rwxr-xr-x root:root  .../scripts/migrate_autotemp.py
  world-writable 文件总数: 265
    api 24 / services 59 / models 21 / core 6 / schemas 3 / db 2

  $ cat /etc/cron.d/aohuang-img-migrate
  0 2 * * * root cd /opt/miaoshou-listing-v2/server && ... \
    scripts/migrate_autotemp.py >> ... && ... scripts/image_zones.py --clean >> ...

  $ runuser -u www-data -- test -w .../scripts/image_zones.py && echo YES
  YES www-data 可写

  $ runuser -u www-data -- test -w .../server/main.py && echo YES
  YES www-data 可写 main.py

④ 两种部署途径并存（mtime 证据）
  2026-11-21 14:56  core/utils.py / services/source_link.py      ← 单独传
  2026-11-24 23:50  services/image_upload.py                     ← 单独传
  2026-11-25 10:39  services/claim_job_service.py                ← 单独传
  2026-11-25 20:07  21 个文件同一时刻 = deploy_server.py FILES 条数
  （find -newermt "2026-11-25 19:59" | wc -l == 21）

⑤ .bak 是否可被 import（证伪 Lead 的假设）
  $ python -c "importlib.util.find_spec('.bak-20261118122340')"
  ImportError: no package specified for '.bak-20261118122340'
  $ python -c "print([p for p in sys.path if 'bak' in p] or '无')"
  无 .bak 在 sys.path
  $ python -c "import services.publish_service as p; print(p.__file__)"
  /opt/miaoshou-listing-v2/server/services/publish_service.py      ← 正确
  $ python -m ruff --version
  No module named ruff                                            ← 也不存在扫描风险

⑥ cron 脚本从未真正生效
  $ tail /var/log/aohuang-img-migrate.log
  ... 跳过原因：未配置本地图床目录（AOHUANG_IMAGE_LOCAL_DIR），无法清理
  Found 0 products with auto-temp images
  Done: 0 products updated, 0 detail images + 0 main images migrated
  （每次都 no-op，但每天以 root 跑一次）
```

### 仓库侧

```
$ bash scripts/build.ps1
scripts/build.ps1: line 6: syntax error near unexpected token `[switch]$SkipChecks'
scripts/build.ps1: line 6: `param([switch]$SkipChecks)'
真实 exit=2

$ git ls-files | grep -cE "__pycache__|_meas|_build_tmp|\.pytest_cache|\.ruff_cache|\.pyc"
134
（其中 _meas/ 根目录 94 个，server/_meas/ 40 个）

$ git ls-files | grep -cE "^\.[a-z0-9]"
23   （合法 6 个 + 残留 17 个）
```

### v1 中被证伪的测量（保留作为教训）

```
$ python -X utf8 -c "...deploy 清单差集..."
清单条数: 21
server 下非测试 py 文件数: 157
未在清单里 (136): ...
```

这组数字本身没错（本地确实有 136 个文件不在清单里），
**错在把它当成「服务器上没有这 136 个文件」的证据**。
「本地清单里没有」和「生产上没有」是两件事，中间还隔着别的部署途径。
这正是本次复核要记住的第二条教训 —— 与换行符那条并列：

> 1. 比对服务器与本地代码必须统一换行符，否则 CRLF/LF 会伪装成代码漂移。
> 2. 「清单里没有」不等于「服务器上没有」。判断生产状态必须直接测生产，
>    不能用本地静态分析代替。
