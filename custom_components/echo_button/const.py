"""Constants for the Amazon Echo Button integration."""

DOMAIN = "echo_button"

CONF_FEEDBACK_MS = "feedback_ms"
CONF_ON_COLOR = "on_color"
CONF_OFF_COLOR = "off_color"
CONF_PRESS_COLOR = "press_color"
CONF_KEEP_AWAKE = "keep_awake"
CONF_KEEP_AWAKE_SECONDS = "keep_awake_seconds"

DEFAULT_FEEDBACK_MS = 2000
DEFAULT_ON_COLOR = [0, 255, 0]
DEFAULT_OFF_COLOR = [255, 0, 0]
DEFAULT_PRESS_COLOR = [0, 0, 255]  # the stock press flash; black turns it off
DEFAULT_KEEP_AWAKE = False
DEFAULT_KEEP_AWAKE_SECONDS = 240  # two requests in every 10-minute sleep window

SERVICE_PLAY_ANIMATION = "play_animation"

# AButton: Echo Buttons running the open firmware (firmware/), reached over BLE.
CONF_KIND = "kind"
KIND_ABUTTON = "abutton"
CONF_BRIGHTNESS = "brightness"
CONF_LONG_PRESS_MS = "long_press_ms"
CONF_MULTI_CLICK_MS = "multi_click_ms"
CONF_ADVERT_INTERVAL_MS = "advert_interval_ms"
CONF_LOW_BATTERY_PCT = "low_battery_pct"
CONF_LOW_BATTERY_WARNING = "low_battery_warning"
CONF_BOOT_BATTERY = "boot_battery"
CONF_RESTORE_LED = "restore_led"
DEFAULT_ABUTTON_PRESS_COLOR = [255, 255, 255]
CONF_TX_POWER = "tx_power"      # "max" (the radio's default) or a dBm step as a string
CONF_SLEEP = "sleep"
TX_POWER_MAX = "max"
