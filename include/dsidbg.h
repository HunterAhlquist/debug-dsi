// SPDX-License-Identifier: MIT
// debug-dsi: remote debugging agent for DSi homebrew (BlocksDS, DSi mode).
//
// The agent joins Wi-Fi, connects to the `dsidbg` daemon on your computer and lets it press buttons, take
// screenshots, read logs, profile, copy files to and from the SD card, and reboot into a new build. It also turns
// crashes and hangs into register dumps the daemon can symbolize.
//
// Typical use:
//
//     dsidbg_config cfg;
//     dsidbg_config_defaults(&cfg);
//     cfg.app_name = "mygame";
//     cfg.capture_bank = 3;        // VRAM bank D is free for screenshots
//     dsidbg_init(&cfg);
//     while (1) {
//         dsidbg_scan_keys();      // instead of scanKeys(); read dsidbg_keys_held()/down()
//         ...
//         dsidbg_frame();          // once per frame: heartbeat, network, injected input
//         swiWaitForVBlank();
//     }
//
// Safety: the agent only reads and writes files under "sd:/". It never mounts the DSi NAND, and it turns NAND
// write protection on at start-up.
#ifndef DSIDBG_H
#define DSIDBG_H

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DSIDBG_VERSION "0.1.1"

// Screens for screenshots.
#define DSIDBG_SCREEN_TOP    0 // the main 2D/3D engine (display capture)
#define DSIDBG_SCREEN_BOTTOM 1 // only through the app's screenshot hook

// dsidbg_config.flags
#define DSIDBG_NO_CRASH_HANDLER (1u << 0) // keep the app's own exception handler
#define DSIDBG_NO_IRQ_HOOK      (1u << 1) // no profiler, no hang watchdog
#define DSIDBG_TRACE            (1u << 2) // log the agent's own connection and transfer steps
#define DSIDBG_ECHO_LOG         (1u << 3) // also print every log line on the libnds console
// Save crash reports that can't be sent to sd:/dsidbg/crash/ (the loader uploads them later). Off by default: if the
// crash interrupted a file operation, writing another file can corrupt the card's filesystem. Only for apps that
// don't use the card while they run.
#define DSIDBG_SAVE_CRASHES     (1u << 4)

typedef struct dsidbg_config {
    const char *app_name;    // shown by the daemon; default "app"
    const char *build_id;    // e.g. a git hash; default ""
    const char *config_path; // default "sd:/dsidbg/config.ini"

    // Overrides for the config file. NULL or 0 means "read it from the file".
    const char *host;        // daemon IPv4 address
    uint16_t port;           // daemon port (default 7710)
    const char *key;         // shared secret
    const char *ssid;        // Wi-Fi network; NULL uses the console's saved connections
    const char *pass;        // Wi-Fi password

    // VRAM bank (0 = A ... 3 = D) the agent may overwrite to capture the top screen, or -1 for none. The bank must
    // be unused while a screenshot is taken: capture writes 96 KiB into it.
    int capture_bank;

    // Seconds without dsidbg_frame() before the agent reports a hang (0 = off). Default 15.
    unsigned watchdog_seconds;

    unsigned flags;

    // Send queue size in KiB for logs, status and data (0 = 16). Allocated by dsidbg_init() along with a 12 KiB
    // receive buffer; messages that don't fit while the network is slow are dropped (counted as log_drops).
    unsigned tx_queue_kib;

    // Optional: fill a 256x192 RGB555 image of `screen`. Return 0 on success. Called before the display capture
    // for the top screen, and is the only way to capture the bottom screen.
    int (*screenshot)(int screen, uint16_t *pixels);

    // Optional: handle a text command from the daemon (`dsidbg cmd ...`). Write a reply into `reply` and return 0
    // on success or a negative value on failure.
    int (*command)(const char *line, char *reply, size_t reply_cap);
} dsidbg_config;

void dsidbg_config_defaults(dsidbg_config *cfg);

// Starts Wi-Fi and the agent. Call once, after fatInitDefault() (the agent calls it if needed). Returns 0 on
// success; on failure the app keeps running without the agent.
int dsidbg_init(const dsidbg_config *cfg);

// Call once per frame. Counts frames for input injection and the watchdog, and lets the network run.
void dsidbg_frame(void);

// Tells the watchdog the app is alive during long loads that don't call dsidbg_frame().
void dsidbg_heartbeat(void);

// 1 when the daemon is connected and authenticated.
int dsidbg_connected(void);

// Short human-readable state, e.g. "wifi: ASSOCIATING" or "connected 192.168.1.20".
const char *dsidbg_status(void);

// Logging. Lines are buffered and sent from dsidbg_frame(). Not IRQ-safe.
void dsidbg_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void dsidbg_vlog(const char *fmt, va_list args);
void dsidbg_write(const char *text, size_t len);

// Sends stdout/stderr to the daemon. With echo != 0 the text also goes to the libnds console.
void dsidbg_capture_stdout(int echo);

// Sends app-defined binary data on a channel (`dsidbg data CHANNEL`). Returns 0, or -1 if the queue is full.
int dsidbg_send(uint16_t channel, const void *data, size_t len);

// Input. dsidbg_scan_keys() replaces scanKeys(): it reads the hardware and merges the daemon's injected presses.
void dsidbg_scan_keys(void);
uint32_t dsidbg_keys_held(void);
uint32_t dsidbg_keys_down(void);
uint32_t dsidbg_keys_up(void);
// For apps that read keys themselves: returns `held` with this frame's injected keys applied.
uint32_t dsidbg_keys(uint32_t held);
// If an injected touch is active this frame, stores it and returns 1.
int dsidbg_touch(int16_t *x, int16_t *y);

// Stops the app, shows `message`, and reports it to the daemon like a crash.
void dsidbg_panic(const char *message) __attribute__((noreturn));

// Reboots the console. With a path ("sd:/...nds") Unlaunch boots that file; with NULL it boots Unlaunch's default.
void dsidbg_reboot(const char *sd_path) __attribute__((noreturn));

#ifdef __cplusplus
}
#endif

#endif // DSIDBG_H
