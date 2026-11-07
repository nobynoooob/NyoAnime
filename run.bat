@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Always run from the repository root, no matter where the shortcut lives.
cd /d "%~dp0"

title NyoAnime Launcher

echo ============================================
echo   NyoAnime - One-Click Windows Launcher
echo ============================================
echo.

REM ---------------------------------------------------------------
REM 1. Python
REM ---------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] Python was not found on this system.
    echo [*] Installing Python 3.11 with winget...
    winget install --id Python.Python.3.11 -e --silent --accept-package-agreements --accept-source-agreements

    if errorlevel 1 (
        echo.
        echo [X] Automatic Python installation failed.
        echo     Install Python 3.11 manually from https://www.python.org/downloads/
        echo     and tick "Add python.exe to PATH", then run this file again.
        echo.
        pause
        exit /b 1
    )

    echo.
    echo [OK] Python 3.11 installed.
    echo [*] Close this window and run run.bat again so PATH is refreshed.
    echo.
    pause
    exit /b 0
)

for /f "delims=" %%v in ('python --version 2^>^&1') do set "PY_VERSION=%%v"
echo [OK] Found %PY_VERSION%

REM ---------------------------------------------------------------
REM 2. Local .env overrides (optional)
REM ---------------------------------------------------------------
if exist ".env" (
    echo [*] Loading environment variables from .env...
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        set "LINE=%%a"
        if not "!LINE!"=="" if not "!LINE:~0,1!"=="#" set "%%a=%%b"
    )
)

REM ---------------------------------------------------------------
REM 3. Virtual environment
REM ---------------------------------------------------------------
if not exist "venv\Scripts\python.exe" (
    echo [*] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo [X] Could not create the virtual environment.
        echo     Make sure Python's venv module is available.
        echo.
        pause
        exit /b 1
    )
)

call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo.
    echo [X] Could not activate the virtual environment.
    echo.
    pause
    exit /b 1
)
echo [OK] Virtual environment active.

REM ---------------------------------------------------------------
REM 4. Dependencies
REM ---------------------------------------------------------------
if not exist "requirements.txt" (
    echo.
    echo [X] requirements.txt was not found in %CD%
    echo.
    pause
    exit /b 1
)

echo [*] Upgrading pip...
python -m pip install --upgrade pip --quiet

echo [*] Installing dependencies from requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [X] Dependency installation failed. Scroll up for the pip error.
    echo.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM 5. Run
REM ---------------------------------------------------------------
echo.
echo [*] Starting NyoAnime...
echo.
python main.py %*
set "EXIT_CODE=%errorlevel%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo [X] NyoAnime exited with code %EXIT_CODE%. See the messages above.
) else (
    echo [OK] NyoAnime closed.
)
echo.
pause
exit /b %EXIT_CODE%
