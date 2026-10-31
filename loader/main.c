// SPDX-License-Identifier: MIT
// dsidbg-loader: the recovery/updater app. Set it as Unlaunch's default so the console always comes back here
// after a reboot or a crash. While it runs, the daemon can copy builds to the SD card and launch them
// (`dsidbg push --launch`), and it uploads crash dumps saved by earlier runs.
//
//   A      launch the last build again (sd:/dsidbg/last.txt)
//   START  power off
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#include <fat.h>
#include <nds.h>

#include "dsidbg.h"

#define CONFIG_DIR  "sd:/dsidbg"
#define CONFIG_PATH "sd:/dsidbg/config.ini"

static PrintConsole top, bottom;
static char last[256];
static int sd_ok, config_created;

static const char config_template[] =
    "# dsidbg agent settings. Fill in host and key, or run `dsidbg setup --sd <card>` on your computer.\n"
    "# host: your computer's IPv4 address. key: the contents of ~/.dsidbg/key on that computer.\n"
    "host=\n"
    "port=7710\n"
    "key=\n"
    "# Without ssid/pass the DSi uses its saved Wi-Fi connections (System Settings > Internet).\n"
    "# ssid=MyNetwork\n"
    "# pass=MyPassword\n";

// Writes a blank config if there is none, so the file is there to edit. Returns 1 if it was created.
static int ensure_config(void)
{
    FILE *f = fopen(CONFIG_PATH, "rb");
    if (f) {
        fclose(f);
        return 0;
    }
    mkdir(CONFIG_DIR, 0777);
    f = fopen(CONFIG_PATH, "wb");
    if (!f) return 0;
    fputs(config_template, f);
    fclose(f);
    return 1;
}

static void read_last(void)
{
    last[0] = 0;
    FILE *f = fopen("sd:/dsidbg/last.txt", "rb");
    if (!f) return;
    size_t n = fread(last, 1, sizeof last - 1, f);
    fclose(f);
    last[n] = 0;
    char *e = last + strlen(last);
    while (e > last && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ')) *--e = 0;
}

static void draw_top(void)
{
    consoleSelect(&top);
    consoleClear();
    printf("\x1b[36;1mdsidbg loader %s\x1b[39m\n\n", DSIDBG_VERSION);
    printf("A      launch last build\n");
    printf("START  power off\n\n");
    printf("Last build:\n%.96s\n", last[0] ? last : "(none yet)");
    if (!sd_ok) printf("\n\x1b[31;1mCan't open the SD card.\x1b[39m\n");
    if (config_created) {
        // 32 columns: keep each line shorter than that
        printf("\n\x1b[33;1mNo settings file was found;\n");
        printf("a blank one was made at\n%s\n\n", CONFIG_PATH);
        printf("Fill in host and key, or run\n");
        printf("dsidbg setup --sd <card>\x1b[39m\n");
    }
}

int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    videoSetMode(MODE_0_2D);
    videoSetModeSub(MODE_0_2D);
    vramSetBankA(VRAM_A_MAIN_BG);
    vramSetBankC(VRAM_C_SUB_BG);
    consoleInit(&top, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, true, true);
    consoleInit(&bottom, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, false, true);

    // Show each start-up step before doing it, so a step that never returns is visible instead of a black screen.
    consoleSelect(&top);
    printf("dsidbg loader %s\n\n", DSIDBG_VERSION);
    printf("Opening the SD card...\n");
    sd_ok = fatInitDefault();
    if (sd_ok) {
        printf("Checking the settings file...\n");
        config_created = ensure_config();
    }
    printf("Starting the agent...\n");

    dsidbg_config cfg;
    dsidbg_config_defaults(&cfg);
    cfg.app_name = "dsidbg-loader";
    cfg.build_id = DSIDBG_VERSION;
    cfg.capture_bank = 1; // bank B is unused here
    int ok = dsidbg_init(&cfg) == 0;
    read_last();
    draw_top();
    dsidbg_log("loader ready%s%s", last[0] ? ", last build " : "", last);

    for (uint32_t frame = 0;; frame++) {
        dsidbg_scan_keys();
        uint32_t down = dsidbg_keys_down();
        if ((down & KEY_A) && last[0]) {
            consoleSelect(&bottom);
            printf("\nLaunching %s\n", last);
            dsidbg_reboot(last);
        }
        if (down & KEY_START) systemShutDown();

        if (frame % 30 == 0) {
            consoleSelect(&bottom);
            consoleSetCursor(NULL, 0, 0);
            printf("%-32.32s", ok ? dsidbg_status() : "agent off:");
            if (!ok) printf("%-32.32s", dsidbg_status());
            if (frame % 300 == 0) { read_last(); draw_top(); }
        }
        dsidbg_frame();
        swiWaitForVBlank();
    }
}
