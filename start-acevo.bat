@echo off
rem Lanza el dashboard de AC EVO. Reusa el venv del dash de AMS2 (tiene websockets).
rem Abri AC EVO y entra a una sesion antes de arrancar (o el bridge reintenta solo).
cd /d "%~dp0"
"C:\Users\gians\sim\ams2-dash\.venv\Scripts\python.exe" acevo_bridge.py
pause
