; Inno Setup installer for Arc GPU Control — fan curve + overclock for the
; Intel Arc Pro B60 / B70 on Windows. Produces a single Setup.exe.
;
; Build with:  powershell -File build-installer.ps1   (compiles the binaries too)
; or directly: "ISCC.exe" arc-gpu-control.iss   (binaries must already be built)

#define AppName    "Arc GPU Control"
#define AppVersion "1.1.2"
#define Publisher  "exzile"
#define RepoURL    "https://github.com/exzile/intel-arc-pro-fan-control"

[Setup]
AppId={{7F3A9C21-4E8B-4C6D-9B2A-A6C0FA4C0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppSupportURL={#RepoURL}
AppPublisherURL={#RepoURL}
DefaultDirName={autopf}\ArcFanControl
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
OutputDir=output
OutputBaseFilename=ArcGpuControl-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\src\gui\app.ico
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\arc-gpu-gui.exe

[Files]
Source: "..\build\Release\arc-gpu.exe";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\Release\arc-fan-service.exe";  DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\Release\arc-gpu-gui.exe";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\install.ps1";                        DestDir: "{app}"; Flags: ignoreversion
Source: "..\uninstall.ps1";                      DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";                          DestDir: "{app}"; Flags: ignoreversion isreadme

; Post-install: register + start the boot service, disable Intel's contending
; service, grant config permissions, add the Start-Menu shortcut + tray auto-start.
; (install.ps1 skips the file copy when BuildDir == the install dir.)
[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -BuildDir ""{app}"""; \
  StatusMsg: "Registering the fan / overclock service..."; \
  Flags: runhidden waituntilterminated
Filename: "{app}\arc-gpu-gui.exe"; Parameters: "--tray"; \
  Description: "Launch Arc GPU Control (tray)"; \
  Flags: postinstall nowait skipifsilent

; Uninstall: stop + remove the service, re-enable Intel, clear the auto-start.
; -KeepInstallDir: Inno removes the program files itself.
[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall.ps1"" -KeepInstallDir"; \
  RunOnceId: "ArcUninstallTasks"; \
  Flags: runhidden waituntilterminated
