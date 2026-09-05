@echo off
cd /d "%~dp0"
title English Reader (dev)
set ER_DEV_MODE=true
set ER_LOG_LEVEL=DEBUG
uv run python run.py --dev
if errorlevel 1 pause
