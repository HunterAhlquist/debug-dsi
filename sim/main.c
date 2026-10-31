// SPDX-License-Identifier: MIT
// dsidbg-sim: runs the real agent on the host so the daemon and the protocol can be tested without a DSi.
//
//   dsidbg-sim --host 127.0.0.1 --port 7710 --key SECRET --root DIR [--name sim]
//
// It behaves like a small app: it logs button changes, sends a counter on data channel 1 every second, feeds fake
// profiler samples, and answers `dsidbg cmd` with: echo TEXT, keys, crash, panic TEXT, hang.
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../src/internal.h"

void dsidbg_sim_set_root(const char *root);
int dsidbg_sim_watchdog_expired(void);

static int hang;

static int command(const char *line, char *reply, size_t cap)
{
    if (!strncmp(line, "echo ", 5)) { snprintf(reply, cap, "%s", line + 5); return 0; }
    if (!strcmp(line, "keys")) { snprintf(reply, cap, "held=0x%x", (unsigned)dsidbg_keys_held()); return 0; }
    if (!strcmp(line, "crash")) {
        memset(&dsidbg__crash, 0, sizeof dsidbg__crash);
        dsidbg__crash.kind = CRASH_DATA_ABORT;
        dsidbg__crash.regs[15] = 0x02001234;
        dsidbg__crash.regs[14] = 0x02000100;
        dsidbg__crash.fault_addr = 0x10000000;
        dsidbg__crash.cpsr = 0x1f;
        dsidbg__panic_main();
    }
    if (!strncmp(line, "panic", 5)) dsidbg_panic(line[5] ? line + 6 : "sim panic");
    if (!strcmp(line, "hang")) { hang = 1; snprintf(reply, cap, "hanging"); return 0; }
    snprintf(reply, cap, "unknown command '%s'", line);
    return -1;
}

int main(int argc, char **argv)
{
    dsidbg_config cfg;
    dsidbg_config_defaults(&cfg);
    cfg.app_name = "sim";
    cfg.build_id = "sim-build";
    cfg.capture_bank = 0;
    cfg.command = command;
    cfg.config_path = NULL;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!strcmp(argv[i], "--host")) cfg.host = argv[i + 1];
        else if (!strcmp(argv[i], "--port")) cfg.port = (uint16_t)atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--key")) cfg.key = argv[i + 1];
        else if (!strcmp(argv[i], "--root")) dsidbg_sim_set_root(argv[i + 1]);
        else if (!strcmp(argv[i], "--name")) cfg.app_name = argv[i + 1];
        else if (!strcmp(argv[i], "--watchdog")) cfg.watchdog_seconds = (unsigned)atoi(argv[i + 1]);
        else { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
    }
    signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (dsidbg_init(&cfg) != 0) { fprintf(stderr, "init failed: %s\n", dsidbg_status()); return 1; }
    dsidbg_log("sim started");

    uint32_t frame = 0, last_held = 0;
    for (;;) {
        if (hang) {
            // No dsidbg_frame() calls: wait for the watchdog like the DSi's VBlank check would.
            if (dsidbg_sim_watchdog_expired()) {
                memset(&dsidbg__crash, 0, sizeof dsidbg__crash);
                dsidbg__crash.kind = CRASH_HANG;
                dsidbg__crash.regs[15] = 0x02004000;
                dsidbg__panic_main();
            }
            usleep(16667);
            continue;
        }
        dsidbg_scan_keys();
        uint32_t h = dsidbg_keys_held();
        int16_t tx, ty;
        if (h != last_held) {
            if (dsidbg_touch(&tx, &ty)) dsidbg_log("frame %u keys held=0x%x touch=%d,%d", frame, h, tx, ty);
            else dsidbg_log("frame %u keys held=0x%x", frame, h);
            last_held = h;
        }
        for (int i = 0; i < 16; i++) dsidbg__prof_sample(0x02000000u + (uint32_t)((frame * 7 + i) % 5) * 0x100u);
        if (frame % 60 == 0) dsidbg_send(1, &frame, sizeof frame);
        frame++;
        dsidbg_frame();
        usleep(16667);
    }
}
