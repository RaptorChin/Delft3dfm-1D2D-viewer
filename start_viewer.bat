@echo off
rem 雙擊開啟「Delft3D-FM 多分區水理成果展示圖台」(第一次使用會自動完成環境設定)
rem 環境裝好後以 pythonw 在背景執行圖台；此命令視窗只顯示啟動進度，等瀏覽器開啟後自動關閉，
rem 啟動失敗時會停在畫面上顯示原因。關掉圖台的瀏覽器分頁約 3 分鐘後圖台自動結束。
rem 訊息記錄在 %LOCALAPPDATA%\dfm_viewer\viewer.log；需要看即時訊息時，請改在命令視窗執行 pixi run viewer
cd /d "%~dp0"
if not exist "%~dp0.pixi\envs\default\pythonw.exe" (
  call "%~dp0_setup_env.bat" || (pause & exit /b 1)
)

rem 圖台開好瀏覽器後會寫入這個通知檔 (內容 ok 或 error)
set "DFM_READY_FILE=%TEMP%\dfm_viewer_ready_%RANDOM%%RANDOM%.txt"
if exist "%DFM_READY_FILE%" del "%DFM_READY_FILE%" >nul 2>&1
start "" "%~dp0.pixi\envs\default\pythonw.exe" "%~dp0src\dfm_viewer\main.py" %*

echo ============================================================
echo  Delft3D-FM 圖台啟動中，請稍候...
echo  (第一次執行可能需要 1～2 分鐘；瀏覽器開啟後此視窗會自動關閉)
echo ============================================================
set /a WAITED=0

:wait
if exist "%DFM_READY_FILE%" goto :done
rem 等 1 秒 (不用 timeout：有裝 Git 的電腦上可能叫到同名的其他程式而失效)
"%SystemRoot%\System32\ping.exe" -n 2 127.0.0.1 >nul
set /a WAITED+=1
set /a TICK=WAITED %% 15
if %TICK%==0 echo    已等候 %WAITED% 秒...
if %WAITED%==600 (
  echo.
  echo  已等候超過 10 分鐘仍未開啟。若有指定資料夾，可能正在轉檔，請再等一下；
  echo  也可以直接關閉此視窗，不影響圖台。記錄檔：%LOCALAPPDATA%\dfm_viewer\viewer.log
)
goto :wait

:done
set "STATUS="
set /p STATUS=<"%DFM_READY_FILE%"
del "%DFM_READY_FILE%" >nul 2>&1
if /i "%STATUS:~0,2%"=="ok" exit /b 0
echo.
echo [失敗] 圖台沒有成功啟動。
echo        詳細訊息請見記錄檔：%LOCALAPPDATA%\dfm_viewer\viewer.log
echo        請把該檔案或錯誤訊息截圖給負責人。
pause
exit /b 1
