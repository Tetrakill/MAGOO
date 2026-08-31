; Inno Setup script for Magoo (compiles under 6.x and 7.x). Built by
; packaging/build.ps1, which passes
; MyAppVersion on the command line.
;
; Per-user by design. pyfa — the closest comparable EVE tool, same
; Python/PyInstaller/Inno stack — installs per-machine and takes a UAC
; prompt for it; Magoo does not need administrator rights for anything, so
; asking for them would be a worse install for no benefit.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Magoo"
#define MyAppExeName "Magoo.exe"
#define MyAppPublisher "Magoo"

[Setup]
; This GUID identifies the application across releases and drives upgrade
; detection. It must NEVER change, or every new version installs alongside
; the old one instead of replacing it.
AppId={{5B5C8D54-5629-48E4-9C1C-4390E86C2409}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}

; "lowest" keeps the whole install inside the user profile: no UAC prompt,
; and {autopf} resolves to %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\dist
OutputBaseFilename=MagooSetup-{#MyAppVersion}
SetupIconFile=magoo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The PyInstaller onedir tree. The portable marker is deliberately NOT
; shipped here: an installed build keeps its data in %LOCALAPPDATA%\Magoo,
; where an uninstall cannot take it.
Source: "..\dist\Magoo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key =
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2KeyUser =
    'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2Installed: Boolean;
var
  Version: String;
begin
  { Microsoft ships the Evergreen runtime with Windows 11 and it is present
    on the large majority of Windows 10 machines, but they explicitly say to
    handle the gap. Magoo falls back to the default browser when it is
    missing, so this only warns. }
  Result := False;
  if RegQueryStringValue(HKLM, WebView2Key, 'pv', Version) then
    if (Version <> '') and (Version <> '0.0.0.0') then
      Result := True;
  if not Result then
    if RegQueryStringValue(HKCU, WebView2KeyUser, 'pv', Version) then
      if (Version <> '') and (Version <> '0.0.0.0') then
        Result := True;
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if not WebView2Installed then
    MsgBox(
      'Magoo could not find the Microsoft Edge WebView2 runtime, which it '
      + 'uses for its window.' + #13#10#13#10
      + 'Magoo will still work — it opens in your default browser instead. '
      + 'To get the app window, install the WebView2 runtime from Microsoft '
      + 'and restart Magoo.',
      mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  { Never delete someone's plans, settings and ESI tokens without asking,
    and default to keeping them: an uninstall is often just a step in
    reinstalling. }
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Magoo');
    if DirExists(DataDir) then
      if MsgBox('Also delete your Magoo data?' + #13#10#13#10
        + 'This is your plans, settings, price history and saved EVE '
        + 'logins, in:' + #13#10 + DataDir + #13#10#13#10
        + 'Choose No if you plan to reinstall Magoo.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
