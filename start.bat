@echo off
cd /d "%~dp0"
title English Reader
uv run python run.py
if errorlevel 1 pause
