@echo off
rem One-time setup for Mic Control: Python, the .NET runtime (monitor + tray icon), the plugins and a desktop shortcut.
rem Equalizer APO has to be installed by hand first, see README.md.
cd /d "%~dp0"
echo.
echo  Mic Control setup
echo  =================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python is missing, installing it with winget...
  winget install -e --id Python.Python.3.12
  echo Close this window and run setup.bat again, so Windows finds Python.
  pause
  exit /b
)

dotnet --list-runtimes 2>nul | findstr /c:"Microsoft.WindowsDesktop.App 9." >nul
if errorlevel 1 (
  echo The .NET 9 Desktop Runtime is missing ^(needed for the monitor and the tray icon^), installing it...
  winget install -e --id Microsoft.DotNet.DesktopRuntime.9
)

reg query "HKLM\SOFTWARE\EqualizerAPO" /v InstallPath >nul 2>nul
if errorlevel 1 (
  echo.
  echo  ! Equalizer APO is not installed yet. Install it first: https://sourceforge.net/projects/equalizerapo/
  echo    In its Device Selector, tick your microphone under "Capture devices", then restart the PC.
  echo.
)

echo Downloading the plugins from their makers' websites...
python tools\get_plugins.py

powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Mic Control.lnk'); $s.TargetPath = '%~dp0Mic Control.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save()"
echo.
echo Done. Open "Mic Control" from your desktop.
pause
