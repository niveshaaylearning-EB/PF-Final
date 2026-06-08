@echo off
title NIA Performance Center
cd /D "%~dp0"
backend\venv\Scripts\python.exe run.py
pause
