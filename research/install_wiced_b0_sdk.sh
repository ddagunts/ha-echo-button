#!/bin/sh
# Install the WICED Studio 6.0 20735-B0 platform (the B0 SDK) next to a base install.
#
# The public WICED-Studio-6.0.0.43 installer carries every platform, but its bundled
# config.eml ("Broad Market Configuration") marks only 43xxx_Wi-Fi and 20706-A2 for
# install, so 20735-B0 is silently skipped on any OS. The platform archives (*.7e) and
# config.eml are mbedTLS "aescrypt2" files; the installer's own install_config_linux64
# IS aescrypt2. The key isn't published here: the installer carries it, in the install
# script it runs, as the argument it passes to install_config_linux64. Extract it from
# your copy of the installer and pass it in WICED_PLATFORM_KEY (aescrypt2 syntax, e.g.
# hex:<bytes>).
#
# Prerequisite: run the installer once normally (it works on Debian/Ubuntu; only the
# 2017 IDE needs GTK2), which provides wiced_tools/ and the ARM GCC under 43xxx_Wi-Fi/.
#
# Usage: WICED_PLATFORM_KEY=... research/install_wiced_b0_sdk.sh WICED-Studio-6.0.0.43-IDE-Installer.bin [WICED_DIR]
#   WICED_DIR defaults to ~/Documents/WICED-Studio-6.0
# Then build from the CLI, e.g.:
#   cd ~/Documents/WICED-Studio-6.0/20735-B0_Bluetooth && ./make demo.hello_sensor-BCM920735EVAL_QFN60
set -eu

BIN=${1:?usage: $0 INSTALLER.bin [WICED_DIR]}
WICED=${2:-$HOME/Documents/WICED-Studio-6.0}
KEY=${WICED_PLATFORM_KEY:?set WICED_PLATFORM_KEY to the key the installer uses for its platform archives (see the header)}
PLAT=20735-B0_Bluetooth

[ -d "$WICED/wiced_tools" ] || { echo "no $WICED/wiced_tools: run the installer's base install first" >&2; exit 1; }
[ -e "$WICED/$PLAT" ] && { echo "$WICED/$PLAT already exists; remove it to reinstall" >&2; exit 1; }
for t in unzip 7z tar; do command -v $t >/dev/null || { echo "need $t" >&2; exit 1; }; done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# The .bin is a shell stub + JRE + zip archives; unzip finds the resource zip by its directory.
unzip -q -j -o "$BIN" '$IA_PROJECT_DIR$/install_config_linux64' "\$IA_PROJECT_DIR\$/$PLAT.7e" -d "$TMP" 2>/dev/null || true
[ -f "$TMP/$PLAT.7e" ] || { echo "could not find $PLAT.7e in $BIN" >&2; exit 1; }
chmod +x "$TMP/install_config_linux64"

"$TMP/install_config_linux64" 1 "$TMP/$PLAT.7e" "$TMP/$PLAT.7z" "$KEY"   # fails on a bad HMAC
7z x -y -o"$TMP" "$TMP/$PLAT.7z" >/dev/null
mkdir "$TMP/x"
tar -xf "$TMP/$PLAT.tar" -C "$TMP/x"

cp -a "$TMP/x/$PLAT" "$WICED/"
# API docs go under Doc/ without overwriting what the base install put there.
if [ -d "$TMP/x/Doc/$PLAT" ]; then
    mkdir -p "$WICED/Doc/$PLAT"
    cp -a -n "$TMP/x/Doc/$PLAT/." "$WICED/Doc/$PLAT/"
fi

# The installer's post-install steps for this platform.
chmod 775 "$WICED/$PLAT/make"
for os in Linux64 Linux32; do
    d="$WICED/$PLAT/tools/common/$os"
    [ -d "$d" ] && rm -f "$d/perl" && ln -s /usr/bin/perl "$d/perl"
done

echo "Installed $WICED/$PLAT"
echo "B0 ROM symbols: $WICED/$PLAT/WICED/wpan/internal/chips/20735B0/platforms/BCM920735EVAL_QFN60/patches/patch.elf"
