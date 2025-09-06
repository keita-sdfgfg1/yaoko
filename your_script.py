import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone
from PIL import Image
import dropbox

# ===== 共通設定 =====
JST = timezone(timedelta(hours=9))

# ----- HTTP -----
def http_get(url: str) -> requests.Response:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r

# ----- チラシ抽出 -----
def extract_leaflet_ids_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"/leaflets/(\d+)", a["href"])
        if m:
            ids.append(m.group(1))
    return list(set(ids))

def find_all_leaflet_print_urls(store_url: str):
    html = http_get(urljoin(store_url, "./leaflets")).text
    ids = extract_leaflet_ids_from_html(html)
    if not ids:
        raise RuntimeError("leaflet ID が見つかりませんでした")
    return [urljoin(store_url, f"./leaflets/{lid}/print") for lid in ids]

def download_print_images(print_url: str, outdir="downloads") -> list[str]:
    html = http_get(print_url).text
    soup = BeautifulSoup(html, "html.parser")
    imgs = [img["src"] for img in soup.find_all("img") if "src" in img.attrs]

    os.makedirs(outdir, exist_ok=True)
    saved = []
    for i, url in enumerate(imgs, 1):
        fn = os.path.join(outdir, f"{os.path.basename(print_url)}_p{i}.png")
        r = http_get(url)
        with open(fn, "wb") as f:
            f.write(r.content)
        print("saved:", fn)
        saved.append(fn)
    return saved

# ----- PDF生成（Pillowのみ使用） -----
def make_pdf(img_paths: list[str], out_pdf: str):
    if not img_paths:
        return
    imgs = [Image.open(p).convert("RGB") for p in img_paths]
    base, rest = imgs[0], imgs[1:]
    base.save(out_pdf, save_all=True, append_images=rest)
    print("pdf:", out_pdf)

# ----- Dropbox -----
def dropbox_client():
    rf = os.getenv("DROPBOX_REFRESH_TOKEN")
    ak = os.getenv("DROPBOX_APP_KEY")
    sk = os.getenv("DROPBOX_APP_SECRET")

    if rf and ak and sk:
        return dropbox.Dropbox(
            oauth2_refresh_token=rf,
            app_key=ak,
            app_secret=sk,
            timeout=90,
        )

    token = os.getenv("DROPBOX_ACCESS_TOKEN")
    if token:
        return dropbox.Dropbox(token, timeout=90)

    raise RuntimeError(
        "Dropbox 認証情報がありません。"
        "DROPBOX_REFRESH_TOKEN / DROPBOX_APP_KEY / DROPBOX_APP_SECRET "
        "または（非推奨）DROPBOX_ACCESS_TOKEN を設定してください。"
    )

def dropbox_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    try:
        dbx.files_get_metadata(path)
        return True
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.GetMetadataError):
            return False
        raise

def save_to_dropbox(store_name: str, leaflet_id: str, img_paths: list[str]):
    dbx = dropbox_client()
    date_str = datetime.now(JST).strftime("%Y-%m-%d")

    out_pdf = f"downloads/{store_name}_{date_str}_{leaflet_id}.pdf"
    make_pdf(img_paths, out_pdf)

    for fn in img_paths + [out_pdf]:
        dbx_path = f"/{store_name}/{os.path.basename(fn)}"
        if dropbox_exists(dbx, dbx_path):
            print("skip existing:", dbx_path)
            continue
        with open(fn, "rb") as f:
            dbx.files_upload(f.read(), dbx_path, mode=dropbox.files.WriteMode.overwrite)
        print("uploaded:", dbx_path)

# ----- Main -----
def main():
    store_url = os.environ.get("STORE_URL")
    if not store_url:
        raise RuntimeError("STORE_URL が指定されていません")

    store_name = store_url.split("/")[-2]
    print(f"===== {store_name} =====")

    print_urls = find_all_leaflet_print_urls(store_url)
    for url in print_urls:
        leaflet_id = re.search(r"/leaflets/(\d+)/print", url).group(1)
        img_paths = download_print_images(url)
        save_to_dropbox(store_name, leaflet_id, img_paths)

if __name__ == "__main__":
    main()
