#!/usr/bin/env bash
# Build the release assets for a tag:
#
#     scripts/package_release.sh <tag> <outdir>
#
# writes <outdir>/gc-hub-<tag>.zip and <outdir>/gc-hub-<tag>.zip.sha256.
#
# This is the single source of truth for what a release contains. The GitHub
# workflow (.github/workflows/release.yml) calls it; tests/test_release_package.py
# tests it. The contract comes from the ASAPSV1 updater
# (coa-reviewer/deploy/updater/updater.py):
#   * one top-level folder (unpack() flattens it into releases\<tag>\)
#   * app.py, requirements.txt, VERSION (= the tag), templates/, static/
#   * a .sha256 file in `sha256sum` format naming the zip by its bare filename
#     (parse_sha256_file; a mismatch is a hard stop on the server)
#
# Source only. State is excluded by name rather than by trusting .gitignore,
# and the staged tree is checked again before zipping.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <tag> <outdir>" >&2
  exit 2
fi
TAG="$1"
OUT="$2"

# The tag becomes a directory name on the server (releases\<tag>\) and a
# filename here, so: v + MAJOR.MINOR.PATCH, optionally a -prerelease or +build
# suffix of letters, digits, dots and hyphens that does not end in '.' or '-'
# (Windows silently drops a trailing dot from a folder name).
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]*[0-9A-Za-z])?$ ]]; then
  echo "refusing tag '$TAG': expected vMAJOR.MINOR.PATCH, e.g. v1.2.3 or v1.2.3-rc.1" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="gc-hub-${TAG}"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/$NAME"
mkdir -p "$STAGE"

# An outdir inside the repo must not be packaged into the next build.
OUT_EXCLUDE=()
case "$OUT/" in
  "$ROOT/"*) OUT_EXCLUDE=(--exclude="/${OUT#"$ROOT/"}/") ;;
esac

# In a git checkout, only tracked files are candidates: a local build must not
# ship a scratch file that CI would never have seen. (Tracked but deleted
# files are skipped.) Without a usable .git, e.g. a plain copy of the tree,
# the whole working tree is the candidate set. Either way the exclude rules
# below are applied on top.
SRC="$ROOT"
top="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$top" ] && [ "$top" -ef "$ROOT" ]; then
  SRC="$WORK/tracked"
  mkdir -p "$SRC"
  git -C "$ROOT" ls-files -z --cached |
    while IFS= read -r -d '' f; do
      if [ -e "$ROOT/$f" ] || [ -L "$ROOT/$f" ]; then printf '%s\0' "$f"; fi
    done |
    rsync -a --from0 --files-from=- "$ROOT/" "$SRC/"
else
  echo "note: $ROOT is not a git checkout; packaging the whole working tree" >&2
fi

# Leading '/' anchors a pattern to the repo root; unanchored ones match at any
# depth. A trailing '/' matches directories only. --prune-empty-dirs drops the
# folders exclusions leave empty; git never tracks an empty folder, so a CI
# checkout has none to lose.
rsync -a --prune-empty-dirs \
  --exclude='/.git' --exclude='/.github/' --exclude='/dist/' \
  --exclude='/.venv/' --exclude='/venv/' --exclude='/env/' \
  --exclude='/tests/' --exclude='/docs/' --exclude='/scripts/' \
  --exclude='/exports/' --exclude='/.claude/' --exclude='node_modules/' \
  --exclude='__pycache__/' --exclude='*.py[cod]' \
  --exclude='.pytest_cache/' --exclude='.mypy_cache/' \
  --exclude='processed_cdf*' --exclude='[Cc][Dd][Ff]/' --exclude='*.[Cc][Dd][Ff]' \
  --exclude='*.csv' --exclude='*.csv.bak' --exclude='*.bak' --exclude='*.bak_*' \
  --exclude='*.pid' --exclude='*.log' --exclude='*.log.*' \
  --exclude='.*_cache.json' --exclude='.processed_index.json' \
  --exclude='/switch-requested' --exclude='/switch-accepted' --exclude='/switch-refused' \
  --exclude='/switching' --exclude='/staged.json' --exclude='/held-tags.json' \
  --exclude='/paused' --exclude='/VERSION' \
  --exclude='qbench.json' --exclude='qbenchlogin.txt' --exclude='.env' --exclude='.env.*' \
  --exclude='.secret_key' --exclude='credentials.json' --exclude='*.pem' --exclude='*.key' \
  --exclude='client_secret*.json' --exclude='service_account*.json' \
  --exclude='*.zip' --exclude='.DS_Store' --exclude='Thumbs.db' --exclude='desktop.ini' \
  ${OUT_EXCLUDE[@]+"${OUT_EXCLUDE[@]}"} \
  "$SRC/" "$STAGE/"

# The stamp comes from the tag. /healthz reports it and the updater compares it
# with the tag it staged; a stale value makes a good swap look like a failure.
printf '%s\n' "$TAG" > "$STAGE/VERSION"

# Positive control: what the updater and the hub need at runtime (v2: hub
# mode only), the admin CLI tools run on the server, and the agent sources the
# hub builds its agent package and installers from.
# (tests/test_release_package.py also checks every locally imported module.)
missing=0
for f in app.py requirements.txt VERSION templates/index.html templates/calibration.html \
         templates/admin_setup.html templates/hub_admin.html static \
         paths.py version.py instance.py settings.py distill.py \
         supervisor.py restart_update.py restart_policy.py \
         hub.py hub_admin.py store.py pipeline.py exports.py corrections.py \
         import_match.py instruments.py admin_auth.py ingest_api.py notifications.py \
         methods/__init__.py methods/d2887.py jobs/__init__.py jobs/load_folder.py \
         tools/load_folder.py tools/parity_report.py tools/import_dry_run.py \
         agent/build_package.py agent/requirements-agent.txt \
         agent/launcher.pyw agent/install.pyw agent/gc_agent/__init__.py; do
  if [ ! -e "$STAGE/$f" ]; then echo "MISSING from package: $f" >&2; missing=1; fi
done
[ "$missing" -eq 0 ] || { echo "refusing to package" >&2; exit 1; }

# Negative control: nothing that is state, secrets, or dev-only.
# Matches at any depth, so it also catches what an anchored exclude let through.
leaked="$(cd "$STAGE" && find . \( -iname '*.csv' -o -iname '*.pid' -o -iname '*.cdf' \
  -o -name '*.log' -o -name '*.log.*' -o -name '*.pyc' -o -name '__pycache__' \
  -o -name '.venv' -o -name 'venv' -o -name '.git' -o -name 'processed_cdf*' \
  -o -name 'qbench.json' -o -name 'switch-*' -o -name 'switching' \
  -o -name 'staged.json' -o -name 'held-tags.json' -o -name 'paused' \
  -o -name '.env' -o -name '.env.*' -o -name '.secret_key' -o -name 'credentials.json' \
  -o -iname 'client_secret*.json' -o -iname 'service_account*.json' \
  -o -iname '*.bak' -o -name '*.bak_*' -o -iname '*.pem' -o -iname '*.key' \) -print)"
if [ -n "$leaked" ]; then
  echo "state leaked into package:" >&2
  echo "$leaked" >&2
  exit 1
fi

ZIP="$OUT/${NAME}.zip"
SUM="$OUT/${NAME}.zip.sha256"
rm -f "$ZIP" "$SUM"          # zip appends to an existing archive
( cd "$WORK" && zip -q -r -X "$ZIP" "$NAME" )

# `<hex>  <bare filename>`, which is what both tools print when run from the
# directory holding the file.
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$OUT" && sha256sum "${NAME}.zip" > "${NAME}.zip.sha256" && sha256sum -c "${NAME}.zip.sha256" >/dev/null )
else
  ( cd "$OUT" && shasum -a 256 "${NAME}.zip" > "${NAME}.zip.sha256" && shasum -a 256 -c "${NAME}.zip.sha256" >/dev/null )
fi

cat "$SUM"
