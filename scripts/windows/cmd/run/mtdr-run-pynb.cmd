@echo off

REM Run lab.
set "script_dir=%~dp0"
if "%script_dir:~-1%"=="\" set "script_dir=%script_dir:~0,-1%"
for %%i in ("%script_dir%\..\..\..\..") do set "repo_root=%%~fi"
cd %repo_root%\src\mtdr\python
call .venv\Scripts\activate.bat
jupyter lab --ip='*' --no-browser --port=9999
