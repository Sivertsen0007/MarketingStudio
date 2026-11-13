#!/usr/bin/env bash
# Build "Marketing Studio.app" from the SPM output and bundle the Python engine
# (../engine) so the .app is self-contained: double-click, no external files.
# Pattern copied from Mattdaemon's macapp/build.sh.
set -euo pipefail

cd "$(dirname "$0")"

# Usage: ./build.sh [release|debug] [--install]
#   --install  also puts the app in ~/Applications (or $MSTUDIO_INSTALL_DIR),
#              quitting a running copy first (by its PID) and starting it again.
#
# Env:
#   MSTUDIO_SIGN_ID              signing identity (default "Mattdaemon Local Signing";
#                                ad-hoc if it is not on this Mac)
#   MSTUDIO_SKIP_IMPORT_CHECK=1  downgrade the "bundled engine imports" check to a
#                                warning (only while the engine is mid-refactor)
CONFIG="release"
INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        release|debug) CONFIG="$arg" ;;
        *) echo "usage: $0 [release|debug] [--install]" >&2; exit 2 ;;
    esac
done

APP_NAME="Marketing Studio.app"
EXE="MarketingStudio"

echo "==> swift build -c $CONFIG"
swift build -c "$CONFIG"
BIN_PATH="$(swift build -c "$CONFIG" --show-bin-path)"
OUT="build/$APP_NAME"

# Assemble and sign OUTSIDE the repo: ~/Documents is usually iCloud-synced, and
# the File Provider keeps re-adding extended attributes that make codesign
# refuse the bundle ("resource fork, Finder information ... not allowed").
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mstudio-build.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/$APP_NAME"
RES="$APP/Contents/Resources"

echo "==> Assembling $OUT (staged in $STAGE)"
mkdir -p "$APP/Contents/MacOS" "$RES"

# 1. Native binary + Info.plist + icon, version stamped from ../VERSION
cp "$BIN_PATH/$EXE" "$APP/Contents/MacOS/$EXE"
cp Resources/Info.plist "$APP/Contents/Info.plist"
VERSION="$(tr -d '[:space:]' < ../VERSION)"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist"
echo "==> Version $VERSION"
cp Resources/AppIcon.icns "$RES/AppIcon.icns"

# 2. The Python engine -> Contents/Resources/engine (tests, caches, plan docs,
#    and anything secret-shaped are left behind).
echo "==> Bundling engine into Contents/Resources/engine"
rsync -a \
    --exclude 'tests/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
    --exclude '*.md' --exclude '.env' --exclude '.env*' --exclude '*.env' \
    --exclude 'settings.json' --exclude '*_cache*.json' \
    ../engine/ "$RES/engine/"
echo "$VERSION" > "$RES/engine/VERSION"

# 2a. Nothing personal or secret travels with the app. Fail the build rather
#     than ship a token, a settings file or a data cache.
BAD="$(cd "$RES/engine" && find . -type f \( \
        -name '.env' -o -name '.env.*' -o -name '*.env' \
        -o -name 'settings*.json' -o -name '*cache*.json' -o -name 'state.json' \
        -o -name 'account_currency.json' -o -iname '*secret*' -o -iname '*credential*' \
        -o -iname '*token*.json' -o -name '*.pem' -o -name '*.p12' -o -name '*.key' \
        -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db' -o -name '*.sops.*' \
    \) -print)"
if [[ -n "$BAD" ]]; then
    echo "!! secret/settings/cache files ended up inside the app - refusing to build:" >&2
    echo "$BAD" | sed 's/^/     /' >&2
    exit 1
fi

# 2b. The bundled engine must compile and import. `import server` only defines
#     things (the HTTP server starts in main(), under __main__), so importing it
#     is a safe check that no module is missing. -B: no __pycache__ in the bundle.
echo "==> Verifying the bundled engine compiles"
( cd "$RES/engine" && python3 -B - <<'EOF'
import os, sys, py_compile
bad = []
for root, _, files in os.walk("."):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            try:
                compile(open(p, "rb").read(), p, "exec")
            except SyntaxError as e:
                bad.append("%s: %s" % (p, e))
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
EOF
)
echo "==> Verifying the bundled engine imports (import server, updater)"
IMPORT_HOME="$(mktemp -d "${TMPDIR:-/tmp}/mstudio-importhome.XXXXXX")"
if ( cd "$RES/engine" && HOME="$IMPORT_HOME" PYTHONDONTWRITEBYTECODE=1 python3 -B -c "import updater, server" ) 2>"$STAGE/import.err"; then
    echo "    ok"
else
    if [[ "${MSTUDIO_SKIP_IMPORT_CHECK:-}" == 1 ]]; then
        echo "!! WARNING: the bundled engine does not import (MSTUDIO_SKIP_IMPORT_CHECK=1, continuing):" >&2
        tail -3 "$STAGE/import.err" | sed 's/^/     /' >&2
    else
        echo "!! the bundled engine does not import - a module is missing from engine/:" >&2
        tail -5 "$STAGE/import.err" | sed 's/^/     /' >&2
        rm -rf "$IMPORT_HOME"
        exit 1
    fi
fi
rm -rf "$IMPORT_HOME"
find "$RES/engine" -name '__pycache__' -type d -prune -exec rm -rf {} +

# 3. Codesign. A stable identity keeps macOS privacy grants across rebuilds and
#    is what the in-app updater checks a download against (same certificate).
xattr -cr "$APP" 2>/dev/null || true
SIGN_ID="${MSTUDIO_SIGN_ID:-Mattdaemon Local Signing}"
if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_ID"; then
    echo "==> Codesigning as \"$SIGN_ID\""
    codesign --force --deep --sign "$SIGN_ID" "$APP"
else
    echo "==> Ad-hoc codesigning (signing identity \"$SIGN_ID\" not found)"
    echo "    This copy will not be able to verify (or install) in-app updates."
    codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

# 4. Hand the signed app over to build/. ditto keeps the signature intact.
rm -rf "$OUT"
mkdir -p "$(dirname "$OUT")"
ditto "$APP" "$OUT"
codesign --verify --deep "$OUT"
echo "==> Done: $OUT"

if [[ "$INSTALL" != 1 ]]; then
    echo "Run with: open \"$OUT\"   (or \"$OUT/Contents/MacOS/$EXE\" for console logs)"
    echo "Or rebuild with --install to put it in ~/Applications."
    exit 0
fi

# 5. Install from the clean staged bundle (never from build/, which may have
#    picked up iCloud attributes).
DEST_DIR="${MSTUDIO_INSTALL_DIR:-$HOME/Applications}"
DEST="$DEST_DIR/$APP_NAME"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP/Contents/Info.plist")"

# The running copy, found by its exact executable path, then quit by PID.
running_pid() {
    ps -axo pid=,comm= | awk -v exe="$DEST/Contents/MacOS/$EXE" '{pid=$1; $1=""; sub(/^ /,""); if ($0 == exe) print pid}' | head -1
}
WAS_RUNNING=0
PID="$(running_pid)"
if [[ -n "$PID" ]]; then
    WAS_RUNNING=1
    echo "==> Quitting the running Marketing Studio (pid $PID)"
    osascript -e "quit app id \"$BUNDLE_ID\"" >/dev/null 2>&1 || kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$PID" 2>/dev/null; then
        echo "!! Marketing Studio (pid $PID) did not quit - close it and run again" >&2
        exit 1
    fi
fi

echo "==> Installing to $DEST"
mkdir -p "$DEST_DIR"
rm -rf "$DEST"
ditto --noextattr --noqtn "$APP" "$DEST"
xattr -rd com.apple.FinderInfo "$DEST" 2>/dev/null || true
codesign --verify --deep --strict "$DEST"

if [[ "$WAS_RUNNING" == 1 ]]; then
    echo "==> Starting it again"
    open "$DEST"
fi
echo "==> Installed: $DEST"
