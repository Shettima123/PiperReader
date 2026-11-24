#define MyAppName "Piper Reader"
#define MyAppVersion "1.6.4"
#define MyAppPublisher "Piper Reader"
#define MyAppExeName "PiperReader.exe"

[Setup]
AppId={{9A094CA5-10A4-4B8D-A2F7-B81871422950}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\PiperReader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=PiperReaderSetup-{#MyAppVersion}
SetupIconFile=..\build_assets\piper_reader.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
ChangesEnvironment=no
ChangesAssociations=no
VersionInfoVersion=1.6.4.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Tasks]
Name: "startup"; Description: "Start Piper Reader when I sign in"; GroupDescription: "Additional tasks:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional tasks:"; Flags: unchecked

[Files]
Source: "..\dist\PiperReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\licenses"; DestName: "PIPER_READER_THIRD_PARTY_NOTICES.md"; Flags: ignoreversion
Source: "..\build_assets\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\INSTALLER_README.md"; DestDir: "{app}"; DestName: "INSTALLATION_AND_DISTRIBUTION.md"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autoprograms}\{#MyAppName} browser extension folder"; Filename: "{sys}\explorer.exe"; Parameters: """{localappdata}\PiperReader\browser_extension"""; WorkingDir: "{localappdata}\PiperReader"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Piper Reader"; ValueData: """{app}\{#MyAppExeName}"" --start-hidden"; Flags: uninsdeletevalue; Tasks: startup
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Piper Reader"; Flags: deletevalue; Tasks: not startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
procedure RequestRunningAppExit;
var
  ResultCode: Integer;
  AppExe: String;
begin
  AppExe := ExpandConstant('{app}\{#MyAppExeName}');
  if FileExists(AppExe) then
  begin
    Exec(AppExe, '--shutdown-for-update', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
    Sleep(1000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  RequestRunningAppExit;
  Result := '';
end;

function InitializeUninstall: Boolean;
begin
  RequestRunningAppExit;
  Result := True;
end;
