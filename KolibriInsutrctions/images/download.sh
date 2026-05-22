#!/usr/bin/env bash
# Download Kolibri documentation screenshots for the teacher guide.
# Run from the images/ directory: bash download.sh

set -euo pipefail
cd "$(dirname "$0")"

BASE="https://kolibri.readthedocs.io/en/latest/_images"
IMAGES=(
  "create-account.png"
  "manage-users.png"
  "coach-type.png"
  "groups-home.png"
  "learner-groups.png"
  "lessons-home.png"
  "lesson-visible.png"
  "quizzes-home.png"
  "coach-home.png"
  "learners-home.png"
)

for name in "${IMAGES[@]}"; do
  if [[ -f "$name" && "${1:-}" != "--force" ]]; then
    echo "  ok    $name (already present)"
    continue
  fi
  echo "  fetch $name"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$name" "$BASE/$name"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$name" "$BASE/$name"
  else
    echo "Neither curl nor wget is installed." >&2
    exit 1
  fi
done

echo "All done."
