@echo off
cd /d %~dp0

python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt

if not exist .env (
    copy .env.example .env
    echo.
    echo ==^> Fichier .env cree. Ouvre-le et colle ta cle OpenRouter avant de lancer run.bat
)

echo.
echo Installation terminee. Lance run.bat pour demarrer le serveur.
pause
