"""Delft3D-FM Viewer and Converter Package"""
from . import _winenv  # noqa: F401  (必須在 import rasterio 之前執行，避免載入到其他軟體的 GDAL)

__version__ = "1.2.0"


def code_id() -> str:
    """程式碼 (.py) 內容的識別碼；改過程式後就不同，用來判斷背景執行中的圖台是不是舊版。"""
    import hashlib
    from pathlib import Path

    h = hashlib.md5()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]
