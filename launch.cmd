@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0Windows\JARVIS_Robot_Console.exe" goto run_executable
if not exist "%~dp0main.py" goto missing_source
if exist "%~dp0.venv\Scripts\python.exe" goto run_venv

where py >nul 2>&1
if errorlevel 1 goto try_python
py -3.13 -c "import sys" >nul 2>&1
if errorlevel 1 goto try_python
py -3.13 "%~dp0main.py"
goto source_exit

:try_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 13))" >nul 2>&1
if errorlevel 1 goto missing_python
python "%~dp0main.py"
goto source_exit

:run_venv
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
goto source_exit

:run_executable
start "" "%~dp0Windows\JARVIS_Robot_Console.exe"
exit /b 0

:missing_source
echo The application source is missing. Keep launch.cmd beside main.py.
pause
exit /b 1

:missing_python
echo Python 3.13 was not found. Install 64-bit Python 3.13 with Tcl/Tk support.
echo Then follow the virtual environment and dependency setup in README.md.
pause
exit /b 1

:source_exit
set "JARVIS_EXIT=%ERRORLEVEL%"
if not "%JARVIS_EXIT%"=="0" (
    echo.
    echo The application exited with an error. Check dependencies and data\app.log.
    echo See README.md for setup instructions.
    pause
)
exit /b %JARVIS_EXIT%
