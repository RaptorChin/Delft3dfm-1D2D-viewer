"""Delft3D-FM Viewer 伺服器入口：自動避開被占用的連接埠、可指定預設載入的 Zarr 資料夾。

用法 (在專案資料夾):
    pixi run viewer                                 # 開啟圖台後，在網頁上選擇資料夾
    pixi run viewer --data "D:\\成果\\output"          # 啟動時直接載入 (原始 .nc 資料夾會自動轉檔)
    pixi run viewer --host 0.0.0.0                  # 開放區網內其他電腦連線 (請注意資料保密)
"""
import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def find_available_port(host: str, start_port: int = 8088, max_attempts: int = 50) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"無法在 {start_port} 至 {start_port + max_attempts} 範圍內找到可用連接埠。")


def disable_quick_edit():
    """
    關閉 Windows 命令視窗的「快速編輯模式」。
    開啟時，只要在黑色視窗裡點一下就會進入「選取」狀態，程式下一次輸出文字時會被凍結，
    圖台就停住不回應 (看起來像當掉，要關掉重開)。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except Exception:
        pass  # 不是在命令視窗中執行 (例如輸出被導到檔案)，略過


def wait_until_ready(port: int, timeout: float = 300.0) -> bool:
    """等到伺服器真的能連線為止 (第一次啟動可能因防毒掃描而較慢)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def announce_when_ready(url: str, port: int, lan_url: str, open_browser: bool, opener=webbrowser.open):
    if not wait_until_ready(port):
        print("⚠️ 伺服器啟動逾時，請關閉此視窗後重新執行。", flush=True)
        return
    print("=" * 60)
    print("🚀 Delft3D-FM 多分區水理成果展示圖台已啟動！")
    print(f"👉 圖台網址: {url}")
    if lan_url:
        print(f"🌐 區網其他電腦請用: {lan_url}")
    print("   結束程式請在此視窗按 Ctrl + C（使用期間請勿關閉此視窗）")
    print("=" * 60, flush=True)
    if open_browser:
        try:
            opener(url)
        except Exception:
            pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    disable_quick_edit()

    parser = argparse.ArgumentParser(description="Delft3D-FM 多分區水理成果展示圖台")
    parser.add_argument("--data", default=None,
                        help="啟動時載入的資料夾：Zarr 成果，或 Delft3D-FM 原始成果 (會自動轉檔)")
    parser.add_argument("--port", type=int, default=8088, help="連接埠 (被占用時自動往後找，預設 8088)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="監聽位址；預設只有本機能開，填 0.0.0.0 可讓區網其他電腦連線")
    parser.add_argument("--no-browser", action="store_true", help="啟動時不自動開啟瀏覽器")
    parser.add_argument("--warmup", action="store_true",
                        help="只載入一次所有套件後結束 (第一次安裝後執行，讓防毒掃描與快取在設定階段完成)")
    args = parser.parse_args()

    # 載入 GDAL 等套件需要幾秒；第一次執行時防毒軟體會掃描新安裝的檔案，可能需要 1～2 分鐘。
    # 先顯示訊息，避免使用者以為畫面當掉而關閉。
    print("⏳ 圖台啟動中，請稍候…（第一次執行可能需要 1～2 分鐘，請勿關閉此視窗）", flush=True)

    src_path = Path(__file__).resolve().parent.parent
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    t0 = time.time()
    import uvicorn
    from dfm_viewer.api import app, catalog, open_folder

    if args.warmup:
        print(f"✅ 啟動準備完成（{time.time() - t0:.0f} 秒）", flush=True)
        return

    if args.data:
        try:
            open_folder(args.data, background=False)  # 原始 .nc 資料夾會先自動轉檔
            print(f"✅ 已載入 {len(catalog.partitions)} 個分區: {catalog.current_path}")
        except Exception as e:
            print(f"⚠️ 無法載入 {args.data}: {e}\n   (可在網頁上重新選擇資料夾)")

    port = find_available_port(args.host, args.port)
    url = f"http://127.0.0.1:{port}/"
    lan_url = f"http://{socket.gethostbyname(socket.gethostname())}:{port}/" if args.host == "0.0.0.0" else ""

    # 伺服器真的可以連線後才顯示網址、開瀏覽器 (原本固定等 1.5 秒，啟動較慢時瀏覽器會先開到空白頁)
    threading.Thread(target=announce_when_ready, args=(url, port, lan_url, not args.no_browser),
                     daemon=True).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
