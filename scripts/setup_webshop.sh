#!/usr/bin/env bash
# Set up WebShop (princeton-nlp/WebShop) locally for scripts/webshop_eval.py.
#
# - Clones WebShop next to this repo (../webshop) at a pinned commit.
# - Applies scripts/webshop.patch: pure-Python BM25 search instead of the
#   Java/pyserini index, server bound to 127.0.0.1, 10k-product subset.
# - Creates a Python 3.10 venv with only the web app's dependencies.
# - Downloads product data. The original Google Drive links are dead; the
#   data comes from the Hugging Face mirror YWZBrandon/webshop-data.
#
# Then: (cd ../webshop && .venv/bin/python -m web_agent_site.app --attrs)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${WEBSHOP_DIR:-$REPO/../webshop}"
COMMIT=64fa2a5c15c7daa698b9ac93f5bb5437b634c9bd
MIRROR=https://huggingface.co/datasets/YWZBrandon/webshop-data/resolve/main

if [ ! -d "$DEST/.git" ]; then
  git clone -q https://github.com/princeton-nlp/WebShop.git "$DEST"
fi
cd "$DEST"
git checkout -q "$COMMIT"
git apply --check "$REPO/scripts/webshop.patch" 2>/dev/null && git apply "$REPO/scripts/webshop.patch"

uv venv -q --python 3.10 .venv
VIRTUAL_ENV=.venv uv pip install -q "flask==2.1.2" "werkzeug==2.2.2" beautifulsoup4 "cleantext==1.1.4" \
  "rank_bm25==0.2.2" rich thefuzz tqdm "spacy>=3.5,<3.8" "numpy<2" \
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"

mkdir -p data
curl -sfL -o data/items_human_ins.json "$MIRROR/items_human_ins.json"
curl -sfL -o data/items_shuffle_10000.json "$MIRROR/subsets/10k/items_shuffle_10000.json"
curl -sfL -o data/items_ins_v2_10000.json "$MIRROR/subsets/10k/items_ins_v2_10000.json"
echo "WebShop ready in $DEST (106 human tasks: fixed_0 .. fixed_105)"
