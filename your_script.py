# your_script.py
# -*- coding: utf-8 -*-
"""
トクバイ チラシ自動保存スクリプト（ID重複スキップ + 日付入りファイル名）
 - 複数チラシ対応
 - 表裏/複数面対応
 - MD5で重複画像排除
 - Dropbox: Refresh Token 方式
 - チラシが無い店舗は SKIP
 - アップロード前に「同じ leaflet_id のファイルが既にあるか」をDropbox全体（対象ディレクトリ内）でチェックしてスキップ
 - ファイル名: 〈企業名〉_〈YYYY-MM-DD〉_〈leaflet_id〉.pdf / _pN.png
"""

import os, re, time, datetime, pathlib, hashlib
from typing import List, Set, Iterable
from urllib.parse import urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup
from PIL import Image

import dropbox
from dropbox.files import WriteMode, ListFolderResult

# ========= 環境変数 =========
STORE_URL = os.getenv("STORE_URL") or ""
if not STORE_URL:
    raise RuntimeError("STORE_URL が未設定です")

STORE_CHAIN_OVERRIDE = os.getenv("STORE_CHAIN")
DROPBOX_ROOT = os.getenv("DROPBOX_ROOT", "/Tokubai")

parts = urlparse(STORE_URL).path.strip("/").split("/")
CHAIN_FROM_URL = parts[0] if parts else "unknown"
SHOP_ID = parts[1] if len(parts) > 1 else "unknown"

def sanitize(s: str) -> str:
    return s.replace("/", "／").replace("\\", "＼").strip()

CHAIN = sanitize(STORE_CHAIN_OVERRIDE or CHAIN_FROM_URL)
DROPBOX_BASE_DIR = f"{DROPBOX_ROOT}/{CHAIN}/{SHOP_ID}"         # ← この直下に保存
SAVE_DIR = "downloads"
MAX_LEAFLETS = 3

BASE_HOST = "https://tokubai.co.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TokubaiSaver/1.4)",
    "Accept-Language": "ja,en;q=0.8",
    "Referer": "https://tokubai.co.jp/",
}
TIMEOUT, RETRY = 30, 3

STORE_PATH = urlparse(STORE_URL).path.rstrip("/") + "/"
PRINT_RE   = re.compile(r"/leaflets/(\d+)/print/?$")
LEAFLET_RE = re.compile(r"/leaflets/(\d+)/?$")
IMG_EXTS   = (".png", ".jpg", ".jpeg", ".webp")

# ---------- HTTP ----------
def http_get(url: str) -> requests.Response:
    last = None
    for i in range(RETRY):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last

# ---------- Leaflet 検出 ----------
def build_print_url_from_id(leaflet_id: str) -> str:
    return urljoin(BASE_HOST, f"{STORE_PATH}leaflets/{leaflet_id}/print")

def extract_leaflet_ids_from_html(html: str) -> Set[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids: Set[str] = set()
    for tag in soup.find_all(["a", "meta", "link"]):
        href = tag.get("href") or tag.get("content")
        if not href:
            continue
        m = PRINT_RE.search(href) or LEAFLET_RE.search(href)
        if m:
            ids.add(m.group(1))
    return ids

def find_all_leaflet_print_urls() -> List[str]:
    ids: Set[str] = set()
    try:
        ids |= extract_leaflet_ids_from_html(http_get(STORE_URL).text)
    except Exception as e:
        print("店舗トップ取得失敗:", e)
    try:
        ids |= extract_leaflet_ids_from_html(http_get(urljoin(STORE_URL, "leaflets")).text)
    except Exception:
        print("leaflets ページなし → スキップ")

    if not ids:
        print("leaflet ID なし（この店舗は今日はチラシなしの可能性）")
        return []

    sorted_ids = sorted(ids, key=lambda x: int(x), reverse=True)[:MAX_LEAFLETS]
    return [build_print_url_from_id(i) for i in sorted_ids]

# ---------- 画像収集 ----------
def collect_images_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    cand = []
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy"):
            v = img.get(attr)
            if v and not v.startswith("data:"):
                cand.append(urljoin(BASE_HOST, v))
        srcset = img.get("srcset")
        if srcset:
            for part in [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]:
                if not part.startswith("data:"):
                    cand.append(urljoin(BASE_HOST, part))
    out, seen = [], set()
    for u in cand:
        if any(u.lower().split("?")[0].endswith(ext) for ext in IMG_EXTS):
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out

def enumerate_print_variants(base_print_url: str) -> Iterable[str]:
    vs = [base_print_url]
    for q in [{"page": "2"}, {"page": "3"}, {"surface": "back"}, {"surface": "2"}]:
        sep = "&" if "?" in base_print_url else "?"
        vs.append(f"{base_print_url}{sep}{urlencode(q)}")
    m = PRINT_RE.search(base_print_url)
    if m:
        i = m.group(1)
        vs += [
            urljoin(BASE_HOST, f"{STORE_PATH}leaflets/{i}/back/print"),
            urljoin(BASE_HOST, f"{STORE_PATH}leaflets/{i}/2/print"),
        ]
    seen = set()
    for v in vs:
        if v not in seen:
            seen.add(v)
            yield v

def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def download_leaflet_images(print_url: str, today: str) -> List[str]:
    pathlib.Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    leaflet_id = PRINT_RE.search(print_url).group(1) if PRINT_RE.search(print_url) else "unknown"

    saved, seen_hashes = [], set()
    for u in enumerate_print_variants(print_url):
        try:
            r = http_get(u)
        except Exception as e:
            print("print変種取得失敗:", u, e)
            continue
        imgs = collect_images_from_html(r.text)
        for url in sorted(imgs):
            data = http_get(url).content
            h = md5(data)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            fname = f"{CHAIN}_{today}_{leaflet_id}_p{len(saved) + 1}.png"
            fpath = os.path.join(SAVE_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            saved.append(fpath)
            print("saved:", fpath)
    return saved

def images_to_pdf(pngs: List[str], out_pdf: str):
    if not pngs:
        return
    pages = [Image.open(p).convert("RGB") for p in pngs]
    pages[0].save(out_pdf, save_all=True, append_images=pages[1:])
    print("pdf:", out_pdf)

# ---------- Dropbox ----------
def dropbox_client():
    """
    Refresh Token 方式を最優先。無ければ ACCESS_TOKEN にフォールバック。
    """
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
        "Dropbox 認証情報がありません（REFRESH_TOKEN/APP_KEY/APP_SECRET か ACCESS_TOKEN）。"
    )

def ensure_folder(dbx, path: str):
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError as e:
        if "conflict" in str(e).lower():
            return
        raise

def dropbox_any_entry_with_id(dbx: dropbox.Dropbox, base_dir: str, leaflet_id: str) -> bool:
    """
    base_dir 配下を再帰で走査し、ファイル名に _{leaflet_id} を含むものがあれば True。
    日付や拡張子が違っても '同じチラシIDは保存済み' と判定。
    """
    try:
        result: ListFolderResult = dbx.files_list_folder(base_dir, recursive=True)
    except dropbox.exceptions.ApiError as e:
        # フォルダが無い場合などは存在しないとみなす
        print("list_folder 失敗（初回作成の可能性）:", e)
        return False

    target = f"_{leaflet_id}"
    def hit(result: ListFolderResult) -> bool:
        for entry in result.entries:
            # ファイルのみ対象（フォルダは無視）
            if hasattr(entry, "name"):
                name = entry.name
                if target in name:
                    return True
        return False

    if hit(result):
        return True
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        if hit(result):
            return True
    return False

def upload_file(dbx, local_path: str, dropbox_path: str):
    with open(local_path, "rb") as f:
        dbx.files_upload(f.read(), dropbox_path, mode=WriteMode("add"), autorename=True, mute=True)
    print("uploaded:", dropbox_path)

# ---------- main ----------
def main():
    print_urls = find_all_leaflet_print_urls()
    if not print_urls:
        print(f"[SKIP] {CHAIN}/{SHOP_ID}: チラシなし。処理をスキップします。")
        return

    today = datetime.date.today().isoformat()
    dbx = dropbox_client()
    ensure_folder(dbx, DROPBOX_BASE_DIR)

    for purl in print_urls:
        m = PRINT_RE.search(purl)
        leaflet_id = m.group(1) if m else "leaflet"

        # === ここが「ID重複スキップ」 ===
        if dropbox_any_entry_with_id(dbx, DROPBOX_BASE_DIR, leaflet_id):
            print(f"[SKIP] 既に同じIDのファイルあり: leaflet_id={leaflet_id}")
            continue

        # まだ保存されていないIDだけダウンロード
        imgs = download_leaflet_images(purl, today)

        pdf_path = None
        if imgs:
            pdf_name = f"{CHAIN}_{today}_{leaflet_id}.pdf"     # ← 日付入り + ID
            pdf_path = os.path.join(SAVE_DIR, pdf_name)
            images_to_pdf(imgs, pdf_path)

        # アップロード（ベース直下に置く）
        if pdf_path:
            upload_file(dbx, pdf_path, f"{DROPBOX_BASE_DIR}/{os.path.basename(pdf_path)}")
        for p in imgs:
            # 画像も同じ命名規則で置く（必要なければこのforをコメントアウト可）
            upload_file(dbx, p, f"{DROPBOX_BASE_DIR}/{os.path.basename(p)}")

    print("Done.")

if __name__ == "__main__":
    main()
