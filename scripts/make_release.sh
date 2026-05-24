#!/usr/bin/env bash
# Create or update a GitHub release on ternip_claude for the current
# iteration. Attaches artifacts collected by collect_artifacts.sh.
#
# Usage: scripts/make_release.sh <tag> <title> <body_file>
#   tag        = git tag (e.g. "hard_16")
#   title      = release title (one-line summary)
#   body_file  = path to markdown body (e.g. artifacts/hard_16/summary.md)

set -euo pipefail

TAG="${1:?usage: make_release.sh <tag> <title> <body_file>}"
TITLE="${2:?missing title}"
BODY_FILE="${3:?missing body file}"

REPO_DIR="/soe/esifferm/GitHub/ternip_claude"
ART_DIR="$REPO_DIR/artifacts/$TAG"

cd "$REPO_DIR"

# Ensure the tag exists locally; create if not. Tag on HEAD (which should
# already point at the commit with the submodule pointer update).
if ! git rev-parse "$TAG" >/dev/null 2>&1; then
    git tag -a "$TAG" -m "$TITLE"
    git push origin "$TAG"
fi

# Create or update the release
if gh release view "$TAG" >/dev/null 2>&1; then
    gh release edit "$TAG" --title "$TITLE" --notes-file "$BODY_FILE"
else
    gh release create "$TAG" --title "$TITLE" --notes-file "$BODY_FILE"
fi

# Attach artifacts
for asset in "$ART_DIR"/*.csv "$ART_DIR"/build.tar.gz; do
    [[ -f "$asset" ]] || continue
    gh release upload "$TAG" "$asset" --clobber
done

echo "Release $TAG ready: $(gh release view "$TAG" --json url -q .url)"
