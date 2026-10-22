# Delft3D-FM 1D2D 多分區水理成果展示圖台與轉檔系統

把 Delft3D-FM 平行計算（多分區）的模式成果 `*_map.nc`／`*_fou.nc`：

1. **轉檔**成 Zarr（逐時水深）與 GeoTIFF（最大淹水深度，可直接用 QGIS 開啟）
2. 在**網頁圖台**上疊加臺灣通用電子地圖／正射影像，播放淹水過程、點選任一位置查看水深歷線
3. **依行政區出圖**：鎖定新北市／基隆市的鄉鎮市區（區外淹水不顯示），把目前畫面匯出成含標題、圖例、比例尺、指北針的 PNG；可切換逐時或「最大值」

---

## 🚀 快速開始（Windows）

### 開啟圖台：雙擊 `start_viewer.bat`
- **第一次使用**會自動完成設定，不用打任何指令：
  1. 詢問是否安裝 Pixi（管理 Python 環境的工具）→ 按 **Y**
  2. 自動下載並建立執行環境（約 3～10 分鐘，需連網）
- 之後瀏覽器會自動開啟 `http://127.0.0.1:8088/`。圖台在背景執行，不會留下黑色視窗；關掉圖台的瀏覽器分頁約 3 分鐘後自動結束。
- 在左側按「📂 點選瀏覽資料夾」，**直接選 Delft3D-FM 成果資料夾或模型專案資料夾**（會自動往下層找 map.nc / fou.nc）。第一次開啟會自動轉檔並顯示進度，結果存在 .nc 所在成果資料夾底下的 `ZARR`，下次就不用再等。

### 只要轉檔：雙擊 `convert.bat`
依提示把「模式成果資料夾」拖進視窗後按 Enter。會產出逐時水深 Zarr 與最大淹水深度 GeoTIFF（可用 QGIS 開啟）。

<details>
<summary>指令方式（進階）</summary>

```powershell
cd <本專案資料夾>
pixi run convert -i "D:\模型"          # .nc 在 D:\模型\output → 結果放在 D:\模型\output\ZARR
pixi run viewer --data "D:\模型\output"
```
</details>

> 第一次設定需要連上網路（下載 Pixi 與套件）。公司網路若擋住下載，請改用離線安裝方式（洽負責人）。

---

> 成果檔名不需要是 `XXX_0000_fou.nc` 的預設格式：程式會**依檔案內容**判斷 map／fou 與分區編號，資料夾與檔名也可以含中文。

## 📦 轉檔產出

| 檔案 | 內容 |
|---|---|
| `XXX_000N_map.zarr` | 各分區逐時水深（圖台使用） |
| `XXX_000N_fou_max_depth_cog.tif` | 各分區最大淹水深度 GeoTIFF |
| `XXX_max_depth_merged_cog.tif` | **全區合併**最大淹水深度 GeoTIFF |
| `mesh1d.zarr`、`mesh1d_network.geojson` | 1D 渠道／管線的逐時數值與線形（GeoJSON 可用 QGIS 開啟） |
| `his.zarr`、`his_objects.geojson` | 觀測站、抽水站、閘門、堰、橋梁等的時序與位置（含檢核摘要） |
| `partitions_metadata.json` | 坐標系、解析度與檔案清單 |

## ⚠️ 已知限制
- **結構物位置**：優先依模型 `structures.ini` 的「管段＋里程」計算設計位置（自動由成果資料夾上一層的 `.mdu` 找到）。找不到時改用 his.nc 的座標，那是模式把結構物吸附到 1D 計算連線後的位置，**可能與設計位置差數十公尺**，彈出視窗會註明。成果資料夾若被單獨複製出來，請把該次模擬的 `structures.ini` 一起放進成果資料夾。
- 非方形網格（三角形）在柵格化時會有少量誤差：網格加密交界處的小三角形，其數值可能被相鄰網格取代。
- 「📂 點選瀏覽資料夾」只能在執行圖台的那台電腦上使用。

## 📖 詳細說明
請參閱 [docs/USER_MANUAL.md](docs/USER_MANUAL.md)。

## 📄 授權條款
本專案採用 [MIT License](LICENSE) 授權。
