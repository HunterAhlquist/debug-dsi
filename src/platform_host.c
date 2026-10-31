// SPDX-License-Identifier: MIT
// Host platform for the simulator (sim/): POSIX sockets, files under a root directory, fake screen and keys.
// It runs the same agent.c as the DSi so the protocol and the daemon can be tested without hardware.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <arpa/inet.h>

#include "internal.h"

static const char *sd_root = ".";
static unsigned watchdog_s;
static uint32_t last_beat;

void dsidbg_sim_set_root(const char *root) { sd_root = root; }

uint32_t plat_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

int plat_init(const dsidbg_config *cfg, const char *ssid, const char *pass)
{
    (void)cfg; (void)ssid; (void)pass;
    return 0;
}

int plat_fs_init(void) { return 0; }
int plat_net_poll(void) { return 1; }
const char *plat_net_state(void) { return "host network"; }
uint32_t plat_ip(void) { return 0x0100007f; }

uint32_t plat_entropy(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint32_t)ts.tv_nsec ^ (uint32_t)ts.tv_sec * 2654435761u ^ (uint32_t)getpid() << 16;
}
uint32_t plat_heap_used(void) { return 0; }

int plat_discovery_targets(uint32_t *addrs, int max)
{
    // The simulator asks on loopback (DSIDBG_SIM_DISCOVER overrides it, e.g. a broadcast address).
    const char *to = getenv("DSIDBG_SIM_DISCOVER");
    struct in_addr a;
    if (max < 1 || inet_pton(AF_INET, to ? to : "127.0.0.1", &a) != 1) return 0;
    addrs[0] = a.s_addr;
    return 1;
}
void plat_yield(void) { usleep(200); }
void plat_wait_frame(void) { usleep(16667); }
void plat_heartbeat(void) { last_beat = plat_ms(); }
void plat_set_watchdog(unsigned seconds) { watchdog_s = seconds; }
uint32_t plat_prof_hz(void) { return 1000; }
uint32_t plat_hw_keys(void) { return 0; }

int plat_capture(int screen, uint16_t *pixels, int bank)
{
    (void)bank;
    // Gradient test pattern: red grows to the right, green downwards, blue marks the screen.
    for (int y = 0; y < 192; y++)
        for (int x = 0; x < 256; x++)
            pixels[y * 256 + x] = (uint16_t)((x * 31 / 255) | ((y * 31 / 191) << 5) | ((screen ? 31 : 0) << 10) | 0x8000);
    return 0;
}

void plat_reboot(const char *sd_path)
{
    printf("SIM REBOOT %s\n", sd_path ? sd_path : "(default)");
    fflush(stdout);
    exit(sd_path ? 4 : 3);
}

void plat_info(char *buf, size_t cap) { snprintf(buf, cap, "mode=sim\nroot=%s\n", sd_root); }
void plat_crash_install(unsigned flags) { (void)flags; }
int plat_panic_on_main_thread(dsidbg_crash *crash) { (void)crash; return 1; }

void plat_crash_show(const dsidbg_crash *c, const char *state)
{
    fprintf(stderr, "SIM CRASH kind=%u pc=%08x msg=%s [%s]\n", (unsigned)c->kind, (unsigned)c->regs[15], c->msg, state);
}

int plat_valid_ram(uint32_t addr, uint32_t len)
{
    (void)addr; (void)len;
    return 0; // host addresses don't fit in 32 bits; skip the stack copy
}

void plat_console_write(const char *text, size_t len) { fwrite(text, 1, len, stdout); }
void plat_hook_stdout(int enable) { (void)enable; }

int plat_path(const char *sd_path, char *out, size_t cap)
{
    int n = snprintf(out, cap, "%s/%s", sd_root, sd_path + 4);
    return n < 0 || (size_t)n >= cap ? -1 : 0;
}

// The simulator checks this itself (there is no VBlank interrupt on the host).
int dsidbg_sim_watchdog_expired(void)
{
    return watchdog_s && plat_ms() - last_beat > watchdog_s * 1000u;
}
