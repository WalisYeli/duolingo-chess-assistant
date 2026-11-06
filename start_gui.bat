@echo off
cd /d "%~dp0"
rem 优先用 Python 官方安装器带的 pyw 启动器 (无控制台窗口), 找不到再退回 pythonw / python
where pyw >nul 2>nul && (start "" pyw duolingo_gui.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw duolingo_gui.py & exit /b)
if exist "%USERPROFILE%\Python\pythonw.exe" (start "" "%USERPROFILE%\Python\pythonw.exe" duolingo_gui.py & exit /b)
python duolingo_gui.py
pause
