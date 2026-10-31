@echo off
rem dsidbg command line for Windows (see `dsidbg -h`). Needs only Python 3.
set "PYTHONPATH=%~dp0..\host;%PYTHONPATH%"
python -m dsidbg %*
