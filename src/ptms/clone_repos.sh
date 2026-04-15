#!/usr/bin/env bash
set -euo pipefail

# -------------------------------------------------------------
# clone_repos.sh — clone/refresh GitHub repositories from a CSV
# -------------------------------------------------------------
#
# USAGE
#   ./clone_repos.sh --db {db_name}
#                    [--csv PATH_TO_CSV]
#                    [--token GITHUB_TOKEN]
#                    [--depth N]
#                    [-h|--help]
#
# REQUIRED
#   --db {db_name}
#       Selects the storage folder to mirror RawFileConfig
#
# OPTIONS
#   --csv PATH_TO_CSV
#       CSV file containing at least the columns:
#         - git_url   (e.g., git://github.com/org/repo.git or https://github.com/org/repo.git)
#         - full_name (e.g., org/repo)
#
#   --token GITHUB_TOKEN
#       Use an access token for authenticated HTTPS clones/fetches.
#       The script injects the token into the HTTPS URL as:
#         https://<TOKEN>@github.com/org/repo.git
#
#   --depth N
#       Shallow clone depth. Use 0 for full history.
#       Default: 0
#
# DESTINATION LAYOUT
#   Root path resolves exactly like RawFileConfig
#
# BEHAVIOR
#   - Converts git://github.com/... URLs to https://github.com/... automatically.
#   - If the repo directory already exists (has .git), performs:
#       git fetch --all --tags --prune
#     otherwise performs a fresh clone (honoring --depth).
#   - Idempotent and safe to re-run; existing repos are updated, not recloned.
#
# EXAMPLES
#   # Open-source dataset (maps to storage/raw_data)
#   ./clone_repos.sh --db <db_name> \
#     --csv <path_to_csv>
#
#   # Authenticated cloning (token from environment)
#   ./clone_repos.sh --db <db_name> \
#     --csv <path_to_csv> \
#     --token "$GITHUB_TOKEN"
#
#   # Shallow clone with depth 5
#   ./clone_repos.sh --db <db_name> --depth 5
#
# EXIT CODES
#   0  Success
#   1+ Error (e.g., missing arguments, CSV not found, git failure)
#
# -------------------------------------------------------------
DB_CHOICE=""
CSV_PATH="<path_to_csv>"
GH_TOKEN=""
DEPTH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      DB_CHOICE="${2:-}"; shift 2;;
    --csv)
      CSV_PATH="${2:-}"; shift 2;;
    --token)
      GH_TOKEN="${2:-}"; shift 2;;
    --depth)
      DEPTH="${2:-0}"; shift 2;;
    -h|--help)
      grep -E '^#' "$0" | sed -E 's/^# ?//'; exit 0;;
    *)
      echo "Unknown argument: $1" >&2; exit 1;;
  esac
done

if [[ -z "$DB_CHOICE" ]]; then
  echo "Error: --db is required <db_name>" >&2
  exit 1
fi

FOLDER="<path_to_folder_based_on_db>"

# Root path: two levels up from current working directory, then storage/<folder>
ROOT_BASE="$(dirname "$(dirname "$PWD")")"
ROOT_PATH="$ROOT_BASE/storage/$FOLDER"
REPOS_DIR="$ROOT_PATH/repos"

mkdir -p "$REPOS_DIR"

echo "[info] DB: $DB_CHOICE"
echo "[info] Storage folder: $FOLDER"
echo "[info] Root path: $ROOT_PATH"
echo "[info] Repos directory: $REPOS_DIR"
echo "[info] CSV path: $CSV_PATH"
echo "[info] Depth: ${DEPTH} (0 = full history)"
[[ -n "$GH_TOKEN" ]] && echo "[info] Using GitHub token for authenticated HTTPS clones"

# use Python's csv to safely parse and emit "git_url<TAB>full_name"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required to parse the CSV safely." >&2
  exit 1
fi

# Read rows and iterate
# ---------- FIXED SECTION START ----------

# Pre-check CSV
if [[ ! -f "$CSV_PATH" ]]; then
  echo "Error: CSV file not found at: $CSV_PATH" >&2
  exit 1
fi

# Absolute CSV path (robust against working dir changes)
if command -v realpath >/dev/null 2>&1; then
  CSV_ABS="$(realpath -m "$CSV_PATH")"
else
  # Fallback for systems without realpath
  pushd "$(dirname "$CSV_PATH")" >/dev/null
  CSV_ABS="$(pwd)/$(basename "$CSV_PATH")"
  popd >/dev/null
fi

TMP_TSV="$(mktemp)"
trap 'rm -f "$TMP_TSV"' EXIT

# Use Python's csv to safely parse and emit "git_url<TAB>full_name"
python3 - "$CSV_ABS" > "$TMP_TSV" <<'PYCODE'
import csv, sys
from pathlib import Path

csv_path = Path(sys.argv[1])
with csv_path.open(newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f, skipinitialspace=True)
    # Accepts header names: git_url, full_name (as in your sample)
    # Skips rows missing either field
    for row in reader:
        git_url = (row.get('git_url') or '').strip()
        full_name = (row.get('full_name') or '').strip()
        if git_url and full_name:
            print(f"{git_url}\t{full_name}")
PYCODE

# Function to normalize git:// to https:// and inject token if provided
normalize_url() {
  local raw_url="$1" token="$2"

  # Convert git://github.com/user/repo.git -> https://github.com/user/repo.git
  local https_url
  https_url="${raw_url/git:\/\/github.com/https://github.com}"

  # Inject token if provided
  if [[ -n "$token" ]]; then
    # Strip leading scheme and any existing auth, then rebuild
    local without_scheme="${https_url#https://}"
    without_scheme="${without_scheme#*@}"
    https_url="https://${token}@${without_scheme}"
  fi

  echo "$https_url"
}

# Loop over repos
while IFS=$'\t' read -r GIT_URL FULL_NAME; do
  [[ -z "$GIT_URL" || -z "$FULL_NAME" ]] && continue

  # Destination directory: ROOT_PATH/repos/<owner>/<repo>
  DEST_DIR="$REPOS_DIR/$FULL_NAME"
  mkdir -p "$(dirname "$DEST_DIR")"

  # Normalize URL and possibly inject token
  CLONE_URL="$(normalize_url "$GIT_URL" "$GH_TOKEN")"

  # Shallow flag
  if [[ "$DEPTH" -gt 0 ]]; then
    DEPTH_ARGS=(--depth "$DEPTH")
  else
    DEPTH_ARGS=()
  fi

  if [[ -d "$DEST_DIR/.git" ]]; then
    echo "[update] $FULL_NAME already exists. Fetching updates..."
    git -C "$DEST_DIR" remote set-url origin "$CLONE_URL" || true
    git -C "$DEST_DIR" fetch --all --tags --prune
  else
    echo "[clone] $FULL_NAME -> $DEST_DIR"
    git clone "${DEPTH_ARGS[@]}" "$CLONE_URL" "$DEST_DIR"
    if [[ "$DEPTH" -gt 0 ]]; then
      git -C "$DEST_DIR" fetch --tags --prune
    fi
  fi
done < "$TMP_TSV"

echo "[done] All repositories processed."
