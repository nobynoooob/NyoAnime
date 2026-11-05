@echo off

REM One-click launcher for NyoAnime Watch Together on Windows

set SCRIPT_DIR=%~dp0

REM Load .env file if it exists
if exist "%SCRIPT_DIR%.env" (
    echo Loading environment variables from .env...
    for /f "tokens=1,2 delims==" %%a in ("%SCRIPT_DIR%.env") do (
        set "%%a=%%b"
    )
)

REM Check if Python is installed
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python is not installed. Please install Python and try again.
    pause
    exit /b 1
)

REM Check if virtual environment exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate virtual environment
call "%SCRIPT_DIR%venv\Scripts\activate.bat"

REM Check if dependencies are installed
if not exist "requirements.txt" (
    echo requirements.txt not found in project root
    pause
    exit /b 1
)

REM Install dependencies if needed
if not exist "venv\.dependencies-installed" (
    echo Installing dependencies...
    pip install -r requirements.txt
    echo. > "venv\.dependencies-installed"
)

REM Check if essential modules are available
python -c "import rich, supabase" >nul 2>&1
if errorlevel 1 (
    echo [!] Essential modules missing, reinstalling dependencies...
    pip install -r requirements.txt
    echo. > "venv\.dependencies-installed"
)

REM Run the main application
python "%SCRIPT_DIR%main.py" %*