; 奥皇上品 桌面端安装程序（Inno Setup 6）
; 注意：exe 文件名必须是纯 ASCII（electron-builder 的 executableName 是
; AohuangShangpin），中文名在 ISCC/cmd 下会被按 GBK 误解码。用户看到的中文
; 来自快捷方式名与窗口标题，不受影响。
#define MyAppName "奥皇上品"
#define MyAppVersion "0.1.5"
#define MyAppPublisher "AOHUANG"
#define MyAppExeName "AohuangShangpin.exe"

[Setup]
AppId={{8F3A2C41-7B5E-4D9A-9C1F-2E6B8D4A7F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 装到当前用户目录、不需要管理员权限：
;   之前 {autopf} + PrivilegesRequired=admin 需要 UAC 提权，在受限账户/远程桌面
;   里会直接报「拒绝访问」「RegCreateKeyEx 失败」，装不上。
;   普通用户装到 %LOCALAPPDATA%\Programs 是 Electron/VS Code 等主流软件的做法，
;   全程不弹 UAC，也不需要写 HKEY_LOCAL_MACHINE。
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
; 安装/卸载列表里显示的名字：中文 + 版本，避免出现英文或纯版本号
AppVerName={#MyAppName} {#MyAppVersion}
UninstallDisplayName={#MyAppName}
OutputDir=..\release
OutputBaseFilename=奥皇上品-{#MyAppVersion}-安装程序
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 普通用户安装：不请求管理员权限，避免受限账户装不上
PrivilegesRequired=lowest
; 允许覆盖提权要求：受限环境（UAC 被拒）下也能装
PrivilegesRequiredOverridesAllowed=dialog
; 32/64 位都按默认模式装（不再强制 64 位注册表视图，减少权限相关失败）
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\app\build\icon.ico

; 「浏览...」改为打开带「新建文件夹」的系统对话框（见 browse.iss 顶部说明）
#include "browse.iss"


[Languages]
; 用 Inno Setup 自带的官方简体中文语言包，而不是 Default.isl（那是英文的）。
; 之前写 compiler:Default.isl 会让所有向导文字、按钮、提示都变英文。
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 %1
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=即将在你的电脑上安装 [name] %n%n点击「下一步」继续。
SelectDirLabel3=安装程序将把 [name] 安装到下面的文件夹。
ReadyLabel1=安装程序已准备好，点击「安装」开始。
InstallingLabel=正在安装 [name]，请稍候。
FinishedHeadingLabel=正在完成 [name] 安装
FinishedLabel=安装完成，点击「完成」启动软件。
ButtonNext=下一步(&N) >
ButtonBack=< 上一步(&B)
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消

[Files]
Source: "..\release\win-unpacked\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; 快捷方式必须带 --no-sandbox：本机 Electron 沙箱在原生层就崩（JS 都没执行），
; 双击 exe 不带参数起不来，参数只能写在快捷方式里。
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--no-sandbox"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
; 用 {userdesktop} 而不是 {autodesktop}：后者在免管理员安装时会指向
; C:\Users\Public\Desktop，受限环境写它会报「拒绝访问」导致安装失败。
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--no-sandbox"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--no-sandbox"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
