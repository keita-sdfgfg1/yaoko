# your_script.py
# -*- coding: utf-8 -*-
"""
最新のトクバイチラシ(印刷ページ)を自動検出し、
画像を保存→PDF化→Dropboxにアップロードするスクリプト

■ 事前に編集する定数
- STORE_URL         : 対象店舗トップURL
- DROPBOX_BASE_DIR  : Dropbox内の保存先ルート

■ 必須環境変数（GitHub Secretsに設定）
- DROPBOX_ACCESS_TOKEN

■ 任意環境変数（保険として使う）
- FALLBACK_PRINT_URL : 既知の印刷URL（検出に失敗したときに使用）
"""
import os
import re
import io
import time
import datetime
import pathlib
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image

# Dropbox SDK
import dropbox
from dropbox.files import WriteMode

# =========================================
# ★★ ここを自分の店舗に合わせて編集 ★★
# =========================================
STORE_URL = "https://tokubai.co.jp/%E3%83%A4%E3%82%AA%E3%82%B3%E3%83%BC/14997/"
SAVE_DIR = "downloads"  # ローカル保存先（Actions内の一時フォルダ）
DROPBOX_BASE_DIR = "/Tokubai/ヤオコー/14997"  # Dropbox側ルート

# =========================================
# 共通設定
# =========================================
BASE_HOST = "https://tokubai.co.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TokubaiSaver/1.0; +https://example.local)",
    "Accept-Language": "ja,en;q=0.8",
    "Referer": "https://tokubai.co.jp/",
    "Cache-Control": "no-cache",
}
TIMEOUT = 30
RETRY = 3

# =========================================
# HTTPユーティリティ
# =========================================
def http_get(url: str) -> requests.Response:
    last_exc = None
    for i in range(RETRY):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            time.sleep(1.2 * (i + 1))
    raise last_exc

# =========================================
# チラシURL検出
# =========================================
PRINT_RE = re.compile(r"/leaflets/(\d+)/print/?$")
LEAFLET_RE = re.compile(r"/leaflets/(\d+)/?$")

def _extract_leaflet_print_from_html(html: str, base: str) -> Optional[str]:
    """HTMLから /leaflets/{id}/print を最優先で拾い、無ければ /leaflets/{id} を拾って/printを付ける"""
    soup = BeautifulSoup(html, "html.parser")

    # 1st: 直接 /print があるリンク
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if PRINT_RE.search(href):
            return urljoin(base, href)

    # 2nd: /leaflets/{id} を拾って /print を付ける
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = LEAFLET_RE.search(href)
        if m:
            return urljoin(base, f"/leaflets/{m.group(1)}/print")

    # 3rd: meta og:url 等にIDが潜んでいる場合
    for tag in soup.find_all(["meta", "link"]):
        href = tag.get("content") or tag.get("href")
        if not href:
            continue
        if PRINT_RE.search(href):
            return urljoin(base, href)
        m = LEAFLET_RE.search(href)
        if m:
            return urljoin(base, f"/leaflets/{m.group(1)}/print")

    return None

def find_latest_print_url() -> str:
    # 0) 保険：環境変数で強制指定
    fb = os.getenv("FALLBACK_PRINT_URL")
    if fb:
        return fb

    # 1) 店舗トップを解析
    r = http_get(STORE_URL)
    url = _extract_leaflet_print_from_html(r.text, BASE_HOST)
    if url:
        return url

    # 2) /leaflets 一覧ページも見る
    leaflets_index = urljoin(STORE_URL, "./leaflets")
    try:
        r2 = http_get(leaflets_index)
        url = _extract_leaflet_print_from_html(r2.text, BASE_HOST)
        if url:
            return url
    except Exception:
        pass

    raise RuntimeError("最新チラシのprint URLが見つかりませんでした")

# =========================================
# 画像取得 & PDF化
# =========================================
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

def _collect_print_images(html: str, base: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    cand = []

    # <img src> / srcset / data-src などから候補収集
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy"):
            val = img.get(attr)
            if not val:
                continue
            if val.startswith("data:"):
                continue
            cand.append(urljoin(base, val))
        # srcset対応
        srcset = img.get("srcset")
        if srcset:
            parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
            for p in parts:
                if p and not p.startswith("data:"):
                    cand.append(urljoin(base, p))

    # 拡張子ベースでフィルタ＋重複排除
    seen = set()
    out = []
    for u in cand:
        if any(u.lower().split("?")[0].endswith(ext) for ext in IMG_EXTS):
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out

def download_print_images(print_url: str) -> List[str]:
    r = http_get(print_url)
    img_urls = _collect_print_images(r.text, BASE_HOST)

    pathlib.Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()

    m = PRINT_RE.search(print_url)
    leaflet_id = m.group(1) if m else "unknown"

    saved = []
    # 順序が保証されない場合もあるため、URLでソートしておく
    for idx, url in enumerate(sorted(img_urls), start=1):
        img_bytes = http_get(url).content
        fname = f"{today}_{leaflet_id}_p{idx}.png"
        fpath = os.path.join(SAVE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(img_bytes)
        saved.append(fpath)
        print(f"saved: {fpath}")
    return saved

def images_to_pdf(png_paths: List[str], out_pdf: str) -> None:
    if not png_paths:
        return
    # すべてRGBにして結合
    images = [Image.open(p).convert("RGB") for p in png_paths]
    cover, tail = images[0], images[1:]
    cover.save(out_pdf, save_all=True, append_images=tail)
    print(f"pdf: {out_pdf}")

# =========================================
# Dropbox アップロード
# =========================================
def dropbox_client() -> dropbox.Dropbox:
    token = os.getenv("DROPBOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("環境変数 DROPBOX_ACCESS_TOKEN が未設定です")
    return dropbox.Dropbox(token, timeout=90)

def ensure_folder(dbx: dropbox.Dropbox, path: str) -> None:
    """存在しない場合作成（既存ならそのまま）"""
    try:
        dbx.files_get_metadata(path)
    except dropbox.exceptions.ApiError:
        dbx.files_create_folder_v2(path)

def upload_file(dbx: dropbox.Dropbox, local_path: str, dropbox_path: str) -> None:
    with open(local_path, "rb") as f:
        data = f.read()
    dbx.files_upload(
        data,
        dropbox_path,
        mode=WriteMode("add"),   # 同名は自動リネーム
        autorename=True,
        mute=True,
    )
    print(f"uploaded: {dropbox_path}")

# =========================================
# メイン
# =========================================
def main():
    print_url = find_latest_print_url()
    print(f"print_url: {print_url}")

    saved_imgs = download_print_images(print_url)

    # PDF出力
    pdf_path = None
    if saved_imgs:
        m = PRINT_RE.search(print_url)
        leaflet_id = m.group(1) if m else "leaflet"
        pdf_path = os.path.join(
            SAVE_DIR, f"{datetime.date.today().isoformat()}_{leaflet_id}.pdf"
        )
        images_to_pdf(saved_imgs, pdf_path)

    # Dropboxへアップロード
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
