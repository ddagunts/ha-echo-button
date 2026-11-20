/*
 * AButton: open firmware for the Amazon Echo Button (Cypress CYW20735B0, board rev 1).
 *
 *   - Never sleeps: the sleep framework is told sleep is not allowed, and there is no
 *     idle shutdown, so the button is always advertising and every press counts.
 *   - BTHome v2 over BLE advertisements (service data UUID 0xFCD2), which ESPHome
 *     Bluetooth proxies relay to Home Assistant: button events (press, double, triple,
 *     long, long-double, long-triple) and battery %. Optionally encrypted (AES-CCM, the
 *     BTHome bind key), so nobody nearby can read or fake the presses.
 *   - The AButton GATT service (see "GATT protocol" below): the LED (a steady color and
 *     animations), settings kept in NVRAM, the name, and commands.
 *   - Status on the LED: battery color at boot, a low-battery warning after presses, OTA
 *     progress, and an identify blink.
 *   - Signed OTA firmware upgrade (WICED secure OTA service, ECDSA P-256 + SHA-256):
 *     only images signed with the owner's key are accepted.
 *   - UART flash mode: hold the button for 8 s right after power-up (LED turns magenta)
 *     and the chip accepts the UART flasher (research/echobtn_resume_flash.py), as it does
 *     under the stock firmware. Bluetooth stops until the next power cycle.
 *   - Identity: the name comes from NVRAM (set over GATT), else a record in flash sector 0,
 *     else the stock scheme; the BD_ADDR from the static section. One image serves every
 *     unit and OTA never changes either.
 *
 * Board facts (decoded from the stock firmware's rev-1 hardware table, see
 * research/firmware_access.md): button P6 active low with pull-up (polled); LED R/G/B on
 * P27/P28/P29 = PWM1/2/3 from ACLK1 at 256 kHz, 512-tick period; battery on ADC P0 with
 * P13 (divider enable) high, 0.9 V..1.6 V = 0..100 %; P5 is the power latch.
 *
 * GATT protocol (service 0e5a1000-a8b9-4c3b-9d1a-ab7ff0ab7700; all multi-byte fields
 * little-endian unless noted; every write fits in 20 bytes, the default ATT MTU):
 *
 *   LED     0e5a1001  read: R G B <animation playing 0/1>
 *                     write 00                      stop the animation
 *                     write 01 R G B                steady color (000000 = off)
 *                     write 02 <repeat> <n> [steps] start an animation of n (1..40) steps,
 *                                                   repeat times (0 = forever)
 *                     write 03 [steps]              more steps; it plays once all n arrived
 *                     step = <ms u16 BIG-endian, 1..65535> R G B <fade 0/1> (as stock 0x06)
 *   Config  0e5a1002  read/write the 13-byte abtn_config_t below (version 1)
 *   Name    0e5a1003  read/write 0..26 printable ASCII; empty = the default name
 *   Command 0e5a1004  write 01 reboot, 02 restore defaults (settings, name, LED), 03 identify
 *   Key     0e5a1005  read: <BTHome encryption on 0/1> (the key itself is never readable)
 *                     write <16-byte key>           encrypt BTHome adverts with this key
 *                     write 00                      stop encrypting
 *                     Only within KEY_PRESENCE_S of a button press (else Insufficient
 *                     Authorization, 0x08), so changing the key needs someone at the button.
 *                     Restore defaults keeps the key.
 *
 * BTHome encryption: AES-128-CCM as Home Assistant's BTHome integration expects (bthome.c).
 * The counter in each advert keeps rising across reboots (reserved in NVRAM in blocks), so
 * no nonce repeats and Home Assistant's replay check keeps accepting the adverts.
 *
 * LED priority: OTA status > press light (while held) > status blinks > animation > steady.
 */

#include "sparcommon.h"
#include "string.h"
#include "wiced_bt_dev.h"
#include "wiced_bt_stack.h"
#include "wiced_bt_ble.h"
#include "wiced_bt_gatt.h"
#include "wiced_bt_cfg.h"
#include "wiced_bt_uuid.h"
#include "wiced_bt_trace.h"
#include "wiced_timer.h"
#include "wiced_transport.h"
#include "wiced_hal_gpio.h"
#include "wiced_sleep.h"
#include "wiced_hal_pwm.h"
#include "wiced_hal_aclk.h"
#include "wiced_hal_adc.h"
#include "wiced_hal_sflash.h"
#include "wiced_hal_nvram.h"
#include "wiced_hal_wdog.h"
#include "wiced_bt_firmware_upgrade.h"
#include "wiced_bt_fw_upgrade.h"
#include "bt_types.h"
#include "p_256_multprecision.h"
#include "p_256_ecc_pp.h"
#include "bthome.h"

#ifndef ABTN_FW_VERSION
#define ABTN_FW_VERSION         "0.1.0"
#endif

/* ---- board ---------------------------------------------------------------------- */
#define BTN_PIN                 WICED_P06
#define AUX_OUT_PIN             WICED_P13           /* battery divider enable: high to read */
/* Power latch. A press powers the board for only ~2 s; the firmware must drive P5 high to
 * stay on, and low to switch itself off. Stock: 0x21bf10(1) at the end of its rev-1 board
 * init (from BTM_ENABLED) does configure_pin(P5, OUTPUT, HIGH); its 10-minute idle timer
 * runs 0x21bf68, which drives P5 low: the stock "sleep" is really a hard power-off. */
#define POWER_HOLD_PIN          WICED_P05
#define BATT_ADC                ADC_INPUT_P0
#define PWM_INIT                0xFDFF              /* counter start: 512-tick period */

static const uint8_t  led_pin[3]  = { WICED_P27, WICED_P28, WICED_P29 };
static const uint8_t  led_pwm[3]  = { PWM1, PWM2, PWM3 };
/* The stock firmware's white balance: duty = v * (1 - atten), atten R 0, G 0.593, B 0.4. */
static const uint16_t led_gain[3] = { 1000, 407, 600 };

/* ---- identity record (flash 0xD00, sector 0; written by abutton_image.py) ---------- */
#define IDENTITY_ADDR           0xD00
#define IDENTITY_MAGIC          "ABTN"
#define IDENTITY_NAME_MAX       26
#define FACTORY_SERIAL_ADDR     0xC00               /* stock: 16-char serial */
#define NAME_ADV_MAX            15                  /* fits next to the BTHome data */
#define NAME_ADV_MAX_ENC        7                   /* ...when it is encrypted (8 bytes longer) */
#define NAME_RSP_MAX            11                  /* fits next to the 128-bit service UUID */

typedef struct
{
    char    magic[4];
    uint8_t version;
    uint8_t name_len;
    char    name[IDENTITY_NAME_MAX];
} abtn_identity_t;                                   /* 32 bytes */

/* ---- settings (GATT config characteristic, kept in NVRAM) ------------------------- */
#define CFG_VERSION             1
#define F_LOW_BATT_WARN         0x01                /* two red blinks after a press when low */
#define F_BOOT_BATTERY          0x02                /* boot blink: green / amber / red by battery */
#define F_RESTORE_LED           0x04                /* the steady LED color survives a reboot */
#define F_ALL                   (F_LOW_BATT_WARN | F_BOOT_BATTERY | F_RESTORE_LED)

#pragma pack(1)
typedef struct
{
    uint8_t  version;                               /* CFG_VERSION */
    uint8_t  flags;                                 /* F_* */
    uint8_t  brightness;                            /* 1..255: scales every LED output */
    uint8_t  press_rgb[3];                          /* LED while held; 000000 = none */
    uint16_t long_press_ms;                         /* 300..5000 */
    uint16_t multi_click_ms;                        /* 100..1000 */
    uint16_t adv_interval_ms;                       /* 100..10000, the idle advert interval */
    uint8_t  low_batt_pct;                          /* 0..100: warn below this */
} abtn_config_t;                                    /* 13 bytes */

/* NVRAM records (VS ids from WICED_NVRAM_VSID_START). The VS area is left as the stock
 * firmware wrote it, so every record starts with a magic and is validated on load. */
typedef struct { uint8_t magic[2]; abtn_config_t cfg; } nv_config_t;
typedef struct { uint8_t magic[2]; uint8_t len; char name[IDENTITY_NAME_MAX]; } nv_name_t;
typedef struct { uint8_t magic[2]; uint8_t rgb[3]; } nv_led_t;
typedef struct { uint8_t magic[2]; uint8_t on; uint8_t key[BTHOME_KEY_LEN]; } nv_key_t;
typedef struct { uint8_t magic[2]; uint32_t next; } nv_counter_t;   /* first counter not yet used */
#pragma pack()

#define NV_CONFIG               (WICED_NVRAM_VSID_START + 0)
#define NV_NAME                 (WICED_NVRAM_VSID_START + 1)
#define NV_LED                  (WICED_NVRAM_VSID_START + 2)
#define NV_KEY                  (WICED_NVRAM_VSID_START + 3)
#define NV_COUNTER              (WICED_NVRAM_VSID_START + 4)

static const abtn_config_t cfg_default =
{
    CFG_VERSION, F_ALL, 255, { 255, 255, 255 }, 1000, 350, 250, 20
};

/* ---- behavior ------------------------------------------------------------------- */
#define BTN_POLL_MS             10                  /* button sampling period */
#define DEBOUNCE_SAMPLES        2                   /* equal samples to accept a change */
#define BURST_MS                2500                /* fast advertising after an event */
#define LED_TICK_MS             20                  /* animation step resolution */
#define ANIM_MAX_STEPS          40
#define LED_SAVE_DELAY_S        5                   /* store the steady color this long after the last change */
#define FLASH_MODE_HOLD_MS      8000                /* hold at power-up to enter UART flash mode */
#define KEY_PRESENCE_S          60                  /* key writes allowed this long after a press */
#define COUNTER_BLOCK           256                 /* encryption counters reserved per NVRAM write */
#ifdef WICED_BT_TRACE_ENABLE
#define BATTERY_PERIOD_S        5                   /* bring-up: trace the raw readings often */
#else
#define BATTERY_PERIOD_S        600
#endif

/* BTHome v2 button events */
#define BTH_PRESS               0x01
#define BTH_DOUBLE              0x02
#define BTH_TRIPLE              0x03
#define BTH_LONG                0x04
#define BTH_LONG_DOUBLE         0x05
#define BTH_LONG_TRIPLE         0x06

/* ---- GATT database ---------------------------------------------------------------- */
/* 0e5a10xx-a8b9-4c3b-9d1a-ab7ff0ab7700, little-endian as the GATT macros want it */
#define ABTN_UUID(x)            0x00, 0x77, 0xab, 0xf0, 0x7f, 0xab, 0x1a, 0x9d, \
                                0x3b, 0x4c, 0xb9, 0xa8, (x), 0x10, 0x5a, 0x0e

enum
{
    HDL_GATT_SERVICE = 0x01,
    HDL_GAP_SERVICE  = 0x10,
    HDL_GAP_NAME,
    HDL_GAP_NAME_VAL,
    HDL_GAP_APPEARANCE,
    HDL_GAP_APPEARANCE_VAL,
    HDL_DIS_SERVICE  = 0x20,
    HDL_DIS_MFR,
    HDL_DIS_MFR_VAL,
    HDL_DIS_MODEL,
    HDL_DIS_MODEL_VAL,
    HDL_DIS_FW,
    HDL_DIS_FW_VAL,
    HDL_ABTN_SERVICE = 0x30,
    HDL_ABTN_LED,
    HDL_ABTN_LED_VAL,
    HDL_ABTN_CONFIG,
    HDL_ABTN_CONFIG_VAL,
    HDL_ABTN_NAME,
    HDL_ABTN_NAME_VAL,
    HDL_ABTN_CMD,
    HDL_ABTN_CMD_VAL,
    HDL_ABTN_KEY,
    HDL_ABTN_KEY_VAL,
};

#define RW_PROPS                (LEGATTDB_CHAR_PROP_READ | LEGATTDB_CHAR_PROP_WRITE)
#define RW_PERMS                (LEGATTDB_PERM_READABLE | LEGATTDB_PERM_WRITE_REQ | LEGATTDB_PERM_VARIABLE_LENGTH)

static const char dis_mfr[]   = "abuttons";
static const char dis_model[] = "Echo Button (AButton fw)";
static const char dis_fw[]    = ABTN_FW_VERSION;

const uint8_t abtn_gatt_db[] =
{
    PRIMARY_SERVICE_UUID16(HDL_GATT_SERVICE, UUID_SERVICE_GATT),

    PRIMARY_SERVICE_UUID16(HDL_GAP_SERVICE, UUID_SERVICE_GAP),
        CHARACTERISTIC_UUID16(HDL_GAP_NAME, HDL_GAP_NAME_VAL, UUID_CHARACTERISTIC_DEVICE_NAME,
                              LEGATTDB_CHAR_PROP_READ, LEGATTDB_PERM_READABLE),
        CHARACTERISTIC_UUID16(HDL_GAP_APPEARANCE, HDL_GAP_APPEARANCE_VAL, UUID_CHARACTERISTIC_APPEARANCE,
                              LEGATTDB_CHAR_PROP_READ, LEGATTDB_PERM_READABLE),

    PRIMARY_SERVICE_UUID16(HDL_DIS_SERVICE, UUID_SERVICE_DEVICE_INFORMATION),
        CHARACTERISTIC_UUID16(HDL_DIS_MFR, HDL_DIS_MFR_VAL, UUID_CHARACTERISTIC_MANUFACTURER_NAME_STRING,
                              LEGATTDB_CHAR_PROP_READ, LEGATTDB_PERM_READABLE),
        CHARACTERISTIC_UUID16(HDL_DIS_MODEL, HDL_DIS_MODEL_VAL, UUID_CHARACTERISTIC_MODEL_NUMBER_STRING,
                              LEGATTDB_CHAR_PROP_READ, LEGATTDB_PERM_READABLE),
        CHARACTERISTIC_UUID16(HDL_DIS_FW, HDL_DIS_FW_VAL, UUID_CHARACTERISTIC_FIRMWARE_REVISION_STRING,
                              LEGATTDB_CHAR_PROP_READ, LEGATTDB_PERM_READABLE),

    PRIMARY_SERVICE_UUID128(HDL_ABTN_SERVICE, ABTN_UUID(0x00)),
        CHARACTERISTIC_UUID128_WRITABLE(HDL_ABTN_LED, HDL_ABTN_LED_VAL, ABTN_UUID(0x01),
            RW_PROPS | LEGATTDB_CHAR_PROP_WRITE_NO_RESPONSE, RW_PERMS | LEGATTDB_PERM_WRITE_CMD),
        CHARACTERISTIC_UUID128_WRITABLE(HDL_ABTN_CONFIG, HDL_ABTN_CONFIG_VAL, ABTN_UUID(0x02),
            RW_PROPS, RW_PERMS),
        CHARACTERISTIC_UUID128_WRITABLE(HDL_ABTN_NAME, HDL_ABTN_NAME_VAL, ABTN_UUID(0x03),
            RW_PROPS, RW_PERMS),
        CHARACTERISTIC_UUID128_WRITABLE(HDL_ABTN_CMD, HDL_ABTN_CMD_VAL, ABTN_UUID(0x04),
            LEGATTDB_CHAR_PROP_WRITE, LEGATTDB_PERM_WRITE_REQ | LEGATTDB_PERM_VARIABLE_LENGTH),
        CHARACTERISTIC_UUID128_WRITABLE(HDL_ABTN_KEY, HDL_ABTN_KEY_VAL, ABTN_UUID(0x05),
            RW_PROPS, RW_PERMS),

    /* WICED secure OTA firmware upgrade service (handles 0xff00..; handled by fw_upgrade_lib) */
    PRIMARY_SERVICE_UUID128(HANDLE_OTA_FW_UPGRADE_SERVICE, UUID_OTA_SEC_FW_UPGRADE_SERVICE),
        CHARACTERISTIC_UUID128_WRITABLE(HANDLE_OTA_FW_UPGRADE_CHARACTERISTIC_CONTROL_POINT,
            HANDLE_OTA_FW_UPGRADE_CONTROL_POINT, UUID_OTA_FW_UPGRADE_CHARACTERISTIC_CONTROL_POINT,
            LEGATTDB_CHAR_PROP_WRITE | LEGATTDB_CHAR_PROP_NOTIFY | LEGATTDB_CHAR_PROP_INDICATE,
            LEGATTDB_PERM_VARIABLE_LENGTH | LEGATTDB_PERM_WRITE_REQ),
            CHAR_DESCRIPTOR_UUID16_WRITABLE(HANDLE_OTA_FW_UPGRADE_CLIENT_CONFIGURATION_DESCRIPTOR,
                UUID_DESCRIPTOR_CLIENT_CHARACTERISTIC_CONFIGURATION,
                LEGATTDB_PERM_READABLE | LEGATTDB_PERM_WRITE_REQ),
        CHARACTERISTIC_UUID128_WRITABLE(HANDLE_OTA_FW_UPGRADE_CHARACTERISTIC_DATA,
            HANDLE_OTA_FW_UPGRADE_DATA, UUID_OTA_FW_UPGRADE_CHARACTERISTIC_DATA,
            LEGATTDB_CHAR_PROP_WRITE, LEGATTDB_PERM_VARIABLE_LENGTH | LEGATTDB_PERM_WRITE_REQ),
};

/* ---- state ---------------------------------------------------------------------- */
extern wiced_bt_cfg_settings_t      app_cfg_settings;
extern const wiced_bt_cfg_buf_pool_t app_buf_pools[];
extern Point                        ecdsa256_public_key;   /* generated from the owner's key */

static abtn_config_t  cfg;
static char           abtn_name[IDENTITY_NAME_MAX + 1] = "AButton";
static uint16_t       conn_id;
static uint8_t        adv_mode = BTM_BLE_ADVERT_OFF;
static uint8_t        bthome_pid;
static uint8_t        battery_pct = 100;
static wiced_bool_t   ota_active;

static wiced_bool_t   btn_down;
static wiced_bool_t   boot_press;                   /* the press that powered the board up */
static volatile uint32_t btn_irqs;                 /* button interrupts seen (traced) */
static wiced_bool_t   long_fired;
static uint8_t        clicks;

static wiced_timer_t  poll_timer, long_timer, click_timer, burst_timer, battery_timer;
static wiced_timer_t  led_timer, led_save_timer, reboot_timer, flash_mode_timer;
static wiced_bool_t   flash_mode;                   /* UART flash mode: BLE off, HCI open */

static uint8_t        local_addr[6];                /* our BD_ADDR, most significant byte first */
static wiced_bool_t   bthome_encrypt;
static uint8_t        bthome_key[BTHOME_KEY_LEN];
static uint32_t       bthome_counter;               /* next counter to use */
static uint32_t       bthome_reserved;              /* counters below this are reserved in NVRAM */
static wiced_bool_t   presence;                     /* the button was pressed in the last KEY_PRESENCE_S */
static wiced_timer_t  presence_timer;

/* ROM (B0): the embedded host stack's HCI event hook. While it is set, the ROM's
 * mpaf_df_hci_cmd_filter (0x745bc) answers every vendor HCI command from the UART that
 * isn't in its small table (READ/WRITE/LAUNCH_RAM, Download_Minidriver) with 0x0C Command
 * Disallowed. The stock firmware runs with it cleared at times, which is why the UART
 * flasher works under stock. */
extern void *btu_hcievt_handler;

static void adv_update(uint8_t button_event);
static void adv_restart(void);
static void scan_rsp_update(void);

/* Anything the host sends (our tools send HCI_Reset while waiting) lands here and is dropped.
 * With no handler the transport would call NULL on the first received packet, as the stock
 * firmware and the SDK samples avoid by always registering one. */
static uint32_t uart_rx(uint8_t *p_data, uint32_t len)
{
    if (p_data)
        wiced_transport_free_buffer(p_data);
    return 0;
}

static const wiced_transport_cfg_t transport_cfg =
{
    WICED_TRANSPORT_UART, { { WICED_TRANSPORT_UART_HCI_MODE, 115200 } }, { 0, 0 }, NULL, uart_rx, NULL
};

#ifdef WICED_BT_TRACE_ENABLE
/* Boot breadcrumbs: how far the firmware gets, and a heartbeat that tells a hang (heartbeat
 * stops, chip stays up) from a power loss (everything stops at once). */
#define DBG(...)                WICED_BT_TRACE("ABTN: " __VA_ARGS__)
static wiced_timer_t          heartbeat_timer;
static uint32_t               heartbeat_n;

static void heartbeat(uint32_t arg)
{
    DBG("alive %u p6=%u irqs=%u down=%u\n", (unsigned)++heartbeat_n,
        (unsigned)wiced_hal_gpio_get_pin_input_status(BTN_PIN), (unsigned)btn_irqs, (unsigned)btn_down);
}
#else
#define DBG(...)
#endif

/* ---- identity ------------------------------------------------------------------- */
static wiced_bool_t printable(const char *s, int n)
{
    int i;
    for (i = 0; i < n; i++)
        if (s[i] < 0x20 || s[i] > 0x7e)
            return WICED_FALSE;
    return WICED_TRUE;
}

static void identity_load(void)
{
    abtn_identity_t id;
    nv_name_t       nv;
    wiced_result_t  st;
    char            serial[16];

    /* 1. A name set over GATT (NVRAM). */
    if (wiced_hal_read_nvram(NV_NAME, sizeof(nv), (uint8_t *)&nv, &st) == sizeof(nv) && st == WICED_SUCCESS &&
        nv.magic[0] == 'A' && nv.magic[1] == 'N' && nv.len >= 1 && nv.len <= IDENTITY_NAME_MAX &&
        printable(nv.name, nv.len))
    {
        memcpy(abtn_name, nv.name, nv.len);
        abtn_name[nv.len] = 0;
        return;
    }
    /* 2. An identity record in sector 0 (abutton_image.py patch --name). */
    if (wiced_hal_sflash_read(IDENTITY_ADDR, sizeof(id), (uint8_t *)&id) == sizeof(id) &&
        memcmp(id.magic, IDENTITY_MAGIC, 4) == 0 && id.version == 1 &&
        id.name_len >= 1 && id.name_len <= IDENTITY_NAME_MAX && printable(id.name, id.name_len))
    {
        memcpy(abtn_name, id.name, id.name_len);
        abtn_name[id.name_len] = 0;
        return;
    }
    /* 3. Like the stock firmware: "EchoBtn" + serial[13..15]. */
    strcpy(abtn_name, "AButton");
    if (wiced_hal_sflash_read(FACTORY_SERIAL_ADDR, sizeof(serial), (uint8_t *)serial) == sizeof(serial) &&
        printable(serial, sizeof(serial)))
    {
        memcpy(abtn_name, "EchoBtn", 7);
        memcpy(abtn_name + 7, serial + 13, 3);
        abtn_name[10] = 0;
    }
}

/* ---- settings ------------------------------------------------------------------- */
static wiced_bool_t cfg_valid(const abtn_config_t *c)
{
    return c->version == CFG_VERSION && (c->flags & ~F_ALL) == 0 && c->brightness >= 1 &&
           c->long_press_ms >= 300 && c->long_press_ms <= 5000 &&
           c->multi_click_ms >= 100 && c->multi_click_ms <= 1000 &&
           c->adv_interval_ms >= 100 && c->adv_interval_ms <= 10000 && c->low_batt_pct <= 100;
}

static void cfg_load(void)
{
    nv_config_t    nv;
    wiced_result_t st;
    cfg = cfg_default;
    if (wiced_hal_read_nvram(NV_CONFIG, sizeof(nv), (uint8_t *)&nv, &st) == sizeof(nv) && st == WICED_SUCCESS &&
        nv.magic[0] == 'A' && nv.magic[1] == 'C' && cfg_valid(&nv.cfg))
        cfg = nv.cfg;
}

static void cfg_save(void)
{
    nv_config_t    nv = { { 'A', 'C' }, cfg };
    wiced_result_t st;
    wiced_hal_write_nvram(NV_CONFIG, sizeof(nv), (uint8_t *)&nv, &st);
}

/* The idle (low duty) advert interval, in 0.625 ms units, connectable and not. */
static void cfg_apply_adv_interval(void)
{
    uint16_t units = (uint16_t)((uint32_t)cfg.adv_interval_ms * 8 / 5);
    app_cfg_settings.ble_advert_cfg.low_duty_min_interval         = units;
    app_cfg_settings.ble_advert_cfg.low_duty_max_interval         = units;
    app_cfg_settings.ble_advert_cfg.low_duty_nonconn_min_interval = units;
    app_cfg_settings.ble_advert_cfg.low_duty_nonconn_max_interval = units;
}

/* ---- LED ------------------------------------------------------------------------ */
typedef struct
{
    uint16_t ms;
    uint8_t  rgb[3];
    uint8_t  fade;
} led_step_t;

typedef struct
{
    led_step_t   step[ANIM_MAX_STEPS];
    uint8_t      n, have;                           /* steps expected / received */
    uint8_t      repeat, left;                      /* 0 = forever */
    uint8_t      idx;
    uint16_t     t;                                 /* ms into the current step */
    uint8_t      from[3], cur[3];
    wiced_bool_t on;
} led_player_t;

static uint8_t       base_rgb[3];                   /* the steady color */
static uint8_t       led_out[3];                    /* what the LED shows (before brightness) */
static uint8_t       ota_rgb[3];
static wiced_bool_t  ota_led;
static wiced_bool_t  led_timer_on;
static led_player_t  user_anim, status_anim;

static void led_write(const uint8_t *v)
{
    int i;
    for (i = 0; i < 3; i++)
    {
        uint32_t level  = (uint32_t)v[i] * cfg.brightness / 255;
        uint32_t duty   = level * led_gain[i] / 1000;                /* 0..255 */
        uint32_t toggle = 0xFFFF - duty * 2008 / 1000;              /* stock mapping; 255 -> always on */
        wiced_hal_pwm_change_values(led_pwm[i], toggle, PWM_INIT);
    }
}

static void player_color(led_player_t *p)
{
    const led_step_t *s = &p->step[p->idx];
    int i;
    for (i = 0; i < 3; i++)
        p->cur[i] = s->fade ? (uint8_t)(p->from[i] + ((int32_t)s->rgb[i] - p->from[i]) * p->t / s->ms) : s->rgb[i];
}

static void player_start(led_player_t *p)
{
    p->idx  = 0;
    p->t    = 0;
    p->left = p->repeat;
    memcpy(p->from, led_out, 3);
    p->on   = WICED_TRUE;
    player_color(p);
}

static void player_tick(led_player_t *p)
{
    if (!p->on)
        return;
    p->t += LED_TICK_MS;
    while (p->on && p->t >= p->step[p->idx].ms)
    {
        p->t -= p->step[p->idx].ms;
        memcpy(p->from, p->step[p->idx].rgb, 3);
        if (++p->idx >= p->n)
        {
            p->idx = 0;
            if (p->repeat && --p->left == 0)
                p->on = WICED_FALSE;
        }
    }
    if (p->on)
        player_color(p);
}

static void led_refresh(void)
{
    const uint8_t *c;
    wiced_bool_t   want_timer = status_anim.on || user_anim.on;

    if (ota_led)
        c = ota_rgb;
    else if (btn_down && !boot_press && (cfg.press_rgb[0] | cfg.press_rgb[1] | cfg.press_rgb[2]))
        c = cfg.press_rgb;
    else if (status_anim.on)
        c = status_anim.cur;
    else if (user_anim.on)
        c = user_anim.cur;
    else
        c = base_rgb;
    memcpy(led_out, c, 3);
    led_write(led_out);

    if (want_timer && !led_timer_on)
        wiced_start_timer(&led_timer, LED_TICK_MS);
    else if (!want_timer && led_timer_on)
        wiced_stop_timer(&led_timer);
    led_timer_on = want_timer;
}

static void led_tick(uint32_t arg)
{
    player_tick(&status_anim);
    player_tick(&user_anim);
    led_refresh();
}

/* A built-in status pattern: pairs of (ms, r, g, b), played once, over any animation. */
static void status_play(const uint8_t *pattern, uint8_t n)
{
    uint8_t i;
    for (i = 0; i < n && i < ANIM_MAX_STEPS; i++)
    {
        const uint8_t *e = pattern + 4 * i;
        status_anim.step[i].ms   = (uint16_t)e[0] * 10;   /* pattern times are in 10 ms */
        memcpy(status_anim.step[i].rgb, e + 1, 3);
        status_anim.step[i].fade = 0;
    }
    status_anim.n = status_anim.have = i;
    status_anim.repeat = 1;
    player_start(&status_anim);
    led_refresh();
}

static void status_boot(void)
{
    uint8_t p[4] = { 15, 255, 255, 255 };          /* 150 ms white */
    if (cfg.flags & F_BOOT_BATTERY)
    {
        if (battery_pct >= 50)                    { p[1] = 0;   p[2] = 255; p[3] = 0; }   /* green */
        else if (battery_pct >= cfg.low_batt_pct) { p[1] = 255; p[2] = 120; p[3] = 0; }   /* amber */
        else                                      { p[1] = 255; p[2] = 0;   p[3] = 0; }   /* red */
        p[0] = 40;                                 /* 400 ms: long enough to read */
    }
    status_play(p, 1);
}

static void status_low_battery(void)
{
    static const uint8_t p[] = { 15, 0, 0, 0,  15, 255, 0, 0,  15, 0, 0, 0,  15, 255, 0, 0 };
    if ((cfg.flags & F_LOW_BATT_WARN) && battery_pct < cfg.low_batt_pct)
        status_play(p, 4);
}

static void status_identify(void)
{
    static const uint8_t p[] = { 20, 255, 255, 255,  20, 0, 0, 0,  20, 255, 255, 255,  20, 0, 0, 0,
                                 20, 255, 255, 255,  20, 0, 0, 0 };
    status_play(p, 6);
}

/* The BTHome key changed: two green blinks (encrypting) or two amber blinks (stopped). */
static void status_key(wiced_bool_t on)
{
    static const uint8_t green[] = { 15, 0, 255, 0,  15, 0, 0, 0,  15, 0, 255, 0,  15, 0, 0, 0 };
    static const uint8_t amber[] = { 15, 255, 120, 0,  15, 0, 0, 0,  15, 255, 120, 0,  15, 0, 0, 0 };
    status_play(on ? green : amber, 4);
}

static void led_save(uint32_t arg)
{
    nv_led_t       nv = { { 'A', 'L' }, { base_rgb[0], base_rgb[1], base_rgb[2] } };
    wiced_result_t st;
    if (cfg.flags & F_RESTORE_LED)
        wiced_hal_write_nvram(NV_LED, sizeof(nv), (uint8_t *)&nv, &st);
}

static void led_load(void)
{
    nv_led_t       nv;
    wiced_result_t st;
    if ((cfg.flags & F_RESTORE_LED) &&
        wiced_hal_read_nvram(NV_LED, sizeof(nv), (uint8_t *)&nv, &st) == sizeof(nv) && st == WICED_SUCCESS &&
        nv.magic[0] == 'A' && nv.magic[1] == 'L')
        memcpy(base_rgb, nv.rgb, 3);
}

static void led_init(void)
{
    int i;
    wiced_hal_aclk_enable(256000, ACLK1, ACLK_FREQ_24_MHZ);
    for (i = 0; i < 3; i++)
    {
        wiced_hal_gpio_configure_pin(led_pin[i], GPIO_OUTPUT_ENABLE, 0);
        wiced_hal_pwm_start(led_pwm[i], PMU_CLK, 0xFFFF, PWM_INIT, 0);
    }
    wiced_init_timer(&led_timer, led_tick, 0, WICED_MILLI_SECONDS_PERIODIC_TIMER);
    wiced_init_timer(&led_save_timer, led_save, 0, WICED_SECONDS_TIMER);
}

/* ---- battery -------------------------------------------------------------------- */
/* Measure now. Only while the LED is dark: its load sags the reading (stock's readings
 * dropped from 75 to 0-50 at full white). So a constantly lit LED freezes the value. */
static void battery_read(void)
{
    int32_t mv;
#ifdef WICED_BT_TRACE_ENABLE
    DBG("battery: P0 %d mV with P13 low\n", (int)wiced_hal_adc_read_voltage(BATT_ADC));
#endif
    /* P13 switches the battery divider onto ADC P0, as stock's 0x21c994 does it: P13 high,
     * read, P13 low. With P13 low (how AButton used to read it) P0 reads under 0.9 V. */
    wiced_hal_gpio_set_pin_output(AUX_OUT_PIN, GPIO_PIN_OUTPUT_HIGH);
    mv = (int32_t)wiced_hal_adc_read_voltage(BATT_ADC);
    wiced_hal_gpio_set_pin_output(AUX_OUT_PIN, GPIO_PIN_OUTPUT_LOW);
    DBG("battery: P0 %d mV with P13 high\n", (int)mv);
    mv -= 900;
    if (mv < 0)
        mv = 0;
    if (mv > 700)
        mv = 700;
    battery_pct = (uint8_t)(mv / 7);
}

static void battery_measure(void)
{
    if (!(btn_down || ota_active || led_out[0] || led_out[1] || led_out[2]))
        battery_read();
}

static void battery_timeout(uint32_t arg)
{
    battery_measure();
    adv_update(0);
}

/* ---- BTHome encryption ---------------------------------------------------------- */
static void presence_timeout(uint32_t arg)
{
    presence = WICED_FALSE;
}

/* A press: someone is at the button, so the key may change for the next KEY_PRESENCE_S. */
static void presence_mark(void)
{
    presence = WICED_TRUE;
    wiced_stop_timer(&presence_timer);
    wiced_start_timer(&presence_timer, KEY_PRESENCE_S);
}

static void key_load(void)
{
    nv_key_t       nv;
    nv_counter_t   ctr;
    wiced_result_t st;
    if (wiced_hal_read_nvram(NV_KEY, sizeof(nv), (uint8_t *)&nv, &st) == sizeof(nv) && st == WICED_SUCCESS &&
        nv.magic[0] == 'A' && nv.magic[1] == 'K' && nv.on == 1)
    {
        memcpy(bthome_key, nv.key, BTHOME_KEY_LEN);
        bthome_encrypt = WICED_TRUE;
    }
    memset(&nv, 0, sizeof(nv));
    if (wiced_hal_read_nvram(NV_COUNTER, sizeof(ctr), (uint8_t *)&ctr, &st) == sizeof(ctr) && st == WICED_SUCCESS &&
        ctr.magic[0] == 'A' && ctr.magic[1] == 'T')
        bthome_counter = ctr.next;
    bthome_reserved = bthome_counter;             /* nothing reserved yet in this boot */
}

/* The counter for the next encrypted advert. Counters are reserved in NVRAM a block at a
 * time before they are used, so after a reboot the counter starts above every counter
 * already sent: no nonce repeats, and Home Assistant's check for a rising counter holds.
 * If the NVRAM write fails, the next advert tries again. */
static uint32_t counter_next(void)
{
    if (bthome_counter >= bthome_reserved)
    {
        nv_counter_t   ctr = { { 'A', 'T' }, bthome_counter + COUNTER_BLOCK };
        wiced_result_t st;
        if (wiced_hal_write_nvram(NV_COUNTER, sizeof(ctr), (uint8_t *)&ctr, &st) == sizeof(ctr) &&
            st == WICED_SUCCESS)
            bthome_reserved = ctr.next;
    }
    return bthome_counter++;
}

static wiced_bt_gatt_status_t key_write(const uint8_t *v, uint16_t len)
{
    static const uint8_t zero[BTHOME_KEY_LEN];
    nv_key_t       nv;
    wiced_result_t st;
    uint16_t       written;

    if (len != BTHOME_KEY_LEN && !(len == 1 && v[0] == 0))
        return WICED_BT_GATT_INVALID_ATTR_LEN;
    if (!presence)
        return WICED_BT_GATT_INSUF_AUTHORIZATION; /* press the button first */
    if (len == BTHOME_KEY_LEN && memcmp(v, zero, BTHOME_KEY_LEN) == 0)
        return WICED_BT_GATT_ILLEGAL_PARAMETER;
    memset(&nv, 0, sizeof(nv));
    nv.magic[0] = 'A';
    nv.magic[1] = 'K';
    nv.on       = len == BTHOME_KEY_LEN;
    if (nv.on)
        memcpy(nv.key, v, BTHOME_KEY_LEN);
    written = wiced_hal_write_nvram(NV_KEY, sizeof(nv), (uint8_t *)&nv, &st);
    if (written != sizeof(nv) || st != WICED_SUCCESS)
    {
        memset(&nv, 0, sizeof(nv));
        return WICED_BT_GATT_INTERNAL_ERROR;      /* not stored: keep the current key */
    }
    memcpy(bthome_key, nv.key, BTHOME_KEY_LEN);
    bthome_encrypt = nv.on;
    memset(&nv, 0, sizeof(nv));
    WICED_BT_TRACE("BTHome encryption %s\n", bthome_encrypt ? "on" : "off");
    adv_update(0);
    status_key(bthome_encrypt);
    return WICED_BT_GATT_SUCCESS;
}

/* ---- button --------------------------------------------------------------------- */
static void emit(uint8_t event)
{
    WICED_BT_TRACE("button event %d\n", event);
    adv_update(event);
}

static void click_timeout(uint32_t arg)
{
    if (clicks)
        emit(clicks == 1 ? BTH_PRESS : clicks == 2 ? BTH_DOUBLE : BTH_TRIPLE);
    clicks = 0;
}

static void long_timeout(uint32_t arg)
{
    long_fired = WICED_TRUE;
    emit(clicks == 0 ? BTH_LONG : clicks == 1 ? BTH_LONG_DOUBLE : BTH_LONG_TRIPLE);
    clicks = 0;
}

static void flash_mode_hold(uint32_t arg);

static void on_button(wiced_bool_t down)
{
    if (flash_mode)
        return;                                   /* presses do nothing in UART flash mode */
    btn_down = down;
    led_refresh();
    if (down)
    {
        presence_mark();
        long_fired = WICED_FALSE;
        wiced_stop_timer(&click_timer);
        wiced_start_timer(&long_timer, cfg.long_press_ms);
        return;
    }
    wiced_stop_timer(&long_timer);
    wiced_stop_timer(&flash_mode_timer);          /* let go before 8 s: no flash mode */
    if (!boot_press)
        status_low_battery();                     /* on release, so the press light can't hide it */
    boot_press = WICED_FALSE;
    if (long_fired)
        return;
    if (++clicks >= 3)
    {
        click_timeout(0);
        return;
    }
    wiced_start_timer(&click_timer, cfg.multi_click_ms);
}

/* The button is polled. On a rev-1 unit the P6 edge interrupt never fired (irqs=0 in the
 * trace while p6 read 0 on every press, 2026-11-19), with the same pin config as stock
 * (0x40D). Polling costs nothing here: the firmware never sleeps. The interrupt is still
 * registered (in stock's order, then cleared, as stock's 0x2185ec does) and only counted,
 * so a trace shows whether it works. */
static void button_poll(uint32_t arg)
{
    static uint8_t differ;
    wiced_bool_t down = wiced_hal_gpio_get_pin_input_status(BTN_PIN) == 0;   /* active low */
    if (down == btn_down)
    {
        differ = 0;
        return;
    }
    if (++differ < DEBOUNCE_SAMPLES)
        return;
    differ = 0;
    DBG("button %s (irqs=%u)\n", down ? "down" : "up", (unsigned)btn_irqs);
    on_button(down);
}

static void button_irq(void *data, uint8_t port_pin)
{
    wiced_hal_gpio_clear_pin_interrupt_status(BTN_PIN);
    btn_irqs++;
}

static void button_init(void)
{
    wiced_init_timer(&poll_timer, button_poll, 0, WICED_MILLI_SECONDS_PERIODIC_TIMER);
    wiced_init_timer(&long_timer, long_timeout, 0, WICED_MILLI_SECONDS_TIMER);
    wiced_init_timer(&click_timer, click_timeout, 0, WICED_MILLI_SECONDS_TIMER);
    wiced_hal_gpio_configure_pin(BTN_PIN, GPIO_INPUT_ENABLE | GPIO_PULL_UP | GPIO_EN_INT_BOTH_EDGE,
                                 GPIO_PIN_OUTPUT_HIGH);
    wiced_hal_gpio_register_pin_for_interrupt(BTN_PIN, button_irq, NULL);
    wiced_hal_gpio_clear_pin_interrupt_status(BTN_PIN);
    /* The press that powered the board up is still held: start "down" if it reads down, so
     * its release isn't mistaken for a click. */
    btn_down   = wiced_hal_gpio_get_pin_input_status(BTN_PIN) == 0;
    long_fired = btn_down;                        /* on_button(up) then skips the click */
    boot_press = btn_down;                        /* ...and doesn't light the press color */
    wiced_init_timer(&flash_mode_timer, flash_mode_hold, 0, WICED_MILLI_SECONDS_TIMER);
    wiced_init_timer(&presence_timer, presence_timeout, 0, WICED_SECONDS_TIMER);
    if (boot_press)
    {
        wiced_start_timer(&flash_mode_timer, FLASH_MODE_HOLD_MS);
        presence_mark();                          /* the press that powered it up counts too */
    }
    wiced_start_timer(&poll_timer, BTN_POLL_MS);
}

/* ---- advertising (BTHome v2) ------------------------------------------------------ */
/* While a GATT client is connected the button can't take a second connection, so it
 * advertises non-connectable: BTHome events keep reaching Home Assistant. */
static uint8_t adv_low(void)  { return conn_id ? BTM_BLE_ADVERT_NONCONN_LOW  : BTM_BLE_ADVERT_UNDIRECTED_LOW; }
static uint8_t adv_high(void) { return conn_id ? BTM_BLE_ADVERT_NONCONN_HIGH : BTM_BLE_ADVERT_UNDIRECTED_HIGH; }

static void adv_start(uint8_t mode)
{
    if (mode != adv_mode)
        wiced_bt_start_advertisements(mode, 0, NULL);
}

/* Stop and start again, so new intervals or a new connection state take effect. The
 * BTM_BLE_ADVERT_STATE_CHANGED_EVT handler restarts it in the right mode. */
static void adv_restart(void)
{
    if (adv_mode != BTM_BLE_ADVERT_OFF)
        wiced_bt_start_advertisements(BTM_BLE_ADVERT_OFF, 0, NULL);
    else
        adv_start(adv_low());
}

static void burst_timeout(uint32_t arg)
{
    adv_update(0);                                /* drop the event from the payload */
    if (adv_mode == BTM_BLE_ADVERT_UNDIRECTED_HIGH || adv_mode == BTM_BLE_ADVERT_NONCONN_HIGH)
        adv_start(adv_low());
}

/* The advert (31 bytes): flags (3) + BTHome service data (2 + 3 + objects, + 8 when
 * encrypted) + as much of the name as fits: 15 characters plain, 7 encrypted. */
static void adv_update(uint8_t button_event)
{
    wiced_bt_ble_advert_elem_t elem[3];
    uint8_t  flags = BTM_BLE_GENERAL_DISCOVERABLE_FLAG | BTM_BLE_BREDR_NOT_SUPPORTED;
    uint8_t  objs[6];
    uint8_t  svc[BTHOME_HEADER_LEN + sizeof(objs) + BTHOME_ENC_OVERHEAD];
    uint8_t  n = 0;
    uint16_t name_len = strlen(abtn_name);
    uint16_t name_max = bthome_encrypt ? NAME_ADV_MAX_ENC : NAME_ADV_MAX;

    objs[n++] = 0x00; objs[n++] = ++bthome_pid;   /* packet id: HA de-duplicates on it */
    objs[n++] = 0x01; objs[n++] = battery_pct;    /* battery % */
    if (button_event)
    {
        objs[n++] = 0x3A; objs[n++] = button_event; /* button event */
    }

    elem[0].advert_type = BTM_BLE_ADVERT_TYPE_FLAG;
    elem[0].len         = 1;
    elem[0].p_data      = &flags;
    elem[1].advert_type = BTM_BLE_ADVERT_TYPE_SERVICE_DATA;
    elem[1].len         = bthome_service_data(svc, objs, n, bthome_encrypt ? bthome_key : NULL, local_addr,
                                              bthome_encrypt ? counter_next() : 0);
    elem[1].p_data      = svc;
    elem[2].advert_type = name_len > name_max ? BTM_BLE_ADVERT_TYPE_NAME_SHORT : BTM_BLE_ADVERT_TYPE_NAME_COMPLETE;
    elem[2].len         = name_len > name_max ? name_max : name_len;
    elem[2].p_data      = (uint8_t *)abtn_name;
    wiced_bt_ble_set_raw_advertisement_data(3, elem);

    if (button_event)
    {
        adv_start(adv_high());
        wiced_stop_timer(&burst_timer);
        wiced_start_timer(&burst_timer, BURST_MS);
    }
}

/* Scan response: the AButton service UUID (so Home Assistant can discover the button)
 * and the name, shortened to fit next to it. */
static void scan_rsp_update(void)
{
    static const uint8_t svc_uuid[16] = { ABTN_UUID(0x00) };
    wiced_bt_ble_advert_elem_t rsp[2];
    uint16_t name_len = strlen(abtn_name);

    rsp[0].advert_type = BTM_BLE_ADVERT_TYPE_128SRV_COMPLETE;
    rsp[0].len         = sizeof(svc_uuid);
    rsp[0].p_data      = (uint8_t *)svc_uuid;
    rsp[1].advert_type = name_len > NAME_RSP_MAX ? BTM_BLE_ADVERT_TYPE_NAME_SHORT : BTM_BLE_ADVERT_TYPE_NAME_COMPLETE;
    rsp[1].len         = name_len > NAME_RSP_MAX ? NAME_RSP_MAX : name_len;
    rsp[1].p_data      = (uint8_t *)abtn_name;
    wiced_bt_ble_set_raw_scan_response_data(2, rsp);
}

static void adv_init(void)
{
    wiced_init_timer(&burst_timer, burst_timeout, 0, WICED_MILLI_SECONDS_TIMER);
    cfg_apply_adv_interval();
    scan_rsp_update();
    adv_update(0);
    adv_start(adv_low());
}

/* ---- OTA ------------------------------------------------------------------------ */
static void ota_status(uint8_t status)
{
    static const uint8_t blue[3] = { 0, 0, 40 }, red[3] = { 60, 0, 0 }, green[3] = { 0, 60, 0 };
    switch (status)
    {
    case OTA_FW_UPGRADE_STATUS_STARTED:   ota_active = WICED_TRUE;  memcpy(ota_rgb, blue, 3);  break;
    case OTA_FW_UPGRADE_STATUS_ABORTED:   ota_active = WICED_FALSE; memcpy(ota_rgb, red, 3);   break;
    case OTA_FW_UPGRADE_STATUS_COMPLETED:                           memcpy(ota_rgb, green, 3); break;
    default: return;
    }
    ota_led = WICED_TRUE;
    led_refresh();
}

/* ---- UART flash mode ------------------------------------------------------------ */
/* Entered by holding the button for FLASH_MODE_HOLD_MS right after power-up (physical access
 * only: the flasher needs wires on the test pads anyway). Advertising stops, the LED turns
 * magenta, the HCI UART is brought up (release builds don't start it otherwise), and the
 * stack's HCI event hook is cleared, so vendor commands from the UART reach the ROM handlers.
 * Bluetooth doesn't work again until the next power cycle; P5 keeps the board on. */
static void flash_mode_enter(void)
{
    static const uint8_t magenta[3] = { 120, 0, 120 };
    if (flash_mode)
        return;
    flash_mode = WICED_TRUE;
    WICED_BT_TRACE("AButton: UART flash mode\n");
    wiced_stop_timer(&burst_timer);
    wiced_stop_timer(&battery_timer);
    wiced_stop_timer(&click_timer);
    wiced_stop_timer(&long_timer);
    wiced_bt_start_advertisements(BTM_BLE_ADVERT_OFF, 0, NULL);
    user_anim.on = status_anim.on = WICED_FALSE;
    memcpy(ota_rgb, magenta, 3);
    ota_led  = WICED_TRUE;                        /* top LED priority */
    btn_down = WICED_FALSE;
    led_refresh();
#ifndef WICED_BT_TRACE_ENABLE
    wiced_transport_init(&transport_cfg);
#endif
    btu_hcievt_handler = NULL;
}

static void flash_mode_hold(uint32_t arg)
{
    if (boot_press && btn_down)
        flash_mode_enter();
}

/* ---- the AButton service ---------------------------------------------------------- */
static wiced_bt_gatt_status_t anim_add(const uint8_t *p, uint16_t len)
{
    if (len % 6)
        return WICED_BT_GATT_INVALID_ATTR_LEN;
    for (; len; p += 6, len -= 6)
    {
        led_step_t *s;
        uint16_t    ms = ((uint16_t)p[0] << 8) | p[1];
        if (user_anim.have >= user_anim.n || ms == 0 || p[5] > 1)
            return WICED_BT_GATT_ILLEGAL_PARAMETER;
        s = &user_anim.step[user_anim.have++];
        s->ms   = ms;
        memcpy(s->rgb, p + 2, 3);
        s->fade = p[5];
    }
    if (user_anim.have == user_anim.n)
    {
        player_start(&user_anim);
        led_refresh();
    }
    return WICED_BT_GATT_SUCCESS;
}

static wiced_bt_gatt_status_t led_command(const uint8_t *v, uint16_t len)
{
    if (len < 1)
        return WICED_BT_GATT_INVALID_ATTR_LEN;
    switch (v[0])
    {
    case 0x00:                                    /* stop the animation */
        user_anim.on = WICED_FALSE;
        user_anim.n  = 0;
        led_refresh();
        return WICED_BT_GATT_SUCCESS;
    case 0x01:                                    /* steady color */
        if (len != 4)
            return WICED_BT_GATT_INVALID_ATTR_LEN;
        memcpy(base_rgb, v + 1, 3);
        led_refresh();
        wiced_stop_timer(&led_save_timer);
        wiced_start_timer(&led_save_timer, LED_SAVE_DELAY_S);
        return WICED_BT_GATT_SUCCESS;
    case 0x02:                                    /* start an animation */
        if (len < 3)
            return WICED_BT_GATT_INVALID_ATTR_LEN;
        if (v[2] < 1 || v[2] > ANIM_MAX_STEPS)
            return WICED_BT_GATT_ILLEGAL_PARAMETER;
        user_anim.on     = WICED_FALSE;
        user_anim.repeat = v[1];
        user_anim.n      = v[2];
        user_anim.have   = 0;
        return anim_add(v + 3, len - 3);
    case 0x03:                                    /* more steps */
        if (user_anim.n == 0 || user_anim.have >= user_anim.n)
            return WICED_BT_GATT_ILLEGAL_PARAMETER;
        return anim_add(v + 1, len - 1);
    }
    return WICED_BT_GATT_ILLEGAL_PARAMETER;
}

static wiced_bt_gatt_status_t config_write(const uint8_t *v, uint16_t len)
{
    abtn_config_t c;
    if (len != sizeof(c))
        return WICED_BT_GATT_INVALID_ATTR_LEN;
    memcpy(&c, v, sizeof(c));
    if (!cfg_valid(&c))
        return WICED_BT_GATT_ILLEGAL_PARAMETER;
    if (memcmp(&c, &cfg, sizeof(c)) == 0)
        return WICED_BT_GATT_SUCCESS;             /* unchanged: no NVRAM write */
    cfg = c;
    cfg_save();
    cfg_apply_adv_interval();
    adv_restart();
    led_refresh();                                /* brightness, press color */
    DBG("config: flags %02x brightness %u long %u multi %u adv %u low %u\n", cfg.flags, cfg.brightness,
        cfg.long_press_ms, cfg.multi_click_ms, cfg.adv_interval_ms, cfg.low_batt_pct);
    return WICED_BT_GATT_SUCCESS;
}

static wiced_bt_gatt_status_t name_write(const uint8_t *v, uint16_t len)
{
    nv_name_t      nv;
    wiced_result_t st;
    if (len > IDENTITY_NAME_MAX)
        return WICED_BT_GATT_INVALID_ATTR_LEN;
    if (!printable((const char *)v, len))
        return WICED_BT_GATT_ILLEGAL_PARAMETER;
    memset(&nv, 0, sizeof(nv));
    nv.magic[0] = 'A';
    nv.magic[1] = 'N';
    nv.len      = (uint8_t)len;                   /* 0 = back to the default name */
    memcpy(nv.name, v, len);
    wiced_hal_write_nvram(NV_NAME, sizeof(nv), (uint8_t *)&nv, &st);
    identity_load();
    scan_rsp_update();
    adv_update(0);
    return WICED_BT_GATT_SUCCESS;
}

static void reboot_timeout(uint32_t arg)
{
    wiced_hal_wdog_reset_system();
}

static wiced_bt_gatt_status_t command_write(const uint8_t *v, uint16_t len)
{
    if (len != 1)
        return WICED_BT_GATT_INVALID_ATTR_LEN;
    switch (v[0])
    {
    case 0x01:                                    /* reboot, after the write response is out */
        wiced_start_timer(&reboot_timer, 300);
        return WICED_BT_GATT_SUCCESS;
    case 0x02:                                    /* restore defaults (the BTHome key stays) */
        cfg = cfg_default;
        cfg_save();
        cfg_apply_adv_interval();
        memset(base_rgb, 0, 3);
        led_save(0);
        name_write((const uint8_t *)"", 0);
        adv_restart();
        led_refresh();
        return WICED_BT_GATT_SUCCESS;
    case 0x03:
        status_identify();
        return WICED_BT_GATT_SUCCESS;
    }
    return WICED_BT_GATT_ILLEGAL_PARAMETER;
}

/* ---- GATT ----------------------------------------------------------------------- */
static wiced_bt_gatt_status_t read_value(wiced_bt_gatt_read_t *p, const uint8_t *val, uint16_t len)
{
    uint16_t to_copy;
    if (p->offset > len)
        return WICED_BT_GATT_INVALID_OFFSET;
    to_copy = len - p->offset;
    if (*p->p_val_len < to_copy)
        to_copy = *p->p_val_len;
    memcpy(p->p_val, val + p->offset, to_copy);
    *p->p_val_len = to_copy;
    return WICED_BT_GATT_SUCCESS;
}

static wiced_bt_gatt_status_t gatt_read(uint16_t conn, wiced_bt_gatt_read_t *p)
{
    uint8_t led[4];
    if (p->handle > HANDLE_OTA_FW_UPGRADE_SERVICE)
        return wiced_ota_fw_upgrade_read_handler(conn, p);
    switch (p->handle)
    {
    case HDL_GAP_NAME_VAL:
    case HDL_ABTN_NAME_VAL:      return read_value(p, (const uint8_t *)abtn_name, strlen(abtn_name));
    case HDL_GAP_APPEARANCE_VAL: return read_value(p, (const uint8_t *)&app_cfg_settings.gatt_cfg.appearance, 2);
    case HDL_DIS_MFR_VAL:        return read_value(p, (const uint8_t *)dis_mfr, sizeof(dis_mfr) - 1);
    case HDL_DIS_MODEL_VAL:      return read_value(p, (const uint8_t *)dis_model, sizeof(dis_model) - 1);
    case HDL_DIS_FW_VAL:         return read_value(p, (const uint8_t *)dis_fw, sizeof(dis_fw) - 1);
    case HDL_ABTN_CONFIG_VAL:    return read_value(p, (const uint8_t *)&cfg, sizeof(cfg));
    case HDL_ABTN_LED_VAL:
        memcpy(led, base_rgb, 3);
        led[3] = user_anim.on;
        return read_value(p, led, sizeof(led));
    case HDL_ABTN_KEY_VAL:
        led[0] = bthome_encrypt;                  /* on/off only: the key is never read back */
        return read_value(p, led, 1);
    }
    return WICED_BT_GATT_INVALID_HANDLE;
}

static wiced_bt_gatt_status_t gatt_write(uint16_t conn, wiced_bt_gatt_write_t *p)
{
    if (p->handle > HANDLE_OTA_FW_UPGRADE_SERVICE)
        return wiced_ota_fw_upgrade_write_handler(conn, p);
    if (p->is_prep || p->offset)
        return WICED_BT_GATT_REQ_NOT_SUPPORTED;   /* every AButton write fits in one PDU */
    switch (p->handle)
    {
    case HDL_ABTN_LED_VAL:    return led_command(p->p_val, p->val_len);
    case HDL_ABTN_CONFIG_VAL: return config_write(p->p_val, p->val_len);
    case HDL_ABTN_NAME_VAL:   return name_write(p->p_val, p->val_len);
    case HDL_ABTN_CMD_VAL:    return command_write(p->p_val, p->val_len);
    case HDL_ABTN_KEY_VAL:    return key_write(p->p_val, p->val_len);
    }
    return WICED_BT_GATT_WRITE_NOT_PERMIT;
}

static wiced_bt_gatt_status_t gatt_request(wiced_bt_gatt_attribute_request_t *p)
{
    switch (p->request_type)
    {
    case GATTS_REQ_TYPE_READ:
        return gatt_read(p->conn_id, &p->data.read_req);
    case GATTS_REQ_TYPE_WRITE:
        return gatt_write(p->conn_id, &p->data.write_req);
    case GATTS_REQ_TYPE_WRITE_EXEC:
    case GATTS_REQ_TYPE_MTU:
        return WICED_BT_GATT_SUCCESS;
    case GATTS_REQ_TYPE_CONF:
        if (p->data.handle > HANDLE_OTA_FW_UPGRADE_SERVICE)
            return wiced_ota_fw_upgrade_indication_cfm_handler(p->conn_id, p->data.handle);
        return WICED_BT_GATT_INVALID_HANDLE;
    default:
        return WICED_BT_GATT_INVALID_PDU;
    }
}

static wiced_bt_gatt_status_t gatt_callback(wiced_bt_gatt_evt_t event, wiced_bt_gatt_event_data_t *p_data)
{
    if (event == GATT_CONNECTION_STATUS_EVT)
    {
        wiced_bt_gatt_connection_status_t *s = &p_data->connection_status;
        DBG("gatt %s\n", s->connected ? "connected" : "disconnected");
        if (s->connected)
        {
            conn_id = s->conn_id;
        }
        else
        {
            conn_id    = 0;
            ota_active = WICED_FALSE;
            if (ota_led && !flash_mode)
            {
                ota_led = WICED_FALSE;            /* a finished or aborted OTA: back to normal */
                led_refresh();
            }
        }
        adv_restart();                            /* connectable <-> non-connectable */
        wiced_ota_fw_upgrade_connection_status_event(s);
        return WICED_BT_GATT_SUCCESS;
    }
    if (event == GATT_ATTRIBUTE_REQUEST_EVT)
        return gatt_request(&p_data->attribute_request);
    return WICED_BT_GATT_INVALID_PDU;
}

/* ---- sleep ---------------------------------------------------------------------- */
/* Sleep is never allowed: AButton's "off" is the P5 power latch, like stock's. Without
 * wiced_sleep_configure (0.1.3-0.1.5) the release build, which has no transport to keep it
 * awake, died within minutes on a test unit (2026-11-19), so the permit handler is back. No
 * wake source: the button is polled, and the device-wake GPIO isn't needed when sleep never
 * happens (0.1.2 gave it P6, a suspect for the dead button interrupt). */
static uint32_t sleep_permit(wiced_sleep_poll_type_t type)
{
    return type == WICED_SLEEP_POLL_TIME_TO_SLEEP ? 0 : WICED_SLEEP_NOT_ALLOWED;
}

static void sleep_init(void)
{
    static wiced_sleep_config_t sleep_cfg;
    sleep_cfg.sleep_mode           = WICED_SLEEP_MODE_NO_TRANSPORT;
    sleep_cfg.host_wake_mode       = WICED_SLEEP_WAKE_ACTIVE_LOW;
    sleep_cfg.device_wake_mode     = WICED_SLEEP_WAKE_ACTIVE_LOW;
    sleep_cfg.device_wake_source   = 0;
    sleep_cfg.device_wake_gpio_num = BTN_PIN;
    sleep_cfg.sleep_permit_handler = sleep_permit;
    wiced_sleep_configure(&sleep_cfg);
}

/* ---- start-up ------------------------------------------------------------------- */
static void app_init(void)
{
    /* The stack and HAL are up now (BTM_ENABLED): safe to read flash and configure sleep.
     * First, latch the power on: a press only powers the board for ~2 s (see POWER_HOLD_PIN). */
    wiced_hal_gpio_configure_pin(POWER_HOLD_PIN, GPIO_OUTPUT_ENABLE, GPIO_PIN_OUTPUT_HIGH);
#ifdef WICED_BT_TRACE_ENABLE
    wiced_init_timer(&heartbeat_timer, heartbeat, 0, WICED_MILLI_SECONDS_PERIODIC_TIMER);
    wiced_start_timer(&heartbeat_timer, 500);
#endif
    DBG("app_init: settings\n");
    cfg_load();
    identity_load();                              /* reads NVRAM and the serial flash */
    app_cfg_settings.device_name = (uint8_t *)abtn_name;
    wiced_bt_dev_read_local_addr(local_addr);     /* part of the BTHome encryption nonce */
    key_load();
    DBG("addr %B, BTHome encryption %s, counter %u\n", local_addr, bthome_encrypt ? "on" : "off",
        (unsigned)bthome_counter);
    DBG("app_init: sleep (never)\n");
    sleep_init();
    WICED_BT_TRACE("AButton %s booting as %s\n", ABTN_FW_VERSION, abtn_name);

    DBG("app_init: gatt\n");
    wiced_bt_gatt_register(gatt_callback);
    wiced_bt_gatt_db_init(abtn_gatt_db, sizeof(abtn_gatt_db));
    wiced_bt_set_pairable_mode(WICED_FALSE, 0);
    wiced_init_timer(&reboot_timer, reboot_timeout, 0, WICED_MILLI_SECONDS_TIMER);

    DBG("app_init: ota\n");
    if (!wiced_ota_fw_upgrade_init(&ecdsa256_public_key, ota_status))
    {
        WICED_BT_TRACE("OTA init failed\n");
    }

    DBG("app_init: gpio/led\n");
    wiced_hal_gpio_configure_pin(AUX_OUT_PIN, GPIO_OUTPUT_ENABLE, GPIO_PIN_OUTPUT_LOW);
    led_init();
    DBG("app_init: button\n");
    button_init();
    DBG("app_init: adc\n");
    wiced_hal_adc_init();
    battery_read();                               /* the LED is still dark, even if the boot press is held */
    wiced_init_timer(&battery_timer, battery_timeout, 0, WICED_SECONDS_PERIODIC_TIMER);
    wiced_start_timer(&battery_timer, BATTERY_PERIOD_S);
    DBG("app_init: adv (battery %u%%)\n", battery_pct);
    adv_init();

    led_load();                                   /* the steady color, if kept */
    status_boot();                                /* boot blink: white, or the battery color */
    WICED_BT_TRACE("AButton up\n");
}

static wiced_result_t management_callback(wiced_bt_management_evt_t event, wiced_bt_management_evt_data_t *p_data)
{
    DBG("mgmt event %u\n", (unsigned)event);
    switch (event)
    {
    case BTM_ENABLED_EVT:
        app_init();
        break;
    case BTM_BLE_ADVERT_STATE_CHANGED_EVT:
        adv_mode = p_data->ble_advert_state_changed;
        if (adv_mode == BTM_BLE_ADVERT_OFF && !flash_mode)
            adv_start(adv_low());                 /* advertise forever */
        break;
    case BTM_SECURITY_REQUEST_EVT:
        wiced_bt_ble_security_grant(p_data->security_request.bd_addr, WICED_BT_SUCCESS);
        break;
    case BTM_PAIRED_DEVICE_LINK_KEYS_REQUEST_EVT:
    case BTM_LOCAL_IDENTITY_KEYS_REQUEST_EVT:
        return WICED_BT_ERROR;                    /* no bonding */
    default:
        break;
    }
    return WICED_BT_SUCCESS;
}

void application_start(void)
{
    /* Do only this here: the HAL and flash aren't ready until the stack enables (BTM_ENABLED).
     * Reading the serial flash or configuring sleep before that hard-faults the chip. */
#ifdef WICED_BT_TRACE_ENABLE
    wiced_transport_init(&transport_cfg);
    wiced_set_debug_uart(WICED_ROUTE_DEBUG_TO_WICED_UART);
    DBG("application_start %s\n", ABTN_FW_VERSION);
#endif
    {
        wiced_result_t r = wiced_bt_stack_init(management_callback, &app_cfg_settings, app_buf_pools);
        DBG("stack_init returned %d\n", (int)r);
        (void)r;
    }
}
