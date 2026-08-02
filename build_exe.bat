@echo off
cd /d "%~dp0"

echo ========================================
echo  Lumveil 2.0.0 - Release Build
echo ========================================

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python 3.12 was not found.
    exit /b 1
)

"%PYTHON_EXE%" -c "import PyInstaller, PIL, tkinterdnd2" 2>nul || (
    echo [INFO] Installing build dependencies...
    "%PYTHON_EXE%" -m pip install pyinstaller pillow tkinterdnd2 python-mpv
)

echo [INFO] Building Lumveil...
"%PYTHON_EXE%" -m PyInstaller ^
  --onedir ^
  --windowed ^
  --name Lumveil ^
  --icon Lumveil.ico ^
  --clean ^
  --noconfirm ^
  lumveil.py

if errorlevel 1 (
    echo [ERROR] Lumveil build failed.
    exit /b 1
)

echo [INFO] Building Associate Tool...
"%PYTHON_EXE%" -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name Lumveil_Associate ^
  --icon Lumveil.ico ^
  --uac-admin ^
  --distpath dist\Lumveil ^
  lumveil_associate.py

if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo [INFO] Copying extra files...
if exist libmpv-2.dll    copy /Y libmpv-2.dll    dist\Lumveil\ >nul
if exist libmpv-2.dll    copy /Y libmpv-2.dll    dist\Lumveil\_internal\ >nul
if exist ffmpeg.exe      copy /Y ffmpeg.exe      dist\Lumveil\ >nul
if exist ffmpeg.exe      copy /Y ffmpeg.exe      dist\Lumveil\_internal\ >nul
if exist README.md       copy /Y README.md       dist\Lumveil\ >nul
if exist Lumveil.ico     copy /Y Lumveil.ico     dist\Lumveil\ >nul
if exist THIRD_PARTY_NOTICES.md copy /Y THIRD_PARTY_NOTICES.md dist\Lumveil\ >nul
if exist licenses        xcopy /E /I /Y licenses dist\Lumveil\licenses\ >nul
if exist shaders         xcopy /E /I /Y shaders  dist\Lumveil\shaders\ >nul

set "MAKENSIS=vendor\nsis-3.12\makensis.exe"
if not exist "%MAKENSIS%" (
    echo [ERROR] NSIS compiler was not found at %MAKENSIS%.
    exit /b 1
)

echo [INFO] Building installer...
"%MAKENSIS%" installer\Lumveil.nsi
if errorlevel 1 (
    echo [ERROR] Installer build failed.
    exit /b 1
)

echo.
echo ========================================
echo  Done: dist\Lumveil_v2.0.0_Setup.exe
echo ========================================
