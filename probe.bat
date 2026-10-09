@echo off
cd /d "%~dp0"
title memescope probe
where py >nul 2>nul && (py probe.py) || (python probe.py)
pause
