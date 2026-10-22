"""
Delft3D-FM 模式輸出 NetCDF (map.nc / fou.nc) 自動轉 Zarr 與 COG GeoTIFF 工具
Delft3D-FM Output Converter: Map/Fou NetCDF to Zarr & Cloud Optimized GeoTIFF (COG)

功能說明:
1. 掃描指定目錄下所有 NetCDF，依「檔案內容」判斷 map / fou 與分區編號 (0000, 0001, ...)，檔名可任意命名。
2. 以網格「面多邊形」(face polygon) 柵格化，支援三角形／四邊形混合、局部加密等非等間距網格，
   不會因網格大小與輸出解析度不同而出現破洞。
3. 自動排除平行計算的鄰區重疊格 (ghost cells，依 Mesh2d_flowelem_domain 判斷)。
4. 坐標系統自動讀取 NetCDF 內的 projected_coordinate_system / wgs84 (EPSG 代碼)。
5. 時序水深 (Mesh2d_waterdepth；若無則以 s1 - bl 推算) 分批寫入 Zarr，不會一次載入全部時間步。
6. 最大淹水深度 (優先取 fou.nc，否則取 map.nc 全時段最大值) 輸出 COG GeoTIFF：
   各分區一份，另輸出一份全區合併檔 (*_max_depth_merged_cog.tif)，可直接在 QGIS 開啟。

7. 1D 渠道／管線 (mesh1d) 依網路實際線形輸出向量線段 (mesh1d_network.geojson) 與逐時數值
   (mesh1d.zarr：水深、水位、地面溢淹水深、流量)，供圖台疊加顯示。
"""

import argparse
import json
import os
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# 必須在 import rasterio 之前：避免載入 PATH 上其他軟體 (QGIS、OSGeo4W…) 的 GDAL
try:
    from . import _winenv  # noqa: F401  (以套件方式 import 時)
except ImportError:
    import _winenv  # noqa: F401  (直接以腳本執行 convert.py 時)

import netCDF4
import numpy as np
import xarray as xr
from xarray.backends import NetCDF4DataStore
from xarray.backends.file_manager import DummyFileManager
from rasterio import features
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy
from rasterio.transform import Affine

# zarr 3 對 consolidated metadata 的格式提醒，不影響結果 (圖台可正常讀取)，避免嚇到使用者
warnings.filterwarnings("ignore", message="Consolidated metadata", category=UserWarning)

NODATA = -9999.0
OUTPUT_FORMAT_VERSION = 5  # 轉檔產物格式；圖台遇到較舊版本會自動重轉 (v2 起含 1D 網路，v3 起含觀測站／結構物，v4 結構物改用設計位置，v5 解析度改看全部分區)
TIME_CHUNK = 8  # 每批讀寫的時間步數 (控制記憶體用量)


# ───────────────────────────── 開檔 (支援中文路徑) ─────────────────────────────
# Windows 版 netCDF-C 以系統 ANSI 編碼 (繁中 Windows 為 cp950) 解讀路徑，Python 預設傳 UTF-8，
# 路徑含中文時會打不開 (OSError: Invalid argument)。改以 "mbcs" (= 系統 ANSI 編碼) 傳路徑。

def _needs_ansi_path(path: str) -> bool:
    return os.name == "nt" and not path.isascii()


class _AnsiPathDataset:
    """包住 netCDF4.Dataset，只把 filepath() 改用 ANSI 解碼 (xarray 會呼叫它)，其餘照轉。"""

    def __init__(self, nc):
        self._nc = nc

    def filepath(self, encoding=None):
        return self._nc.filepath(encoding="mbcs")

    def __getattr__(self, name):
        return getattr(self._nc, name)


def open_netcdf(path) -> "netCDF4.Dataset":
    p = str(path)
    return netCDF4.Dataset(p, encoding="mbcs") if _needs_ansi_path(p) else netCDF4.Dataset(p)


def open_nc(path) -> xr.Dataset:
    """以 xarray 開啟 NetCDF；路徑含中文時改走 ANSI 編碼。"""
    p = str(path)
    if not _needs_ansi_path(p):
        return xr.open_dataset(p)
    store = NetCDF4DataStore(DummyFileManager(_AnsiPathDataset(open_netcdf(p))))
    return xr.open_dataset(store)


# ───────────────────────────── 檔案掃描 ─────────────────────────────

def classify_nc(path: Path) -> Optional[Tuple[str, str]]:
    """
    打開 NetCDF 看內容 (不看檔名) 判斷檔案種類與分區編號，檔名可以任意命名。
      種類: 有 Mesh2d_fourier* 變數 → "fou"；有隨時間變化的 Mesh2d_waterdepth / Mesh2d_s1 → "map"；
            其他 (his、rst、雨量檔…) → None
      分區: Mesh2d_flowelem_domain 中出現最多的編號 (每個分區檔自己負責的網格佔絕大多數，
            其餘是鄰區重疊格)；沒有此變數或沒有明顯多數 (例如合併後的 map) → "single"
    """
    try:
        ds = open_netcdf(path)
    except Exception as e:  # 檔案損毀、模式仍在計算中被鎖住、路徑含系統編碼無法表示的字
        print(f"   ⚠️ 無法開啟 {path.name}，已略過: {e}")
        return None
    try:
        names = ds.variables.keys()
        if "Mesh2d_face_x" not in names:
            return None
        if any(n.startswith("Mesh2d_fourier") for n in names):
            kind = "fou"
        elif any(n in names and "time" in ds.variables[n].dimensions
                 for n in ("Mesh2d_waterdepth", "Mesh2d_s1")):
            kind = "map"
        else:
            return None

        part_id = "single"
        if "Mesh2d_flowelem_domain" in names:
            dom = np.asarray(ds.variables["Mesh2d_flowelem_domain"][:]).ravel()
            values, counts = np.unique(dom, return_counts=True)
            top = int(np.argmax(counts))
            if len(values) > 1 and counts[top] > 0.5 * dom.size:
                part_id = f"{int(values[top]):04d}"
        return kind, part_id
    finally:
        ds.close()


def detect_dfm_partitions(input_dir: str) -> Dict[str, Dict[str, str]]:
    """
    掃描目錄中的 Delft3D-FM 輸出檔案，判讀分區與對應的 map.nc / fou.nc。
    檔名不限 (依內容判斷，見 classify_nc)；檔名若含 _0003_ 之類的編號但與內容不符，會顯示提醒。
    先找資料夾本身；找不到才往子資料夾找。若同一分區找到多個同種檔案 (例如多次模擬放在一起)，
    會直接報錯，請使用者指定到正確的資料夾，避免不同模擬的成果被混在一起。

    回傳: {"0000": {"map": ".../X_0000_map.nc", "fou": ".../X_0000_fou.nc"}, ...}
    """
    p = Path(input_dir).resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"找不到指定的輸入資料夾: {input_dir}")

    def scan(files):
        # 檔名明顯是重啟檔／測站檔的先跳過 (只為了加速，其餘一律看內容)
        files = [f for f in files if not re.search(r"_(rst|his)\.nc$", f.name, re.IGNORECASE)]
        return [(f, c) for f in files if (c := classify_nc(f))]

    found = scan(sorted(p.glob("*.nc"))) or scan(sorted(p.rglob("*.nc")))

    partitions: Dict[str, Dict[str, str]] = {}
    for f, (kind, part_id) in found:
        m = re.search(r"_(\d{4})_", f.name)
        if m and part_id != "single" and m.group(1) != part_id:
            print(f"   ⚠️ {f.name}：檔名編號 {m.group(1)} 與檔案內容的分區 {part_id} 不同，以內容為準")
        slot = partitions.setdefault(part_id, {})
        if kind in slot:
            raise ValueError(
                f"分區 [{part_id}] 找到不只一個 {kind}.nc：\n  {slot[kind]}\n  {f}\n"
                f"請把 -i 指定到只含單一模擬成果的資料夾。"
            )
        slot[kind] = str(f)

    # 同時有分區檔與合併檔 (例如 X_merged_map.nc) 時，只用分區檔
    if "single" in partitions and len(partitions) > 1:
        print(f"   ℹ️ 同時找到分區檔與單一檔，忽略: {list(partitions['single'].values())}")
        partitions.pop("single")

    return dict(sorted(partitions.items()))


# his.nc 中可辨識的物件類別：(維度名稱, 中文名稱)。維度存在才處理，其餘類別 (例如涵洞) 也會自動加入
HIS_CATEGORIES = {
    "station": "觀測站", "pump": "抽水站", "orifice": "閘門", "weirgen": "堰", "weir": "堰",
    "general_structure": "通用結構物", "culvert": "涵洞", "bridge": "橋梁", "uniweir": "特殊堰",
    "dambreak": "潰堤", "compound": "複合結構物", "cross_section": "觀測斷面",
}


def _his_size(path: Path) -> int:
    """his.nc 的物件總數 (觀測站＋結構物＋斷面)；不是 his 檔回傳 -1。"""
    try:
        ds = open_netcdf(path)
    except Exception:
        return -1
    try:
        if "time" not in ds.dimensions:
            return -1
        n = sum(ds.dimensions[c].size for c in HIS_CATEGORIES
                if c in ds.dimensions and f"{c}_name" in ds.variables)
        return n if n > 0 else -1
    finally:
        ds.close()


def detect_his_file(input_dir: str, partitions: Dict[str, Dict[str, str]]) -> Optional[str]:
    """
    在 map/fou 所在資料夾找 his.nc (依內容判斷，檔名不限)。平行計算時通常只有 0000 分區輸出 his，
    若找到多個，取物件數最多者 (rank 0 會包含全部觀測站與結構物)。
    """
    used = {f for files in partitions.values() for f in files.values()}
    folders = {Path(f).parent for f in used} or {Path(input_dir).resolve()}
    candidates = []
    for folder in folders:
        for f in sorted(folder.glob("*.nc")):
            if str(f) in used or re.search(r"_rst\.nc$", f.name, re.IGNORECASE):
                continue
            n = _his_size(f)
            if n > 0:
                candidates.append((n, f))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    if len(candidates) > 1:
        print(f"   ℹ️ 找到 {len(candidates)} 個 his 檔，使用物件最多的 {candidates[0][1].name}")
    return str(candidates[0][1])


# ───────────────────────────── 網格讀取 ─────────────────────────────

@dataclass
class MeshFaces:
    """一個分區 (已排除 ghost cells) 的 2D 網格面資料"""
    keep: np.ndarray          # 此分區自己負責的面 (bool, 長度 = 原始面數)
    polygons: List[np.ndarray]  # 每個保留面的頂點 [(x, y), ...]
    cx: np.ndarray            # 保留面中心 x
    cy: np.ndarray            # 保留面中心 y
    crs: str


def detect_crs(ds: xr.Dataset, default: str = "EPSG:3826") -> str:
    """從 NetCDF 的坐標系統變數讀取 EPSG 代碼；讀不到時依坐標範圍推測。"""
    for name in ("projected_coordinate_system", "wgs84", "crs", "spatial_ref"):
        if name in ds.variables:
            attrs = ds[name].attrs
            code = attrs.get("epsg") or attrs.get("EPSG_code")
            if code is not None:
                code = str(code).strip()
                if code.upper().startswith("EPSG:"):
                    code = code.split(":", 1)[1]
                if code.isdigit() and int(code) > 0:
                    return f"EPSG:{int(code)}"
    fx = ds["Mesh2d_face_x"].values
    if np.nanmax(np.abs(fx)) <= 180.0:
        return "EPSG:4326"
    return default


def _face_vertices(ds: xr.Dataset) -> Tuple[np.ndarray, np.ndarray]:
    """回傳每個面的頂點坐標 (nFaces, nMax)，不存在的頂點為 NaN。"""
    if "Mesh2d_face_x_bnd" in ds and "Mesh2d_face_y_bnd" in ds:
        return ds["Mesh2d_face_x_bnd"].values.astype("f8"), ds["Mesh2d_face_y_bnd"].values.astype("f8")
    # 退而使用 face_nodes 對照節點坐標
    fn = ds["Mesh2d_face_nodes"]
    start = int(fn.attrs.get("start_index", 0))
    idx = fn.values
    valid = np.isfinite(idx)
    idx = np.where(valid, idx, start).astype(np.int64) - start
    nx_ = ds["Mesh2d_node_x"].values.astype("f8")[idx]
    ny_ = ds["Mesh2d_node_y"].values.astype("f8")[idx]
    nx_[~valid] = np.nan
    ny_[~valid] = np.nan
    return nx_, ny_


def read_mesh_faces(ds: xr.Dataset, part_id: str, crs_default: str) -> MeshFaces:
    fx = ds["Mesh2d_face_x"].values.astype("f8")
    fy = ds["Mesh2d_face_y"].values.astype("f8")

    keep = np.ones(fx.shape[0], dtype=bool)
    if part_id.isdigit() and "Mesh2d_flowelem_domain" in ds:
        dom = ds["Mesh2d_flowelem_domain"].values
        keep = dom == int(part_id)
        if not keep.any():  # 編號對不上時不排除，避免整個分區消失
            keep[:] = True

    vx, vy = _face_vertices(ds)
    polygons = []
    for x_row, y_row in zip(vx[keep], vy[keep]):
        ok = np.isfinite(x_row) & np.isfinite(y_row)
        polygons.append(np.column_stack([x_row[ok], y_row[ok]]))

    return MeshFaces(keep=keep, polygons=polygons, cx=fx[keep], cy=fy[keep], crs=detect_crs(ds, crs_default))


# ───────────────────────────── 柵格化 ─────────────────────────────

@dataclass
class GridSpec:
    """輸出柵格的解析度與對齊基準 (所有分區共用，合併時格子才會對齊)"""
    res: float
    x0: float
    y0: float

    def frame(self, xmin: float, ymin: float, xmax: float, ymax: float) -> Tuple[Affine, int, int]:
        r = self.res
        gx0 = self.x0 + np.floor((xmin - self.x0) / r) * r
        gx1 = self.x0 + np.ceil((xmax - self.x0) / r) * r
        gy0 = self.y0 + np.floor((ymin - self.y0) / r) * r
        gy1 = self.y0 + np.ceil((ymax - self.y0) / r) * r
        nx = max(1, int(round((gx1 - gx0) / r)))
        ny = max(1, int(round((gy1 - gy0) / r)))
        return Affine(r, 0.0, gx0, 0.0, -r, gy1), nx, ny


def auto_grid_spec(paths: List[str], crs: str, res: Optional[float]) -> GridSpec:
    """
    未指定解析度時，取「最小的常見網格寬度」(第 10 百分位，避開零星的極小網格)，
    局部加密網格 (例如 5/10/40 m) 會以最細的 5 m 輸出；並以網格節點對齊，讓像素與模式網格對齊。
    須看所有分區：加密區可能只在部分分區 (例如 0000 只有 10 m 以上，5 m 在其他分區)。
    """
    widths, xmin, ymin = [], np.inf, np.inf
    for path in paths:
        with open_nc(path) as ds:
            vx, vy = _face_vertices(ds)
        widths.append(np.nanmax(vx, axis=1) - np.nanmin(vx, axis=1))
        xmin, ymin = min(xmin, float(np.nanmin(vx))), min(ymin, float(np.nanmin(vy)))
    if res is None or res <= 0:
        res = float(np.nanpercentile(np.concatenate(widths), 10))
        if not np.isfinite(res) or res <= 0:
            res = 0.00005 if crs == "EPSG:4326" else 5.0
        res = round(res, 6)
    return GridSpec(res=res, x0=xmin % res, y0=ymin % res)


def build_index_raster(polygons: List[np.ndarray], cx: np.ndarray, cy: np.ndarray,
                       grid: GridSpec) -> Tuple[np.ndarray, Affine]:
    """
    把每個面多邊形塗進柵格，像素值 = 面的序號 + 1 (0 = 網格外)。
    比像素還小的面 (塗不到任何像素) 改以中心點補上，避免遺漏。
    """
    allx = np.concatenate([poly[:, 0] for poly in polygons])
    ally = np.concatenate([poly[:, 1] for poly in polygons])
    tf, nx, ny = grid.frame(allx.min(), ally.min(), allx.max(), ally.max())

    shapes = (
        ({"type": "Polygon", "coordinates": [poly.tolist() + [poly[0].tolist()]]}, i + 1)
        for i, poly in enumerate(polygons) if len(poly) >= 3
    )
    index = features.rasterize(shapes, out_shape=(ny, nx), transform=tf, fill=0, dtype="int32")

    seen = np.zeros(len(polygons) + 1, dtype=bool)
    seen[np.unique(index)] = True
    missing = np.nonzero(~seen[1:])[0]
    if missing.size:
        col = np.clip(((cx[missing] - tf.c) / tf.a).astype(int), 0, nx - 1)
        row = np.clip(((cy[missing] - tf.f) / tf.e).astype(int), 0, ny - 1)
        empty = index[row, col] == 0
        index[row[empty], col[empty]] = missing[empty] + 1
    return index, tf


def apply_index(index: np.ndarray, values: np.ndarray) -> np.ndarray:
    """依索引柵格把面數值 (…, nFaces) 展開成 (…, ny, nx)，網格外為 NaN。"""
    lut = np.concatenate([np.full(values.shape[:-1] + (1,), np.nan, dtype="f4"),
                          values.astype("f4")], axis=-1)
    return np.take(lut, index, axis=-1)


def pixel_centers(tf: Affine, nx: int, ny: int) -> Tuple[np.ndarray, np.ndarray]:
    x = tf.c + (np.arange(nx) + 0.5) * tf.a
    y = tf.f + (np.arange(ny) + 0.5) * tf.e
    return x, y


# ───────────────────────────── 變數挑選 ─────────────────────────────

def _depth_series(ds: xr.Dataset, sl: slice) -> np.ndarray:
    """取某段時間的 2D 水深 (time, nFaces)；無 waterdepth 時以水位 - 底床推算。"""
    if "Mesh2d_waterdepth" in ds:
        return ds["Mesh2d_waterdepth"].isel(time=sl).values
    if "Mesh2d_s1" in ds and "Mesh2d_flowelem_bl" in ds:
        s1 = ds["Mesh2d_s1"].isel(time=sl).values
        return np.maximum(s1 - ds["Mesh2d_flowelem_bl"].values[None, :], 0.0)
    raise ValueError("map.nc 中找不到 Mesh2d_waterdepth 或 Mesh2d_s1 + Mesh2d_flowelem_bl")


def find_fou_max_depth_var(ds: xr.Dataset) -> Optional[str]:
    """
    依 long_name 找 fou.nc 的 2D 最大水深變數 (編號 001/002/... 依 .fou 設定而不同，不可寫死)：
      1. long_name 含 "water depth" 且為 maximum value  (.fou 設定 wd max)
      2. 名稱以 _max_depth 結尾                          (.fou 設定 wl max 時附帶的最大水深)
    """
    candidates = [v for v in ds.data_vars
                  if v.startswith("Mesh2d_fourier") and ds[v].dims == ("Mesh2d_nFaces",)]
    for v in candidates:
        ln = str(ds[v].attrs.get("long_name", "")).lower()
        if "water depth" in ln and "maximum value" in ln and not v.endswith("_max_depth"):
            return v
    for v in candidates:
        if v.endswith("_max_depth"):
            return v
    return None


# ───────────────────────────── 輸出 ─────────────────────────────

def write_cog(path: Path, arr: np.ndarray, tf: Affine, crs: str) -> None:
    """以 GDAL COG 驅動輸出 (自動建立金字塔 overviews)。"""
    arr = np.where(np.isfinite(arr), arr, NODATA).astype("f4")
    ny, nx = arr.shape
    profile = dict(driver="GTiff", height=ny, width=nx, count=1, dtype="float32",
                   crs=crs, transform=tf, nodata=NODATA)
    path.parent.mkdir(parents=True, exist_ok=True)
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(arr, 1)
        with mem.open() as src:
            rio_copy(src, str(path), driver="COG", compress="DEFLATE", predictor="2",
                     overview_resampling="nearest", blocksize=512)


def convert_map_nc_to_zarr(nc_path: str, output_zarr_path: str, part_id: str,
                           grid: GridSpec, crs_default: str = "EPSG:3826") -> Tuple[str, np.ndarray]:
    """
    把 map.nc 的時序水深轉成 Zarr (time, y, x)，依 TIME_CHUNK 分批寫入。
    回傳 (zarr 路徑, 全時段最大水深 (保留面))，供沒有 fou.nc 時產製最大淹水深度。
    """
    print(f"🔄 讀取 map.nc: {nc_path}")
    with open_nc(nc_path) as ds:
        mesh = read_mesh_faces(ds, part_id, crs_default)
        index, tf = build_index_raster(mesh.polygons, mesh.cx, mesh.cy, grid)
        ny, nx = index.shape
        xs, ys = pixel_centers(tf, nx, ny)
        nt = ds.sizes["time"]
        print(f"   面數: {mesh.keep.sum()} (排除重疊格 {int((~mesh.keep).sum())}) | 時間步: {nt} | "
              f"坐標系: {mesh.crs} | 柵格: {ny}x{nx} @ {grid.res} m")

        out_p = Path(output_zarr_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        max_faces = np.full(int(mesh.keep.sum()), np.nan, dtype="f4")
        attrs = {
            "crs": mesh.crs,
            "transform": list(tf)[:6],
            "resolution": grid.res,
            "source_file": Path(nc_path).name,
            "institution": ds.attrs.get("institution", "Deltares"),
            "source": ds.attrs.get("source", "D-Flow FM"),
        }

        for start in range(0, nt, TIME_CHUNK):
            sl = slice(start, min(start + TIME_CHUNK, nt))
            depth = _depth_series(ds, sl)[:, mesh.keep].astype("f4")
            max_faces = np.fmax(max_faces, np.nanmax(depth, axis=0))
            block = xr.Dataset(
                {"Mesh2d_waterdepth": (("time", "y", "x"), apply_index(index, depth))},
                coords={"time": ds["time"].isel(time=sl).values, "y": ys, "x": xs},
                attrs=attrs,
            )
            if start == 0:
                enc = {"Mesh2d_waterdepth": {"chunks": (1, min(512, ny), min(512, nx))}}
                block.to_zarr(str(out_p), mode="w", encoding=enc, consolidated=True)
            else:
                block.to_zarr(str(out_p), append_dim="time", consolidated=True)
            print(f"   已寫入時間步 {sl.stop}/{nt}")

    print(f"   ✅ Zarr 完成: {out_p}")
    return str(out_p), max_faces


def read_fou_max_depth(fou_path: str, part_id: str, crs_default: str) -> Tuple[MeshFaces, np.ndarray, str]:
    with open_nc(fou_path) as ds:
        var = find_fou_max_depth_var(ds)
        if var is None:
            raise ValueError(f"{Path(fou_path).name} 中找不到 2D 最大水深變數 (請確認 .fou 有設定 wd max 或 wl max)")
        mesh = read_mesh_faces(ds, part_id, crs_default)
        vals = ds[var].values[mesh.keep].astype("f4")
    return mesh, vals, var


# ───────────────────────────── 1D 渠道／管線 ─────────────────────────────
# 以向量線段輸出：mesh1d.zarr (time, edge) 存逐時數值；mesh1d_network.geojson (WGS84) 存線形。
# 每段 1D 連線 (mesh1d edge) 只由「兩端點中全域編號較小者」所屬分區輸出，避免平行計算的重疊段重複。

MESH1D_VARS = {
    # 輸出名稱: (map.nc 變數, 位置, 兩端點合併方式)
    "waterdepth": ("mesh1d_waterdepth", "node", "max"),
    "s1": ("mesh1d_s1", "node", "mean"),
    "waterdepth_on_ground": ("mesh1d_waterdepth_on_ground", "node", "max"),
    "q1": ("mesh1d_q1", "edge", None),
}
MESH1D_FORMAT = 1


def _chars(var, i) -> str:
    """NetCDF 字元陣列的第 i 筆轉字串 (xarray 可能已合併成 bytes，也可能仍是單字元陣列)。"""
    v = var[i]
    if isinstance(v, (bytes, np.bytes_)):
        raw = bytes(v)
    elif isinstance(v, str):
        return v.strip()
    else:
        raw = b"".join(bytes(c) for c in np.atleast_1d(v))
    return raw.decode("utf-8", errors="ignore").strip()


def _branch_geometry(ds: xr.Dataset):
    """各 branch 的線形頂點 (網路幾何) 與實際長度；檔案沒有網路幾何時回傳 None。"""
    if not all(v in ds for v in ("network_geom_node_count", "network_geom_x", "network_geom_y")):
        return None
    cnt = ds["network_geom_node_count"].values.astype(int)
    starts = np.concatenate([[0], np.cumsum(cnt)[:-1]])
    gx, gy = ds["network_geom_x"].values, ds["network_geom_y"].values
    real = ds["network_edge_length"].values if "network_edge_length" in ds else None
    return starts, cnt, gx, gy, real


def _sub_polyline(geom, branch: int, s0: float, s1: float):
    """取 branch 上 offset s0 → s1 之間的線形 (offset 依 branch 實際長度與幾何長度比例換算)。"""
    starts, cnt, gx, gy, real = geom
    if branch < 0 or branch >= len(cnt) or cnt[branch] < 2:
        return None
    xs = gx[starts[branch]:starts[branch] + cnt[branch]]
    ys = gy[starts[branch]:starts[branch] + cnt[branch]]
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
    scale = cum[-1] / real[branch] if real is not None and real[branch] > 0 else 1.0
    a, b = sorted((s0 * scale, s1 * scale))
    inner = (cum > a) & (cum < b)
    pts = [(np.interp(a, cum, xs), np.interp(a, cum, ys))] + list(zip(xs[inner], ys[inner])) \
        + [(np.interp(b, cum, xs), np.interp(b, cum, ys))]
    return pts if s0 <= s1 else pts[::-1]


def extract_mesh1d(map_path: str, part_id: str):
    """讀一個分區 map.nc 的 1D 連線：回傳 (線形清單[投影坐標], 屬性清單, {變數: (time, edge) 陣列}, time)。無 1D 時回傳 None。"""
    with open_nc(map_path) as ds:
        if "mesh1d_edge_nodes" not in ds or "mesh1d_node_x" not in ds:
            return None
        en = ds["mesh1d_edge_nodes"]
        edges = en.values.astype(np.int64) - int(en.attrs.get("start_index", 0))
        nx_, ny_ = ds["mesh1d_node_x"].values, ds["mesh1d_node_y"].values
        n_nodes = len(nx_)

        keep = np.ones(len(edges), dtype=bool)
        if part_id.isdigit() and "mesh1d_flowelem_domain" in ds:
            dom = ds["mesh1d_flowelem_domain"].values
            gnr = ds["mesh1d_flowelem_globalnr"].values if "mesh1d_flowelem_globalnr" in ds else np.arange(n_nodes)
            first = np.where(gnr[edges[:, 0]] <= gnr[edges[:, 1]], edges[:, 0], edges[:, 1])
            keep = dom[first] == int(part_id)
        idx = np.nonzero(keep)[0]
        if idx.size == 0:
            return None

        geom = _branch_geometry(ds)
        nb = ds["mesh1d_node_branch"].values.astype(int) if "mesh1d_node_branch" in ds else None
        no = ds["mesh1d_node_offset"].values if "mesh1d_node_offset" in ds else None
        branch_ids = ds["network_branch_id"].values if "network_branch_id" in ds else None

        lines, props = [], []
        for e in idx:
            a, b = edges[e]
            pts = None
            if geom is not None and nb is not None and no is not None and nb[a] == nb[b]:
                pts = _sub_polyline(geom, nb[a], float(no[a]), float(no[b]))
            if pts is None:  # 跨 branch 的連接段 (例如匯流點) 以直線連接
                pts = [(nx_[a], ny_[a]), (nx_[b], ny_[b])]
            lines.append(pts)
            br = int(nb[a]) if nb is not None else -1
            props.append({"branch": _chars(branch_ids, br) if branch_ids is not None and br >= 0 else ""})

        values = {}
        for name, (var, loc, how) in MESH1D_VARS.items():
            if var not in ds:
                continue
            arr = ds[var].values.astype("f4")  # (time, nodes) 或 (time, edges)
            if loc == "edge":
                values[name] = arr[:, idx]
            else:
                va, vb = arr[:, edges[idx, 0]], arr[:, edges[idx, 1]]
                values[name] = np.fmax(va, vb) if how == "max" else (va + vb) / 2
        return lines, props, values, ds["time"].values


def convert_mesh1d(partitions: Dict[str, Dict[str, str]], out_dir: Path, crs: str) -> Optional[str]:
    """合併各分區 1D 連線，輸出 mesh1d.zarr 與 mesh1d_network.geojson；沒有 1D 時回傳 None。"""
    from pyproj import Transformer

    lines, props, values, times = [], [], {}, None
    for part_id, files in partitions.items():
        if "map" not in files:
            continue
        got = extract_mesh1d(files["map"], part_id)
        if got is None:
            continue
        l, p, v, t = got
        if times is None or len(t) > len(times):
            times = t
        for q in p:
            q["partition"] = part_id
        lines += l
        props += p
        for k, arr in v.items():
            values.setdefault(k, []).append(arr)
    if not lines:
        return None

    nt = len(times)
    merged = {}
    for k, parts in values.items():
        if sum(a.shape[1] for a in parts) != len(lines):
            continue  # 某些分區缺此變數，略過以免錯位
        padded = [np.pad(a, ((0, nt - a.shape[0]), (0, 0)), constant_values=np.nan) for a in parts]
        merged[k] = np.concatenate(padded, axis=1)

    to84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    features_ = []
    for i, (pts, pr) in enumerate(zip(lines, props)):
        xs, ys = to84.transform([p[0] for p in pts], [p[1] for p in pts])
        features_.append({
            "type": "Feature",
            "properties": {"id": i, **pr},
            "geometry": {"type": "LineString",
                         "coordinates": [[round(float(x), 7), round(float(y), 7)] for x, y in zip(xs, ys)]},
        })
    with open(out_dir / "mesh1d_network.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features_}, f, ensure_ascii=False)

    attrs = {"crs": crs, "format": MESH1D_FORMAT}
    for k, arr in merged.items():  # 圖例範圍：第 2～98 百分位 (流量取絕對值)
        a = np.abs(arr) if k == "q1" else arr
        fin = a[np.isfinite(a)]
        attrs[f"range_{k}"] = [float(np.percentile(fin, 2)), float(np.percentile(fin, 98))] if fin.size else [0.0, 1.0]
    ds_out = xr.Dataset({k: (("time", "edge"), v) for k, v in merged.items()},
                        coords={"time": times, "edge": np.arange(len(lines))}, attrs=attrs)
    zpath = out_dir / "mesh1d.zarr"
    ds_out.to_zarr(str(zpath), mode="w", consolidated=True)
    print(f"   ✅ 1D 完成: {len(lines)} 段 | 變數: {', '.join(merged)} → mesh1d.zarr、mesh1d_network.geojson")
    return str(zpath)


# ───────────────────────────── 觀測站、結構物 (his.nc) ─────────────────────────────
# 輸出 his.zarr (變數名稱「類別__變數」，維度 (time, 類別)) 與 his_objects.geojson (位置＋檢核摘要)。

def _valid_xy(a: np.ndarray) -> np.ndarray:
    return np.isfinite(a) & (np.abs(a) < 1e30)  # NetCDF 填充值約 9.97e36


def _group_nodes(ds: xr.Dataset, prefix: str, n: int):
    """依 <prefix>_geom_node_count 把頂點分給 n 個物件；資料無效時回傳 None。"""
    xv, yv = f"{prefix}_geom_node_coordx", f"{prefix}_geom_node_coordy"
    if xv not in ds or yv not in ds:
        return None
    x, y = ds[xv].values.astype("f8"), ds[yv].values.astype("f8")
    if not (_valid_xy(x).all() and _valid_xy(y).all()):
        return None
    cnt_var = f"{prefix}_geom_node_count"
    cnt = ds[cnt_var].values if cnt_var in ds else None
    if cnt is not None and np.all((cnt > 0) & (cnt < 1e6)) and int(cnt.sum()) == len(x):
        cnt = cnt.astype(int)
    elif len(x) % n == 0:
        cnt = np.full(n, len(x) // n)
    else:
        return None
    starts = np.concatenate([[0], np.cumsum(cnt)[:-1]])
    return [list(zip(x[s:s + c], y[s:s + c])) for s, c in zip(starts, cnt)]


def _his_locations(ds: xr.Dataset, cat: str, n: int):
    """各物件位置 [(幾何類型, [(x, y), ...]) 或 None]。依序嘗試 D-Flow FM 不同版本的寫法。"""
    if cat == "station" and "station_x_coordinate" in ds:
        x, y = ds["station_x_coordinate"].values, ds["station_y_coordinate"].values
        return [("Point", [(x[i], y[i])]) if _valid_xy(np.array([x[i], y[i]])).all() else None for i in range(n)]
    if cat == "cross_section":
        groups = _group_nodes(ds, cat, n)
        return [("LineString", g) for g in groups] if groups else [None] * n
    # 結構物：*_input_geom (輸入位置) 最可靠，其次 *_geom 中點、*_xmid
    for prefix in (f"{cat}_input", f"{cat.replace('gen', '')}_input", cat):
        groups = _group_nodes(ds, prefix, n)
        if groups:
            return [("Point", [tuple(np.mean(g, axis=0))]) for g in groups]
    if f"{cat}_xmid" in ds:
        x, y = ds[f"{cat}_xmid"].values, ds[f"{cat}_ymid"].values
        return [("Point", [(x[i], y[i])]) if _valid_xy(np.array([x[i], y[i]])).all() else None for i in range(n)]
    return [None] * n


def _round(v, d=3):
    return None if v is None or not np.isfinite(v) else round(float(v), d)


def _his_summary(cat: str, data: Dict[str, np.ndarray], i: int) -> dict:
    """單一物件的檢核摘要 (放進 GeoJSON properties，圖台用來著色與列出異常)。"""
    def col(var):
        a = data.get(f"{cat}_{var}") if cat != "station" else data.get(var)
        return None if a is None else a[:, i]

    s = {}
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # 某物件整段無值 (All-NaN) 屬正常，不顯示警告
        if cat == "station":
            for var in ("waterlevel", "waterdepth"):
                a = col(var)
                if a is not None:
                    s[f"{var}_max"] = _round(np.nanmax(a))
        elif cat == "pump":
            q, cap = col("structure_discharge"), col("capacity")
            if q is not None:
                qa = np.abs(q)
                s["q_max"] = _round(np.nanmax(qa))
                s["ran"] = bool(np.nanmax(qa) > 1e-6)
                s["run_frac"] = _round(np.mean(qa > 1e-6), 3)
                if cap is not None:
                    s["cap_max"] = _round(np.nanmax(cap))
                    full = (cap > 0) & (qa >= 0.99 * cap)
                    s["full_frac"] = _round(np.mean(full), 3)
        else:
            q = col("discharge")
            if q is not None:
                s["q_absmax"] = _round(np.nanmax(np.abs(q)))
    return s


# ── 結構物設計位置 ──
# his.nc 的結構物座標是「模式計算位置」：D-Flow FM 把結構物吸附到最近的 1D 計算連線，寫入的是該連線中點，
# 與 Delta Shell 中定義的位置 (branchId + chainage) 可差數十公尺。能找到模型的 structures.ini 時，
# 改用 branchId + chainage 沿網路線形計算設計位置。

def _ini_blocks(path: Path) -> List[Dict[str, str]]:
    """逐行讀取 Delft3D .ini (不用 configparser：同名區塊重複、行尾 # 註解、key 大小寫都會出錯)。"""
    blocks, cur = [], None
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = {"__section__": line[1:-1].strip().lower()}
            blocks.append(cur)
        elif "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip().lower()] = v.strip()
    return blocks


def find_structure_files(partitions: Dict[str, Dict[str, str]], input_dir: str) -> List[str]:
    """
    找模型的結構物定義檔：在成果資料夾、上一層、上兩層找 .mdu，讀其中的 StructureFile；
    找不到 .mdu 時，也接受直接放在成果資料夾的 structures.ini。
    """
    folders = {Path(f).parent for fs in partitions.values() for f in fs.values()} or {Path(input_dir).resolve()}
    found: List[str] = []
    for folder in sorted(folders):
        for d in (folder, folder.parent, folder.parent.parent):
            for mdu in sorted(d.glob("*.mdu")):
                for raw in mdu.read_text(encoding="utf-8", errors="ignore").splitlines():
                    m = re.match(r"\s*StructureFile\s*=\s*([^#]*)", raw, re.IGNORECASE)
                    if m:
                        for name in re.split(r"[;\s]+", m.group(1).strip()):
                            p = (mdu.parent / name).resolve()
                            if name and p.is_file() and str(p) not in found:
                                found.append(str(p))
            if found:
                return found
        direct = folder / "structures.ini"
        if direct.is_file():
            return [str(direct)]
    return found


def read_structure_locations(files: List[str]) -> Dict[str, dict]:
    """{結構物 id: {"branch": ..., "chainage": ...} 或 {"x": ..., "y": ...}}"""
    locs = {}
    for f in files:
        for b in _ini_blocks(Path(f)):
            if b.get("__section__") != "structure" or "id" not in b:
                continue
            try:
                if "branchid" in b and "chainage" in b:
                    locs[b["id"]] = {"branch": b["branchid"], "chainage": float(b["chainage"])}
                elif "xcoordinates" in b and "ycoordinates" in b:  # 2D 結構物：以折線中點表示
                    xs = [float(v) for v in b["xcoordinates"].split()]
                    ys = [float(v) for v in b["ycoordinates"].split()]
                    locs[b["id"]] = {"x": float(np.mean(xs)), "y": float(np.mean(ys))}
            except ValueError:
                continue
    return locs


def design_location(loc: dict, network) -> Optional[Tuple[float, float]]:
    """依 branch + chainage 沿網路線形算出設計位置；network = (branch id → 索引, _branch_geometry)。"""
    if "x" in loc:
        return loc["x"], loc["y"]
    index, geom = network
    bi = index.get(loc["branch"])
    if bi is None or geom is None:
        return None
    pts = _sub_polyline(geom, bi, loc["chainage"], loc["chainage"])
    return (float(pts[0][0]), float(pts[0][1])) if pts else None


def _network_from_map(map_path: Optional[str]):
    if not map_path:
        return None
    with open_nc(map_path) as ds:
        if "network_branch_id" not in ds:
            return None
        ids = ds["network_branch_id"].values
        index = {_chars(ids, i): i for i in range(len(ids))}
        return index, _branch_geometry(ds)


def convert_his(his_path: str, out_dir: Path, crs: str, structure_files: Optional[List[str]] = None,
                map_path: Optional[str] = None) -> Optional[str]:
    """his.nc → his.zarr + his_objects.geojson；沒有可用物件時回傳 None。"""
    from pyproj import Transformer

    to84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    out_vars, features_, counts = {}, [], {}
    design = read_structure_locations(structure_files or [])
    network = _network_from_map(map_path) if design else None
    n_design = n_fallback = 0
    with open_nc(his_path) as ds:
        times = ds["time"].values
        for cat, label in HIS_CATEGORIES.items():
            if cat not in ds.dims or f"{cat}_name" not in ds:
                continue
            n = ds.sizes[cat]
            names = [_chars(ds[f"{cat}_name"].values, i) for i in range(n)]
            locs = _his_locations(ds, cat, n)
            data = {v: ds[v].values.astype("f4") for v in ds.data_vars
                    if ds[v].dims == ("time", cat) and np.issubdtype(ds[v].dtype, np.number)}
            for v, arr in data.items():
                out_vars[f"{cat}__{v}"] = xr.Variable(("time", cat), arr, attrs={
                    k: ds[v].attrs[k] for k in ("long_name", "units") if k in ds[v].attrs})
            located = 0
            for i in range(n):
                loc_src = "his"  # his.nc 的模式計算位置
                if cat != "station" and names[i] in design:
                    xy = design_location(design[names[i]], network) if network or "x" in design[names[i]] else None
                    if xy is not None:
                        locs[i], loc_src = ("Point", [xy]), "design"
                        n_design += 1
                    else:
                        n_fallback += 1
                if locs[i] is None:
                    continue
                gtype, pts = locs[i]
                xs, ys = to84.transform([p[0] for p in pts], [p[1] for p in pts])
                coords = [[round(float(x), 7), round(float(y), 7)] for x, y in zip(xs, ys)]
                features_.append({
                    "type": "Feature",
                    "properties": {"cat": cat, "idx": i, "name": names[i], "loc_src": loc_src,
                                   **({"branch": design[names[i]]["branch"], "chainage": design[names[i]]["chainage"]}
                                      if loc_src == "design" and "branch" in design[names[i]] else {}),
                                   **_his_summary(cat, data, i)},
                    "geometry": {"type": gtype, "coordinates": coords[0] if gtype == "Point" else coords},
                })
                located += 1
            counts[cat] = {"label": label, "count": n, "located": located}
            if located < n:
                print(f"   ⚠️ {label} {n - located}/{n} 個沒有坐標，地圖上不顯示")

        wb = {}
        for key in ("water_balance_total_volume", "water_balance_volume_error"):
            if key in ds:
                wb[key] = float(ds[key].values[-1])
    if not features_:
        return None
    if structure_files:
        print(f"   結構物位置：{n_design} 個依 {', '.join(Path(f).name for f in structure_files)} 的管段＋里程計算"
              + (f"，{n_fallback} 個在網路中找不到管段，改用 his.nc 計算位置" if n_fallback else ""))
    else:
        print("   ⚠️ 找不到模型的 structures.ini，結構物位置使用 his.nc 的模式計算位置 (可能與設計位置差數十公尺)")

    with open(out_dir / "his_objects.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features_}, f, ensure_ascii=False)
    attrs = {"source_file": Path(his_path).name, "categories": json.dumps(counts, ensure_ascii=False),
             "structure_files": json.dumps([str(f) for f in (structure_files or [])], ensure_ascii=False),
             "water_balance": json.dumps(wb)}
    ds_out = xr.Dataset(out_vars, coords={"time": times}, attrs=attrs)
    zpath = out_dir / "his.zarr"
    ds_out.to_zarr(str(zpath), mode="w", consolidated=True)
    print("   ✅ 觀測站／結構物完成: " + "、".join(f"{c['label']} {c['located']}" for c in counts.values())
          + " → his.zarr、his_objects.geojson")
    return str(zpath)


def source_signature(partitions: Dict[str, Dict[str, str]], extra: Optional[List[str]] = None) -> Dict[str, list]:
    """原始檔 (含 his.nc 等 extra) 的 [大小, 修改時間]，寫進 partitions_metadata.json；圖台據此判斷轉檔結果是否過期。"""
    sig = {}
    files = [f for fs in partitions.values() for f in fs.values()] + list(extra or [])
    for f in files:
        st = Path(f).stat()
        sig[str(Path(f).resolve())] = [st.st_size, int(st.st_mtime)]
    return dict(sorted(sig.items()))


def default_output_dir(input_dir, partitions: Optional[Dict[str, Dict[str, str]]] = None) -> Path:
    """
    轉檔結果的預設位置：放 map.nc / fou.nc 的成果資料夾底下的 ZARR (不會和專案的輸入檔混在一起)。
    指定整個專案資料夾時，.nc 在下層的 output 裡，結果就放在 output/ZARR。
    """
    folders = {str(Path(f).parent) for fs in (partitions or {}).values() for f in fs.values()}
    base = Path(os.path.commonpath(sorted(folders))) if folders else Path(input_dir).resolve()
    return base / "ZARR"


def process_dfm_directory_pipeline(input_dir: str, output_dir: Optional[str] = None,
                                   crs_code: str = "EPSG:3826", res: Optional[float] = None,
                                   skip_zarr: bool = False,
                                   progress: Optional[Callable[[str, float], None]] = None,
                                   partitions: Optional[Dict[str, Dict[str, str]]] = None,
                                   structure_files: Optional[List[str]] = None,
                                   ) -> Dict[str, Dict[str, str]]:
    """
    全自動批次處理：
    1. 掃描 input_dir 判斷所有分區 (已掃描過可直接傳入 partitions)
    2. 各分區 map.nc → .zarr (時序水深，供展示圖台使用)
    3. 各分區 fou.nc (或 map.nc 全時段最大值) → *_cog.tif，並輸出全區合併 COG
    progress(訊息, 0~1)：圖台用來顯示進度。
    """
    def report(msg: str, frac: float):
        if progress:
            progress(msg, frac)

    partitions = partitions or detect_dfm_partitions(input_dir)
    if not partitions:
        print(f"⚠️ 在 {input_dir} 中未找到任何 Delft3D-FM 輸出檔案 (*_map.nc / *_fou.nc)")
        return {}

    target_out_dir = Path(output_dir).resolve() if output_dir else default_output_dir(input_dir, partitions)
    target_out_dir.mkdir(parents=True, exist_ok=True)

    first = next(iter(partitions.values()))
    with open_nc(first.get("map") or first["fou"]) as ds0:
        crs = detect_crs(ds0, crs_code)
    grid = auto_grid_spec([f.get("fou") or f["map"] for f in partitions.values()], crs, res)

    print("=" * 70)
    print("🚀 Delft3D-FM 多分區轉檔開始")
    print(f"📁 輸入目錄: {input_dir}")
    print(f"📁 輸出目錄: {target_out_dir}")
    print(f"🧩 分區數量: {len(partitions)} ({', '.join(partitions.keys())})")
    print(f"🌐 坐標系: {crs} | 輸出解析度: {grid.res}")
    print("=" * 70)

    summary: Dict[str, Dict[str, str]] = {}
    merged_polys: List[np.ndarray] = []
    merged_cx, merged_cy, merged_vals = [], [], []

    n_steps = len(partitions) + 1
    for i, (part_id, files) in enumerate(partitions.items()):
        print(f"\n▶ 處理分區 [{part_id}]")
        report(f"轉檔中：分區 {part_id}（{i + 1}/{len(partitions)}）", i / n_steps)
        part_summary: Dict[str, str] = {}
        map_max = None

        if "map" in files and not skip_zarr:
            zarr_out = target_out_dir / f"{Path(files['map']).stem}.zarr"
            part_summary["zarr"], map_max = convert_map_nc_to_zarr(files["map"], str(zarr_out), part_id, grid, crs)

        mesh = None
        if "fou" in files:
            try:
                mesh, vals, var = read_fou_max_depth(files["fou"], part_id, crs)
                print(f"   最大水深來源: {Path(files['fou']).name} [{var}]")
                tif_out = target_out_dir / f"{Path(files['fou']).stem}_max_depth_cog.tif"
            except ValueError as e:
                if "map" not in files:
                    raise
                print(f"   ⚠️ {e}，改用 map.nc")
        if mesh is None and "map" in files:
            with open_nc(files["map"]) as ds:
                mesh = read_mesh_faces(ds, part_id, crs)
                if map_max is None:  # 略過 Zarr 時另外算全時段最大值
                    map_max = np.full(int(mesh.keep.sum()), np.nan, dtype="f4")
                    for start in range(0, ds.sizes["time"], TIME_CHUNK):
                        sl = slice(start, start + TIME_CHUNK)
                        map_max = np.fmax(map_max, np.nanmax(_depth_series(ds, sl)[:, mesh.keep], axis=0))
            vals = map_max
            print(f"   最大水深來源: {Path(files['map']).name} 全時段最大值")
            tif_out = target_out_dir / f"{Path(files['map']).stem.replace('_map', '')}_max_depth_cog.tif"
        elif mesh is None:
            summary[part_id] = part_summary
            continue

        index, tf = build_index_raster(mesh.polygons, mesh.cx, mesh.cy, grid)
        write_cog(tif_out, apply_index(index, vals), tf, crs)
        print(f"   ✅ COG 完成: {tif_out.name}")
        part_summary["cog"] = str(tif_out)
        summary[part_id] = part_summary

        merged_polys += mesh.polygons
        merged_cx.append(mesh.cx)
        merged_cy.append(mesh.cy)
        merged_vals.append(vals)

    if len(merged_vals) > 1:
        print("\n▶ 產製全區合併最大淹水深度")
        report("轉檔中：產製全區合併最大淹水深度", len(partitions) / n_steps)
        index, tf = build_index_raster(merged_polys, np.concatenate(merged_cx), np.concatenate(merged_cy), grid)
        # 以各分區檔名的共同開頭命名 (例 MyModel_0000_fou、MyModel_0001_fou → MyModel)
        stems = [Path(f.get("fou") or f["map"]).stem for f in partitions.values()]
        stem = re.sub(r"[_\-\s\d]+$", "", os.path.commonprefix(stems)) or "dfm"
        merged_out = target_out_dir / f"{stem}_max_depth_merged_cog.tif"
        write_cog(merged_out, apply_index(index, np.concatenate(merged_vals)), tf, crs)
        print(f"   ✅ 合併 COG 完成: {merged_out.name}")
        summary["merged"] = {"cog": str(merged_out)}

    mesh1d_out = None
    if not skip_zarr:
        print("\n▶ 產製 1D 渠道／管線")
        report("轉檔中：1D 渠道／管線", (len(partitions) + 0.5) / n_steps)
        mesh1d_out = convert_mesh1d(partitions, target_out_dir, crs)
        if mesh1d_out is None:
            print("   (此成果沒有 1D 網路)")

    his_file = detect_his_file(input_dir, partitions)
    struct_files = (structure_files or find_structure_files(partitions, input_dir)) if his_file else []
    his_out = None
    if his_file and not skip_zarr:
        print(f"\n▶ 產製觀測站／結構物 ({Path(his_file).name})")
        report("轉檔中：觀測站／結構物", (len(partitions) + 0.8) / n_steps)
        first_map = next((f["map"] for f in partitions.values() if "map" in f), None)
        his_out = convert_his(his_file, target_out_dir, crs, struct_files, first_map)

    meta_path = target_out_dir / "partitions_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"format_version": OUTPUT_FORMAT_VERSION, "crs": crs, "resolution": grid.res,
                   "source_dir": str(Path(input_dir).resolve()),
                   "source_signature": source_signature(partitions, ([his_file] if his_file else []) + struct_files),
                   "partitions": summary, "mesh1d": mesh1d_out, "his": his_out},
                  f, indent=2, ensure_ascii=False)
    report("轉檔完成", 1.0)

    print("\n" + "=" * 70)
    print(f"🎉 全部完成！索引檔: {meta_path}")
    print("=" * 70)
    return summary


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # 避免中文 Windows 終端機輸出 emoji 時出錯
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Delft3D-FM 模式輸出 (map.nc / fou.nc) 自動轉 Zarr 與 COG GeoTIFF 工具",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-i", "--input-dir", required=True,
                        help="Delft3D-FM 成果資料夾 (含 XXX_0000_map.nc / XXX_0000_fou.nc 等)；\n"
                             "也可指定整個專案資料夾，會自動往下層尋找 .nc")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="輸出資料夾 (預設: <放 .nc 的成果資料夾>\\ZARR)")
    parser.add_argument("--crs", default="EPSG:3826",
                        help="NetCDF 內讀不到坐標系時使用的預設值 (預設: EPSG:3826 TWD97/TM2)")
    parser.add_argument("--res", type=float, default=None,
                        help="輸出柵格解析度 (公尺)；預設自動取網格大小")
    parser.add_argument("--cog-only", action="store_true",
                        help="只輸出最大淹水深度 GeoTIFF，不產製時序 Zarr")
    parser.add_argument("--structures", nargs="+", default=None,
                        help="模型的結構物定義檔 (structures.ini)；預設自動由 .mdu 的 StructureFile 尋找")
    args = parser.parse_args()
    try:
        process_dfm_directory_pipeline(args.input_dir, args.output_dir, args.crs, args.res, args.cog_only,
                                       structure_files=args.structures)
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
