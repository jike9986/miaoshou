; Aohuang Desktop - NSIS hooks
; Must stay pure ASCII: makensis mis-decodes multibyte characters in this
; file when the project path contains Chinese characters.
; Shortcut names (CJK) are constructed at runtime from Unicode code points
; inside a temp PowerShell script, so this file stays pure ASCII.
;
; Chinese name: [char]0x5965+[char]0x7687+[char]0x4E0A+[char]0x54C1 = 奥皇上品
; Uninstall prefix: [char]0x5378+[char]0x8F7D+' ' = 卸载

!macro customInstall
  ; Write a PowerShell script to temp dir to create shortcuts with correct CJK names.
  ; FileWrite expands $INSTDIR at build/install time; $$ becomes $ in the .ps1 file.
  FileOpen $R0 "$TEMP\_ah_sc.ps1" w
  FileWrite $R0 "$$n = [char]0x5965 + [char]0x7687 + [char]0x4E0A + [char]0x54C1$\r$\n"
  FileWrite $R0 "$$u = [char]0x5378 + [char]0x8F7D$\r$\n"
  FileWrite $R0 "$$d = [System.Environment]::GetFolderPath('Desktop')$\r$\n"
  FileWrite $R0 "$$p = [System.Environment]::GetFolderPath('Programs')$\r$\n"
  FileWrite $R0 "$$exe = '$INSTDIR\AohuangShangpin.exe'$\r$\n"
  FileWrite $R0 "$$uninst = '$INSTDIR\Uninstall $\"奥皇上品$\".exe'$\r$\n"
  FileWrite $R0 "function MkLnk($$path, $$target, $$args, $$icon) {$\r$\n"
  FileWrite $R0 "  $$s = (New-Object -COM WScript.Shell).CreateShortcut($$path)$\r$\n"
  FileWrite $R0 "  $$s.TargetPath = $$target$\r$\n"
  FileWrite $R0 "  if ($$args) { $$s.Arguments = $$args }$\r$\n"
  FileWrite $R0 "  if ($$icon) { $$s.IconLocation = $$icon }$\r$\n"
  FileWrite $R0 "  $$s.Save()$\r$\n"
  FileWrite $R0 "}$\r$\n"
  FileWrite $R0 "MkLnk ($$d + '\' + $$n + '.lnk') $$exe '--no-sandbox' ($$exe + ',0')$\r$\n"
  FileWrite $R0 "$$sm = $$p + '\' + $$n$\r$\n"
  FileWrite $R0 "New-Item -ItemType Directory -Force -Path $$sm | Out-Null$\r$\n"
  FileWrite $R0 "MkLnk ($$sm + '\' + $$n + '.lnk') $$exe '--no-sandbox' ($$exe + ',0')$\r$\n"
  FileClose $R0
  nsExec::ExecToLog 'powershell.exe -WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File "$TEMP\_ah_sc.ps1"'
  Delete "$TEMP\_ah_sc.ps1"
!macroend

!macro customUnInstall
  FileOpen $R0 "$TEMP\_ah_rm.ps1" w
  FileWrite $R0 "$$n = [char]0x5965 + [char]0x7687 + [char]0x4E0A + [char]0x54C1$\r$\n"
  FileWrite $R0 "$$d = [System.Environment]::GetFolderPath('Desktop')$\r$\n"
  FileWrite $R0 "$$p = [System.Environment]::GetFolderPath('Programs')$\r$\n"
  FileWrite $R0 "Remove-Item -Force ($$d + '\' + $$n + '.lnk') -ErrorAction SilentlyContinue$\r$\n"
  FileWrite $R0 "Remove-Item -Recurse -Force ($$p + '\' + $$n) -ErrorAction SilentlyContinue$\r$\n"
  FileClose $R0
  nsExec::ExecToLog 'powershell.exe -WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File "$TEMP\_ah_rm.ps1"'
  Delete "$TEMP\_ah_rm.ps1"
!macroend
