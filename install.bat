@echo off
setlocal

echo ========================================
echo   AlphaSolve Installer
echo ========================================
echo.

where uv >nul 2>nul
if %errorlevel%==0 (
    echo uv already installed, skipping...
    goto :install_alphasolve
)

echo Step 1/2: Installing uv (Python package manager)...
echo.
powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to install uv. Please try:
    echo   pip install uv
    echo Then re-run this script.
    pause
    exit /b 1
)

:: uv installs to ~/.cargo/bin; add it to PATH for this session
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"

:install_alphasolve
echo.
echo Step 2/2: Installing AlphaSolve...
echo.
uv tool install -e .
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Installation failed.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Installation complete!
echo ========================================
echo.
echo You can now open any folder, create a problem.md,
echo right-click - "Open in Terminal", and run:
echo   alphasolve
echo.
pause
