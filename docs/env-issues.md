# 环境问题记录

> 本文件此前写过「这台机器无法创建原生应用窗口」、以及「原因是 userData 不可写」。
> **两个结论都不完全对**。下面是用决定性实验重新测出来的真实情况。

## 1. Electron 双击 exe 崩：沙箱在 JS 之前就崩了（已解决）

### 决定性实验

不猜，直接看崩溃发生在哪一层。建一个最小 Electron 应用，把日志写在 `m.js` **第一行**：

```js
const fs = require("node:fs");
fs.writeFileSync("trace.log", "JS-STARTED pid=" + process.pid + "\n");
```

然后三种启动方式对比：

| 启动方式 | 退出码 | `trace.log` |
|---|---|---|
| 无参数（等同双击） | `-2147483645` (0xC0000005) | **未生成** |
| 只加 `--user-data-dir=可写路径` | `-2147483645` | **未生成** |
| 只加 `--no-sandbox` | `0` | 生成，走到 `READY` |

**结论：**

1. 日志文件根本没生成 → 崩溃发生在**加载 JavaScript 之前**的原生初始化阶段，
   不是我们的代码问题，主进程里 `appendSwitch("no-sandbox")` 也救不了自己。
2. 只加 `--user-data-dir` 仍然崩 → **userData 不可写不是崩溃原因**（之前那条结论是错的）。

唯一有效条件：`--no-sandbox` 必须在**进程命令行**里，从第一瞬间就带着。

### 修复

双击 exe 天然不带参数，所以换入口：

| 文件（打包后在 exe 同级） | 作用 |
|---|---|
| `launch.bat` | 纯 ASCII，`start "" "AohuangShangpin.exe" --no-sandbox` |
| `启动 奥皇上品.bat` | 同上，中文名便于识别 |

**实测结果**（干净环境，无任何环境变量）：

| 启动方式 | 进程数 | 窗口 | 内置后端 |
|---|---|---|---|
| 双击 `launch.bat` | 5 | ✅「奥皇上品」 | 8812 OK |
| 双击 `启动 奥皇上品.bat` | 5 | ✅「奥皇上品」 | 8812 OK |
| 直接双击 `AohuangShangpin.exe` | 0 | ❌ | 无 |

### 为什么 exe 名改成了 ASCII

`奥皇上品.exe` 在 `cmd.exe` 和 `makensis` 下会被按 GBK 误解码
（实测报 `'奥皇上品' is not recognized`），启动脚本拼不出正确命令行。
改成 `AohuangShangpin.exe` 后一切正常。**用户看到的中文全部来自窗口标题、
快捷方式名和任务栏**，不受影响。

### 快捷方式方案（已配好，但本机无法验证安装流程）

`app/build/installer.nsh` 里给 NSIS 加了钩子，安装后桌面/开始菜单快捷方式的
Target 是 `"...\AohuangShangpin.exe"`，Arguments 是 `--no-sandbox`，
用户点的还是正常的奥皇图标。

**已经单独验证过这条路径有效**：手工创建同样内容的 `.lnk`，用
`ProcessStartInfo` 直接执行其 TargetPath + Arguments → 进程 4 个、窗口正常。

但**安装器本体在本机跑不起来**，原因是老问题：NSIS 需要写 `%TEMP%`，
而本机派生进程写不进去（见第 2 节）。静默安装 `/S /D=C:\AohuangTest`
退出码 2，目录未创建。所以「装完点快捷方式」这一步要在正常机器上验证。

## 2. 派生子进程写不了 %LOCALAPPDATA% / %TEMP%（未解决，有绕过）

### 现象

- 后端（PyInstaller 出的 `server.exe`）写日志/数据库时报
  `PermissionError: [Errno 13]` 或 `sqlite3.OperationalError: unable to open database file`
- `electron-builder` 打 NSIS 时报 `Error writing temporary file`
- NSIS 安装器静默安装退出码 2
- 同样路径，PowerShell / cmd 自己 `mkdir` 是成功的

### 绕过（已固化进脚本）

- 运行时：`AOHUANG_DATA_DIR` 指到工作区
- 打包：`$env:TEMP=$env:TMP=<工作区>\_build_tmp`（见 `scripts/build.ps1`）

### 已做的健壮性改动

- `server/core/config.py`：`ensure_dirs()` 失败时抛 `RuntimeError`，消息带实际路径、
  原始原因和 `AOHUANG_DATA_DIR` 提示
- `server/main.py`：`configure_logging()` 遇 `OSError` 降级到 stdout，不因日志写不了而起不来
- `app/src/main.ts`：userData 四个候选依次尝试（`AOHUANG_DATA_DIR` → `%LOCALAPPDATA%`
  → `%TEMP%` → exe 同级 `userdata`）

## 3. 妙手上游连不上：两个独立原因（已修）

之前报 `ConnectError`，实际是**两个叠加的问题**，都修了：

### 3.1 域名写错

```diff
- miaoshou_base = "https://openapi.91miaoshou.com"      # 不通，SSL 握手失败
+ miaoshou_base = "https://openapi-erp.91miaoshou.com"  # 0.1.4 老版用的就是这个
```

0.1.4 的 `src/miaoshou_client.py` 里 `DEFAULT_BASE_URL = "https://openapi-erp.91miaoshou.com"`，
重写时漏了 `-erp`。实测：老域名 TLS 直接失败，`openapi` 那个连不上。

这就是「以前能连采集箱、现在连不上」的原因。

### 3.2 httpx 不读 Windows 系统代理

本机系统代理是开着的（注册表 `ProxyEnable=1`、`ProxyServer=127.0.0.1:7892`），
但：

```python
>>> urllib.request.getproxies()
{'no': 'localhost,127.0.0.1,::1'}   # 只读到 no_proxy
```

httpx 的 `trust_env` 只认 `HTTP_PROXY`/`HTTPS_PROXY` **环境变量**，不读注册表。
结果是浏览器能开妙手后台、程序连不上。而且 DNS 被代理软件接管成 fake-ip
（`198.18.x.x`），直连那个假 IP 必然 TLS EOF。

修法：`services/miaoshou.py` 加 `system_proxy()`，先读环境变量，再读注册表
（含 `http=x;p=y` 这种格式），传给 `httpx.Client(proxy=...)`。
`services/image_service.py` 的 4 处客户端也一并接上。

### 修复后的实际表现

```
上游: https://openapi-erp.91miaoshou.com
代理: http://127.0.0.1:7892
```

`GET /api/v1/collect-box` 从 `ConnectError`（连接层失败）变成真实业务回执：

```json
{"error":{"code":"UPSTREAM_ERROR","message":"妙手接口失败 [appNotFound]：应用不存在或已禁用"}}
```

**网络层已经通了**。剩下 `[appNotFound]` 是妙手侧对当前 AppKey 的判定，
需要在妙手后台确认应用状态/是否解绑，不是代码问题。

## 4. 机器特征

```
显卡：AMD Radeon Graphics + NVIDIA RTX 5070
虚拟显示器：OrayIddDriver、GameViewer Virtual Display、Todesk Virtual Display
会话：console / Administrator（本地会话，非 RDP）
Defender：实时防护开启
系统代理：127.0.0.1:7892（开启）
```
