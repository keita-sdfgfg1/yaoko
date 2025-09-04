# your_script.py
# -*- coding: utf-8 -*-
import os
import re
import time
import datetime
import pathlib
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup
from PIL import Image

import dropbox
from dropbox.files import WriteMode

# ========= ユーザー設定 =========
STORE_URL = "https://tokubai.co.jp/%E3%83%A4%E3%82%AA%E3%82%B3%E3%83%BC/14997/"
SAVE_DIR = "downloads"
DROPBOX_BASE_DIR = "/Tokubai/ヤオコー/14997"

# ========= 共通設定 =========
BASE_HOST = "https://tokubai.co.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TokubaiSaver/1.1)",
    "Accept-Language": "ja,en;q=0.8",
    "Referer": "https://tokubai.co.jp/",
    "Cache-Control": "no-cache",
}
TIMEOUT = 30
RETRY = 3

STORE_PATH = urlparse(STORE_URL).path.rstrip("/") + "/"
PRINT_RE = re.compile(r"/leaflets/(\d+)/print/?$")
LEAFLET_RE = re.compile(r"/leaflets/(\d+)/?$")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

def http_get(url: str) -> requests.Response:
    last = None
    for i in range(RETRY):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1.1 * (i + 1))
    raise last

def build_print_url_from_id(leaflet_id: str) -> str:
    return urljoin(BASE_HOST, f"{STORE_PATH}leaflets/{leaflet_id}/print")

def _extract_leaflet_print_from_html(html: str, base: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    # /.../leaflets/{id}/print
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = PRINT_RE.search(href)
        if m:
            return build_print_url_from_id(m.group(1)) if href.startswith("/leaflets/") else urljoin(base, href)
    # /leaflets/{id} → /print
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = LEAFLET_RE.search(href)
        if m:
            return build_print_url_from_id(m.group(1))
    # meta / link
    for tag in soup.find_all(["meta", "link"]):
        href = tag.get("content") or tag.get("href")
        if not href: continue
        m = PRINT_RE.search(href)
        if m:
            return build_print_url_from_id(m.group(1)) if href.startswith("/leaflets/") else urljoin(base, href)
        m = LEAFLET_RE.search(href)
        if m:
            return build_print_url_from_id(m.group(1))
    return None

def find_latest_print_url() -> str:
    fb = os.getenv("FALLBACK_PRINT_URL")
    if fb: return fb
    # 店舗トップ
    r = http_get(STORE_URL)
    url = _extract_leaflet_print_from_html(r.text, BASE_HOST)
    if url: return url
    # 一覧
    r2 = http_get(urljoin(STORE_URL, "./leaflets"))
    url = _extract_leaflet_print_from_html(r2.text, BASE_HOST)
    if url: return url
    raise RuntimeError("最新チラシのprint URLが見つかりませんでした")

def collect_images_from_html(html: str, base: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    cand: List[str] = []
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy"):
            v = img.get(attr)
            if v and not v.startswith("data:"):
                cand.append(urljoin(base, v))
        srcset = img.get("srcset")
        if srcset:
            for part in [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]:
                if part and not part.startswith("data:"):
                    cand.append(urljoin(base, part))
    # フィルタ & 重複除去
    out, seen = [], set()
    for u in cand:
        if any(u.lower().split("?")[0].endswith(ext) for ext in IMG_EXTS):
            if u not in seen:
                seen.add(u); out.append(u)
    return out

def enumerate_print_variants(base_print_url: str) -> List[str]:
    """表裏など複数面に対応するため、想定バリエーションを列挙して順に叩く"""
    variants: List[str] = []
    # 基本
    variants.append(base_print_url)
    # よくあるクエリ系
    for q in [{"page": "2"}, {"page": "3"}, {"surface": "back"}, {"surface": "2"}]:
        sep = "&" if "?" in base_print_url else "?"
        variants.append(f"{base_print_url}{sep}{urlencode(q)}")
    # パス系（存在すれば当たる）
    m = PRINT_RE.search(base_print_url)
    if m:
        leaflet_id = m.group(1)
        variants.append(urljoin(BASE_HOST, f"{STORE_PATH}leaflets/{leaflet_id}/back/print"))
        variants.append(urljoin(BASE_HOST, f"{STORE_PATH}leaflets/{leaflet_id}/2/print"))
    # 重複除去
    seen: Set[str] = set()
    uniq = []
    for v in variants:
        if v not in seen:
            seen.add(v); uniq.append(v)
    return uniq

def download_all_print_images(print_url: str) -> List[str]:
    """複数面（表裏）を含めて画像URLを総取得→保存"""
    pathlib.Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    m = PRINT_RE.search(print_url)
    leaflet_id = m.group(1) if m else "unknown"

    saved: List[str] = []
    found_any = False

    for u in enumerate_print_variants(print_url):
        try:
            r = http_get(u)
        except Exception:
            continue
        imgs = collect_images_from_html(r.text, BASE_HOST)
        if not imgs:
            continue
        found_any = True
        # URLで安定ソート
        for url in sorted(imgs):
            idx = len(saved) + 1
            fname = f"{today}_{leaflet_id}_p{idx}.png"
            fpath = os.path.join(SAVE_DIR, fname)
            data = http_get(url).content
            with open(fpath, "wb") as f:
                f.write(data)
            saved.append(fpath)
            print("saved:", fpath)

    if not found_any:
        raise RuntimeError("印刷ページから画像が取得できませんでした")
    return saved

def images_to_pdf(png_paths: List[str], out_pdf: str) -> None:
    if not png_paths: return
    pages = [Image.open(p).convert("RGB") for p in png_paths]
    pages[0].save(out_pdf, save_all=True, append_images=pages[1:])
    print("pdf:", out_pdf)

def dropbox_client() -> dropbox.Dropbox:
    token = os.getenv("DROPBOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("環境変数 DROPBOX_ACCESS_TOKEN が未設定です")
    return dropbox.Dropbox(token, timeout=90)

def ensure_folder(dbx: dropbox.Dropbox, path: str) -> None:
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError as e:
        if "path/conflict/" in str(e.error):
            return
        raise

def upload_file(dbx: dropbox.Dropbox, local_path: str, dropbox_path: str) -> None:
    with open(local_path, "rb") as f:
        dbx.files_upload(f.read(), dropbox_path, mode=WriteMode("add"), autorename=True, mute=True)
    print("uploaded:", dropbox_path)

def main():
    print_url = find_latest_print_url()
    print("print_url:", print_url)

    # ここで表裏ふくめて全部落とす
    saved_imgs = download_all_print_images(print_url)

    # PDF
    pdf_path = None
    if saved_imgs:
        leaflet_id = PRINT_RE.search(print_url).group(1) if PRINT_RE.search(print_url) else "leaflet"
        pdf_path = os.path.join(SAVE_DIR, f"{datetime.date.today().isoformat()}_{leaflet_id}.pdf")
        images_to_pdf(saved_imgs, pdf_path)

    # Dropbox
    dbx = dropbox_client()
    today_dir = f"{DROPBOX_BASE_DIR}/{datetime.date.today().isoformat()}"
    ensure_folder(dbx, DROPBOX_BASE_DIR)
    ensure_folder(dbx, today_dir)
    for p in saved_imgs:
        upload_file(dbx, p, f"{today_dir}/{os.path.basename(p)}")
    if pdf_path:
        upload_file(dbx, pdf_path, f"{today_dir}/{os.path.basename(pdf_path)}")
    print("Done.")

if __name__ == "__main__":
    main()
