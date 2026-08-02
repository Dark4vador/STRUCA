@echo off
cd /d %~dp0
call venv\Scripts\activate.bat

if "%~1"=="" (
    echo Usage: glisse un PDF sur ce fichier, ou lance:
    echo   classify.bat "chemin\vers\document.pdf"
    pause
    exit /b 1
)

python classify_only.py %1
pause
