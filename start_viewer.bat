@echo off
rem 雙擊開啟「Delft3D-FM 多分區水理成果展示圖台」(第一次使用會自動完成環境設定)
rem 環境裝好後以 pythonw 在背景執行，不顯示命令視窗；關掉圖台的瀏覽器分頁約 3 分鐘後自動結束。
rem 訊息記錄在 %LOCALAPPDATA%\dfm_viewer\viewer.log；需要看即時訊息時，請改在命令視窗執行 pixi run viewer
cd /d "%~dp0"
if not exist "%~dp0.pixi\envs\default\pythonw.exe" (
  call "%~dp0_setup_env.bat" || (pause & exit /b 1)
)
start "" "%~dp0.pixi\envs\default\pythonw.exe" "%~dp0src\dfm_viewer\main.py" %*
