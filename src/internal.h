// SPDX-License-Identifier: MIT
// Internal interface between the portable agent (agent.c) and a platform (platform_nds.c, platform_host.c).
// Protocol constants must match host/dsidbg/proto.py and docs/protocol.md.
#ifndef DSIDBG_INTERNAL_H
#define DSIDBG_INTERNAL_H

#include <stddef.h>
#include <stdint.h>

#include "dsidbg.h"

#define PROTO_VERSION 1
#define PROTO_HEADER  8           // u32 payload length, u16 type, u16 seq (little-endian)
#define PROTO_MAX_RX  (20 * 1024) // largest message the device accepts

enum {
    // Handshake
    MSG_HELLO = 0x0001,     // dev->host: u16 version, u16 0, nonce_d[16], "app\0build\0"
    MSG_CHALLENGE = 0x0002, // host->dev: nonce_h[16], HMAC(key, "dsidbg-host" nonce_d nonce_h)
    MSG_PROOF = 0x0003,     // dev->host: HMAC(key, "dsidbg-dev" nonce_h nonce_d)

    // Host -> device. Each one gets an ACK with the same seq unless noted.
    MSG_PING = 0x0100,        // reply PONG with the same payload
    MSG_INPUT = 0x0101,       // n x {u32 delay, u16 frames, u16 flags, u32 keys, i16 x, i16 y}
    MSG_INPUT_CLEAR = 0x0102,
    MSG_SHOT = 0x0103,        // u8 screen; reply SHOT_DATA (same seq) or ACK on error
    MSG_REBOOT = 0x0104,      // optional "sd:/path.nds"
    MSG_PUT_BEGIN = 0x0105,   // u32 size, u32 crc32, path
    MSG_PUT_DATA = 0x0106,    // bytes (no ACK)
    MSG_PUT_END = 0x0107,
    MSG_GET = 0x0108,         // path; reply FILE_DATA..., FILE_END (same seq) or ACK on error
    MSG_LIST = 0x0109,        // path; ACK text "D|F\tsize\tname\n"...
    MSG_DELETE = 0x010A,      // path (files only)
    MSG_PROF_START = 0x010B,
    MSG_PROF_STOP = 0x010C,   // flushes PROF samples, then ACK
    MSG_APP_CMD = 0x010D,     // text; ACK carries the app's reply
    MSG_WATCHDOG = 0x010E,    // u32 seconds (0 = off)

    // Device -> host
    MSG_PONG = 0x0200,
    MSG_ACK = 0x0201,       // i32 status, text
    MSG_LOG = 0x0202,       // text
    MSG_INFO = 0x0203,      // "key=value\n" text, sent once after the handshake
    MSG_STATUS = 0x0204,    // "key=value\n" text, about once a second
    MSG_SHOT_DATA = 0x0205, // u8 screen, u8 format (0 = RGB555), u16 w, u16 h, u16 0, pixels
    MSG_FILE_DATA = 0x0206, // bytes
    MSG_FILE_END = 0x0207,  // i32 status, u32 size, u32 crc32
    MSG_PROF = 0x0208,      // u32 hz, u32 pc[]
    MSG_CRASH = 0x0209,     // dsidbg_crash (128 bytes), stack bytes
    MSG_DATA = 0x020A,      // u16 channel, u16 0, bytes
};

// Input record flags
#define INPUT_REPLACE 1u // ignore the real buttons while this record is active

// Crash kinds
enum { CRASH_DATA_ABORT = 1, CRASH_UNDEFINED = 2, CRASH_HANG = 3, CRASH_PANIC = 4 };
#define CRASH_FROM_FILE 1u // flags: sent from a dump saved on the SD card

typedef struct dsidbg_crash {
    uint32_t kind;
    uint32_t regs[16]; // r0..r15; r15 is the faulting/interrupted instruction
    uint32_t cpsr;     // of the crashed code
    uint32_t fault_addr;
    uint32_t frame;
    uint32_t stack_base;
    uint32_t stack_len;
    uint32_t flags;
    char msg[36];
} dsidbg_crash;
_Static_assert(sizeof(dsidbg_crash) == 128, "crash header is part of the protocol");

#define CRASH_STACK_MAX 1024

// Shared state (agent.c)
extern dsidbg_crash dsidbg__crash;
extern volatile int dsidbg__in_panic;
void dsidbg__prof_sample(uint32_t pc); // IRQ context
void dsidbg__panic_main(void) __attribute__((noreturn));

// Platform (one of platform_*.c)
uint32_t plat_ms(void);
int plat_init(const dsidbg_config *cfg, const char *ssid, const char *pass); // 0 = ok
int plat_fs_init(void);
int plat_net_poll(void);         // 1 when the network has an address
const char *plat_net_state(void);
uint32_t plat_ip(void);          // network byte order
uint32_t plat_entropy(void);     // varies between boots (clock, timers)
int plat_discovery_targets(uint32_t *addrs, int max); // broadcast addresses (network order)
uint32_t plat_heap_used(void);   // bytes allocated from the heap (0 if unknown)
void plat_yield(void);           // let the network stack run
void plat_wait_frame(void);      // wait about one frame (and yield)
void plat_heartbeat(void);       // reset the watchdog
void plat_set_watchdog(unsigned seconds);
uint32_t plat_prof_hz(void);
uint32_t plat_hw_keys(void);     // scan and return the real buttons held
int plat_capture(int screen, uint16_t *pixels, int bank); // 0 = ok
void plat_reboot(const char *sd_path) __attribute__((noreturn));
void plat_info(char *buf, size_t cap);
void plat_crash_install(unsigned flags);
void plat_crash_show(const dsidbg_crash *crash, const char *state);
int plat_valid_ram(uint32_t addr, uint32_t len);
void plat_console_write(const char *text, size_t len);
void plat_hook_stdout(int enable);
// Maps "sd:/x" to a local path. Returns 0, or -1 if the path is not allowed.
int plat_path(const char *sd_path, char *out, size_t cap);

// Portable helpers
void dsidbg__hmac_sha1(const void *key, size_t key_len, const void *msg, size_t msg_len, uint8_t out[20]);
uint32_t dsidbg__crc32(uint32_t crc, const void *data, size_t len);

#endif // DSIDBG_INTERNAL_H
