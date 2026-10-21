@echo off
rem Encerra o painel em execucao de forma organizada (para o paper trading; nao envia ordens; preserva posicoes simuladas).
setlocal
set PYTHONUTF8=1
cd /d "%~dp0.."
if not exist .venv\Scripts\activate.bat (
  echo Ambiente nao encontrado.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m app.dashboard --parar
pause
