# -*- coding: utf-8 -*-
import os
import re
import time
from typing import List, Optional
from urllib.parse import quote
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
import dropbox
from datetime import datetime, timedelta, timezone

# ===== 基本設定 =====
JST = timezone(timedelta(hours=9))
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}
TIMEOUT = 30
RETRY = 3
DOWNLOAD_DIR = "downloads"  # ローカルの一時保存

# ===== HTTPユーティリティ =====
def http_get(url: str, referer: Optional[str] = None, retry: int = RETRY) -> requests.Response:
    """UA/Referer付き + リトライ付き GET"""
    sess = requests.Session()
    last_err = None
    for i in range(retry):
        try:
            headers = HEADERS_BASE.copy()
            if referer:
                # 日本語URLをそのままヘッダに入れないようにする
                safe_referer = referer.encode("utf-8")
                headers["Referer"] = quote(referer, safe=":/?#[]@!$&'()*+,;=")
            r = sess.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            last_err = e
            code = getattr(e.response, "status_code", None)
            if code in (403, 404, 429, 500, 502, 503, 504):
                time.sleep(1.2 * (i + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (i + 1))
    raise last_err

# ===== チラシ ID 抽出 =====
LEAFLET_ID_RE = re.compile(r"/leaflets/(\d+)")

def extract_leaflet_ids_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for a in soup.find_all("a", href=True):
        m = LEAFLET_ID_RE.search(a["href"])
        if m:
            ids.add(m.group(1))
    # 新しいIDが先頭になるように降順で返す
    return sorted(ids, key=lambda x: int(x), reverse=True)

def find_all_leaflet_print_urls(store_url: str) -> List[str]:
    """/leaflets が 403/404 なら店舗トップから抽出にフォールバック"""
    ids: List[str] = []
    # 1) /leaflets
    try:
        html = http_get(urljoin(store_url, "./leaflets"), referer=store_url).text
        ids = extract_leaflet_ids_from_html(html)
    except Exception as e:
        print("leaflets ページ取得失敗 → トップから抽出に切替:", e)

    # 2) トップから抽出（1で見つからなかった場合）
    if not ids:
        try:
            top = http_get(store_url).text
            ids = extract_leaflet_ids_from_html(top)
        except Exception as e2:
            print("トップ取得も失敗:", e2)

    # 3) それでも空ならこの店舗はSKIP
    if not ids:
        print("[SKIP] チラシIDが見つかりませんでした:", store_url)
        return []

    return [urljoin(store_url, f"./leaflets/{lid}/print") for lid in ids]

# ===== 画像ダウンロード =====
def collect_image_urls_from_print(html: str, base_url: str) -> List[str]:
    """printページ内の画像URL（絶対URL）を収集"""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        # 絶対URLへ
        absurl = urljoin(base_url, src)
        # 拡張子でざっくり絞る
        if any(absurl.lower().split("?")[0].endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")):
            urls.append(absurl)
    # 重複除去を維持
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def download_print_images(print_url: str) -> List[str]:
    """printページから画像をダウンロードしてローカルに保存し、パス一覧を返す"""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    html = http_get(print_url, referer=print_url).text
    img_urls = collect_image_urls_from_print(html, print_url)
    if not img_urls:
        print("printページで画像が見つからず:", print_url)

    saved = []
    for idx, u in enumerate(img_urls, 1):
        ext = os.path.splitext(urlparse(u).path)[1] or ".png"
        local = os.path.join(DOWNLOAD_DIR, f"leaflet_{os.getpid()}_{idx}{ext}")
        r = http_get(u, referer=print_url)
        with open(local, "wb") as f:
            f.write(r.content)
        saved.append(local)
        print("saved image:", local)
    return saved

# ===== PDF作成（Pillowのみ） =====
def make_pdf(img_paths: List[str], out_pdf: str):
    if not img_paths:
        return
    imgs = [Image.open(p).convert("RGB") for p in img_paths]
    base, rest = imgs[0], imgs[1:]
    base.save(out_pdf, save_all=True, append_images=rest)
    print("made pdf:", out_pdf)

# ===== Dropbox =====
def dropbox_client():
    rf = os.getenv("DROPBOX_REFRESH_TOKEN")
    ak = os.getenv("DROPBOX_APP_KEY")
    sk = os.getenv("DROPBOX_APP_SECRET")
    if rf and ak and sk:
        return dropbox.Dropbox(
            oauth2_refresh_token=rf, app_key=ak, app_secret=sk, timeout=90
        )
    token = os.getenv("DROPBOX_ACCESS_TOKEN")
    if token:
        return dropbox.Dropbox(token, timeout=90)
    raise RuntimeError(
        "Dropbox 認証情報がありません。"
        "DROPBOX_REFRESH_TOKEN / DROPBOX_APP_KEY / DROPBOX_APP_SECRET "
        "（推奨）または DROPBOX_ACCESS_TOKEN を設定してください。"
    )

def ensure_folder(dbx: dropbox.Dropbox, path: str):
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError as e:
        # 既存は正常
        if "conflict" in str(e).lower():
            return
        # それ以外は再スロー
        raise

def any_file_with_id_exists(dbx: dropbox.Dropbox, base_dir: str, leaflet_id: str) -> bool:
    """base_dir配下(再帰)に leaflet_id を含む名前のファイルがあれば True"""
    try:
        result = dbx.files_list_folder(base_dir, recursive=True)
    except dropbox.exceptions.ApiError:
        return False
    needle = f"_{leaflet_id}"
    def hit(res):
        for e in res.entries:
            if hasattr(e, "name") and needle in e.name:
                return True
        return False
    if hit(result):
        return True
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        if hit(result):
            return True
    return False

def upload(dbx: dropbox.Dropbox, local_path: str, dropbox_path: str):
    with open(local_path, "rb") as f:
        dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode("add"))
    print("uploaded:", dropbox_path)

# ===== Main =====
def main():
    store_url = os.environ.get("STORE_URL")
    if not store_url:
        raise RuntimeError("STORE_URL が未設定です")

    # 企業（チェーン）名はURLの1セグメント目、日本語OK。店舗IDは次のセグメント。
    path_parts = urlparse(store_url).path.strip("/").split("/")
    chain_name = path_parts[0] if len(path_parts) >= 1 else "store"
    shop_id = path_parts[1] if len(path_parts) >= 2 else "shop"
    store_name = chain_name   # 保存先のフォルダ名はチェーン名でOK

    print(f"===== {store_name} / {shop_id} =====")
    print_urls = find_all_leaflet_print_urls(store_url)
    if not print_urls:
        return  # この店舗は今日はチラシなし

    # Dropbox 準備
    dbx = dropbox_client()
    base_dir = f"/{store_name}"
    ensure_folder(dbx, base_dir)

    for purl in print_urls:
        m = LEAFLET_ID_RE.search(purl)
        leaflet_id = m.group(1) if m else "leaflet"
        # 既に同じIDのファイルが存在するならスキップ
        if any_file_with_id_exists(dbx, base_dir, leaflet_id):
            print(f"[SKIP] 既に同じIDのファイルあり: {leaflet_id}")
            continue

        # 画像取得
        imgs = download_print_images(purl)
        if not imgs:
            print("[SKIP] 画像無し:", purl)
            continue

        # JST日付入りのファイル名でアップロード
        today = datetime.now(JST).strftime("%Y-%m-%d")

        # PNG
        for i, p in enumerate(imgs, 1):
            ext = os.path.splitext(p)[1] or ".png"
            fname = f"{store_name}_{today}_{leaflet_id}_p{i}{ext}"
            upload(dbx, p, f"{base_dir}/{fname}")

        # PDF
        out_pdf_local = os.path.join(DOWNLOAD_DIR, f"{store_name}_{today}_{leaflet_id}.pdf")
        make_pdf(imgs, out_pdf_local)
        upload(dbx, out_pdf_local, f"{base_dir}/{os.path.basename(out_pdf_local)}")

    print("Done.")

if __name__ == "__main__":
    main()

