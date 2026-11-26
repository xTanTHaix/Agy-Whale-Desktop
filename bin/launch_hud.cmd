@echo off
title Whale Desktop HUD
set "VENV_PY=%~dp0..\.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
  "%VENV_PY%" "%~dp0whale_hud.py"
) else (
  python "%~dp0whale_hud.py"
)
