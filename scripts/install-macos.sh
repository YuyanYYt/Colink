#!/bin/bash
# Inspectable ZIP installer: explicit apply, checksum, no overwrite or cleanup.
set -euo pipefail

colink_archive=""
colink_expected=""
colink_destination=""
colink_apply=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --archive|--sha256|--destination)
      [ "$#" -ge 2 ] || { echo "Missing option value" >&2; exit 2; }
      case "$1" in
        --archive) colink_archive="$2" ;;
        --sha256) colink_expected="$2" ;;
        --destination) colink_destination="$2" ;;
      esac
      shift 2 ;;
    --apply) colink_apply=true; shift ;;
    *) echo "Usage: install-macos.sh --archive ZIP --sha256 HASH --destination ABSOLUTE_DIR [--apply]" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || {
  echo "This installer supports Apple Silicon macOS only" >&2; exit 1;
}
colink_major=$(sw_vers -productVersion | cut -d. -f1)
[ "$colink_major" -ge 14 ] || { echo "macOS 14 or newer is required" >&2; exit 1; }
[[ "$colink_expected" =~ ^[0-9a-f]{64}$ ]] || { echo "Provide a verified lowercase SHA-256" >&2; exit 2; }
[[ "$colink_destination" = /* ]] && [ "$colink_destination" != / ] || {
  echo "Choose an absolute applications directory, not filesystem root" >&2; exit 2;
}
[ -f "$colink_archive" ] && [ ! -L "$colink_archive" ] || { echo "ZIP archive missing or linked" >&2; exit 1; }
[ ! -L "$colink_destination" ] || { echo "Destination must not be a symlink" >&2; exit 1; }
colink_target="$colink_destination/Colink.app"
[ ! -e "$colink_target" ] && [ ! -L "$colink_target" ] || {
  echo "Colink.app already exists; preserve it and choose another destination" >&2; exit 1;
}
colink_actual=$(shasum -a 256 "$colink_archive" | awk '{print $1}')
[ "$colink_actual" = "$colink_expected" ] || { echo "Checksum mismatch; nothing installed" >&2; exit 1; }
unzip -tq "$colink_archive" >/dev/null || { echo "Invalid ZIP; nothing installed" >&2; exit 1; }
while IFS= read -r colink_entry; do
  # ZIP directory members normally end in one slash; remove that terminator
  # before testing path segments, without accepting doubled separators.
  colink_entry=${colink_entry%/}
  case "$colink_entry" in
    Colink.app|Colink.app/*) ;;
    *) echo "Unexpected archive root; nothing installed" >&2; exit 1 ;;
  esac
  case "/$colink_entry/" in
    *"/../"*|*"/./"*|*"//"*) echo "Unsafe archive path; nothing installed" >&2; exit 1 ;;
  esac
done < <(zipinfo -1 "$colink_archive")

echo "Verified archive: $colink_archive"
echo "Install target: $colink_target"
echo "No credentials, source-folder access, remote connection, startup settings or security bypass."
if ! $colink_apply; then
  echo "Dry run only. After user confirmation, repeat with --apply."
  exit 0
fi

colink_stage=$(mktemp -d "${TMPDIR:-/tmp}/colink-install.XXXXXX")
echo "Retained staging directory: $colink_stage"
ditto -x -k "$colink_archive" "$colink_stage"
codesign --verify --deep --strict "$colink_stage/Colink.app"
colink_name=$(plutil -extract CFBundleName raw "$colink_stage/Colink.app/Contents/Info.plist")
[ "$colink_name" = Colink ] || { echo "Unexpected application identity" >&2; exit 1; }
mkdir -p "$colink_destination"
# Reserve the exact target rather than overwriting a racing install.
mkdir "$colink_target"
ditto "$colink_stage/Colink.app" "$colink_target"
codesign --verify --deep --strict "$colink_target"
echo "Installed: $colink_target"
echo "Open it manually. Existing user data and staging files have been preserved."
