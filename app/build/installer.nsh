; Aohuang Desktop - NSIS hooks
; Must stay pure ASCII: makensis mis-decodes multibyte comments on this
; machine (the project path contains Chinese characters).

!macro customInstall
  CreateShortCut "$DESKTOP\奥皇上品.lnk" "$INSTDIR\AohuangShangpin.exe" "--no-sandbox" "$INSTDIR\AohuangShangpin.exe" 0
  CreateDirectory "$SMPROGRAMS\奥皇上品"
  CreateShortCut "$SMPROGRAMS\奥皇上品\奥皇上品.lnk" "$INSTDIR\AohuangShangpin.exe" "--no-sandbox" "$INSTDIR\AohuangShangpin.exe" 0
  CreateShortCut "$SMPROGRAMS\奥皇上品\卸载 奥皇上品.lnk" "$INSTDIR\Uninstall 奥皇上品.exe"
!macroend

!macro customUnInstall
  Delete "$DESKTOP\奥皇上品.lnk"
  Delete "$SMPROGRAMS\奥皇上品\奥皇上品.lnk"
  Delete "$SMPROGRAMS\奥皇上品\卸载 奥皇上品.lnk"
  RMDir "$SMPROGRAMS\奥皇上品"
!macroend
