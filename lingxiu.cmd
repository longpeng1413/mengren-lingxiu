@echo off
rem Mengren Lingxiu command line for AI agents (Claude Code, Codex). Usage: see the docs folder.
"%~dp0.venv\Scripts\python.exe" "%~dp0app\cli.py" %*
exit /b %errorlevel%
