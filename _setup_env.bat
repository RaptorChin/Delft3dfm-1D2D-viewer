@echo off
rem ============================================================================
rem  由 start_viewer.bat / convert.bat 呼叫，第一次使用時自動完成環境設定：
rem    1. 找不到 Pixi → 詢問後自動安裝 (官方安裝程式，裝在 %USERPROFILE%\.pixi)
rem    2. 尚未建立執行環境 → 自動執行 pixi install (依 pixi.lock 安裝相同版本的套件)
rem  成功時設定變數 PIXI (pixi.exe 的完整路徑) 並回傳 0；失敗回傳 1。
rem ============================================================================

set "PIXI="
for %%P in (pixi.exe) do if not "%%~$PATH:P"=="" set "PIXI=%%~$PATH:P"
if not defined PIXI if exist "%USERPROFILE%\.pixi\bin\pixi.exe" set "PIXI=%USERPROFILE%\.pixi\bin\pixi.exe"
if defined PIXI goto :env

echo ============================================================
echo  第一次使用：需要先安裝 Pixi
echo  (管理 Python 執行環境的工具，約 80 MB，每台電腦只需安裝一次，
echo   會裝在 %USERPROFILE%\.pixi，不影響電腦上其他程式)
echo ============================================================
choice /c YN /m "要現在自動安裝嗎"
if errorlevel 2 (
  echo 已取消。也可以手動安裝，步驟請見 README.md。
  exit /b 1
)
echo 正在下載並安裝 Pixi...
powershell -NoProfile -Command "irm https://pixi.sh/install.ps1 | iex"
if not exist "%USERPROFILE%\.pixi\bin\pixi.exe" (
  echo.
  echo [失敗] Pixi 安裝沒有成功。可能原因：無法連上網路，或公司網路擋住下載。
  echo        請把上面的錯誤訊息截圖給負責人。
  exit /b 1
)
set "PIXI=%USERPROFILE%\.pixi\bin\pixi.exe"
echo Pixi 安裝完成。

:env
if exist "%~dp0.pixi\envs\default\python.exe" exit /b 0

echo.
echo ============================================================
echo  第一次使用：正在建立執行環境
echo  (下載 Python、GDAL 等套件，約需 3～10 分鐘，請勿關閉視窗)
echo ============================================================
pushd "%~dp0"
"%PIXI%" install
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
  echo.
  echo [失敗] 執行環境建立沒有成功。可能原因：無法連上網路，或磁碟空間不足（需約 1.5 GB）。
  echo        請把上面的錯誤訊息截圖給負責人。
  exit /b 1
)
echo 執行環境建立完成。
echo.
echo 正在進行第一次啟動準備（載入套件、防毒軟體掃描新檔案，約 1～2 分鐘，請勿關閉視窗）...
pushd "%~dp0"
"%PIXI%" run viewer --warmup
popd
echo 之後開啟就不用再等。
echo.
exit /b 0
