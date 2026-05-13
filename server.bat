@echo off
echo [1/4] Fermeture de tout processus sur le port 5000...
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5000"') do (
    if "%%a" NEQ "0" (
        taskkill /F /PID %%a
    )
)

echo [2/4] Activation de l'environnement virtuel...
if exist ".\.venv\Scripts\activate.bat" (
    call .\.venv\Scripts\activate.bat
) else (
    echo [INFO] .venv introuvable, utilisation de Python global.
)

echo [3/4] Verification des dependances...
python -m pip install -r requirements.txt

echo [4/4] Lancement du serveur Flask-SocketIO...
set FLASK_ENV=production
set FLASK_DEBUG=0
python -u app.py
pause
