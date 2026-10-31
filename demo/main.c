// SPDX-License-Identifier: MIT
// dsidbg-demo: exercises every agent feature on the console. Top screen: a spinning 3D triangle (screenshots use
// VRAM bank D). Bottom screen: log console. Commands (`dsidbg cmd ...`):
//   echo TEXT | keys | abort | undef | hang | panic [TEXT] | busy MS
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <fat.h>
#include <nds.h>

#include "dsidbg.h"

static volatile int hang_now;
static int busy_ms;

static void busy_loop(int ms)
{
    // Burns CPU in a named function so it shows up in `dsidbg prof`.
    for (volatile int i = 0; i < ms * 20000; i++) {}
}

static int command(const char *line, char *reply, size_t cap)
{
    if (!strncmp(line, "echo ", 5)) { snprintf(reply, cap, "%s", line + 5); return 0; }
    if (!strcmp(line, "keys")) { snprintf(reply, cap, "held=0x%lx", (unsigned long)dsidbg_keys_held()); return 0; }
    if (!strcmp(line, "abort")) { *(volatile uint32_t *)0x10000000 = 1; }
    if (!strcmp(line, "undef")) { __builtin_trap(); }
    if (!strcmp(line, "hang")) { hang_now = 1; snprintf(reply, cap, "hanging in the main loop"); return 0; }
    if (!strncmp(line, "panic", 5)) dsidbg_panic(line[5] ? line + 6 : "demo panic");
    if (!strncmp(line, "busy ", 5)) { busy_ms = atoi(line + 5); snprintf(reply, cap, "busy %d ms/frame", busy_ms); return 0; }
    snprintf(reply, cap, "commands: echo TEXT, keys, abort, undef, hang, panic [TEXT], busy MS");
    return -1;
}

static void draw_triangle(int angle)
{
    glMatrixMode(GL_MODELVIEW);
    glLoadIdentity();
    glTranslatef32(0, 0, floattof32(-3.0f));
    glRotateYi(angle);
    glPolyFmt(POLY_ALPHA(31) | POLY_CULL_NONE);
    glBegin(GL_TRIANGLES);
    glColor3b(255, 0, 0); glVertex3v16(floattov16(-1.0f), floattov16(-0.8f), 0);
    glColor3b(0, 255, 0); glVertex3v16(floattov16(1.0f), floattov16(-0.8f), 0);
    glColor3b(0, 0, 255); glVertex3v16(0, floattov16(1.0f), 0);
    glEnd();
    glFlush(0);
}

int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    videoSetMode(MODE_0_3D);
    consoleDemoInit(); // bottom screen, VRAM bank C
    glInit();
    glClearColor(4, 4, 8, 31);
    glClearPolyID(63);
    glClearDepth(0x7FFF);
    glViewport(0, 0, 255, 191);
    glMatrixMode(GL_PROJECTION);
    glLoadIdentity();
    gluPerspective(60, 256.0 / 192.0, 0.1, 40);

    if (!fatInitDefault()) printf("Can't open the SD card.\n");

    dsidbg_config cfg;
    dsidbg_config_defaults(&cfg);
    cfg.app_name = "dsidbg-demo";
    cfg.build_id = DSIDBG_VERSION;
    cfg.capture_bank = 3; // bank D is free
    cfg.command = command;
    if (dsidbg_init(&cfg) != 0) printf("agent off: %s\n", dsidbg_status());
    dsidbg_capture_stdout(1); // printf goes to the daemon and to this console
    printf("dsidbg demo %s\n", DSIDBG_VERSION);

    uint32_t frame = 0, last_held = 0;
    for (;;) {
        while (hang_now) {}
        dsidbg_scan_keys();
        uint32_t held = dsidbg_keys_held();
        if (held != last_held) {
            int16_t x, y;
            if (dsidbg_touch(&x, &y)) printf("keys 0x%04lx touch %d,%d\n", (unsigned long)held, x, y);
            else printf("keys 0x%04lx\n", (unsigned long)held);
            last_held = held;
        }
        if (busy_ms) busy_loop(busy_ms);
        draw_triangle((int)(frame * 64));
        if (frame % 60 == 0) dsidbg_send(1, &frame, sizeof frame);
        frame++;
        dsidbg_frame();
        swiWaitForVBlank();
    }
}
