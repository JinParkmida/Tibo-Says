@echo off
rem Builds a single "Tibo says.exe" (no console window). Needs Python from python.org.
py -m pip install --upgrade pyinstaller
py -m PyInstaller --onefile --noconsole --name "Tibo says" tibo_says.pyw
echo.
echo Done. Your app is at: dist\Tibo says.exe
pause
