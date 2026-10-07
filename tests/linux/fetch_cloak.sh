#!/bin/sh
# Download a free CloakBrowser build from GitHub Releases, verify it, and unpack it (montybot/browser/cloak.md):
#
#   tests/linux/fetch_cloak.sh DEST [PLATFORM]    # PLATFORM: linux-x64 (default) or darwin-arm64
#
# Prints the binary's path, for MONTYBOT_CLOAK_BINARY. Its license forbids redistributing it: never commit it or put
# it in an image we publish. Only the free builds below; no license key, no sign-in.
#
# Checked twice, as their wrapper does: the archive's SHA-256 against the pin here, and the release's SHA256SUMS
# against CloakHQ's Ed25519 signature (SHA256SUMS.sig) and that same pin. Needs curl, openssl 3 and tar.
set -eu
dest=$1
platform=${2:-linux-x64}
case $platform in
    linux-x64)
        version=146.0.7680.177.5
        sha256=4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e
        binary=chrome ;;
    darwin-arm64)
        version=145.0.7632.109.2
        sha256=505582aa1bd3971c577f70e0cbbe016431702bdb693529abfd943b5bd9120c1c
        binary=Chromium.app/Contents/MacOS/Chromium ;;
    *) echo "no free build pinned for $platform" >&2; exit 1 ;;
esac
# BINARY_SIGNING_PUBKEYS in their wrapper's config.py, as an SPKI public key.
public_key='MCowBQYDK2VwAyEAMKFKwIhUcKWq5xTuNA0Ovg99njcDEcEJvmWYYhApvaU='
release=https://github.com/CloakHQ/cloakbrowser/releases/download/chromium-v$version
archive=cloakbrowser-$platform.tar.gz

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for file in "$archive" SHA256SUMS SHA256SUMS.sig; do
    curl -fsSL --retry 3 -o "$work/$file" "$release/$file"
done
actual=$( (sha256sum "$work/$archive" 2>/dev/null || shasum -a 256 "$work/$archive") | cut -d' ' -f1)
[ "$actual" = "$sha256" ] || { echo "$archive: SHA-256 $actual, expected $sha256" >&2; exit 1; }
printf -- '-----BEGIN PUBLIC KEY-----\n%s\n-----END PUBLIC KEY-----\n' "$public_key" > "$work/key.pem"
openssl base64 -d -A -in "$work/SHA256SUMS.sig" -out "$work/signature"
openssl pkeyutl -verify -pubin -inkey "$work/key.pem" -rawin -in "$work/SHA256SUMS" -sigfile "$work/signature" >&2
grep -qx "version=$version" "$work/SHA256SUMS"
grep -qx "$sha256  $archive" "$work/SHA256SUMS"

mkdir -p "$dest"
tar -xzf "$work/$archive" -C "$dest"
if [ "$platform" = darwin-arm64 ]; then
    xattr -cr "$dest"  # downloaded files are quarantined; Gatekeeper would refuse to start it
fi
echo "$(cd "$dest" && pwd)/$binary"
