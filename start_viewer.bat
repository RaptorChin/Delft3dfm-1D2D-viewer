@echo off
rem 雙擊開啟「Delft3D-FM 多分區水理成果展示圖台」(第一次使用會自動完成環境設定)
cd /d "%~dp0"
call "%~dp0_setup_env.bat" || (pause & exit /b 1)
"%PIXI%" run viewer %*
pause
