#!/bin/sh
# Recreate the exact Homebrew formula patch from the pinned FFmpeg commit.
# Requires git and HTTPS access; refuses to replace an existing destination.
set -eu
if [ "$#" -ne 1 ]; then echo "usage: $0 OUTPUT-PATCH" >&2; exit 2; fi
out=$1
[ ! -e "$out" ] || { echo 'destination exists' >&2; exit 1; }
work=$(mktemp -d "${TMPDIR:-/tmp}/clippie-ffmpeg-patch.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
commit=a5d4c398b411a00ac09d8fe3b66117222323844c
git init --bare "$work/repo" >/dev/null
git -C "$work/repo" fetch --depth=2 https://github.com/FFmpeg/FFmpeg.git "$commit"
[ "$(git -C "$work/repo" rev-parse 'FETCH_HEAD^{commit}')" = "$commit" ]
git -C "$work/repo" -c core.abbrev=10 format-patch --encoding=utf8 --stdout --signature=2.25.1 -1 --root FETCH_HEAD > "$work/patch"
printf '%s  %s\n' 1dbbc1a4cf9834b3902236abc27fefe982da03a14bcaa89fb90c7c8bd10a1664 "$work/patch" | shasum -a 256 -c
cp "$work/patch" "$out"
