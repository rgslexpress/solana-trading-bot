@echo off
rem Encerra o painel em execucao de forma organizada (para o paper trading; nao envia ordens; preserva posicoes simuladas).
cd /d "%~dp0.."
call .venv\Scripts\activate.bat
python -m app.dashboard --parar
