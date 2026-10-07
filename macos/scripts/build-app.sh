#!/bin/sh
# Builds Monty.app into macos/build: `scripts/build-app.sh [server-url]`. The server URL (default: the local dev
# server) is the one the app talks to until the user picks another on the sign-in screen.
#
# The app is signed ad hoc, for this Mac. To give it to users, sign it with a Developer ID and notarize it:
#   codesign --force --options runtime --sign "Developer ID Application: …" build/Monty.app
#   xcrun notarytool submit … && xcrun stapler staple build/Monty.app
set -eu
cd "$(dirname "$0")/.."
SERVER="${1:-https://35-188-200-101.sslip.io}"
VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"

swift build -c release --product Monty
APP=build/Monty.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/Monty" "$APP/Contents/MacOS/Monty"
cp -R Resources/Squirrel "$APP/Contents/Resources/"  # the mascot's loops (MontySquirrel.swift); make mascot renders them

ICONSET="$(mktemp -d)/AppIcon.iconset"
swift scripts/make-icon.swift "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Monty</string>
  <key>CFBundleDisplayName</key><string>Monty</string>
  <key>CFBundleIdentifier</key><string>dev.pydantic.monty</string>
  <key>CFBundleExecutable</key><string>Monty</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>© Pydantic</string>
  <key>MontyServerURL</key><string>$SERVER</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
EOF

codesign --force --sign - "$APP"
echo "$APP"
