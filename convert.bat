@echo off
rem 雙擊執行：把 Delft3D-FM 成果 (*_map.nc / *_fou.nc) 轉成 Zarr 與 GeoTIFF (第一次使用會自動完成環境設定)
cd /d "%~dp0"
call "%~dp0_setup_env.bat" || (pause & exit /b 1)

echo 提示：可以直接把資料夾「拖曳」到這個視窗，再按 Enter。
echo.
set "IN="
set /p IN=Delft3D-FM 成果資料夾 (含 XXX_0000_map.nc / XXX_0000_fou.nc)：
set "IN=%IN:"=%"
if "%IN%"=="" (
  echo 未輸入資料夾。
  pause
  exit /b 1
)

set "OUT="
set /p OUT=輸出資料夾 (直接按 Enter = "%IN%_ZARR")：
set "OUT=%OUT:"=%"
if "%OUT%"=="" set "OUT=%IN%_ZARR"

"%PIXI%" run convert -i "%IN%" -o "%OUT%"
echo.
echo 輸出資料夾：%OUT%
echo 開圖台後在「資料夾路徑」選擇上面這個資料夾即可。
pause
