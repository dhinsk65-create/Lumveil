Unicode true
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"

!define APP_NAME "Lumveil"
!define APP_VERSION "2.1.0"
!define APP_PUBLISHER "Fan"
!define APP_EXE "Lumveil.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumveil"

!cd ".."

!ifndef PAYLOAD
  !define PAYLOAD "dist\Lumveil"
!endif
Name "${APP_NAME} ${APP_VERSION}"
!ifndef OUTFILE
  !define OUTFILE "dist\Lumveil_v${APP_VERSION}_Setup.exe"
!endif
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\Lumveil"
InstallDirRegKey HKLM "${UNINSTALL_KEY}" "InstallLocation"
Icon "Lumveil.ico"
UninstallIcon "Lumveil.ico"
BrandingText "Lumveil ${APP_VERSION}"

!define MUI_ABORTWARNING
!define MUI_ICON "Lumveil.ico"
!define MUI_UNICON "Lumveil.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "$(LumveilRunText)"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Japanese"
!insertmacro MUI_LANGUAGE "English"

Var StageDir
Var BackupDir

LangString LumveilRunText ${LANG_JAPANESE} "Lumveilを実行"
LangString LumveilRunText ${LANG_ENGLISH} "Run Lumveil"
LangString LumveilRunning ${LANG_JAPANESE} "Lumveilが起動中です。Lumveilを終了してからインストーラーを再実行してください。セットアップを終了します。"
LangString LumveilRunning ${LANG_ENGLISH} "Lumveil is running. Close Lumveil and run this installer again. Setup will now exit."
LangString LumveilCheckFailed ${LANG_JAPANESE} "Lumveilの起動状態を確認できませんでした。Lumveilを終了してから続行してください。セットアップを終了します。"
LangString LumveilCheckFailed ${LANG_ENGLISH} "Could not check whether Lumveil is running. Close Lumveil before continuing. Setup will now exit."
LangString LumveilRequire64Bit ${LANG_JAPANESE} "Lumveilには64ビット版Windowsが必要です。"
LangString LumveilRequire64Bit ${LANG_ENGLISH} "Lumveil requires 64-bit Windows."
LangString LumveilStageCollision ${LANG_JAPANESE} "一時インストール先が既に存在します。変更は行いませんでした: $StageDir"
LangString LumveilStageCollision ${LANG_ENGLISH} "The temporary install path already exists. Nothing was changed: $StageDir"
LangString LumveilStageCreateFailed ${LANG_JAPANESE} "一時インストールフォルダーを作成できませんでした。既存のインストールは保持されました。"
LangString LumveilStageCreateFailed ${LANG_ENGLISH} "The temporary install folder could not be created. The existing installation was preserved."
LangString LumveilBackupCollision ${LANG_JAPANESE} "旧バージョンのバックアップ先が既に存在します。変更は行いませんでした: $BackupDir"
LangString LumveilBackupCollision ${LANG_ENGLISH} "The previous-version backup path already exists. Nothing was changed: $BackupDir"
LangString LumveilStageFailed ${LANG_JAPANESE} "Lumveilのファイルを一時フォルダーにコピーできませんでした。既存のインストールは保持されました。"
LangString LumveilStageFailed ${LANG_ENGLISH} "Lumveil files could not be staged. The existing installation was preserved."
LangString LumveilBackupFailed ${LANG_JAPANESE} "旧バージョンを退避できませんでした。ファイルはインストールされていません。"
LangString LumveilBackupFailed ${LANG_ENGLISH} "The previous installation could not be preserved. No files were installed."
LangString LumveilRestoreSucceeded ${LANG_JAPANESE} "Lumveilをインストールできませんでした。旧バージョンを復元しました。"
LangString LumveilRestoreSucceeded ${LANG_ENGLISH} "Lumveil could not be installed. The previous version has been restored."
LangString LumveilNoPrevious ${LANG_JAPANESE} "Lumveilをインストールできませんでした。以前のインストールはありませんでした。"
LangString LumveilNoPrevious ${LANG_ENGLISH} "Lumveil could not be installed. No previous installation was present."
LangString LumveilRestoreFailed ${LANG_JAPANESE} "Lumveilをインストールできず、旧バージョンも元の場所に戻せませんでした。バックアップは次の場所に残っています: $BackupDir"
LangString LumveilRestoreFailed ${LANG_ENGLISH} "Lumveil could not be installed and the previous version could not be moved back. Its backup remains at: $BackupDir"
LangString LumveilRemoved ${LANG_JAPANESE} "Lumveilを削除しました。%APPDATA%\Lumveil の設定は保持されました。"
LangString LumveilRemoved ${LANG_ENGLISH} "Lumveil was removed. Settings in %APPDATA%\Lumveil were preserved."
LangString LumveilUninstallRunning ${LANG_JAPANESE} "Lumveilが起動中です。Lumveilを終了してからアンインストーラーを再実行してください。"
LangString LumveilUninstallRunning ${LANG_ENGLISH} "Lumveil is running. Close Lumveil and run the uninstaller again."
LangString LumveilUnassociateFailed ${LANG_JAPANESE} "関連付けを解除できませんでした。Lumveilのファイルは削除していません。セットアップで関連付けツールを復元するか、関連付けを確認してから再試行してください。"
LangString LumveilUnassociateFailed ${LANG_ENGLISH} "File associations could not be removed. Lumveil files were not deleted. Restore the association tool using setup, or check your associations before retrying."

Function .onInit
  nsExec::ExecToStack 'tasklist /FI "IMAGENAME eq ${APP_EXE}" /FO CSV /NH'
  Pop $0
  Pop $1
  StrCpy $2 $1 13
  StrCmp $2 '"${APP_EXE}"' _lumveil_process_running
  StrCmp $0 "0" _lumveil_process_check_done _lumveil_process_check_failed
_lumveil_process_running:
  MessageBox MB_ICONEXCLAMATION|MB_OK "$(LumveilRunning)"
  Abort
_lumveil_process_check_failed:
  MessageBox MB_ICONEXCLAMATION|MB_OK "$(LumveilCheckFailed)"
  Abort
_lumveil_process_check_done:
FunctionEnd

Function un.onInit
  nsExec::ExecToStack 'tasklist /FI "IMAGENAME eq ${APP_EXE}" /FO CSV /NH'
  Pop $0
  Pop $1
  StrCpy $2 $1 13
  StrCmp $2 '"${APP_EXE}"' uninstall_running
  StrCmp $0 "0" uninstall_check_done uninstall_check_failed
uninstall_running:
  MessageBox MB_ICONEXCLAMATION|MB_OK "$(LumveilUninstallRunning)"
  Abort
uninstall_check_failed:
  MessageBox MB_ICONEXCLAMATION|MB_OK "$(LumveilCheckFailed)"
  Abort
uninstall_check_done:
FunctionEnd

VIProductVersion "2.1.0.0"
VIAddVersionKey /LANG=${LANG_JAPANESE} "ProductName" "Lumveil"
VIAddVersionKey /LANG=${LANG_JAPANESE} "ProductVersion" "2.1.0"
VIAddVersionKey /LANG=${LANG_JAPANESE} "CompanyName" "Fan"
VIAddVersionKey /LANG=${LANG_JAPANESE} "LegalCopyright" "Copyright (c) 2026 Fan"
VIAddVersionKey /LANG=${LANG_JAPANESE} "FileDescription" "Lumveil Video Player Installer"
VIAddVersionKey /LANG=${LANG_JAPANESE} "FileVersion" "2.1.0.0"

Section "Lumveil" SEC_MAIN
  SectionIn RO
  SetRegView 64
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "$(LumveilRequire64Bit)"
    Abort
  ${EndIf}

  SetShellVarContext all

  ; Use unique sibling folders and refuse collisions so existing data is kept.
  System::Call 'kernel32::GetTickCount() i .r0'
  StrCpy $StageDir "$INSTDIR.stage-$0"
  StrCpy $BackupDir "$INSTDIR.previous-$0"
  IfFileExists "$StageDir" stage_collision
  IfFileExists "$BackupDir" backup_collision
  ClearErrors
  CreateDirectory "$StageDir"
  IfErrors stage_create_failed
  SetOutPath "$StageDir"
  File /r "${PAYLOAD}\*.*"
  IfErrors stage_failed

  ; Keep the old install as a sibling backup until the user removes it.
  IfFileExists "$INSTDIR\${APP_EXE}" 0 install_staged
  SetOutPath "$TEMP"
  ClearErrors
  Rename "$INSTDIR" "$BackupDir"
  IfErrors backup_failed

install_staged:
  SetOutPath "$TEMP"
  ClearErrors
  Rename "$StageDir" "$INSTDIR"
  IfErrors install_failed
  Goto install_done

stage_collision:
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilStageCollision)"
  Abort
stage_create_failed:
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilStageCreateFailed)"
  Abort
backup_collision:
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilBackupCollision)"
  Abort
stage_failed:
  ; StageDir is created by this run; do not touch any colliding path.
  SetOutPath "$TEMP"
  RMDir /r "$StageDir"
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilStageFailed)"
  Abort
backup_failed:
  SetOutPath "$TEMP"
  RMDir /r "$StageDir"
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilBackupFailed)"
  Abort
install_failed:
  SetOutPath "$TEMP"
  IfFileExists "$BackupDir\${APP_EXE}" 0 install_failed_no_backup
  ClearErrors
  Rename "$BackupDir" "$INSTDIR"
  IfErrors install_failed_restore_failed
  RMDir /r "$StageDir"
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilRestoreSucceeded)"
  Abort
install_failed_no_backup:
  RMDir /r "$StageDir"
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilNoPrevious)"
  Abort
install_failed_restore_failed:
  RMDir /r "$StageDir"
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilRestoreFailed)"
  Abort
install_done:

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
  ; Repair this install's current-user associations before removing its files.
  IfFileExists "$INSTDIR\Lumveil_Associate.exe" 0 unassociate_failed
  ClearErrors
  ExecWait '"$INSTDIR\Lumveil_Associate.exe" --unassociate-all' $0
  IfErrors unassociate_failed
  StrCmp $0 "0" unassociate_done
unassociate_failed:
  MessageBox MB_ICONSTOP|MB_OK "$(LumveilUnassociateFailed)"
  Abort
unassociate_done:
  Delete "$DESKTOP\Lumveil.lnk"
  RMDir /r "$SMPROGRAMS\Lumveil"
  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  RMDir /r "$INSTDIR"
  MessageBox MB_OK "$(LumveilRemoved)"
SectionEnd
