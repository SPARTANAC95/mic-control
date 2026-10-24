@echo off
rem Starts Mic Control without a console window. The app closes itself when you close its window.
start "" pythonw "%~dp0server.py"
