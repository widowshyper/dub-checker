@echo off
rem Builds the portable app: dist\Dub Checker\ and dist\Dub Checker.zip
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0run.bat" --setup-only
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" "%~dp0packaging\build.py"
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo The build didn't finish - see the messages above.
pause
exit /b 1
