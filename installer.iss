; Inno Setup script: makes DayTradeRiskManager-Setup.exe (normal Windows installer, no admin rights needed)
[Setup]
AppName=Day Trade Risk Manager
AppVersion=1.0
AppPublisher=Your Company
DefaultDirName={localappdata}\Programs\DayTradeRiskManager
DefaultGroupName=Day Trade Risk Manager
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename=DayTradeRiskManager-Setup
Compression=lzma
SolidCompression=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\DayTradeRiskManager.exe

[Files]
Source: "dist\DayTradeRiskManager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Day Trade Risk Manager"; Filename: "{app}\DayTradeRiskManager.exe"
Name: "{autodesktop}\Day Trade Risk Manager"; Filename: "{app}\DayTradeRiskManager.exe"

[Run]
Filename: "{app}\DayTradeRiskManager.exe"; Description: "Start Day Trade Risk Manager"; Flags: nowait postinstall skipifsilent
