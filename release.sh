#!/usr/bin/env bash
# Cut a Marketing Studio release: bump VERSION, build, sign, zip, tag, publish.
#
#   ./release.sh patch|minor|major|X.Y.Z [--notes FILE] [--dry-run] [--allow-branch] [--teamserver-image]
#
# Every copy of the app installed from a release checks GitHub for the newest
# one and offers it ("Update now"), so publishing here IS shipping to everyone.
#
# --notes FILE   the release notes people see in the update box. Without it the
#                notes are the commit subjects since the last tag - fine for a
#                quick release, but written notes read far better.
# --dry-run      build and zip into dist/ but change nothing: no commit, no tag,
#                no push, no GitHub release.
# --allow-branch release from a branch other than main.
# --teamserver-image  OPTIONAL, off by default: after the app release is out, also build the team server
#                image (teamserver/Dockerfile) for linux/amd64 and linux/arm64 with docker buildx and push
#                it to $TS_IMAGE:<version> and :latest. Needs docker with buildx and a prior
#                `docker login ghcr.io` with a token that may write packages. Without the flag nothing
#                about the app release changes. With --dry-run it only prints what it would run.
#                Multi-platform builds need a buildx builder that can do them, once: docker buildx create --use
#
# Must run on the Mac that holds the "Mattdaemon Local Signing" certificate (MSTUDIO_SIGN_ID).
# Installed copies only accept an update signed by the same certificate as
# themselves, so a release signed with anything else (or ad-hoc) would be
# refused by every one of them - this script refuses to publish one.
set -euo pipefail
cd "$(dirname "$0")"

BUMP="" NOTES_FILE="" DRY=0 ALLOW_BRANCH=0 TS_IMAGE_STEP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --notes) NOTES_FILE="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --allow-branch) ALLOW_BRANCH=1; shift ;;
        --teamserver-image) TS_IMAGE_STEP=1; shift ;;
        patch|minor|major) BUMP="$1"; shift ;;
        [0-9]*.[0-9]*.[0-9]*) BUMP="$1"; shift ;;
        *) echo "usage: $0 patch|minor|major|X.Y.Z [--notes FILE] [--dry-run] [--allow-branch] [--teamserver-image]" >&2; exit 2 ;;
    esac
done
[[ -n "$BUMP" ]] || { echo "usage: $0 patch|minor|major|X.Y.Z [--notes FILE] [--dry-run]" >&2; exit 2; }

REPO="Sivertsen0007/MarketingStudio"
ASSET="MarketingStudio-macOS-arm64.zip"
TS_IMAGE="ghcr.io/sivertsen0007/marketingstudio-teamserver"
TS_PLATFORMS="linux/amd64,linux/arm64"
SIGN_ID="${MSTUDIO_SIGN_ID:-Mattdaemon Local Signing}"

die() { echo "!! $*" >&2; exit 1; }

# ── preflight ──────────────────────────────────────────────────────────────
# A dry run changes nothing, so it only warns about these; a real release refuses.
strict() { if [[ "$DRY" == 1 ]]; then echo "!! (dry run, continuing) $*" >&2; else die "$*"; fi; }
if ! git rev-parse -q --verify HEAD >/dev/null; then
    strict "the repo has no commits yet - commit first"
    BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"
else
    { git diff --quiet && git diff --cached --quiet && [[ -z "$(git status --porcelain)" ]]; } \
        || strict "uncommitted changes - commit or stash them first"
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
fi
if [[ "$BRANCH" != "main" && "$ALLOW_BRANCH" != 1 ]]; then
    die "on branch $BRANCH - releases come from main (or pass --allow-branch)"
fi
# The optional team server image: refuse before anything changes when docker or buildx is missing.
if [[ "$TS_IMAGE_STEP" == 1 ]]; then
    command -v docker >/dev/null 2>&1 \
        || die "--teamserver-image needs docker, which is not installed here. Leave the flag off to release only the app."
    docker buildx version >/dev/null 2>&1 \
        || die "--teamserver-image needs docker buildx (docker buildx version failed). Leave the flag off to release only the app."
    [[ "$DRY" == 1 ]] || docker info >/dev/null 2>&1 \
        || die "--teamserver-image: the docker daemon is not running. Start it, or leave the flag off."
fi
security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_ID" \
    || die "signing identity \"$SIGN_ID\" is not on this Mac - releases must be signed with it"

CUR="$(tr -d '[:space:]' < VERSION)"
IFS=. read -r MA MI PA <<<"$CUR"
case "$BUMP" in
    patch) NEW="$MA.$MI.$((PA + 1))" ;;
    minor) NEW="$MA.$((MI + 1)).0" ;;
    major) NEW="$((MA + 1)).0.0" ;;
    *) NEW="$BUMP" ;;
esac
python3 - "$CUR" "$NEW" <<'EOF' || die "new version $NEW is not newer than $CUR"
import sys
a, b = (tuple(map(int, v.split("."))) for v in sys.argv[1:3])
sys.exit(0 if b > a else 1)
EOF
git rev-parse -q --verify "refs/tags/v$NEW" >/dev/null && die "tag v$NEW already exists"

PREV_TAG="$(git describe --tags --abbrev=0 2>/dev/null || true)"
NOTES="$(mktemp)"
trap 'rm -f "$NOTES"' EXIT
if [[ -n "$NOTES_FILE" ]]; then
    cp "$NOTES_FILE" "$NOTES"
else
    git log ${PREV_TAG:+"$PREV_TAG"..HEAD} --no-merges --format='- %s' > "$NOTES" 2>/dev/null || true
    [[ -s "$NOTES" || "$DRY" != 1 ]] || echo "- (dry run: no commits to list)" > "$NOTES"
fi
[[ -s "$NOTES" ]] || die "release notes are empty"

echo "==> Marketing Studio $CUR -> $NEW (from $BRANCH, previous tag ${PREV_TAG:-none})"
echo "---- notes ----"; cat "$NOTES"; echo "---------------"

# ── build ──────────────────────────────────────────────────────────────────
echo "$NEW" > VERSION
restore_version() { [[ "$DRY" == 1 ]] && echo "$CUR" > VERSION; true; }
trap 'restore_version; rm -f "$NOTES"' EXIT

./macapp/build.sh release
APP="macapp/build/Marketing Studio.app"
# -dvv: the Authority line only shows from the second level of detail. Read
# into a variable first so grep -q cannot cut codesign off under pipefail.
SIGINFO="$(codesign -dvv "$APP" 2>&1 || true)"
SIGREQ="$(codesign -d -r- "$APP" 2>&1 || true)"
[[ "$SIGINFO" == *"Authority=$SIGN_ID"* && "$SIGREQ" == *"certificate leaf"* ]] \
    || die "the build is not signed by \"$SIGN_ID\" - refusing to publish it"
[[ "$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Contents/Info.plist")" == "$NEW" ]] \
    || die "the build does not carry version $NEW"

mkdir -p dist
rm -f "dist/$ASSET"
ditto -c -k --keepParent "$APP" "dist/$ASSET"
SHA="$(shasum -a 256 "dist/$ASSET" | cut -d' ' -f1)"
echo "==> dist/$ASSET  sha256 $SHA"

# The optional team server image (only with --teamserver-image), run after the app release below.
TS_CMD=(docker buildx build --platform "$TS_PLATFORMS" -f teamserver/Dockerfile
        --label "org.opencontainers.image.source=https://github.com/$REPO"
        --label "org.opencontainers.image.version=$NEW"
        --label "org.opencontainers.image.licenses=MIT"
        -t "$TS_IMAGE:$NEW" -t "$TS_IMAGE:latest" --push .)
ts_image_cmd() { printf '%q ' "${TS_CMD[@]}"; }

if [[ "$DRY" == 1 ]]; then
    if [[ "$TS_IMAGE_STEP" == 1 ]]; then
        echo "==> Dry run: would build and push the team server image with:"
        echo "    $(ts_image_cmd)"
    fi
    echo "==> Dry run: nothing committed, tagged or published. VERSION is back at $CUR."
    exit 0
fi

# ── publish ────────────────────────────────────────────────────────────────
git add VERSION
git commit -q -m "Release v$NEW"
git tag -a "v$NEW" -F "$NOTES"
git push origin "$BRANCH"
git push origin "v$NEW"

if [[ -z "${GH_TOKEN:-}" ]]; then
    # git already talks to GitHub through the keychain; gh can borrow that token.
    GH_TOKEN="$(printf 'host=github.com\nprotocol=https\n\n' \
        | git -c credential.helper=osxkeychain credential fill 2>/dev/null \
        | sed -n 's/^password=//p')"
    export GH_TOKEN
fi
gh release create "v$NEW" "dist/$ASSET" -R "$REPO" \
    --title "Marketing Studio $NEW" --notes-file "$NOTES"
echo "==> Published v$NEW. Installed copies offer it at their next start, or within a day."

if [[ "$TS_IMAGE_STEP" == 1 ]]; then
    echo "==> Team server image $TS_IMAGE:$NEW (+ :latest) for $TS_PLATFORMS"
    if ! "${TS_CMD[@]}"; then
        die "the app release v$NEW is out, but the team server image was not pushed. Check \`docker login ghcr.io\`, then run from the v$NEW tag: $(ts_image_cmd)"
    fi
    echo "==> Pushed $TS_IMAGE:$NEW and :latest."
fi
