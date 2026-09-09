; Engraphy for Windows: one installer, no Docker, no Python, nothing to run daily.
;
; Build (see deploy/windows/README.md for how the payload is assembled):
;     makensis -DPAYLOAD=..\..\build\windows\payload -DVERSION=0.2.0 installer.nsi
;
; WHY NSIS AND NOT MSIX
;
; MSIX runs its payload in a container with a virtualised filesystem and
; registry, and its autostart is limited to declared extension points. This
; payload is a PostgreSQL cluster writing to a real data directory and a
; long-lived background process, and both of those fight the packaging model
; rather than fitting it. MSIX would buy a cleaner uninstall and Store
; distribution; it would cost the two things the product is made of. NSIS lays
; down files and runs a command, which is exactly what is needed.
;
; Inno Setup would do the same job equally well. NSIS is here because
; engraphy-desktop already ships an NSIS target through electron-builder, so a
; Windows build already means one installer toolchain rather than two.
;
; PER USER, NO ELEVATION
;
; RequestExecutionLevel user. Nothing in this install needs administrator
; rights: the files go under the user's own LOCALAPPDATA, the task runs as the
; user, and the cluster is the user's own data. An installer that asked for
; elevation would be one more dialog for a consultant to be nervous about, and
; would put the database somewhere they cannot read.
;
; UNSIGNED, AND WHAT THAT COSTS
;
; Windows SmartScreen shows "Windows protected your PC" for an unsigned
; installer with no reputation, and the user has to click "More info" and then
; "Run anyway". That is precisely the friction this whole design exists to
; remove, so signing is not cosmetic here. It is a purchasing decision rather
; than a technical one, and it is listed as such in docs/windows-native.md.

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef PAYLOAD
  !error "define PAYLOAD: the assembled directory to install"
!endif

!include "MUI2.nsh"
!include "FileFunc.nsh"

Name "Engraphy ${VERSION}"
OutFile "Engraphy-Setup-${VERSION}-win-x64.exe"
Unicode true
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\Engraphy"
InstallDirRegKey HKCU "Software\Engraphy" "InstallDir"
; The payload is ~230MB of Postgres, ONNX Runtime and a model graph. Solid LZMA
; is worth several minutes of build time and roughly half the download.
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_TEXT "Engraphy is running and will start again every time you sign in. Nothing else needs to be launched."
!define MUI_FINISHPAGE_RUN_TEXT "Show me it is working"
!define MUI_FINISHPAGE_RUN "$INSTDIR\engraphy-status.cmd"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Engraphy" SecMain
  SectionIn RO

  ; An upgrade lands on a running install. Stop it before overwriting the
  ; binaries, or the cluster keeps a file handle on the postgres.exe being
  ; replaced and the copy fails halfway through.
  DetailPrint "Stopping any running Engraphy..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -Command "Stop-ScheduledTask -TaskName Engraphy -ErrorAction SilentlyContinue"'
  IfFileExists "$INSTDIR\engraphy-win.exe" 0 +2
    nsExec::ExecToLog '"$INSTDIR\engraphy-win.exe" stop'

  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*.*"

  FileOpen $0 "$INSTDIR\version.txt" w
  FileWrite $0 "${VERSION}"
  FileClose $0

  ; A one-line shortcut target, so the Start menu entry and the finish-page
  ; button do not have to be a console command with arguments.
  FileOpen $0 "$INSTDIR\engraphy-status.cmd" w
  FileWrite $0 '@echo off$\r$\n"%~dp0engraphy-win.exe" status$\r$\npause$\r$\n'
  FileClose $0

  ; First run, or a repair. Idempotent: an existing cluster is detected and the
  ; steps that already happened are skipped. This is the long step (initdb plus
  ; the migrations), which is why it prints into the install log rather than
  ; running silently.
  DetailPrint "Setting up the database. This takes a minute on first install..."
  nsExec::ExecToLog '"$INSTDIR\engraphy-win.exe" bootstrap'
  Pop $0
  StrCmp $0 "0" +3 0
    DetailPrint "Setup failed with code $0. See the log above."
    Abort "Engraphy could not set up its database."

  ; An upgrade over an existing store: advance the schema before anything
  ; serves against it. On a fresh install bootstrap already did this and the
  ; call is a no-op that prints "schema is current".
  nsExec::ExecToLog '"$INSTDIR\engraphy-win.exe" upgrade'

  DetailPrint "Registering Engraphy to start when you sign in..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\register-task.ps1" -InstallDir "$INSTDIR" -StartNow'
  Pop $0
  StrCmp $0 "0" +2 0
    DetailPrint "Could not register the startup task (code $0). Engraphy will still run when started by hand."

  ; The token handoff. Minting is skipped when a handoff file is already there,
  ; which is what keeps an upgrade from replacing a token the desktop app has
  ; already stored. A fresh token is a `engraphy-win token` away.
  IfFileExists "$APPDATA\Engraphy\engraphy-bootstrap.json" +2 0
    nsExec::ExecToLog '"$INSTDIR\engraphy-win.exe" token --client-name desktop'

  CreateDirectory "$SMPROGRAMS\Engraphy"
  CreateShortcut "$SMPROGRAMS\Engraphy\Engraphy status.lnk" "$INSTDIR\engraphy-status.cmd"

  WriteRegStr HKCU "Software\Engraphy" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Engraphy" "Version" "${VERSION}"

  ; Per-user uninstall entry, so Engraphy appears in Settings > Apps for this
  ; user without the install having needed administrator rights.
  !define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Engraphy"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "Engraphy"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Engraphy"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  ; Order matters: the task first, so nothing restarts the server while the
  ; files are going away, then a clean database shutdown, then the files.
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\register-task.ps1" -Remove'
  IfFileExists "$INSTDIR\engraphy-win.exe" 0 +2
    nsExec::ExecToLog '"$INSTDIR\engraphy-win.exe" stop'

  Delete "$SMPROGRAMS\Engraphy\Engraphy status.lnk"
  RMDir "$SMPROGRAMS\Engraphy"
  RMDir /r "$INSTDIR"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Engraphy"
  DeleteRegKey HKCU "Software\Engraphy"

  ; The memories are NOT removed. %LOCALAPPDATA%\Engraphy holds the cluster and
  ; the backups, and an uninstall that deleted them would destroy the one thing
  ; here that cannot be reinstalled. Reinstalling adopts the existing store; a
  ; user who genuinely wants it gone deletes that folder, and the message says
  ; where it is.
  MessageBox MB_OK|MB_ICONINFORMATION "Engraphy has been removed.$\n$\nYour memories are still on this machine, in:$\n$LOCALAPPDATA\Engraphy$\n$\nReinstalling will pick them up again. Delete that folder to remove them for good."
SectionEnd
