#!/usr/bin/env bash
set -euo pipefail

reference=$1
alias=$2

image_digest() {
  local manifest
  manifest=$(docker buildx imagetools inspect "$1" --format '{{json .Manifest}}') || return
  python3 -c 'import json, sys; print(json.load(sys.stdin)["digest"])' <<<"$manifest"
}

expected=$(image_digest "$reference")
if ! current=$(image_digest "$alias" 2>&1); then
  if ! grep -qiE 'manifest unknown|name unknown|: not found$' <<<"$current"; then
    printf '%s\n' "$current" >&2
    exit 1
  fi
  current=
fi

if [[ "$expected" != "$current" ]]; then
  docker buildx imagetools create --tag "$alias" "$reference"
fi
