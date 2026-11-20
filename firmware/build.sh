#!/bin/sh
# Build the AButton firmware with the WICED Studio 6.0 20735-B0 SDK.
#
#   firmware/build.sh                 # release build
#   TRACE=1 firmware/build.sh         # with WICED traces on the HCI UART (bring-up)
#   ABTN_FW_VERSION=0.2.0 firmware/build.sh
#
# Needs: the SDK (research/install_wiced_b0_sdk.sh), Python 3 with `cryptography`.
# The OTA signing key is created on first use at ~/.config/abutton/ota_key.pem
# (override with ABTN_OTA_KEY). Keep it: the firmware accepts only images it signed.
#
# Outputs in firmware/out/:
#   abutton.hex          SDK flash image (SS + DS)
#   abutton.bin          full 512 KiB image; personalize/flash with abutton_image.py
#   abutton.ota.signed   signed OTA image for firmware/abutton_ble.py ota
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
SDK=${ABTN_SDK:-$HOME/Documents/WICED-Studio-6.0/20735-B0_Bluetooth}
OUT=$REPO/firmware/out
TRACE=${TRACE:-0}
VERSION=${ABTN_FW_VERSION:-0.1.0}
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$REPO/.venv/bin/python" ]; then PYTHON=$REPO/.venv/bin/python; else PYTHON=python3; fi
fi

[ -x "$SDK/make" ] || { echo "no SDK at $SDK; install it with research/install_wiced_b0_sdk.sh" >&2; exit 1; }
"$PYTHON" -c "import cryptography" 2>/dev/null || { echo "$PYTHON lacks the 'cryptography' package" >&2; exit 1; }

KEY=${ABTN_OTA_KEY:-$HOME/.config/abutton/ota_key.pem}
if [ ! -f "$KEY" ]; then
    echo "No OTA signing key yet; creating $KEY"
    "$PYTHON" "$REPO/firmware/ota_sign.py" --key "$KEY" keygen
fi

APP=$SDK/apps/abutton
rm -rf "$APP"
mkdir -p "$APP"
cp "$REPO"/firmware/abutton/*.c "$REPO"/firmware/abutton/*.h "$REPO"/firmware/abutton/makefile.mk "$APP/"
"$PYTHON" "$REPO/firmware/ota_sign.py" --key "$KEY" pubc -o "$APP/ecdsa256_pub.c"

( cd "$SDK" && ./make abutton-BCM920735EVAL_QFN60 build TRACE="$TRACE" ABTN_FW_VERSION="$VERSION" )
ABTN_SDK=$SDK sh "$REPO/firmware/check.sh"

BUILD=$(ls -d "$SDK"/build/abutton-BCM920735EVAL_QFN60-* | head -1)
NAME=$(basename "$BUILD")
mkdir -p "$OUT"
cp "$BUILD/$NAME.hex" "$OUT/abutton.hex"
cp "$BUILD/$NAME.ota.bin" "$OUT/abutton.ota.bin"
"$PYTHON" "$REPO/firmware/ota_sign.py" --key "$KEY" sign "$OUT/abutton.ota.bin" -o "$OUT/abutton.ota.signed"
"$PYTHON" "$REPO/firmware/abutton_image.py" assemble "$OUT/abutton.hex" -o "$OUT/abutton.bin"
echo "Built AButton $VERSION (TRACE=$TRACE) in $OUT"
