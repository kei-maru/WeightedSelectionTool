@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Please create the virtual environment and install requirements first.
  exit /b 1
)

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name WeightedSelectionTool ^
  --add-data "static;static" ^
  main.py

echo.
echo Built: dist\WeightedSelectionTool.exe
