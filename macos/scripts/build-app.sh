#!/bin/sh
# Builds Sammy.app into macos/build: `scripts/build-app.sh [server-url]`. The server URL (default: the local dev
# server) is the one the app talks to until the user picks another on the sign-in screen.
#
# The app is signed ad hoc, for this Mac. To give it to users, sign it with a Developer ID and notarize it (the
# hardened runtime needs the entitlements this writes, for the microphone that dictation uses):
#   codesign --force --options runtime --entitlements build/Sammy.entitlements --sign "Developer ID Application: …" build/Sammy.app
#   xcrun notarytool submit … && xcrun stapler staple build/Sammy.app
set -eu
cd "$(dirname "$0")/.."
SERVER="${1:-https://35-188-200-101.sslip.io}"
VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"

swift build -c release --product Sammy
APP=build/Sammy.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/Sammy" "$APP/Contents/MacOS/Sammy"
cp -R Resources/Squirrel "$APP/Contents/Resources/"  # the mascot's loops (SammySquirrel.swift); make mascot renders them

ICONSET="$(mktemp -d)/AppIcon.iconset"
swift scripts/make-icon.swift "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sammy</string>
  <key>CFBundleDisplayName</key><string>Sammy</string>
  <key>CFBundleIdentifier</key><string>dev.pydantic.sammy</string>
  <key>CFBundleExecutable</key><string>Sammy</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>© Pydantic</string>
  <key>SammyServerURL</key><string>$SERVER</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSMicrophoneUsageDescription</key><string>Sammy listens while you dictate a message, and only then.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Sammy turns what you dictate into text for your message.</string>
</dict>
</plist>
EOF

# Dictation's microphone, for a signature with the hardened runtime (a Developer ID's); ad hoc it is not needed.
cat > build/Sammy.entitlements <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.device.audio-input</key><true/>
</dict>
</plist>
EOF

codesign --force --entitlements build/Sammy.entitlements --sign - "$APP"
echo "$APP"
