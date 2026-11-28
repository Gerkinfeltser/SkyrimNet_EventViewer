@echo off
rem Double-click: builds the in-game view for Magelight UI as a mod in MO2 (the install whose SkyrimNet you played last).
rem Run it again after updating the viewer. Play with sn-eventview.bat running and press F7 in game.
cd /d "%~dp0"
title SkyrimNet event view: Magelight mod
python serve.py --magelight %*
pause
