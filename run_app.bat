@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title Movie Collection App - Run

REM =========================================================
REM 1) PATHS
REM =========================================================
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

set "VENV_DIR=.venv"
set "PY_EXE=python"

REM =========================================================
REM 2) BASIC CHECKS + SHOW WHICH main.py WILL RUN
REM =========================================================
if not exist "main.py" (
  echo [ERROR] Δεν βρεθηκε το main.py στον φακελο: "%APP_DIR%"
  pause
  exit /b 1
)

echo =========================================================
echo [INFO] APP_DIR  : "%APP_DIR%"
echo [INFO] main.py  : "%APP_DIR%main.py"
echo [INFO] main.py info:
dir /-c "%APP_DIR%main.py"
echo.

echo [INFO] Detected version in main.py (findstr):
findstr /i /n /c:"v1.0.0-alpha" "%APP_DIR%main.py"
echo =========================================================
echo.

REM =========================================================
REM 3) LOAD .env (TMDB_API_KEY / TMDB_LANG)
REM =========================================================
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "k=%%A"
    set "v=%%B"

    for /f "tokens=* delims= " %%K in ("!k!") do set "k=%%K"
    for /f "tokens=* delims= " %%V in ("!v!") do set "v=%%V"

    if not "!k!"=="" if /i not "!k:~0,1!"=="#" if /i not "!k:~0,3!"=="REM" (
      set "!k!=!v!"
    )
  )
)

if not defined TMDB_LANG set "TMDB_LANG=en-US"

if not defined TMDB_API_KEY (
  echo [ERROR] Δεν βρεθηκε TMDB_API_KEY.
  echo Φτιαξε/ελεγξε το .env με γραμμη:
  echo TMDB_API_KEY=ΤΟ_ΚΛΕΙΔΙ_ΣΟΥ
  pause
  exit /b 1
)

REM =========================================================
REM 4) VENV CREATE / ACTIVATE
REM =========================================================
if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [INFO] Δημιουργω virtual environment...
  %PY_EXE% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Αποτυχια δημιουργιας venv.
    pause
    exit /b 1
  )
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERROR] Δεν μπορεσα να ενεργοποιησω το venv.
  pause
  exit /b 1
)

REM =========================================================
REM 5) INSTALL DEPENDENCIES (αν λειπουν)
REM =========================================================
echo [INFO] Ελεγχος πακετων...
python -c "import PySide6, requests" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Εγκαθιστω dependencies...
  python -m pip install --upgrade pip
  python -m pip install PySide6 requests
  if errorlevel 1 (
    echo [ERROR] Αποτυχια εγκαταστασης πακετων.
    pause
    exit /b 1
  )
)

REM =========================================================
REM 6) RUN APP (capture error to file)
REM =========================================================
echo [INFO] TMDB_LANG=%TMDB_LANG%
echo [INFO] Τρεχω την εφαρμογη...
echo.

python "%APP_DIR%main.py" 1> "%APP_DIR%run_stdout.txt" 2> "%APP_DIR%run_error.txt"
set "APP_ERR=%errorlevel%"

if not "%APP_ERR%"=="0" (
  echo.
  echo [ERROR] Η εφαρμογη τερματισε με σφαλμα (errorlevel %APP_ERR%).
  echo [INFO] Δες τα αρχεια:
  echo        "%APP_DIR%run_error.txt"
  echo        "%APP_DIR%run_stdout.txt"
  echo.
  pause
  exit /b %APP_ERR%
)

echo [INFO] Η εφαρμογη ετρεξε επιτυχως.
endlocal
