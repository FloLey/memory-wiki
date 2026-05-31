#!/bin/sh
set -e

WIKI_ROOT="${WIKI_ROOT:-/srv/wiki}"

mkdir -p "$WIKI_ROOT"
git config --global --add safe.directory "$WIKI_ROOT" 2>/dev/null || true

# Seed the wiki only when the volume is empty, so redeploys never clobber data.
if [ -z "$(ls -A "$WIKI_ROOT" 2>/dev/null)" ]; then
  echo "Seeding empty wiki at $WIKI_ROOT"
  cp -a /app/seed/. "$WIKI_ROOT"/

  cd "$WIKI_ROOT"
  git init -q
  git config user.email "wiki-daemon@florent-lejoly.be"
  git config user.name "wiki-daemon"
  git add -A
  git commit -q -m "seed: initial wiki structure"
  echo "Wiki seeded and git initialized"
else
  echo "Wiki at $WIKI_ROOT already populated, skipping seed"
fi

exec "$@"
