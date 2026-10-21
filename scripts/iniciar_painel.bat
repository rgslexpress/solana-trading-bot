@echo off
rem Abre o painel LOCAL (somente 127.0.0.1). NAO inicia o paper trading: use o botao "Iniciar" no painel.
cd /d "%~dp0.."
if not exist .venv\Scripts\activate.bat (
  echo Ambiente nao encontrado. Execute primeiro: scripts\instalar_windows.bat
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m app.dashboard --abrir
