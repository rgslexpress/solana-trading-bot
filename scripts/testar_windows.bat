@echo off
rem Executa a suite de testes (pytest, se instalado; senao o runner de contingencia).
cd /d "%~dp0.."
call .venv\Scripts\activate.bat
pip install -r requirements-dev.txt
pytest
if errorlevel 1 python scripts\run_tests_fallback.py
