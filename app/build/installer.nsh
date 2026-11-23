; Aohuang Desktop - NSIS hooks
; Must stay pure ASCII: makensis mis-decodes multibyte characters in this
; file when the project path contains Chinese characters.
; CJK in shortcuts/paths is constructed at runtime inside temp PS scripts.
;
; Product name unicode: [char]0x5965+[char]0x7687+[char]0x4E0A+[char]0x54C1

; 安装目录不要在 customInit 里自己做（2026-11-23 删掉）。
;
; electron-builder 的 templates/nsis/multiUser.nsh 里 setInstallModePerUser
; 已经做了完整的目录决策，顺序是：
;
;   1. ReadRegStr $perUserInstallationFolder HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
;      → 升级时装回上次目录
;   2. ${StdUtils.GetParameter} $R0 "D" ""
;      → 命令行 /D= 优先（模板注释原文：allow /D switch to override installation path）
;
; 我们原来在 customInit 里又 ReadRegStr 一次并 StrCpy $INSTDIR，
; 这段代码在模板之后执行，会把命令行传进来的 /D= 覆盖回注册表里的旧目录 ——
; 于是「客户端想让装到当前运行的目录，安装器偏要装回老地方」，
; 桌面快捷方式和实际安装位置再次分家，就是那个循环更新的根。
!macro customInit
!macroend

!macro customInstall
  ; Write a PS script to temp dir; $INSTDIR is expanded by NSIS at install time.
  ; Use $shellArgs (not $args - that is a reserved PowerShell automatic variable).
  FileOpen $R0 "$TEMP\_ah_sc.ps1" w
  FileWrite $R0 "$$n = [char]0x5965+[char]0x7687+[char]0x4E0A+[char]0x54C1$\r$\n"
  FileWrite $R0 "$$d = [System.Environment]::GetFolderPath('Desktop')$\r$\n"
  FileWrite $R0 "$$p = [System.Environment]::GetFolderPath('Programs')$\r$\n"
  FileWrite $R0 "$$exe = '$INSTDIR\AohuangShangpin.exe'$\r$\n"
  FileWrite $R0 "function MkLnk($$lnkPath, $$target, $$shellArgs, $$icon) {$\r$\n"
  FileWrite $R0 "  $$s = (New-Object -COM WScript.Shell).CreateShortcut($$lnkPath)$\r$\n"
  FileWrite $R0 "  $$s.TargetPath = $$target$\r$\n"
  FileWrite $R0 "  if ($$shellArgs) { $$s.Arguments = $$shellArgs }$\r$\n"
  FileWrite $R0 "  if ($$icon) { $$s.IconLocation = $$icon }$\r$\n"
  FileWrite $R0 "  $$s.Save()$\r$\n"
  FileWrite $R0 "}$\r$\n"
  FileWrite $R0 "MkLnk ($$d+'\'+$$n+'.lnk') $$exe '--no-sandbox' ($$exe+',0')$\r$\n"
  FileWrite $R0 "New-Item -ItemType Directory -Force -Path ($$p+'\'+$$n) | Out-Null$\r$\n"
  FileWrite $R0 "MkLnk ($$p+'\'+$$n+'\'+$$n+'.lnk') $$exe '--no-sandbox' ($$exe+',0')$\r$\n"
  FileClose $R0
  nsExec::ExecToLog 'powershell.exe -WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File "$TEMP\_ah_sc.ps1"'
  Delete "$TEMP\_ah_sc.ps1"
!macroend

!macro customUnInstall
  FileOpen $R0 "$TEMP\_ah_rm.ps1" w
  FileWrite $R0 "$$n = [char]0x5965+[char]0x7687+[char]0x4E0A+[char]0x54C1$\r$\n"
  FileWrite $R0 "$$d = [System.Environment]::GetFolderPath('Desktop')$\r$\n"
  FileWrite $R0 "$$p = [System.Environment]::GetFolderPath('Programs')$\r$\n"
  FileWrite $R0 "Remove-Item -Force ($$d+'\'+$$n+'.lnk') -ErrorAction SilentlyContinue$\r$\n"
  FileWrite $R0 "Remove-Item -Recurse -Force ($$p+'\'+$$n) -ErrorAction SilentlyContinue$\r$\n"
  FileClose $R0
  nsExec::ExecToLog 'powershell.exe -WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File "$TEMP\_ah_rm.ps1"'
  Delete "$TEMP\_ah_rm.ps1"
!macroend
