# 0.1.6 桌面安装交付验收

日期：2026-11-03。安装包与源码版本均为 0.1.6，截图来自最终构建的实际安装目录。

## 交付物

- 安装包：`release/奥皇上品-0.1.6-安装程序.exe`
- 文件大小：81,254,879 字节（77.49 MiB）
- SHA256：`49afa3bd7b2be3c9d5b71c61238b9e4c534cab5b5914c9d5478bda3c454e89ec`
- 本机正常完整性目录副本：`C:\AohuangInstallerAcceptance\setup-0.1.6.exe`
- 实际桌面安装目录：`C:\AohuangDesktop`

生产客户端固定使用 HTTPS 服务端，默认地址为 `https://aohuang888.com/miaoshou-platform`。安装包不分发本地 Python 后端或共享登录令牌，旧测试版的本地后端标记不会绕过软件账号登录。Electron 运行时是安装包的主要体积来源。

## 构建与包内容验证

执行 `scripts/build.ps1` 成功，包含版本一致性检查、`pnpm check`、前端与主进程生产构建、Electron 目录打包、真实目录包校验及 Inno Setup 编译。

- TypeScript、lint、61 个 TSX 文件的设计检查通过；lint 有 25 条既有 warning，0 error。
- `scripts/verify-desktop-package.mjs` 检查真实 `app.asar`、版本、前端文件逐字节一致性、HTTPS 启动地址、空共享令牌及无本地后端资源。
- 校验器模拟 853×480 工作区，初始窗口与最小尺寸均受工作区限制。
- 最终前端主文件：`index-25k293rE.js`，包含本次发布及价格预览的汇率说明修正。

## 真实安装与升级

0.1.6 的相同安装配置先实际执行了自选目录安装、覆盖升级和卸载。该轮使用前一次构建（SHA256 `2dc27e55092fa8ad077dd21a115cd93ba70190235dc97d0f78ac6a27df074e78`）；之后仅更新前端汇率说明并重新构建，最终产物再次覆盖实际目录并验证运行，未重复卸载。桌面与开始菜单快捷方式均指向安装目录下 `AohuangShangpin.exe`，携带本机需要的 `--no-sandbox` 参数；HKCU 64 位卸载注册存在且版本正确。

覆盖升级保留测试缓存哨兵并定向移除旧 `resources/client-token.json`。卸载移除应用和卸载注册项，保留安装器未管理的 `userdata` 哨兵文件。

最终构建已再次覆盖 `C:\AohuangDesktop`，安装后前端与最终 `web/dist` 逐字节一致。原有 `userdata` 的 148 个文件、151,126,460 字节全部保留，升级前后 SHA256 一致。升级前完整备份位于 `C:\AohuangInstallerAcceptance\backup-before-0.1.6`。

## 之前“拒绝访问”的根因

工作区及其构建产物继承 `Low Mandatory Level` 标记。实测从该目录启动的安装进程完整性 SID 为 `S-1-16-4096`，因此 Windows 拒绝其写正常桌面快捷方式和 HKCU。管理员账号身份不会消除这个完整性限制。

将相同字节的安装器复制到正常完整性目录后，安装、升级、注册、快捷方式和卸载均成功。未修改系统安全设置或全局 ACL。最终仍使用 Inno Setup，NSIS 试验内容未进入交付方案。

## 实装运行与界面边界

从最终实际安装的 EXE 启动，CDP 实测版本 0.1.6、默认 HTTPS 地址、共享令牌为空，登录页无 renderer 异常。当前系统 125% 缩放，窗口 1440×920，工作区 2048×1104，无窗口越界。登录截图：`docs/screenshots/installed-0.1.6-login.png`。

安装目录选择使用原生 `BrowseForFolder(..., True)`，启用“新建文件夹”，编译通过。当前工具环境缺少技能要求的原生桌面交互工具，因此尚未对这个原生对话框执行可视点击验收；100% 与 150% 系统缩放也未实际切换。小工作区自动检查不能替代这些真实系统交互。

本次安装验收使用独立运行 profile，未重置原有用户会话；父任务继续验收登录及业务页面。

## 本机证据

以下为不提交 Git 的本机验收文件；`inno-actual-desktop.log`、`actual-desktop-install-0.1.6.json` 和 `installed-runtime-0.1.6.json` 对应最终构建，其余对应前一次构建的安装配置验收：

- `_build_tmp/install-0.1.6-fresh.log`、`install-0.1.6-upgrade.log`
- `_build_tmp/inno-desktop-shortcuts.log`、`inno-uninstall-test.log`
- `_build_tmp/installer-acceptance.json`
- `_build_tmp/inno-actual-desktop.log`
- `_build_tmp/actual-desktop-install-0.1.6.json`
- `_build_tmp/installed-runtime-0.1.6.json`

服务端上传与外网下载字节校验由发布主任务完成，本文件不据此宣称已上线。
