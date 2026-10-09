@echo off
cd /d "%~dp0"
title memescope collector - leave open
where py >nul 2>nul && (py collect.py) || (python collect.py)
pause
