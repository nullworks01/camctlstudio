@echo off
rem Install or repair camctl Studio: installs exactly the libraries in requirements.txt for your Windows user,
rem from pypi.org only. No admin rights, no PATH or system changes, never upgrades pip, never removes anything.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
  echo Python 3.12 was not found at %PY%.
  echo Install Python 3.12 from python.org first ^(the default "Install for me" option^), then run this again.
  echo FAILED: nothing was installed.
  pause
  exit /b 1
)
echo Installing what camctl Studio needs (only what is missing)...
echo.
"%PY%" -m pip install --user --no-input --disable-pip-version-check --upgrade-strategy only-if-needed -r requirements.txt
if errorlevel 1 (
  echo.
  echo FAILED: pip could not install everything. The lines above say why.
  pause
  exit /b 1
)
echo.
"%PY%" -m camctl.needs
if errorlevel 1 (
  echo FAILED: Studio is not ready yet.
  pause
  exit /b 1
)
echo.
echo DONE: camctl Studio is installed and ready. You can start Studio now.
pause
exit /b 0
