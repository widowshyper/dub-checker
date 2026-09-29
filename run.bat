@echo off
rem Dub Checker launcher for Windows.
rem Finds Python 3.10+ without relying on PATH, then hands over to bootstrap.py,
rem which creates .venv, installs the packages and starts the app.
rem Note: never expand %ProgramFiles(x86)% inside a ( ... ) block - its ")" ends the block.
setlocal EnableExtensions
cd /d "%~dp0"
set "PYEXE="
set "PYTMP=%TEMP%\dubchecker_python_%RANDOM%.txt"

rem 1. Per-user installs, newest version first
call :scan_folder "%LOCALAPPDATA%\Programs\Python" "Python3*"
if not defined PYEXE call :scan_folder "%LOCALAPPDATA%\Python" "pythoncore-3*"
rem 2. All-users installs
if not defined PYEXE call :scan_folder "%ProgramFiles%" "Python3*"
if not defined PYEXE call :scan_folder "%ProgramFiles(x86)%" "Python3*"
rem 3. The py launcher, then python on PATH
if not defined PYEXE call :try_command py -3
if not defined PYEXE call :try_command python
if not defined PYEXE goto :not_found

echo Using Python: %PYEXE%
"%PYEXE%" "%~dp0bootstrap.py" %*
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Dub Checker couldn't start - see the message above.
pause
exit /b 1

:not_found
echo.
echo Dub Checker needs Python 3.10 or newer, and none was found.
echo.
echo Looked in:
echo   %LOCALAPPDATA%\Programs\Python
echo   %ProgramFiles%
echo   %ProgramFiles(x86)%
echo   the "py" launcher and "python" on PATH
echo.
echo Install Python from https://www.python.org/downloads/ and run this again,
echo or use the portable DubChecker.exe instead.
pause
exit /b 1

:scan_folder
rem %1 = folder to look in, %2 = pattern. Folders are tried in reverse name order (newest first).
if not exist "%~1\" goto :eof
pushd "%~1" 2>nul || goto :eof
for /f "delims=" %%D in ('dir /b /ad /o-n "%~2" 2^>nul') do call :try_exe "%%~fD\python.exe"
popd
goto :eof

:try_exe
rem %1 = full path to a python.exe; accept it if it runs and is 3.10+.
if defined PYEXE goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:try_command
rem %* = a command such as "py -3"; if it is 3.10+, ask it where its python.exe lives.
if defined PYEXE goto :eof
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
%* -c "import sys; print(sys.executable)" > "%PYTMP%" 2>nul
if errorlevel 1 goto :eof
set /p PYEXE=<"%PYTMP%"
del "%PYTMP%" >nul 2>&1
goto :eof
