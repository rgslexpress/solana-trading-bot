@echo off
rem Instala o ambiente (uma vez). Nao usa nem pede chaves, carteiras ou fundos.
cd /d "%~dp0.."
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  %PY% -m venv .venv || goto erro
)
call .venv\Scripts\activate.bat || goto erro
python -m pip install --upgrade pip || goto erro
pip install -r requirements.txt || goto erro
if not exist .env copy .env.example .env
if not exist config\tokens.json copy config\tokens.example.json config\tokens.json
echo.
echo Instalacao concluida. Revise o arquivo .env e config\tokens.json.
echo Para abrir o painel: scripts\iniciar_painel.bat
exit /b 0
:erro
echo FALHA na instalacao. Verifique se o Python 3.10+ esta instalado e no PATH.
exit /b 1
