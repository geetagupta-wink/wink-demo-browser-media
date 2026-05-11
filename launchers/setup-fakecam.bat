@echo off
REM Wink Fakecam - Setup launcher (Windows).
REM
REM Run once. Downloads ffmpeg from gyan.dev, downloads the sample Virat
REM video from the deployed demo server, and pre-converts it to the Y4M
REM format Chrome's --use-file-for-fake-video-capture expects.
REM
REM After this runs, the "Launch Fakecam" button on the demo page will work.

setlocal enabledelayedexpansion

set INSTALL_DIR=%USERPROFILE%\wink-fakecam
set DEMO_HOST=https://wink-image-demo.fly.dev
set SAMPLE_NAME=Virat-Kohli-realvideo
set FFMPEG_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

echo === Wink Fakecam setup ===
echo Install dir: %INSTALL_DIR%
echo.

mkdir "%INSTALL_DIR%\videos" 2>nul
mkdir "%INSTALL_DIR%\bin" 2>nul

REM 1) ffmpeg
if exist "%INSTALL_DIR%\bin\ffmpeg.exe" (
  echo ffmpeg already installed at %INSTALL_DIR%\bin\ffmpeg.exe
) else (
  echo Downloading ffmpeg from %FFMPEG_URL% ^(about 75MB^)...
  curl -fL -o "%TEMP%\ffmpeg.zip" "%FFMPEG_URL%"
  if errorlevel 1 (
    echo ERROR: Failed to download ffmpeg. Check your internet connection.
    pause
    exit /b 1
  )
  echo Extracting ffmpeg...
  powershell -NoProfile -Command "Expand-Archive -Force '%TEMP%\ffmpeg.zip' '%TEMP%\ffmpeg-extracted'"
  for /d %%G in ("%TEMP%\ffmpeg-extracted\ffmpeg-*") do (
    copy /y "%%G\bin\ffmpeg.exe" "%INSTALL_DIR%\bin\ffmpeg.exe" >nul
  )
  rmdir /s /q "%TEMP%\ffmpeg-extracted" 2>nul
  del "%TEMP%\ffmpeg.zip" 2>nul
  if not exist "%INSTALL_DIR%\bin\ffmpeg.exe" (
    echo ERROR: ffmpeg extraction failed.
    pause
    exit /b 1
  )
  echo ffmpeg installed.
)

REM 2) sample video
set MP4=%INSTALL_DIR%\videos\%SAMPLE_NAME%.mp4
if exist "%MP4%" (
  echo Sample video already present at %MP4%
) else (
  echo Downloading sample video from %DEMO_HOST%/samples/%SAMPLE_NAME%.mp4 ...
  curl -fL -o "%MP4%" "%DEMO_HOST%/samples/%SAMPLE_NAME%.mp4"
  if errorlevel 1 (
    echo ERROR: Failed to download sample video.
    pause
    exit /b 1
  )
)

REM 3) y4m pre-conversion
set Y4M=%INSTALL_DIR%\videos\%SAMPLE_NAME%.y4m
if exist "%Y4M%" (
  echo Y4M already present at %Y4M%
) else (
  echo Converting MP4 -^> Y4M ^(uses ~80MB disk^)...
  "%INSTALL_DIR%\bin\ffmpeg.exe" -y -i "%MP4%" -pix_fmt yuv420p "%Y4M%" -hide_banner -loglevel error
)

REM 4) Local copy of the launch script — fetched via curl so it lands without
REM    a Zone.Identifier ADS, meaning SmartScreen won't pester on double-click.
set LAUNCHER=%INSTALL_DIR%\launch-fakecam.bat
echo Installing local launcher to %LAUNCHER% ...
curl -fL -o "%LAUNCHER%" "%DEMO_HOST%/launcher/launch-fakecam.bat"

echo.
echo === Setup complete ===
echo To run an attack from now on, double-click:
echo     %LAUNCHER%
echo (Or: explorer %INSTALL_DIR%)
echo.
pause
