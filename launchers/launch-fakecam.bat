@echo off
REM Wink Fakecam - Launch launcher (Windows).
REM
REM Opens a fresh Chrome instance with --use-file-for-fake-video-capture
REM pointed at the Y4M produced by Setup. Chrome's getUserMedia will then
REM return that video as if it were the camera.

setlocal enabledelayedexpansion

set INSTALL_DIR=%USERPROFILE%\wink-fakecam
set DEMO_URL=https://wink-image-demo.fly.dev
set Y4M=%INSTALL_DIR%\videos\Virat-Kohli-realvideo.y4m

REM Find Chrome (try the common install paths)
set CHROME=
if exist "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe" set CHROME=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe
if exist "%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe" set CHROME=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe

if "%CHROME%"=="" (
  echo ERROR: Google Chrome not found. Install from https://google.com/chrome
  pause
  exit /b 1
)

if not exist "%Y4M%" (
  echo ERROR: Sample video not prepared. Run Setup Fakecam first.
  echo Expected at: %Y4M%
  pause
  exit /b 1
)

set PROFILE_DIR=%TEMP%\chrome-fakecam-%RANDOM%
echo Launching Chrome (profile: %PROFILE_DIR%)
echo Y4M: %Y4M%
echo URL: %DEMO_URL%

start "" "%CHROME%" --user-data-dir="%PROFILE_DIR%" --use-fake-ui-for-media-stream --use-fake-device-for-media-stream --use-file-for-fake-video-capture="%Y4M%" %DEMO_URL%
