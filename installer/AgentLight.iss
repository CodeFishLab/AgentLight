#define MyAppName "Agent 状态灯"
#define MyAppVersion "1.5.1"
#define MyAppPublisher "AgentLight"
#define MyAppExeName "AgentLight.exe"
; 脚本位于 installer\，仓库根就是它的上一级；克隆到任意位置都能编译。
#define ProjectRoot ExtractFilePath(RemoveBackslash(SourcePath))

[Setup]
AppId={{D1609A0E-F32B-48D0-9B55-8EC683CE1A1D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; PrivilegesRequired=lowest 时 {autopf} 解析为 %LOCALAPPDATA%\Programs，无需管理员权限。
DefaultDirName={autopf}\AgentLight
DisableDirPage=no
UsePreviousAppDir=yes
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#ProjectRoot}\release
OutputBaseFilename=AgentLightSetup
SetupIconFile={#ProjectRoot}\assets\agentlight.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\AgentLight.exe
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName}
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "startup"; Description: "随 Windows 登录启动"; GroupDescription: "其他选项:"; Flags: checkedonce
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他选项:"; Flags: unchecked

[InstallDelete]
; _internal 由 PyInstaller 完整重建；升级前清空以移除不再需要的依赖。
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#ProjectRoot}\dist\AgentLight\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\AgentLight.exe"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\AgentLight.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AgentLight"; ValueData: """{app}\AgentLight.exe"" --background"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\AgentLight.exe"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\agentlightctl.exe"; Parameters: "shutdown"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAgentLightGracefully"
Filename: "{cmd}"; Parameters: "/C taskkill /IM AgentLight.exe /F"; Flags: runhidden waituntilterminated; Check: AgentLightRunning; RunOnceId: "ForceStopAgentLight"
Filename: "{app}\agentlightctl.exe"; Parameters: "integrations remove"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveAgentLightHooks"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function AgentLightRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq AgentLight.exe" | find /I "AgentLight.exe" > nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

{ 装之前先把后台请下来。只靠 CloseApplications 的重启管理器不够稳：
  它发完关闭请求就等着，一旦应用没退出，安装就卡在 Closing applications。 }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
  Waited: Integer;
begin
  if AgentLightRunning() then
  begin
    { 不带 /F 会发 WM_CLOSE，应用能正常收尾并收回托盘图标 }
    Exec(ExpandConstant('{cmd}'), '/C taskkill /IM AgentLight.exe > nul 2>&1',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Waited := 0;
    while AgentLightRunning() and (Waited < 5000) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
    { 五秒还没退就强制结束，宁可丢一次优雅收尾也不要让安装卡死 }
    if AgentLightRunning() then
      Exec(ExpandConstant('{cmd}'), '/C taskkill /IM AgentLight.exe /F > nul 2>&1',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExistingAutoStart: String;
begin
  if (CurStep = ssPostInstall) and
     RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
       'AgentLight', ExistingAutoStart) then
  begin
    RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'AgentLight', ExpandConstant('"{app}\AgentLight.exe" --background'));
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    { 用户可能在安装后从配置页开启自启动，所以不能只依赖 [Registry] 的安装记录。 }
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AgentLight');
end;
