@echo off
rem Double-click: starts the event view helper and opens it in the browser. Close this window to stop it.
cd /d "%~dp0"
title SkyrimNet event view
python serve.py %*
pause
