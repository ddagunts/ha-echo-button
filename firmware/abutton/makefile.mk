#
# AButton firmware for the Amazon Echo Button (CYW20735B0).
# Built by firmware/build.sh, which copies this directory into the SDK as apps/abutton
# and generates ecdsa256_pub.c from the owner's OTA signing key.
#

NAME := abutton

APP_SRC  = abutton.c
APP_SRC += wiced_bt_cfg.c
APP_SRC += ecdsa256_pub.c

$(NAME)_COMPONENTS := fw_upgrade_lib.a

# Secure OTA: only images signed with the owner's ECDSA P-256 key are accepted.
C_FLAGS += -DOTA_SECURE_FIRMWARE_UPGRADE
C_FLAGS += -I../../libraries/fw_upgrade_lib -I../../../libraries/fw_upgrade_lib

ifdef ABTN_FW_VERSION
C_FLAGS += -DABTN_FW_VERSION=\"$(ABTN_FW_VERSION)\"
endif

# TRACE=1: WICED trace output on the HCI UART (115200), for bring-up with a serial adapter.
ifeq ($(TRACE), 1)
C_FLAGS += -DWICED_BT_TRACE_ENABLE
endif
