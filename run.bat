@echo off
REM Run the launcher with the first available Python (pinned path first).
cd /d "%~dp0"
if exist "C:\Python312\python.exe" (
  "C:\Python312\python.exe" "%~dp0launcher.py" %*
  exit /b %ERRORLEVEL%
)
py -3.12 "%~dp0launcher.py" %* 2>nul
if %ERRORLEVEL% NEQ 0 python "%~dp0launcher.py" %*
