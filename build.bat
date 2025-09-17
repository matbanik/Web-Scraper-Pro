@echo off
REM ============================================================================
REM  PyInstaller Build Script for Web Scraper Pro (Tkinter version)
REM  Version 2.3 - Added --collect-data for justext to fix crash.
REM ============================================================================

echo [INFO] Starting the build process for Web Scraper Pro...

REM --- Check if Python is available ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not found in your system's PATH.
    echo Please install Python 3.8+ and ensure it's added to the PATH.
    pause
    exit /b 1
)

REM --- Setup Virtual Environment ---
echo [INFO] Creating a clean virtual environment in '.\venv\'...
if not exist venv (
    python -m venv venv
)

echo [INFO] Activating the virtual environment...
call .\venv\Scripts\activate.bat

REM --- Install Dependencies ---
echo [INFO] Installing all required Python libraries...
pip install scrapy beautifulsoup4 reportlab browser-cookie3 pyinstaller lxml cssselect parsel w3lib tldextract typing-extensions trafilatura dateparser htmldate justext

if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python dependencies. Please check your internet connection.
    pause
    exit /b 1
)

REM --- Clean previous builds ---
del /Q /F dist\webscraper.exe
rmdir /S /Q build
rmdir /S /Q dist

echo [INFO] Running PyInstaller to build the executable...
REM MODIFIED: Added --collect-data for justext to fix the FileNotFoundError crash.
pyinstaller --noconfirm --clean --onefile --windowed --name webscraper ^
  --add-data "venv\Lib\site-packages\lxml-*.dist-info;." ^
  --add-data "venv\Lib\site-packages\cssselect-*.dist-info;." ^
  --add-data "venv\Lib\site-packages\parsel-*.dist-info;." ^
  --add-data "venv\Lib\site-packages\w3lib-*.dist-info;." ^
  --add-data "venv\Lib\site-packages\Twisted-*.dist-info;." ^
  --collect-data trafilatura ^
  --collect-data dateparser ^
  --collect-data htmldate ^
  --collect-data justext ^
  --collect-data tldextract ^
  scraper_app.py

if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller failed to build the executable.
    pause
    exit /b 1
)

echo.
echo ============================================================================
echo  BUILD SUCCESSFUL!
echo ============================================================================
echo Your standalone executable can be found in the 'dist' folder:
echo.
echo     your_project_folder\dist\webscraper.exe
echo.
echo ============================================================================
echo.
call .\venv\Scripts\deactivate.bat
pause

