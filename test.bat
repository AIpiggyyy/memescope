@echo off
cd /d "%~dp0"
title memescope self-test
where py >nul 2>nul && (py tests\test_offline.py & py tests\test_trace.py) || (python tests\test_offline.py & python tests\test_trace.py)
pause
