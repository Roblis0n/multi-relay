@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Configure Codex DeepSeek subagents

if defined DEEPSEEK_MANAGER (
  set "MANAGER=%DEEPSEEK_MANAGER%"
) else (
  set "MANAGER=%~dp0codex-deepseek-subagent\scripts\codex_deepseek.py"
)

if not exist "%MANAGER%" (
  echo DeepSeek setup program was not found:
  echo %MANAGER%
  set "SETUP_EXIT=3"
  goto finish
)

set "PYTHON_EXE="
if defined DEEPSEEK_PYTHON set "PYTHON_EXE=%DEEPSEEK_PYTHON%"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"

if not defined PYTHON_EXE (
  echo A working Python was not found. Install Python 3.11 or newer.
  set "SETUP_EXIT=4"
  goto finish
)

"%PYTHON_EXE%" --version >nul 2>&1
if errorlevel 1 (
  echo The selected Python cannot run: %PYTHON_EXE%
  set "SETUP_EXIT=4"
  goto finish
)

if defined CODEX_HOME (
  set "TARGET_CODEX_HOME=%CODEX_HOME%"
) else (
  set "TARGET_CODEX_HOME=%USERPROFILE%\.codex"
)

echo PASTE YOUR DEEPSEEK API KEY ON THE NEXT LINE.
echo Nothing will appear while typing. Paste the key, then press Enter.
echo.
"%PYTHON_EXE%" "%MANAGER%" setup --codex-home "%TARGET_CODEX_HOME%"
set "SETUP_EXIT=%ERRORLEVEL%"

:finish
echo.
if "%SETUP_EXIT%"=="0" (
  echo Setup and verification completed. Restart Codex before use.
) else (
  echo Setup did not complete. Keep the error message shown above.
)
if not defined DEEPSEEK_NO_PAUSE pause
exit /b %SETUP_EXIT%
