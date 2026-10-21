@echo off
rem Abre o painel LOCAL (somente 127.0.0.1). NAO inicia o paper trading: use o botao "Iniciar" no painel.
rem Para encerrar com seguranca use Ctrl+C nesta janela ou scripts\parar_painel.bat (nao feche a janela no X).
setlocal
set PYTHONUTF8=1
cd /d "%~dp0.."
if not exist .venv\Scripts\activate.bat (
  echo Ambiente nao encontrado. Execute primeiro: scripts\instalar_windows.bat
  pause
  exit /b 1
)
if not exist config\tokens.json copy config\tokens.example.json config\tokens.json >nul
call .venv\Scripts\activate.bat
python -m app.dashboard --abrir
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo O painel terminou com codigo %RC%. Codigo 3 = outra instancia em execucao ou porta ocupada; 2 = configuracao invalida.
  pause
)
exit /b %RC%
