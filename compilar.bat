@echo off
rem Arma SEGA MENU.exe (PyInstaller, carpeta) + datos, y el zip para el release de GitHub.
cd /d "%~dp0"
set PY=C:\Users\bravo\AppData\Local\Programs\Python\Python313\python.exe
for /f "tokens=3" %%v in ('findstr /b /c:"VERSION = " sistema.py') do set VERSION=%%~v
echo Compilando SEGA MENU %VERSION% ...
set DIST=%~dp0build\dist
set OBRA=%~dp0build\obra
if exist "%DIST%" rmdir /s /q "%DIST%"
"%PY%" -m PyInstaller --noconfirm --clean --windowed --name "SEGA MENU" --icon "%~dp0icono.ico" ^
  --hidden-import pygame._sdl2.audio --hidden-import pygame._sdl2.controller ^
  --distpath "%DIST%" --workpath "%OBRA%" --specpath "%OBRA%" "SEGA MENU.pyw"
if errorlevel 1 (echo FALLO PyInstaller & exit /b 1)
robocopy datos "%DIST%\SEGA MENU\datos" /E /NFL /NDL /NJH /NJS >nul
copy /y README.md "%DIST%\SEGA MENU\" >nul
copy /y LICENCIAS.md "%DIST%\SEGA MENU\" >nul
if not exist release mkdir release
if exist "release\SEGA MENU" rmdir /s /q "release\SEGA MENU"
robocopy "%DIST%\SEGA MENU" "release\SEGA MENU" /E /NFL /NDL /NJH /NJS >nul
if exist "release\SEGA-MENU-v%VERSION%.zip" del "release\SEGA-MENU-v%VERSION%.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'release\SEGA MENU' -DestinationPath 'release\SEGA-MENU-v%VERSION%.zip'"
echo Listo: release\SEGA MENU\SEGA MENU.exe  y  release\SEGA-MENU-v%VERSION%.zip
