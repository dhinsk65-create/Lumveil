Unicode true
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "x64.nsh"

!define APP_NAME "Lumveil"
!define APP_VERSION "2.0.0"
!define APP_PUBLISHER "Fan"
!define APP_EXE "Lumveil.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumveil"

!cd ".."

Name "${APP_NAME} ${APP_VERSION}"
OutFile "dist\Lumveil_v${APP_VERSION}_Setup.exe"
InstallDir "$PROGRAMFILES64\Lumveil"
InstallDirRegKey HKLM "${UNINSTALL_KEY}" "InstallLocation"
Icon "Lumveil.ico"
UninstallIcon "Lumveil.ico"
BrandingText "Lumveil ${APP_VERSION}"

!define MUI_ABORTWARNING
!define MUI_ICON "Lumveil.ico"
!define MUI_UNICON "Lumveil.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Run Lumveil"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Japanese"
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "2.0.0.0"
VIAddVersionKey /LANG=${LANG_JAPANESE} "ProductName" "Lumveil"
VIAddVersionKey /LANG=${LANG_JAPANESE} "ProductVersion" "2.0.0"
VIAddVersionKey /LANG=${LANG_JAPANESE} "CompanyName" "Fan"
VIAddVersionKey /LANG=${LANG_JAPANESE} "LegalCopyright" "Copyright (c) 2026 Fan"
VIAddVersionKey /LANG=${LANG_JAPANESE} "FileDescription" "Lumveil Video Player Installer"
VIAddVersionKey /LANG=${LANG_JAPANESE} "FileVersion" "2.0.0.0"

Section "Lumveil" SEC_MAIN
  SectionIn RO
  SetRegView 64
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Lumveil requires 64-bit Windows."
    Abort
  ${EndIf}

  SetShellVarContext all
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\_internal"
  File /r "dist\Lumveil\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Lumveil"
  CreateShortcut "$SMPROGRAMS\Lumveil\Lumveil.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\Lumveil.ico"
  CreateShortcut "$SMPROGRAMS\Lumveil\Video file association.lnk" "$INSTDIR\Lumveil_Associate.exe" "" "$INSTDIR\Lumveil.ico"
  CreateShortcut "$DESKTOP\Lumveil.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\Lumveil.ico"

  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "Lumveil ${APP_VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr HKLM "${UNINSTALL_KEY}" "QuietUninstallString" "$\"$INSTDIR\Uninstall.exe$\" /S"
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetRegView 64
  SetShellVarContext all
  Delete "$DESKTOP\Lumveil.lnk"
  RMDir /r "$SMPROGRAMS\Lumveil"
  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  RMDir /r "$INSTDIR"
  MessageBox MB_OK "Lumveil was removed. Settings in %APPDATA%\Lumveil were preserved."
SectionEnd
