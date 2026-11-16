; Aohuang Desktop - NSIS hooks
; Must stay pure ASCII: makensis mis-decodes multibyte characters in this
; file when the project path contains Chinese characters.
; CJK in shortcuts/paths is constructed at runtime inside temp PS scripts.
;
; Product name unicode: [char]0x5965+[char]0x7687+[char]0x4E0A+[char]0x54C1

; Restore previous install directory so reinstalls / updates go to same place.
!macro customInit
  ReadRegStr $R0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\com.aohuang.listing" "InstallLocation"
  ${If} $R0 != ""
    StrCpy $INSTDIR $R0
  ${EndIf}
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

!macro customInstallEnd
  ; 安装完成后自动重启程序（静默更新后无需用户手动点快捷方式）
  Exec '"$INSTDIR\AohuangShangpin.exe" --no-sandbox'
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
