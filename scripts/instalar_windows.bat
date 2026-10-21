@echo off
rem Instala o ambiente (uma vez). Nao usa nem pede chaves, carteiras ou fundos. LIVE continua bloqueado.
setlocal
set PYTHONUTF8=1
cd /d "%~dp0.."
echo Pasta do projeto: %CD%
echo %CD% | findstr /i "OneDrive" >nul && echo AVISO: pasta dentro do OneDrive. Prefira um caminho como C:\dev\solana-trading-bot (o OneDrive pode corromper o SQLite).

rem Prefere Python 3.12, depois 3.13 (via launcher "py"). NAO usa 3.14: as versoes fixadas (numpy/pandas) ainda nao tem pacotes para ele.
set PY=
py -3.12 -c "import sys" >nul 2>nul && set PY=py -3.12
if not defined PY (
  py -3.13 -c "import sys" >nul 2>nul && set PY=py -3.13
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<(3,14) else 1)" >nul 2>nul && set PY=python
)
if not defined PY goto sempython
%PY% -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<(3,14) else 1)" || goto versao
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
echo Python 3.12 ou 3.13 nao encontrado (o 3.14 ainda nao e suportado pelas versoes fixadas). Instale o 3.12: winget install Python.Python.3.12 ou python.org, marcando "Add python.exe to PATH".
pause
exit /b 1
:versao
echo E necessario Python entre 3.10 e 3.13 (recomendado 3.12). O 3.14 nao e suportado ainda.
pause
exit /b 1
:erro
echo FALHA na instalacao. Veja a mensagem acima (rede/proxy do pip? versao do Python?).
pause
exit /b 1
