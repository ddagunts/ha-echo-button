#!/bin/sh
# Strict compile check of the AButton sources (-Wall -Wextra -Werror), release and TRACE
# variants. The SDK's own build doesn't enable warnings. Run after a build (it reads the
# include paths from the build's dependency file); build.sh calls it.
set -eu
SDK=${ABTN_SDK:-$HOME/Documents/WICED-Studio-6.0/20735-B0_Bluetooth}
GCC=$SDK/../43xxx_Wi-Fi/tools/ARM_GNU/bin/Linux64/arm-none-eabi-gcc
BUILD=$(ls -d "$SDK"/build/abutton-BCM920735EVAL_QFN60-* | head -1)
cd "$SDK/WICED/wpan"
INC="$(tr ' \\' '\n\n' < "$BUILD/abutton.d" | grep '\.h$' | xargs -n1 dirname | sort -u | sed 's/^/-I/' | tr '\n' ' ')"
INC="$INC -I$SDK/../43xxx_Wi-Fi/tools/ARM_GNU/include -I$SDK/libraries/fw_upgrade_lib"
for src in abutton.c wiced_bt_cfg.c bthome.c; do
    for variant in "" "-DWICED_BT_TRACE_ENABLE"; do
        # shellcheck disable=SC2086
        "$GCC" -mcpu=cortex-m4 -mthumb -fsyntax-only -Wall -Wextra -Werror \
            -Wno-unused-parameter -Wno-missing-field-initializers \
            -DOTA_SECURE_FIRMWARE_UPGRADE $variant $INC "$SDK/apps/abutton/$src" 2>&1 \
            | grep -E "apps/abutton/.*(error|warning)" && { echo "strict check failed: $src $variant" >&2; exit 1; }
    done
done
echo "strict check OK"
