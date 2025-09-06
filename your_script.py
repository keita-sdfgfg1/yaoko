import os
import re
import time
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from PIL import Image
import dropbox
from datetime import datetime, timedelta, timezone

# -------- 共通ヘッダー --------
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

# -------- HTTPユーティリティ --------
def http_get(url: str, referer: str | None = None, timeout: int = 30, retry: int = 3) -> requests.Response:
    sess = requests.Session()
    last_err = None
    for i in range(retry):
        try:
            headers = HEADERS_BASE.copy()
            if referer:
                headers["Referer"] = referer
            r = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            last_err = e
            if r.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (i + 1))
    raise last_err

# -------- Leaflet 抽出 --------
def extract_leaflet_ids_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/leaflets/" in href:
            try:
                part = href.split("/leaflets/")[1]
                lid = part.split("/")[0].split("?")[0]
                if lid.isdigit():
                    ids.add(lid)
            except Exception:
                pass
    return sorted(ids, key=lambda x: int(x), reverse=True)

def find_all_leaflet_print_urls(store_url: str) -> list[str]:
    ids: list[str] = []
    try:
        html = http_get(urljoin(store_url, "./leaflets"), referer=store_url).text
        ids = extract_leaflet_ids_from_html(html)
    except Exception as e:
        print("leaflets 直アクセス失敗 → トップから抽出:", e)
        try:
            top = http_get(store_url).text
            ids = extract_leaflet_ids_from_html(top)
        except Exception as e2:
            print("トップ取得も失敗:", e2)

    if not ids:
        raise RuntimeError("leaflet ID が見つかりませんでした")

    return [urljoin(store_url, f"./leaflets/{lid}/print") for lid in ids]

# -------- 画像ダウンロード --------
def download_print_images(print_url: str) -> list[str]:
    r = http_get(print_url, referer=print_url)
    soup = BeautifulSoup(r.text, "html.parser")
    imgs = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if src and ("pages" in src or "image" in src):
            imgs.append(src)
    return imgs

# -------- Dropbox クライアント --------
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
        "Dropbox 認証情報がありません。\n"
        "DROPBOX_REFRESH_TOKEN / DROPBOX_APP_KEY / DROPBOX_APP_SECRET "
        "（推奨）または DROPBOX_ACCESS_TOKEN を設定してください。"
    )

# -------- 保存処理 --------
def save_to_dropbox(img_paths: list[str], store_name: str, leaflet_id: str):
    dbx = dropbox_client()

    # JST日付
    JST = timezone(timedelta(hours=9))
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # フォルダ作成
    base_dir = f"/{store_name}"
    try:
        dbx.files_create_folder_v2(base_dir)
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.CreateFolderError):
            pass
        else:
            raise

    saved = []
    for i, path in enumerate(img_paths, 1):
        ext = os.path.splitext(path)[1]
        fname = f"{store_name}_{today_str}_{leaflet_id}_p{i}{ext}"
        dropbox_path = f"{base_dir}/{fname}"

        # 既に同じファイル名が存在する場合はスキップ
        try:
            dbx.files_get_metadata(dropbox_path)
            print("skip (already exists):", dropbox_path)
            continue
        except dropbox.exceptions.ApiError:
            pass

        with open(path, "rb") as f:
            dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode("add"))
        print("saved:", dropbox_path)
        saved.append(dropbox_path)

    # PDF作成
    if saved:
        out_pdf = f"{store_name}_{today_str}_{leaflet_id}.pdf"
        pdf_path = f"{base_dir}/{out_pdf}"
        if not dropbox_file_exists(dbx, pdf_path):
            make_pdf(img_paths, out_pdf)
            with open(out_pdf, "rb") as f:
                dbx.files_upload(f.read(), pdf_path, mode=dropbox.files.WriteMode("add"))
            print("pdf:", pdf_path)

def dropbox_file_exists(dbx, path: str) -> bool:
    try:
        dbx.files_get_metadata(path)
        return True
    except dropbox.exceptions.ApiError:
        return False

# -------- PDF化 --------
def make_pdf(img_paths: list[str], out_pdf: str):
    # すべてRGBにして1つのPDFへ
    pages = [Image.open(p).convert("RGB") for p in img_paths]
    if not pages:
        return
    first, rest = pages[0], pages[1:]
    first.save(out_pdf, save_all=True, append_images=rest)

# -------- Main --------
def main():
    store_url = os.environ.get("STORE_URL")
    store_name = os.environ.get("STORE_NAME", "store")

    print(f"===== {store_name} =====")
    print_urls = find_all_leaflet_print_urls(store_url)
    for print_url in print_urls:
        # leaflet ID 抽出
        leaflet_id = print_url.split("/leaflets/")[1].split("/")[0]
        img_urls = download_print_images(print_url)

        img_paths = []
        for i, url in enumerate(img_urls, 1):
            fn = f"tmp_{leaflet_id}_{i}.png"
            r = http_get(url, referer=print_url)
            with open(fn, "wb") as f:
                f.write(r.content)
            img_paths.append(fn)

        save_to_dropbox(img_paths, store_name, leaflet_id)

if __name__ == "__main__":
    main()

