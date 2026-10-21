@echo off
rem Instala o ambiente (uma vez). Nao usa nem pede chaves, carteiras ou fundos. LIVE continua bloqueado.
setlocal
set PYTHONUTF8=1
cd /d "%~dp0.."
echo Pasta do projeto: %CD%
echo %CD% | findstr /i "OneDrive" >nul && echo AVISO: pasta dentro do OneDrive. Prefira um caminho como C:\dev\solana-trading-bot (o OneDrive pode corromper o SQLite).

rem Prefere Python 3.12 via launcher "py"; senao usa "python" do PATH.
set PY=
py -3.12 -c "import sys" >nul 2>nul && set PY=py -3.12
if not defined PY (
  python -c "import sys" >nul 2>nul && set PY=python
)
if not defined PY goto sempython
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" || goto versao
%PY% --version

if not exist .venv\Scripts\python.exe (
  %PY% -m venv .venv || goto erro
)
call .venv\Scripts\activate.bat || goto erro
python -m pip install --upgrade pip || goto erro
pip install -r requirements.txt || goto erro
if not exist .env copy .env.example .env >nul
if not exist config\tokens.json copy config\tokens.example.json config\tokens.json >nul
python scripts\check_environment.py
echo.
echo Instalacao concluida. Revise .env e config\tokens.json (confira os mints).
echo Para abrir o painel: scripts\iniciar_painel.bat
pause
exit /b 0

:sempython
echo Python nao encontrado. Instale o Python 3.12 (python.org ou: winget install Python.Python.3.12) e marque "Add python.exe to PATH".
pause
exit /b 1
:versao
echo Python 3.10 ou superior e necessario (recomendado 3.12).
pause
exit /b 1
:erro
echo FALHA na instalacao. Veja a mensagem acima (rede/proxy do pip? versao do Python?).
pause
exit /b 1
