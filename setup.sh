#!/bin/sh
# Create .venv with the PDF renderer beamer_sort needs, then report on LaTeX.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "no $PY on PATH"; exit 1; }

if [ ! -d .venv ]; then
  echo "creating .venv with $("$PY" -V 2>&1) ..."
  "$PY" -m venv .venv
else
  echo ".venv already exists - reusing it"
fi

echo "installing requirements ..."
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

echo
./.venv/bin/python slidewinder.py --check || true

cat <<'EOF'

----------------------------------------------------------------
To use it:

    source .venv/bin/activate
    python slidewinder.py yourtalk.tex

(or without activating:  ./.venv/bin/python slidewinder.py yourtalk.tex)
----------------------------------------------------------------
EOF
