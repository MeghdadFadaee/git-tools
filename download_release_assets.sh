#!/usr/bin/env bash
set -euo pipefail

command -v curl >/dev/null 2>&1 || { echo "curl is required"; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "jq is required"; exit 1; }

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 owner/repo"
  exit 2
fi

REPO_FULL="$1"
OWNER="${REPO_FULL%%/*}"
REPO="${REPO_FULL##*/}"

read -r -p "Enter release tag (default: latest): " TAG_INPUT
REQUESTED_TAG="${TAG_INPUT:-latest}"

AUTH_ARGS=()
if [ -n "${GITHUB_TOKEN:-}" ]; then
  AUTH_ARGS=(-H "Authorization: token ${GITHUB_TOKEN}")
  echo "Using GITHUB_TOKEN"
fi

API_HEADERS=(-H "Accept: application/vnd.github.v3+json")

if [ "$REQUESTED_TAG" = "latest" ]; then
  API_URL="https://api.github.com/repos/${OWNER}/${REPO}/releases/latest"
else
  API_URL="https://api.github.com/repos/${OWNER}/${REPO}/releases/tags/${REQUESTED_TAG}"
fi

tmpfile="$(mktemp)"
http_status=$(curl -sSL -o "$tmpfile" -w "%{http_code}" \
  "${AUTH_ARGS[@]}" "${API_HEADERS[@]}" "$API_URL") || true

if [ "$http_status" -ne 200 ]; then
  msg=$(jq -r '.message // empty' "$tmpfile" 2>/dev/null || true)
  echo "Failed to fetch release (HTTP $http_status). $msg"
  rm -f "$tmpfile"
  exit 3
fi

# 🔑 resolve real tag name
REAL_TAG=$(jq -r '.tag_name' "$tmpfile")
if [ -z "$REAL_TAG" ] || [ "$REAL_TAG" = "null" ]; then
  echo "Could not resolve real tag name"
  rm -f "$tmpfile"
  exit 4
fi

OUTDIR="release-assets/${OWNER}/${REPO}/${REAL_TAG}"
mkdir -p "$OUTDIR"

assets_count=$(jq '.assets | length' "$tmpfile")
if [ "$assets_count" -eq 0 ]; then
  echo "No assets found for ${OWNER}/${REPO} (${REAL_TAG})"
  rm -f "$tmpfile"
  exit 0
fi

echo "Downloading $assets_count asset(s) into:"
echo "  $OUTDIR"

for i in $(seq 0 $((assets_count - 1))); do
  name=$(jq -r ".assets[$i].name" "$tmpfile")
  url=$(jq -r ".assets[$i].browser_download_url" "$tmpfile")

  echo
  echo "-> [$((i+1))/$assets_count] $name"

  curl -L -C - \
    -o "${OUTDIR}/${name}" \
    "${AUTH_ARGS[@]}" \
    -H "Accept: application/octet-stream" \
    "$url"
done

rm -f "$tmpfile"

echo
echo "Done ✅"
echo "Assets saved under:"
echo "release-assets/${OWNER}/${REPO}/${REAL_TAG}"
