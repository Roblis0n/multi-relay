@echo off
setlocal EnableExtensions DisableDelayedExpansion

if defined MULTI_RELAY_MANAGER (
  set "MANAGER=%MULTI_RELAY_MANAGER%"
) else if defined DEEPSEEK_MANAGER (
  set "MANAGER=%DEEPSEEK_MANAGER%"
) else (
  set "MANAGER=%~dp0scripts\multi_relay.py"
)

if not exist "%MANAGER%" (
  echo Multi Relay CLI was not found: "%MANAGER%" 1>&2
  exit /b 3
)

set "PYTHON_EXE=%MULTI_RELAY_PYTHON%"
if not defined PYTHON_EXE if defined DEEPSEEK_PYTHON set "PYTHON_EXE=%DEEPSEEK_PYTHON%"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE (
  echo Python 3.11 or newer is required. 1>&2
  exit /b 4
)

if not defined MULTI_RELAY_NO_PAUSE if defined DEEPSEEK_NO_PAUSE set "MULTI_RELAY_NO_PAUSE=%DEEPSEEK_NO_PAUSE%"

if "%~1"=="" goto default_setup
"%PYTHON_EXE%" "%MANAGER%" %*
exit /b %ERRORLEVEL%

:default_setup
if defined CODEX_HOME set "TARGET_CODEX_HOME=%CODEX_HOME%"
if not defined TARGET_CODEX_HOME set "TARGET_CODEX_HOME=%USERPROFILE%\.codex"
"%PYTHON_EXE%" "%MANAGER%" setup --codex-home "%TARGET_CODEX_HOME%"
exit /b %ERRORLEVEL%
