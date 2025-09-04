name: Save Tokubai Leaflet to Dropbox

on:
  workflow_dispatch:
  schedule:
    - cron: "0 23 * * *"  # JST 08:00

jobs:
  run:
    runs-on: ubuntu-latest
    env:
      PYTHONUNBUFFERED: "1"
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install deps (verbose)
        run: |
          set -x
          python -V
          pip install -U pip
          pip install requests beautifulsoup4 pillow dropbox
          pip show requests beautifulsoup4 pillow dropbox

      - name: Check secret & env
        env:
          DROPBOX_ACCESS_TOKEN: ${{ secrets.DROPBOX_ACCESS_TOKEN }}
        run: |
          python - << 'PY'
          import os, sys
          token = os.getenv("DROPBOX_ACCESS_TOKEN")
          print("HAS_TOKEN:", bool(token))
          print("TOKEN_LEN:", len(token) if token else 0)
          PY

      - name: Run script (capture full traceback)
        env:
          DROPBOX_ACCESS_TOKEN: ${{ secrets.DROPBOX_ACCESS_TOKEN }}
          # ↓ 保険として一時的に固定URLを渡す（検出失敗でも動くように）
          FALLBACK_PRINT_URL: "https://tokubai.co.jp/%E3%83%A4%E3%82%AA%E3%82%B3%E3%83%BC/14997/leaflets/93706324/print"
        run: |
          python - << 'PY'
          import runpy, traceback, sys
          print("=== START your_script.py ===")
          try:
            runpy.run_path('your_script.py', run_name='__main__')
          except SystemExit as e:
            raise
          except:
            traceback.print_exc()
            sys.exit(1)
          print("=== END your_script.py ===")
          PY
