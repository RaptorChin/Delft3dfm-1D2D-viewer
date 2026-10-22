"""Delft3D-FM Multi-Partition Spatio-Temporal Viewer Engine.
掃描 convert.py 產生的 Zarr 分區，提供：分區目錄、各時間步淹水深度 PNG 疊圖、點位水深時序查詢。
"""
import hashlib
import io
import json
import os
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pyproj import Transformer
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

from . import convert as dfm_convert

app = FastAPI(title="Delft3D-FM Multi-Partition Spatio-Temporal Viewer", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WEB_DIR = Path(__file__).parent / "web"


@dataclass
class PartitionInfo:
    id: str
    name: str
    path: str
    crs: str               # 'EPSG:3826'、'EPSG:3825'、'EPSG:4326' ...
    bounds_native: list    # [xmin, ymin, xmax, ymax] 原生坐標 (像素外框)
    bounds_3857: list      # 同上，Web Mercator
    bounds_wgs84: list     # [w, s, e, n]，與 bounds_3857 為同一個矩形 (疊圖才會對齊)
    shape: list            # [ny, nx]
    transform: list        # Affine 前 6 個係數
    times: list
    var_name: str


@dataclass
class DataCatalog:
    current_path: str = ""
    version: int = 0       # 每次重新掃描 +1，前端以此避免瀏覽器快取到舊圖
    crs: str = ""
    partitions: dict = field(default_factory=dict)
    union_bounds_native: list = field(default_factory=lambda: [0, 0, 0, 0])
    union_bounds_wgs84: list = field(default_factory=lambda: [0, 0, 0, 0])
    times: list = field(default_factory=list)
    time_labels: list = field(default_factory=list)
    mesh1d_dir: str = ""   # 含 mesh1d.zarr / mesh1d_network.geojson 的資料夾；空字串 = 無 1D
    mesh1d_vars: dict = field(default_factory=dict)  # {變數: [圖例下限, 上限]}
    his_dir: str = ""      # 含 his.zarr / his_objects.geojson 的資料夾；空字串 = 無觀測站／結構物
    his_frame_index: list = field(default_factory=list)  # 地圖第 i 個時間步 → his 最接近的時間步


catalog = DataCatalog()
_scan_lock = threading.Lock()
_datasets: dict = {}


def _open(path: str) -> xr.Dataset:
    ds = _datasets.get(path)
    if ds is None:
        ds = xr.open_zarr(path, consolidated=None)
        _datasets[path] = ds
    return ds


@lru_cache(maxsize=16)
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


MESH1D_ZARR = "mesh1d.zarr"
MESH1D_GEOJSON = "mesh1d_network.geojson"
HIS_ZARR = "his.zarr"
HIS_GEOJSON = "his_objects.geojson"
NON_2D_ZARR = {MESH1D_ZARR, HIS_ZARR}


def _find_zarr(p: Path) -> list:
    """找 2D 分區 Zarr (排除 1D 的 mesh1d.zarr 與觀測站的 his.zarr)。"""
    if p.name.endswith(".zarr") and p.name not in NON_2D_ZARR:
        return [p]
    found = [z for z in sorted(p.glob("*.zarr")) if z.name not in NON_2D_ZARR]
    return found or [z for z in sorted(p.rglob("*.zarr")) if z.name not in NON_2D_ZARR]


def scan_directory(target_path: str) -> DataCatalog:
    p = Path(target_path.strip().strip('"')).resolve()
    if not p.exists():
        raise ValueError(f"路徑不存在: {target_path}")
    zarr_files = _find_zarr(p)
    if not zarr_files:
        raise ValueError(f"在 {target_path} 中找不到任何 .zarr 分區成果（請先用轉檔工具把 map.nc 轉成 Zarr）")

    parts = {}
    errors = []
    global_times, global_labels = [], []
    for i, zf in enumerate(zarr_files):
        try:
            ds = xr.open_zarr(str(zf), consolidated=None)
            var = "Mesh2d_waterdepth" if "Mesh2d_waterdepth" in ds else list(ds.data_vars)[0]
            crs = str(ds.attrs.get("crs", ds.attrs.get("spatial_ref", "EPSG:3826"))).upper()

            xv, yv = ds.x.values, ds.y.values
            if len(xv) > 1:
                dx = float(abs(xv[1] - xv[0]))
            else:
                dx = float(ds.attrs.get("resolution", 5.0))
            dy = float(abs(yv[1] - yv[0])) if len(yv) > 1 else dx
            if crs != "EPSG:4326" and np.nanmax(np.abs(xv)) <= 180 and np.nanmax(np.abs(yv)) <= 90:
                crs = "EPSG:4326"

            xmin, xmax = float(xv.min()) - dx / 2, float(xv.max()) + dx / 2
            ymin, ymax = float(yv.min()) - dy / 2, float(yv.max()) + dy / 2
            # north-up transform；若 y 為遞增排列，出圖時會先上下翻轉 (見 get_partition_depth_png)
            tf = Affine(dx, 0.0, xmin, 0.0, -dy, ymax)

            b3857 = list(transform_bounds(crs, "EPSG:3857", xmin, ymin, xmax, ymax, densify_pts=21))
            to84 = _transformer("EPSG:3857", "EPSG:4326")
            w, s = to84.transform(b3857[0], b3857[1])
            e, n = to84.transform(b3857[2], b3857[3])

            if np.issubdtype(ds.time.dtype, np.datetime64):
                ts = pd.to_datetime(ds.time.values)
                t_vals = [float(t.timestamp()) for t in ts]
                t_lbls = [t.strftime("%m-%d %H:%M") for t in ts]
            else:
                t_vals = [float(t) for t in ds.time.values]
                t_lbls = [f"Step {j:02d}" for j in range(len(t_vals))]
            if len(t_vals) > len(global_times):
                global_times, global_labels = t_vals, t_lbls

            pid = f"part_{i:03d}"
            parts[pid] = PartitionInfo(
                id=pid, name=zf.name, path=str(zf), crs=crs,
                bounds_native=[xmin, ymin, xmax, ymax], bounds_3857=b3857, bounds_wgs84=[w, s, e, n],
                shape=[len(yv), len(xv)], transform=list(tf)[:6], times=t_vals, var_name=var,
            )
        except Exception as ex:  # 單一分區壞掉不影響其他分區
            errors.append(f"{zf.name}: {ex}")
            print(f"警告: 無法讀取分區 {zf}: {ex}")

    if not parts:
        raise ValueError("沒有可讀取的 Zarr 分區：\n" + "\n".join(errors))

    crs_set = {pi.crs for pi in parts.values()}
    with _scan_lock:
        _datasets.clear()
        catalog.current_path = str(p)
        catalog.version += 1
        catalog.crs = ", ".join(sorted(crs_set))
        catalog.partitions = parts
        bn = np.array([pi.bounds_native for pi in parts.values()])
        bw = np.array([pi.bounds_wgs84 for pi in parts.values()])
        catalog.union_bounds_native = [bn[:, 0].min(), bn[:, 1].min(), bn[:, 2].max(), bn[:, 3].max()]
        catalog.union_bounds_wgs84 = [bw[:, 0].min(), bw[:, 1].min(), bw[:, 2].max(), bw[:, 3].max()]
        catalog.times = global_times
        catalog.time_labels = global_labels
        catalog.mesh1d_dir, catalog.mesh1d_vars = "", {}
        m1 = zarr_files[0].parent / MESH1D_ZARR
        if m1.exists() and (m1.parent / MESH1D_GEOJSON).exists():
            try:
                ds1 = xr.open_zarr(str(m1), consolidated=None)
                catalog.mesh1d_vars = {k: ds1.attrs.get(f"range_{k}", [0.0, 1.0]) for k in ds1.data_vars}
                catalog.mesh1d_dir = str(m1.parent)
            except Exception as ex:
                print(f"警告: 無法讀取 1D 成果 {m1}: {ex}")
        catalog.his_dir, catalog.his_frame_index = "", []
        hz = zarr_files[0].parent / HIS_ZARR
        if hz.exists() and (hz.parent / HIS_GEOJSON).exists():
            try:
                his_t = pd.to_datetime(xr.open_zarr(str(hz), consolidated=None).time.values)
                his_s = np.array([t.timestamp() for t in his_t])
                # 地圖時間步 → his 最接近的時間步 (his 輸出間隔通常比 map 密)
                catalog.his_frame_index = [int(np.argmin(np.abs(his_s - t))) for t in global_times] \
                    if len(global_times) and len(his_s) else []
                catalog.his_dir = str(hz.parent)
            except Exception as ex:
                print(f"警告: 無法讀取觀測站／結構物成果 {hz}: {ex}")
    return catalog


@app.get("/api/catalog")
def get_catalog():
    return {
        "current_path": catalog.current_path,
        "version": catalog.version,
        "crs": catalog.crs,
        "partition_count": len(catalog.partitions),
        "partitions": [vars(pi) for pi in catalog.partitions.values()],
        "union_bounds_native": catalog.union_bounds_native,
        "union_bounds_wgs84": catalog.union_bounds_wgs84,
        "times": catalog.times,
        "time_labels": catalog.time_labels,
        "start_time": catalog.time_labels[0] if catalog.time_labels else "",
        "end_time": catalog.time_labels[-1] if catalog.time_labels else "",
        "mesh1d": {"vars": catalog.mesh1d_vars} if catalog.mesh1d_dir else None,
        "his": bool(catalog.his_dir),
    }


# ───────────────────── 自動轉檔：選到原始 .nc 資料夾時 ─────────────────────
# 轉檔結果優先放在 .nc 所在成果資料夾底下的「ZARR」(與 convert.bat 相同)；沒有寫入權限 (例如唯讀網路磁碟) 時
# 改放本機快取。partitions_metadata.json 記錄原始檔大小與修改時間，原始檔沒變就直接沿用。

CACHE_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "dfm_viewer" / "cache"


@dataclass
class ConvertJob:
    state: str = "idle"    # idle / running / done / error
    message: str = ""
    percent: float = 0.0
    source: str = ""
    output: str = ""


job = ConvertJob()
_job_lock = threading.Lock()


def _output_candidates(src: Path, partitions: dict) -> list:
    key = hashlib.md5(str(src).lower().encode("utf-8")).hexdigest()[:12]
    return [dfm_convert.default_output_dir(src, partitions), CACHE_ROOT / f"{src.name}_{key}"]


def _find_up_to_date(src: Path, partitions: dict, signature: dict):
    # 舊版放在旁邊的「<資料夾>_ZARR」，仍可沿用
    for cand in _output_candidates(src, partitions) + [src.parent / f"{src.name}_ZARR"]:
        meta = cand / "partitions_metadata.json"
        try:
            with open(meta, encoding="utf-8-sig") as f:
                meta_json = json.load(f)
                if (meta_json.get("source_signature") == signature
                        and meta_json.get("format_version", 1) >= dfm_convert.OUTPUT_FORMAT_VERSION
                        and _find_zarr(cand)):
                    return cand
        except (OSError, ValueError):
            continue
    return None


def _writable_output(src: Path, partitions: dict) -> Path:
    for cand in _output_candidates(src, partitions):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_test"
            probe.write_bytes(b"")
            probe.unlink()
            return cand
        except OSError:
            continue
    raise ValueError(f"找不到可寫入的位置存放轉檔結果 (試過: {', '.join(map(str, _output_candidates(src, partitions)))})")


def prepare_folder(target_path: str):
    """
    判斷資料夾要怎麼載入。回傳 ("ready", zarr 資料夾) 或 ("convert", (原始資料夾, 分區, 輸出資料夾))。
    """
    p = Path(target_path.strip().strip('"')).resolve()
    if not p.exists():
        raise ValueError(f"路徑不存在: {target_path}")
    # 直接選到 Zarr 資料夾 (或其中的 .zarr)
    if p.name.endswith(".zarr") or [z for z in p.glob("*.zarr") if z.name not in NON_2D_ZARR]:
        return "ready", p
    # 先找原始 .nc (可能在下層)，才能判斷 output\ZARR 裡的轉檔結果是否過期
    partitions = dfm_convert.detect_dfm_partitions(str(p))
    if not partitions:
        if _find_zarr(p):  # 沒有 .nc，但下層有 Zarr 成果
            return "ready", p
        raise ValueError(f"在 {p} 中找不到 Zarr 成果，也找不到 Delft3D-FM 的 map.nc / fou.nc")
    if not any("map" in f for f in partitions.values()):
        raise ValueError("這個資料夾只有 fou.nc（最大值），沒有 map.nc（逐時水深），圖台無法播放時序。\n"
                         "如只需要最大淹水深度圖，請用 convert.bat 或 pixi run convert --cog-only 產製 GeoTIFF。")
    his = dfm_convert.detect_his_file(str(p), partitions)
    extra = ([his] + dfm_convert.find_structure_files(partitions, str(p))) if his else []
    done = _find_up_to_date(p, partitions, dfm_convert.source_signature(partitions, extra))
    if done:
        return "ready", done
    return "convert", (p, partitions, _writable_output(p, partitions))


def _run_conversion(src: Path, partitions: dict, out: Path):
    def progress(msg: str, frac: float):
        job.message, job.percent = msg, round(frac * 100, 1)

    try:
        dfm_convert.process_dfm_directory_pipeline(str(src), str(out), partitions=partitions, progress=progress)
        job.message = "載入轉檔結果…"
        scan_directory(str(out))
        job.state, job.message, job.percent = "done", "轉檔完成", 100.0
    except Exception as e:
        job.state, job.message = "error", f"轉檔失敗: {e}"


def open_folder(target_path: str, background: bool = True) -> str:
    """載入資料夾；需要轉檔時 background=True 在背景執行並回傳 "converting"，否則回傳 "ready"。"""
    action, info = prepare_folder(target_path)
    if action == "ready":
        scan_directory(str(info))
        return "ready"
    src, partitions, out = info
    with _job_lock:
        if job.state == "running":
            raise ValueError(f"目前正在轉檔 {job.source}，請等完成後再載入其他資料夾")
        job.state, job.message, job.percent = "running", "準備轉檔…", 0.0
        job.source, job.output = str(src), str(out)
    if background:
        threading.Thread(target=_run_conversion, args=(src, partitions, out), daemon=True).start()
        return "converting"
    _run_conversion(src, partitions, out)
    if job.state == "error":
        raise ValueError(job.message)
    return "ready"


@app.post("/api/catalog/scan")
def set_catalog_path(path: str = Form(...)):
    try:
        if open_folder(path) == "converting":
            return JSONResponse({"status": "converting", **vars(job)}, status_code=202)
        return get_catalog()
    except Exception as e:
        raise HTTPException(400, detail=str(e))


@app.get("/api/convert/status")
def convert_status():
    return vars(job)


@app.post("/api/browse")
def browse_folder(request: Request):
    """在執行圖台的這台電腦上跳出「選擇資料夾」視窗 (只允許本機連線使用)。"""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, detail="只有在執行圖台的電腦上才能瀏覽資料夾，請直接輸入路徑")
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="選擇成果資料夾（Zarr，或含 map.nc / fou.nc 的原始成果）", initialdir=catalog.current_path or None)
        root.destroy()
    except Exception as e:
        raise HTTPException(500, detail=f"無法開啟資料夾選擇視窗: {e}")
    return {"path": str(Path(path)) if path else ""}


# 淹水深度分級色階：(下限, 上限, RGB, 基準 alpha)
DEPTH_CLASSES = [
    (0.1, 0.3, (198, 219, 239), 178),   # 淺積水，預設隱藏
    (0.3, 0.5, (198, 219, 239), 102),
    (0.5, 1.0, (120, 180, 214), 102),
    (1.0, 2.0, (66, 146, 198), 102),
    (2.0, 3.0, (37, 81, 156), 102),
    (3.0, np.inf, (8, 48, 107), 102),
]
HAZARD_RGB = {3: (254, 178, 76), 4: (240, 59, 32), 5: (189, 0, 38)}


def colorize(depth: np.ndarray, opacity_scale: float, scheme: str, show_shallow: bool) -> np.ndarray:
    rgba = np.zeros(depth.shape + (4,), dtype="u1")
    for k, (lo, hi, rgb, alpha) in enumerate(DEPTH_CLASSES):
        if k == 0 and not show_shallow:
            continue
        m = (depth >= lo) & (depth < hi)
        if scheme == "hazard" and k in HAZARD_RGB:
            rgb = HAZARD_RGB[k]
        rgba[m, :3] = rgb
        rgba[m, 3] = int(np.clip(alpha * opacity_scale, 0, 255))
    return rgba


@app.get("/api/partition/{pid}/depth/{frame_idx}.png")
def get_partition_depth_png(pid: str, frame_idx: int, opacity_scale: float = 1.0,
                            color_scheme: str = "standard", show_shallow: bool = False):
    pinfo = catalog.partitions.get(pid)
    if pinfo is None:
        raise HTTPException(404, "找不到指定分區")
    try:
        ds = _open(pinfo.path)
        frame_idx = max(0, min(frame_idx, ds.sizes["time"] - 1))
        arr = ds[pinfo.var_name].isel(time=frame_idx).values.astype("f4")
        if ds.y.values[0] < ds.y.values[-1]:
            arr = arr[::-1]

        ny, nx = pinfo.shape
        output = np.full((ny, nx), np.nan, dtype="f4")
        reproject(
            arr, output,
            src_transform=Affine(*pinfo.transform), src_crs=pinfo.crs, src_nodata=np.nan,
            dst_transform=from_bounds(*pinfo.bounds_3857, nx, ny), dst_crs="EPSG:3857",
            dst_nodata=np.nan, resampling=Resampling.nearest,
        )
        buf = io.BytesIO()
        Image.fromarray(colorize(output, opacity_scale, color_scheme, show_shallow)).save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/query/point")
def query_point_timeseries(lng: float, lat: float):
    num_steps = len(catalog.times)
    composite = np.zeros(num_steps, dtype="f4")
    hits, hit_names = [], []
    native_xy = None

    for pid, pinfo in catalog.partitions.items():
        x, y = _transformer("EPSG:4326", pinfo.crs).transform(lng, lat)
        native_xy = native_xy or (x, y, pinfo.crs)
        xmin, ymin, xmax, ymax = pinfo.bounds_native
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            continue
        try:
            da = _open(pinfo.path)[pinfo.var_name].sel(x=x, y=y, method="nearest")
            series = da.values.astype("f4")
            if not np.isfinite(series).any():
                continue  # 點在分區外框內、但不在網格上
            gx, gy = float(da.x.values), float(da.y.values)
            series = np.nan_to_num(series, nan=0.0)
            n = min(num_steps, len(series))
            composite[:n] = np.maximum(composite[:n], series[:n])
            hits.append({
                "partition_id": pid,
                "partition_name": pinfo.name,
                "grid_coord_native": [round(gx, 2), round(gy, 2)],
                "distance_to_click_m": round(float(np.hypot(gx - x, gy - y)), 2),
                "water_depth_series": [round(float(v), 4) for v in series],
                "max_depth": round(float(series.max()), 4),
                "final_depth": round(float(series[-1]), 4),
            })
            hit_names.append(pinfo.name)
        except Exception as e:
            print(f"查詢分區 {pid} 失敗: {e}")

    x, y, crs = native_xy or (lng, lat, "EPSG:4326")
    return {
        "query_coord": {"lng": round(lng, 6), "lat": round(lat, 6),
                        "x_native": round(x, 2), "y_native": round(y, 2), "crs": crs},
        "times": catalog.times,
        "time_labels": catalog.time_labels,
        "composite_series": [round(float(v), 4) for v in composite],
        "composite_max_depth": round(float(composite.max()), 4) if num_steps else 0.0,
        "hit_count": len(hits),
        "hit_partitions": hit_names,
        "hits": hits,
    }


# ───────────────────────────── 1D 渠道／管線 ─────────────────────────────

def _mesh1d_ds() -> xr.Dataset:
    if not catalog.mesh1d_dir:
        raise HTTPException(404, "此成果沒有 1D 網路")
    return _open(str(Path(catalog.mesh1d_dir) / MESH1D_ZARR))


def _clean(values: np.ndarray, digits: int = 3) -> list:
    return [None if not np.isfinite(v) else round(float(v), digits) for v in values]


@app.get("/api/mesh1d/geometry")
def mesh1d_geometry():
    """1D 線段 GeoJSON (WGS84)；properties.id 對應 /api/mesh1d/frame 回傳陣列的位置。"""
    if not catalog.mesh1d_dir:
        raise HTTPException(404, "此成果沒有 1D 網路")
    path = Path(catalog.mesh1d_dir) / MESH1D_GEOJSON
    return Response(path.read_bytes(), media_type="application/geo+json",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/mesh1d/frame/{frame_idx}")
def mesh1d_frame(frame_idx: int, var: str = "waterdepth"):
    """某時間步各線段的數值；frame_idx = -1 時回傳全時段最大值 (流量取絕對值最大)。"""
    ds = _mesh1d_ds()
    if var not in ds:
        raise HTTPException(400, f"1D 成果中沒有變數 {var}")
    da = ds[var]
    if frame_idx < 0:
        vals = (np.abs(da) if var == "q1" else da).max("time", skipna=True).values
    else:
        vals = da.isel(time=max(0, min(frame_idx, ds.sizes["time"] - 1))).values
    return {"var": var, "values": _clean(vals)}


@app.get("/api/mesh1d/series/{edge_id}")
def mesh1d_series(edge_id: int):
    """某線段所有變數的逐時數值 (點擊線段時顯示)。"""
    ds = _mesh1d_ds()
    if not 0 <= edge_id < ds.sizes["edge"]:
        raise HTTPException(404, "找不到指定線段")
    sel = ds.isel(edge=edge_id)
    return {"time_labels": catalog.time_labels,
            "series": {k: _clean(sel[k].values, 4) for k in ds.data_vars}}


# ───────────────────────────── 觀測站、結構物 (his) ─────────────────────────────
# 各類別在時間軸上要著色的主要變數
HIS_MAIN_VAR = {"station": "waterdepth", "pump": "pump_structure_discharge"}


def _his_ds() -> xr.Dataset:
    if not catalog.his_dir:
        raise HTTPException(404, "此成果沒有觀測站／結構物資料")
    return _open(str(Path(catalog.his_dir) / HIS_ZARR))


def _his_main_var(ds: xr.Dataset, cat: str):
    v = HIS_MAIN_VAR.get(cat, f"{cat}_discharge")
    return f"{cat}__{v}" if f"{cat}__{v}" in ds else None


@app.get("/api/his/objects")
def his_objects():
    """所有觀測站／結構物的位置與檢核摘要 (GeoJSON)，另附類別統計與水收支。"""
    ds = _his_ds()
    with open(Path(catalog.his_dir) / HIS_GEOJSON, encoding="utf-8") as f:
        geo = json.load(f)
    return {"geojson": geo,
            "categories": json.loads(ds.attrs.get("categories", "{}")),
            "water_balance": json.loads(ds.attrs.get("water_balance", "{}"))}


@app.get("/api/his/frame/{frame_idx}")
def his_frame(frame_idx: int):
    """地圖時間步對應的各類別主要數值 (觀測站水深、抽水站／結構物流量)，供標記著色。"""
    ds = _his_ds()
    if not catalog.his_frame_index:
        raise HTTPException(404, "沒有可對應的時間")
    t = catalog.his_frame_index[max(0, min(frame_idx, len(catalog.his_frame_index) - 1))]
    out = {}
    for cat in json.loads(ds.attrs.get("categories", "{}")):
        var = _his_main_var(ds, cat)
        if var:
            out[cat] = _clean(ds[var].isel(time=t).values)
    return {"his_time_index": t, "values": out}


@app.get("/api/his/series/{cat}/{idx}")
def his_series(cat: str, idx: int):
    """單一物件全部變數的逐時數值 (點擊標記時顯示)。"""
    ds = _his_ds()
    if cat not in ds.dims or not 0 <= idx < ds.sizes[cat]:
        raise HTTPException(404, "找不到指定物件")
    labels = [t.strftime("%m-%d %H:%M") for t in pd.to_datetime(ds.time.values)]
    series = {}
    for name in ds.data_vars:
        if name.startswith(f"{cat}__"):
            v = ds[name]
            series[name.split("__", 1)[1]] = {"long_name": v.attrs.get("long_name", ""),
                                              "units": v.attrs.get("units", ""),
                                              "data": _clean(v.isel({cat: idx}).values, 4)}
    return {"time_labels": labels, "series": series}


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
