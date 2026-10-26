@echo off
rem Start the RAY video poker emulator (Windows).
rem The first start creates a Python environment in .venv and installs the
rem needed packages. machine68k is compiled from source, which needs the
rem "Microsoft C++ Build Tools" (Desktop development with C++).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First start: setting up Python packages in .venv ...
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Package install failed. Install Microsoft C++ Build Tools and try again.
        rmdir /s /q .venv
        pause
        exit /b 1
    )
)
.venv\Scripts\python -m pyray %*
