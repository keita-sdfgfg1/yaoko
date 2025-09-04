import os, re, datetime, pathlib, io
import requests
from bs4 import BeautifulSoup
from PIL import Image

# 追加: Dropbox SDK
import dropbox
from dropbox.files import WriteMode

STORE_URL = "https://tokubai.co.jp/%E3%83%A4%E3%82%AA%E3%82%B3%E3%83%BC/14997/"
SAVE_DIR = "downloads"
DROPBOX_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_BASE_DIR = "/Tokubai/ヤオコー/14997"  # 好きな保存先に変更OK

headers = {"User-Agent": "Mozilla/5.0"}

def find_latest_print_url():
    r = requests.get(STORE_URL, headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"/leaflets/\d+/print/?$", href):
            return ("https://tokubai.co.jp" + href) if href.startswith("/") else href
    raise RuntimeError("最新チラシのprint URLが見つかりませんでした")

def download_print_images(print_url):
    r = requests.get(print_url, headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    imgs = []
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src.startswith("/"):
            src = "https://tokubai.co.jp" + src
        imgs.append(src)

    pathlib.Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()

    m = re.search(r"/leaflets/(\d+)/print", print_url)
    leaflet_id = m.group(1) if m else "unknown"

    saved = []
    for i, url in enumerate(imgs, start=1):
        img_bytes = requests.get(url, headers=headers, timeout=30).content
        fname = f"{today}_{leaflet_id}_p{i}.png"
        fpath = os.path.join(SAVE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(img_bytes)
        saved.append(fpath)
    return saved

def images_to_pdf(png_paths, out_pdf):
    if not png_paths: return
    pil_imgs = []
    cover = Image.open(png_paths[0]).convert("RGB")
    for p in png_paths[1:]:
        pil_imgs.append(Image.open(p).convert("RGB"))
    cover.save(out_pdf, save_all=True, append_images=pil_imgs)

# --- ここからDropbox関連 ---
def dropbox_client():
    if not DROPBOX_TOKEN:
        raise RuntimeError("DROPBOX_ACCESS_TOKEN が未設定です")
    return dropbox.Dropbox(DROPBOX_TOKEN, timeout=60)

def upload_file_to_dropbox(dbx, local_path, dropbox_path):
    with open(local_path, "rb") as f:
        data = f.read()
    # 既存と同一ハッシュならスキップしたい場合は /files/get_metadata + content_hash 比較も可
    dbx.files_upload(data, dropbox_path, mode=WriteMode("add"), autorename=True, mute=True)

def ensure_folder(dbx, path):
    try:
        dbx.files_get_metadata(path)
    except dropbox.exceptions.ApiError:
        dbx.files_create_folder_v2(path)

if __name__ == "__main__":
    print_url = find_latest_print_url()
    saved_imgs = download_print_images(print_url)

    # PDF化（任意だが便利）
    pdf_path = None
    if saved_imgs:
        leaflet_id = re.search(r"/leaflets/(\d+)/print", print_url)
        leaflet_id = leaflet_id.group(1) if leaflet_id else "leaflet"
        pdf_path = os.path.join(
            SAVE_DIR, f"{datetime.date.today().isoformat()}_{leaflet_id}.pdf"
        )
        images_to_pdf(saved_imgs, pdf_path)

    # Dropboxへアップロード
    dbx = dropbox_client()
    # 例: /Tokubai/ヤオコー/14997/2025-09-04/
    dated_dir = f"{DROPBOX_BASE_DIR}/{datetime.date.today().isoformat()}"
    ensure_folder(dbx, DROPBOX_BASE_DIR)
    ensure_folder(dbx, dated_dir)

    for p in saved_imgs:
        fname = os.path.basename(p)
        upload_file_to_dropbox(dbx, p, f"{dated_dir}/{fname}")
        print("Uploaded:", fname)

    if pdf_path:
        upload_file_to_dropbox(dbx, pdf_path, f"{dated_dir}/{os.path.basename(pdf_path)}")
        print("Uploaded PDF:", os.path.basename(pdf_path))
