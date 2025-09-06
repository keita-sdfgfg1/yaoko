import os
import io
import requests
import dropbox
from bs4 import BeautifulSoup
from PIL import Image
from urllib.parse import urljoin
from datetime import datetime

# ========== 設定 ==========
STORE_URLS = [
    "https://tokubai.co.jp/スーパーアルプス/6674",
    "https://tokubai.co.jp/リオン・ドール/2297",
    "https://tokubai.co.jp/サミット/7303",
    "https://tokubai.co.jp/ヨークベニマル/9656",
    "https://tokubai.co.jp/三和/6845",
    "https://tokubai.co.jp/オギノ/8381",
    "https://tokubai.co.jp/ベルク/174354",
    "https://tokubai.co.jp/オオゼキ/15005",
    "https://tokubai.co.jp/コモディイイダ/7543",
    "https://tokubai.co.jp/マルエツ/3438",
]

DROPBOX_BASE_DIR = "/tokubai"
# ===========================

def http_get(url):
    r = requests.get(url)
    r.raise_for_status()
    return r

def extract_leaflet_ids_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    leaflet_links = soup.select("a[href*='/leaflets/']")
    ids = []
    for a in leaflet_links:
        href = a.get("href")
        if "/leaflets/" in href:
            leaflet_id = href.split("/leaflets/")[1].split("?")[0]
            ids.append(leaflet_id)
    return list(set(ids))

def find_all_leaflet_print_urls(store_url):
    res = http_get(urljoin(store_url, "./leaflets")).text
    ids = extract_leaflet_ids_from_html(res)
    if not ids:
        raise RuntimeError("leaflet ID が見つかりませんでした")
    return [urljoin(store_url, f"./leaflets/{lid}/print") for lid in ids]

def download_print_images(print_url):
    res = http_get(print_url)
    soup = BeautifulSoup(res.text, "html.parser")
    imgs = []
    for img_tag in soup.select("img"):
        img_url = img_tag.get("src")
        if not img_url.startswith("http"):
            img_url = urljoin(print_url, img_url)
        img_res = http_get(img_url)
        imgs.append(Image.open(io.BytesIO(img_res.content)))
    return imgs

# -------- Dropbox ----------
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

    raise RuntimeError("Dropbox 認証情報がありません")

def ensure_folder(dbx, path):
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError:
        pass  # 既に存在する場合は無視

def save_images_to_dropbox(imgs, store_name, leaflet_id):
    dbx = dropbox_client()
    today = datetime.now().strftime("%Y-%m-%d")  # 取得日を使用

    for i, img in enumerate(imgs, start=1):
        fname_base = f"{store_name}_{today}_{leaflet_id}_p{i}"
        png_name = f"{fname_base}.png"
        pdf_name = f"{fname_base}.pdf"

        dropbox_path_png = f"{DROPBOX_BASE_DIR}/{store_name}/{png_name}"
        dropbox_path_pdf = f"{DROPBOX_BASE_DIR}/{store_name}/{pdf_name}"

        # 既に同じファイルが存在すればスキップ
        try:
            dbx.files_get_metadata(dropbox_path_png)
            print("skip:", dropbox_path_png)
            continue
        except dropbox.exceptions.ApiError:
            pass

        # PNG 保存
        with io.BytesIO() as out:
            img.save(out, format="PNG")
            dbx.files_upload(out.getvalue(), dropbox_path_png, mode=dropbox.files.WriteMode("add"))
            print("saved:", dropbox_path_png)

        # PDF 保存
        with io.BytesIO() as out_pdf:
            img.save(out_pdf, format="PDF")
            dbx.files_upload(out_pdf.getvalue(), dropbox_path_pdf, mode=dropbox.files.WriteMode("add"))
            print("saved:", dropbox_path_pdf)

# -------- メイン --------
def main():
    for store_url in STORE_URLS:
        store_name = store_url.split("/")[3]  # 日本語の店舗名部分を使用
        print("=====", store_name, "=====")
        try:
            print_urls = find_all_leaflet_print_urls(store_url)
        except RuntimeError as e:
            print("leaflets ページなし → スキップ", e)
            continue

        for print_url in print_urls:
            print("print_url:", print_url)
            leaflet_id = print_url.split("/leaflets/")[1].split("/")[0]
            imgs = download_print_images(print_url)
            ensure_folder(dropbox_client(), f"{DROPBOX_BASE_DIR}/{store_name}")
            save_images_to_dropbox(imgs, store_name, leaflet_id)

if __name__ == "__main__":
    main()
