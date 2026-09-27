@echo off
REM Build a single-file Windows exe. Config lives in %%APPDATA%%\ohno-launcher,
REM so the exe keeps your keywords on any machine/user profile.
cd /d "%~dp0"
"C:\Python312\python.exe" -m pip install -r requirements.txt
"C:\Python312\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole --name ohno-launcher launcher.py
echo.
echo Built: %~dp0dist\ohno-launcher.exe
pause
