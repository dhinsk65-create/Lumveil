@echo off
cd /d "%~dp0"

echo ========================================
echo  Lumveil - EXE Build
echo ========================================

python -c "import PyInstaller" 2>nul || (
    echo [INFO] Installing PyInstaller...
    pip install pyinstaller
)

echo [INFO] Building Lumveil...
pyinstaller ^
  --onedir ^
  --windowed ^
  --name Lumveil ^
  --icon Lumveil.ico ^
  --clean ^
  --noconfirm ^
  lumveil.py

echo [INFO] Building Associate Tool...
pyinstaller ^
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
if exist README.md       copy /Y README.md       dist\Lumveil\ >nul
if exist shaders         xcopy /E /I /Y shaders  dist\Lumveil\shaders\ >nul

echo [INFO] Clearing icon cache...
taskkill /f /im explorer.exe >nul 2>&1
del /f /q "%LOCALAPPDATA%\IconCache.db" >nul 2>&1
del /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*" >nul 2>&1
del /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache*" >nul 2>&1
start explorer.exe

echo.
echo ========================================
echo  Done: dist\Lumveil\Lumveil.exe
echo ========================================
pause
