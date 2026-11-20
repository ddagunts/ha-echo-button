/*
 * BTHome v2 service data, plain or encrypted (AES-128-CCM, 4-byte MIC), as Home Assistant's
 * BTHome integration (bthome-ble) decodes it. Portable C with no SDK dependencies, so the
 * host tests (tests/test_bthome_crypto.py) compile and check this same file.
 */
#ifndef ABTN_BTHOME_H
#define ABTN_BTHOME_H

#include <stdint.h>

#define BTHOME_KEY_LEN          16
#define BTHOME_ENC_OVERHEAD     8                   /* counter (4) + MIC (4) */
#define BTHOME_HEADER_LEN       3                   /* UUID 0xFCD2 (2) + device info (1) */

/* AES-128 encryption of one block (key expanded on the fly). */
void aes128_encrypt(const uint8_t key[16], const uint8_t in[16], uint8_t out[16]);

/* AES-128-CCM with a 13-byte nonce, no associated data and a 4-byte MIC: writes len
 * bytes of ciphertext to out and the MIC to mic. len <= 255. */
void aes128_ccm4_encrypt(const uint8_t key[16], const uint8_t nonce[13],
                         const uint8_t *in, uint8_t len, uint8_t *out, uint8_t mic[4]);

/* Fill out with BTHome v2 service data: UUID 0xFCD2 (little-endian), the device info byte,
 * then the objects. With a key, the objects are encrypted and followed by the counter
 * (little-endian) and the MIC; the nonce is mac (most significant byte first, as the
 * receiver sees the advertiser address), UUID, device info and counter. key NULL = plain.
 * out must hold BTHOME_HEADER_LEN + n (+ BTHOME_ENC_OVERHEAD). Returns the length. */
uint8_t bthome_service_data(uint8_t *out, const uint8_t *objs, uint8_t n,
                            const uint8_t *key, const uint8_t mac[6], uint32_t counter);

#endif
