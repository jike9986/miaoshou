; 奥皇上品 桌面端安装程序（Inno Setup 6）
#define MyAppName "奥皇上品"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "AOHUANG"
#define MyAppExeName "奥皇上品.exe"

[Setup]
AppId={{8F3A2C41-7B5E-4D9A-9C1F-2E6B8D4A7F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\release
OutputBaseFilename=奥皇上品-{#MyAppVersion}-安装程序
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\app\build\icon.ico

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

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
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
