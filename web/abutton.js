// AButton GATT protocol for the browser. Mirrors custom_components/echo_button/abutton.py
// (the Python encoder) and firmware/abutton/abutton.c ("GATT protocol"). Every write fits
// in 20 bytes, the default ATT MTU.

const uuid = (x) => `0e5a10${x}-a8b9-4c3b-9d1a-ab7ff0ab7700`;

export const SERVICE = uuid("00");
export const LED = uuid("01");
export const CONFIG = uuid("02");
export const NAME = uuid("03");
export const COMMAND = uuid("04");
export const KEY = uuid("05");
export const POWER = uuid("06");

export const WRITE_SIZE = 20;
export const MAX_STEPS = 40;
export const NAME_MAX = 26;
export const KEY_LEN = 16;
export const PRESENCE_S = 60;

export const CMD_REBOOT = 0x01;
export const CMD_DEFAULTS = 0x02;
export const CMD_IDENTIFY = 0x03;

export const FLAG_LOW_BATTERY_WARNING = 0x01;
export const FLAG_BOOT_BATTERY = 0x02;
export const FLAG_RESTORE_LED = 0x04;
const FLAGS_ALL = 0x07;
const CONFIG_VERSION = 1;
const CONFIG_LEN = 13;

export const DEFAULT_CONFIG = Object.freeze({
  flags: FLAGS_ALL, brightness: 255, pressColor: [255, 255, 255], longPressMs: 1000,
  multiClickMs: 350, advertIntervalMs: 250, lowBatteryPct: 20,
});

function checkColor(c) {
  if (!Array.isArray(c) || c.length !== 3 || !c.every((v) => Number.isInteger(v) && v >= 0 && v <= 255)) {
    throw new RangeError(`not an RGB color: ${JSON.stringify(c)}`);
  }
  return c;
}

export function checkConfig(cfg) {
  const range = (v, lo, hi, what) => {
    if (!Number.isInteger(v) || v < lo || v > hi) throw new RangeError(`${what} must be ${lo}–${hi}`);
  };
  if ((cfg.flags & ~FLAGS_ALL) !== 0) throw new RangeError("unknown flags");
  range(cfg.brightness, 1, 255, "Brightness");
  range(cfg.longPressMs, 300, 5000, "Long press");
  range(cfg.multiClickMs, 100, 1000, "Multi-click window");
  range(cfg.advertIntervalMs, 100, 10000, "Advert interval");
  range(cfg.lowBatteryPct, 0, 100, "Low battery level");
  checkColor(cfg.pressColor);
  return cfg;
}

export function encodeConfig(cfg) {
  checkConfig(cfg);
  const b = new Uint8Array(CONFIG_LEN);
  const v = new DataView(b.buffer);
  b[0] = CONFIG_VERSION;
  b[1] = cfg.flags;
  b[2] = cfg.brightness;
  b.set(cfg.pressColor, 3);
  v.setUint16(6, cfg.longPressMs, true);
  v.setUint16(8, cfg.multiClickMs, true);
  v.setUint16(10, cfg.advertIntervalMs, true);
  b[12] = cfg.lowBatteryPct;
  return b;
}

export function decodeConfig(data) {
  const b = bytes(data);
  if (b.length !== CONFIG_LEN) throw new RangeError(`config is ${b.length} bytes, expected ${CONFIG_LEN}`);
  if (b[0] !== CONFIG_VERSION) throw new RangeError(`config version ${b[0]}; this page knows ${CONFIG_VERSION}`);
  const v = new DataView(b.buffer, b.byteOffset, b.length);
  return {
    flags: b[1], brightness: b[2], pressColor: [b[3], b[4], b[5]], longPressMs: v.getUint16(6, true),
    multiClickMs: v.getUint16(8, true), advertIntervalMs: v.getUint16(10, true), lowBatteryPct: b[12],
  };
}

export function decodeLed(data) {
  const b = bytes(data);
  if (b.length !== 4) throw new RangeError(`LED state is ${b.length} bytes, expected 4`);
  return { color: [b[0], b[1], b[2]], animating: Boolean(b[3]) };
}

export const ledColor = (color) => Uint8Array.of(0x01, ...checkColor(color || [0, 0, 0]));
export const ledStop = () => Uint8Array.of(0x00);

// steps: [{ ms, color: [r, g, b], fade }]; repeat 0 = forever. Returns the writes, in order.
export function ledAnimation(steps, repeat = 1, writeSize = WRITE_SIZE) {
  if (steps.length < 1 || steps.length > MAX_STEPS) throw new RangeError(`an animation has 1–${MAX_STEPS} steps`);
  if (!Number.isInteger(repeat) || repeat < 0 || repeat > 255) throw new RangeError("repeat must be 0–255 (0 = forever)");
  const writes = [];
  let pdu = [0x02, repeat, steps.length];
  for (const s of steps) {
    if (!Number.isInteger(s.ms) || s.ms < 1 || s.ms > 0xffff) throw new RangeError("a step lasts 1–65535 ms");
    const step = [s.ms >> 8, s.ms & 0xff, ...checkColor(s.color), s.fade ? 1 : 0];
    if (pdu.length + step.length > writeSize) {
      writes.push(Uint8Array.from(pdu));
      pdu = [0x03];
    }
    pdu.push(...step);
  }
  writes.push(Uint8Array.from(pdu));
  return writes;
}

// "ff0000:300 000000:300:fade" (the abutton_ble.py animate format) -> steps
export function parseSteps(text) {
  const words = text.trim().split(/[\s,]+/).filter(Boolean);
  if (!words.length) throw new RangeError("no steps");
  return words.map((w) => {
    const [hex, ms, fade, extra] = w.split(":");
    if (extra !== undefined || (fade !== undefined && fade !== "fade") || !/^\d+$/.test(ms || "")) {
      throw new RangeError(`step "${w}": use COLOR:MS or COLOR:MS:fade`);
    }
    return { ms: Number(ms), color: parseHexColor(hex), fade: fade === "fade" };
  });
}

export function encodeName(name) {
  if (name.length > NAME_MAX || !/^[\x20-\x7e]*$/.test(name)) {
    throw new RangeError(`a name is 0–${NAME_MAX} printable ASCII characters`);
  }
  return new TextEncoder().encode(name);
}

export function parseHexColor(text) {
  const t = String(text).trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(t)) throw new RangeError(`not a hex color: ${text}`);
  return [0, 2, 4].map((i) => parseInt(t.slice(i, i + 2), 16));
}

export const hexColor = (c) => "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");

export function generateKey() {
  const key = new Uint8Array(KEY_LEN);
  do crypto.getRandomValues(key); while (key.every((b) => b === 0));
  return key;
}

export function parseKey(text) {
  const t = String(text).trim();
  if (!/^[0-9a-fA-F]{32}$/.test(t)) throw new RangeError("a BTHome key is 32 hex characters");
  const key = Uint8Array.from({ length: KEY_LEN }, (_, i) => parseInt(t.slice(2 * i, 2 * i + 2), 16));
  if (key.every((b) => b === 0)) throw new RangeError("the all-zero key is not allowed");
  return key;
}

export const keyHex = (key) => Array.from(key, (b) => b.toString(16).padStart(2, "0")).join("");
export const KEY_OFF = Uint8Array.of(0x00);

// Power settings: { txPower: dBm or null (the radio's default, its maximum), sleep }
export const TX_POWER_STEPS = [12, 8, 4, 0, -4, -8, -12, -16];
const TX_POWER_DEFAULT = 0x7f;
const POWER_VERSION = 1;

export function encodePower({ txPower, sleep }) {
  if (txPower !== null && !(Number.isInteger(txPower) && txPower >= -16 && txPower <= 12)) {
    throw new RangeError("transmit power is -16 to 12 dBm, or the default");
  }
  return Uint8Array.of(POWER_VERSION, txPower === null ? TX_POWER_DEFAULT : txPower & 0xff, sleep ? 1 : 0, 0);
}

export function decodePower(data) {
  const b = bytes(data);
  if (b.length !== 4) throw new RangeError(`power settings are ${b.length} bytes, expected 4`);
  if (b[0] !== POWER_VERSION) throw new RangeError(`power settings version ${b[0]}; this page knows ${POWER_VERSION}`);
  const tx = b[1] > 127 ? b[1] - 256 : b[1];
  return { txPower: b[1] === TX_POWER_DEFAULT ? null : tx, sleep: Boolean(b[2]) };
}

function bytes(data) {
  if (data instanceof DataView) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  return data instanceof Uint8Array ? data : new Uint8Array(data);
}
