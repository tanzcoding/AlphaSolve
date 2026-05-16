@echo off
setlocal

set "REPO_URL=https://github.com/tanzcoding/AlphaSolve/archive/refs/heads/main.zip"
set "REPO_DIR=AlphaSolve-main"

echo ========================================
echo   AlphaSolve Installer
echo ========================================
echo.

:: -- 1. Install uv -------------------------------------------------
where uv >nul 2>nul
if %errorlevel%==0 (
    echo [1/3] uv already installed, skipping...
    goto :download
)

echo [1/3] Installing uv...
echo.
powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install uv.
    pause
    exit /b 1
)
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"

:: -- 2. Download AlphaSolve ----------------------------------------
:download
echo.
echo [2/3] Downloading AlphaSolve...
echo.
if exist "%REPO_DIR%" rmdir /s /q "%REPO_DIR%"
curl -fsSL %REPO_URL% -o alpha.zip
if %errorlevel% neq 0 (
    echo [ERROR] Download failed. Check your internet connection.
    pause
    exit /b 1
)
tar -xf alpha.zip
del alpha.zip

:: -- 3. Install ----------------------------------------------------
echo.
echo [3/3] Installing AlphaSolve...
echo.
cd /d "%REPO_DIR%"
uv tool install .
if %errorlevel% neq 0 (
    echo [ERROR] Installation failed.
    pause
    exit /b 1
)
cd ..
rmdir /s /q "%REPO_DIR%"

echo.
echo ========================================
echo   Installation complete!
echo ========================================
echo.
echo To get started:
echo   1. Create a folder anywhere, put a problem.md in it
echo   2. Right-click the folder - "Open in Terminal"
echo   3. Run: alphasolve
echo.
pause
