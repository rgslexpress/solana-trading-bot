@echo off
rem Executa a suite de testes (pytest; se indisponivel, usa o runner de contingencia). Nenhum teste usa a internet nem fundos reais.
setlocal
set PYTHONUTF8=1
cd /d "%~dp0.."
if not exist .venv\Scripts\activate.bat (
  echo Ambiente nao encontrado. Execute primeiro: scripts\instalar_windows.bat
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
pip install -r requirements-dev.txt || echo AVISO: nao foi possivel instalar as dependencias de desenvolvimento; tentando o runner de contingencia.
python -m pytest --version >nul 2>nul
if errorlevel 1 (
  python scripts\run_tests_fallback.py
) else (
  python -m pytest -q
)
set RC=%ERRORLEVEL%
pause
exit /b %RC%
