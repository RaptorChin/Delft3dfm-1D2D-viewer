"""Windows-only: make sure GDAL/PROJ are loaded from this Python environment.

rasterio registers every PATH folder that contains a gdal*.dll as a DLL search
directory. If another GDAL (Miniforge, QGIS, OSGeo4W, PostgreSQL...) is on PATH,
its older gdal.dll can be loaded instead, giving
"ImportError: DLL load failed while importing _warp: The specified procedure
could not be found". This module removes foreign GDAL folders from PATH and
points GDAL_DATA / PROJ_DATA at this environment before rasterio is imported.
"""
import glob
import os
import sys


def _fix():
    if os.name != "nt":
        return
    prefix = os.path.normcase(os.path.abspath(sys.prefix))
    lib_bin = os.path.join(sys.prefix, "Library", "bin")
    if not os.path.exists(os.path.join(lib_bin, "gdal.dll")):
        return  # not a conda/pixi layout; leave everything alone

    kept, removed = [], []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if not p:
            continue
        ap = os.path.normcase(os.path.abspath(p))
        foreign = not ap.startswith(prefix)
        if foreign and glob.glob(os.path.join(p, "gdal*.dll")):
            removed.append(p)
        else:
            kept.append(p)
    os.environ["PATH"] = os.pathsep.join([lib_bin] + [p for p in kept
                                         if os.path.normcase(os.path.abspath(p)) != os.path.normcase(lib_bin)])
    if removed:
        print("[dfm_viewer] 已忽略 PATH 中其他 GDAL 目錄 / ignored foreign GDAL on PATH: "
              + "; ".join(removed))

    share = os.path.join(sys.prefix, "Library", "share")
    for var, sub in (("GDAL_DATA", "gdal"), ("PROJ_DATA", "proj"), ("PROJ_LIB", "proj")):
        target = os.path.join(share, sub)
        if os.path.isdir(target):
            os.environ[var] = target
    os.environ.pop("GDAL_DRIVER_PATH", None)

    # Pre-load this environment's gdal.dll by full path so later imports bind to it.
    try:
        os.add_dll_directory(lib_bin)
        import ctypes
        ctypes.WinDLL(os.path.join(lib_bin, "gdal.dll"))
    except Exception as exc:  # pragma: no cover
        print(f"[dfm_viewer] 預載 gdal.dll 失敗 / could not preload gdal.dll: {exc}")


_fix()
