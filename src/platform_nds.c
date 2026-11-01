// SPDX-License-Identifier: MIT
// DSi platform: dswifi, SD card, display capture, Unlaunch reboot, exception handler and IRQ hook.
#include <malloc.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include <dswifi9.h>
#include <fat.h>
#include <nds.h>
#include <nds/arm9/sdmmc.h>
#include <netinet/in.h>

#include "internal.h"

// From libnds (exceptions.c): address a faulting load/store instruction accessed.
u32 getExceptionAddress(u32 opcodeAddress, u32 thumbState);

// irq_nds.s
void dsidbg__irq_entry(void);
void dsidbg__panic_switch(void) __attribute__((noreturn));
VoidFn dsidbg__irq_next;

static volatile uint32_t vblanks, since_beat, watchdog_frames;
// vblanks at the last message from the ARM7. Its default core sends the input state every frame, so a gap of more
// than a few frames means the ARM7 is stuck (it also runs Wi-Fi and the SD card for the ARM9).
static volatile uint32_t arm7_seen;
static cothread_info_t *main_ctx; // the main thread, for hang reports
static volatile uint32_t ms_now, ms_frac; // milliseconds, kept in 32-bit integers (no 64-bit division)
static int hooked;

// ---------------------------------------------------------------------------------------------------------------
// Time, watchdog, IRQ hook

// One frame is 16.715 ms (59.83 Hz): add 16 ms, and carry the 0.715 ms remainder in thousandths.
static inline void count_frame(void)
{
    vblanks++;
    ms_now += 16;
    ms_frac += 715;
    if (ms_frac >= 1000) {
        ms_frac -= 1000;
        ms_now++;
    }
}

uint32_t dsidbg__plat_ms(void) { return ms_now; }

void dsidbg__plat_heartbeat(void)
{
    since_beat = 0;
    if (!hooked) count_frame(); // no IRQ hook: approximate time by frames
}

void dsidbg__plat_set_watchdog(unsigned seconds) { watchdog_frames = seconds * 60; }
uint32_t dsidbg__plat_prof_hz(void) { return hooked ? 1000 : 0; } // dswifi's 1 kHz timer drives the sampling

// Called by dsidbg__irq_entry for every IRQ, before libnds' dispatcher. Returns 1 to report a hang.
//
// This runs on the IRQ stack, which is only 256 bytes in BlocksDS' default memory map, with the main thread's stack
// right below it. Keep it to plain loads and stores: no snprintf/memset or other library calls. (A snprintf here
// once overflowed into the thread scheduler's saved state, so the panic loop crashed on its first yield.)
ARM_CODE uint32_t dsidbg__irq_hook(uint32_t pc, uint32_t pending)
{
    if (!pending) return 0;
    if (pending & IRQ_FIFO_NOT_EMPTY) arm7_seen = vblanks;
    // libnds handles the highest pending bit per IRQ entry; count only that one so nothing is counted twice.
    uint32_t bit = 1u << (31 - __builtin_clz(pending));
    if (bit == IRQ_TIMER(LIBNDS_DEFAULT_TIMER_WIFI)) {
        dsidbg__prof_sample(pc);
    } else if (bit == IRQ_VBLANK) {
        count_frame();
        if (watchdog_frames && !dsidbg__in_panic && ++since_beat > watchdog_frames) {
            volatile uint32_t *w = (volatile uint32_t *)&dsidbg__crash;
            for (unsigned i = 0; i < sizeof dsidbg__crash / 4; i++) w[i] = 0;
            dsidbg__crash.kind = CRASH_HANG;
            dsidbg__crash.fault_addr = pc;
            dsidbg__in_panic = 1; // the message is written by the panic loop, on its own stack
            return 1; // dsidbg__irq_entry saves the registers and enters the panic loop
        }
    }
    return 0;
}

static int code_addr_ok(uint32_t a)
{
    return (a >= 0x01000000 && a < 0x01008000) || (a >= 0x02000000 && a < 0x03000000);
}

int dsidbg__plat_valid_ram(uint32_t addr, uint32_t len)
{
    uint32_t end = addr + len;
    if (end < addr) return 0;
    return (addr >= 0x02000000 && end <= 0x03000000) || // main RAM (16 MiB on DSi), includes DTCM's usual address
           (addr >= 0x0B000000 && end <= 0x0B004000) || // DTCM at the libnds default address
           (addr >= 0x01000000 && end <= 0x01008000);   // ITCM
}

static void exception_handler(void)
{
    dsidbg_crash *c = &dsidbg__crash;
    if (dsidbg__in_panic) for (;;) {} // a crash while reporting a crash: nothing safe left to do
    memset(c, 0, sizeof *c);
    for (int i = 0; i < 16; i++) c->regs[i] = (uint32_t)exceptionRegisters[i];
    uint32_t spsr = *(vu32 *)0x02FFFD90; // BIOS exception stack: saved CPSR of the crashed code
    uint32_t thumb = (spsr & 0x20) != 0;
    uint32_t r15 = c->regs[15], pc;
    if ((getCPSR() & 0x1F) == 0x17) {
        c->kind = CRASH_DATA_ABORT;
        pc = r15 - 8;
        c->fault_addr = code_addr_ok(pc) ? getExceptionAddress(pc, thumb) : pc;
        snprintf(c->msg, sizeof c->msg, "data abort");
    } else {
        c->kind = CRASH_UNDEFINED;
        pc = r15 - (thumb ? 2 : 4);
        c->fault_addr = pc;
        snprintf(c->msg, sizeof c->msg, "undefined instruction");
    }
    c->regs[15] = pc;
    c->cpsr = spsr;
    dsidbg__in_panic = 1;
    dsidbg__panic_switch();
}

void dsidbg__plat_crash_install(unsigned flags)
{
    if (!(flags & DSIDBG_NO_CRASH_HANDLER)) setExceptionHandler(exception_handler);
    // dsidbg_init() normally runs on the main thread: remember it, if this stack is the main one.
    extern char __sp_usr[], __dtcm_start[];
    uint32_t here = (uint32_t)(uintptr_t)__builtin_frame_address(0);
    if (here >= (uint32_t)(uintptr_t)__dtcm_start && here < (uint32_t)(uintptr_t)__sp_usr - 0x400)
        main_ctx = (cothread_info_t *)cothread_get_current();
    if (!(flags & DSIDBG_NO_IRQ_HOOK) && !hooked) {
        int ime = enterCriticalSection();
        dsidbg__irq_next = IRQ_HANDLER;
        IRQ_HANDLER = dsidbg__irq_entry;
        hooked = 1;
        leaveCriticalSection(ime);
    }
}

void dsidbg__plat_crash_show(const dsidbg_crash *c, const char *state)
{
    static int ready;
    static const char *kinds[] = {"?", "DATA ABORT", "UNDEFINED INSTRUCTION", "HANG (watchdog)", "PANIC"};
    char line[64];
    REG_IME = 1;
    if (!ready) {
        // Take over the bottom screen with a plain console. The app's VRAM bank C content is lost; it has crashed.
        REG_MASTER_BRIGHT_SUB = 0;
        consoleDemoInit();
        ready = 1;
    }
    // The console is 32 columns: keep every line shorter so nothing wraps. consolePrintString() doesn't parse
    // ANSI escapes, so colours go through consoleSetColor().
    consoleSetCursor(NULL, 0, 0);
    consoleSetColor(NULL, CONSOLE_LIGHT_RED);
    snprintf(line, sizeof line, "dsidbg: %-22.22s\n", c->kind < 5 ? kinds[c->kind] : "?");
    consolePrintString(line);
    consoleSetColor(NULL, CONSOLE_DEFAULT);
    snprintf(line, sizeof line, "%-31.31s\n", c->msg);
    consolePrintString(line);
    snprintf(line, sizeof line, "%-31.31s\n", strlen(c->msg) > 31 ? c->msg + 31 : "");
    consolePrintString(line);
    snprintf(line, sizeof line, "pc   %08lx  addr %08lx\n", (unsigned long)c->regs[15], (unsigned long)c->fault_addr);
    consolePrintString(line);
    for (int i = 0; i < 16; i += 2) {
        snprintf(line, sizeof line, "r%-2d  %08lx   r%-2d  %08lx\n", i, (unsigned long)c->regs[i], i + 1,
                 (unsigned long)c->regs[i + 1]);
        consolePrintString(line);
    }
    snprintf(line, sizeof line, "cpsr %08lx  frame %lu\n", (unsigned long)c->cpsr, (unsigned long)c->frame);
    consolePrintString(line);
    // Words on the stack that point into code: likely return addresses, a backtrace to look up with the ELF.
    extern char __text_start[], __text_end[];
    uint32_t t0 = (uint32_t)(uintptr_t)__text_start, t1 = (uint32_t)(uintptr_t)__text_end;
    const uint32_t *st = (const uint32_t *)(uintptr_t)c->stack_base;
    int shown = 0;
    consolePrintString("calls:");
    for (uint32_t i = 0; c->stack_len && i < c->stack_len / 4 && shown < 9; i++) {
        uint32_t w = st[i];
        if (!((w >= t0 && w < t1) || (w >= 0x01000000 && w < 0x01008000))) continue;
        snprintf(line, sizeof line, "%s%08lx", shown % 3 ? " " : "\n ", (unsigned long)w);
        consolePrintString(line);
        shown++;
    }
    for (; shown < 9; shown++) consolePrintString(shown % 3 ? "         " : "\n ");
    snprintf(line, sizeof line, "\n\n%-31.31s", state);
    consolePrintString(line);
}

// ---------------------------------------------------------------------------------------------------------------
// Wi-Fi

enum { W_INIT, W_INIT_FAILED, W_SCAN, W_ASSOC, W_UP, W_FAIL };
static volatile int wstate = W_INIT;
static uint32_t wtime;
static char w_ssid[33], w_pass[65];
static char wtext[48];

static void wifi_start(void)
{
    wtime = dsidbg__plat_ms();
    if (w_ssid[0]) {
        Wifi_ScanMode();
        wstate = W_SCAN;
    } else {
        Wifi_AutoConnect(); // networks saved in the console's settings
        wstate = W_ASSOC;
    }
}

static int wifi_init_thread(void *arg)
{
    (void)arg;
    if (Wifi_InitDefault(WIFI_ATTEMPT_DSI_MODE | INIT_ONLY)) {
        wifi_start();
    } else {
        snprintf(wtext, sizeof wtext, "wifi init failed");
        wstate = W_INIT_FAILED;
    }
    return 0;
}

int dsidbg__plat_init(const dsidbg_config *cfg, const char *ssid, const char *pass)
{
    (void)cfg;
    snprintf(w_ssid, sizeof w_ssid, "%s", ssid ? ssid : "");
    snprintf(w_pass, sizeof w_pass, "%s", pass ? pass : "");
    snprintf(wtext, sizeof wtext, "starting wifi");
    // Wifi_InitDefault() waits for the ARM7 without a timeout. Run it in its own cooperative thread so a Wi-Fi chip
    // that never answers (or an emulator without DSi Wi-Fi) leaves the app running, with the agent offline.
    if (cothread_create(wifi_init_thread, NULL, 8 * 1024, COTHREAD_DETACHED) < 0) wifi_init_thread(NULL);
    return 0;
}

int dsidbg__plat_net_poll(void)
{
    switch (wstate) {
    case W_INIT:
        if (dsidbg__plat_ms() > 10000) snprintf(wtext, sizeof wtext, "starting wifi (no answer yet)");
        return 0;
    case W_INIT_FAILED:
        return 0;
    case W_SCAN: {
        int n = Wifi_GetNumAP();
        for (int i = 0; i < n; i++) {
            Wifi_AccessPoint ap;
            if (Wifi_GetAPData(i, &ap) != WIFI_RETURN_OK) continue;
            if (ap.ssid_len != strlen(w_ssid) || memcmp(ap.ssid, w_ssid, ap.ssid_len) != 0) continue;
            if (!(ap.flags & WFLAG_APDATA_COMPATIBLE)) {
                snprintf(wtext, sizeof wtext, "%.20s: unsupported security", w_ssid);
                return 0;
            }
            Wifi_SetIP(0, 0, 0, 0, 0);
            if (!w_pass[0] && (ap.flags & WFLAG_APDATA_CONFIG_IN_WFC)) Wifi_ConnectWfcAP(&ap);
            else if (w_pass[0]) Wifi_ConnectSecureAP(&ap, w_pass, strlen(w_pass));
            else Wifi_ConnectSecureAP(&ap, NULL, 0);
            wstate = W_ASSOC;
            wtime = dsidbg__plat_ms();
            return 0;
        }
        snprintf(wtext, sizeof wtext, "scanning for %.20s", w_ssid);
        return 0;
    }
    case W_ASSOC: {
        int s = Wifi_AssocStatus();
        if (s == ASSOCSTATUS_ASSOCIATED) { wstate = W_UP; return 1; }
        if (s == ASSOCSTATUS_CANNOTCONNECT) { wstate = W_FAIL; wtime = dsidbg__plat_ms(); }
        snprintf(wtext, sizeof wtext, "%s", ASSOCSTATUS_STRINGS[s]);
        return 0;
    }
    case W_UP:
        if (Wifi_AssocStatus() == ASSOCSTATUS_ASSOCIATED) return 1;
        wifi_start();
        snprintf(wtext, sizeof wtext, "connection lost");
        return 0;
    default: // W_FAIL: retry after a pause
        snprintf(wtext, sizeof wtext, "can't connect, retrying");
        if (dsidbg__plat_ms() - wtime > 5000) wifi_start();
        return 0;
    }
}

const char *dsidbg__plat_net_state(void) { return wtext; }
uint32_t dsidbg__plat_ip(void) { return wstate == W_UP ? Wifi_GetIP() : 0; }

uint32_t dsidbg__plat_entropy(void)
{
    // RTC seconds differ between boots; the timer and scanline add jitter from Wi-Fi timing.
    return (uint32_t)time(NULL) * 2654435761u ^ (uint32_t)REG_VCOUNT << 16 ^
           TIMER_DATA(LIBNDS_DEFAULT_TIMER_WIFI) ^ vblanks * 40503u;
}
uint32_t dsidbg__plat_heap_used(void)
{
    struct mallinfo mi = mallinfo();
    return (uint32_t)mi.uordblks;
}

int dsidbg__plat_discovery_targets(uint32_t *addrs, int max)
{
    // The limited broadcast address, and the subnet's directed broadcast when DHCP gave us a netmask.
    int n = 0;
    if (max > n) addrs[n++] = 0xFFFFFFFFu;
    struct in_addr gw, mask, dns1, dns2;
    struct in_addr ip = Wifi_GetIPInfo(&gw, &mask, &dns1, &dns2);
    if (ip.s_addr && mask.s_addr && max > n) addrs[n++] = ip.s_addr | ~mask.s_addr;
    return n;
}
static int no_threads; // set by the panic loop when it took over something other than the main thread

void dsidbg__plat_yield(void)
{
    if (!no_threads) cothread_yield();
}

void dsidbg__plat_wait_frame(void)
{
    dsidbg__plat_yield();
    swiWaitForVBlank();
    dsidbg__plat_yield();
}

// BlocksDS runs main() on the DTCM stack below the top 1 KiB, which the thread scheduler keeps (crt0 passes
// __sp_usr - 0x400 to cothread_start); other threads' stacks are on the heap.
int dsidbg__plat_panic_on_main_thread(dsidbg_crash *c)
{
    extern char __sp_usr[], __dtcm_start[];
    uint32_t sp = c->regs[13], bottom = (uint32_t)(uintptr_t)__dtcm_start, top = (uint32_t)(uintptr_t)__sp_usr - 0x400;
    int main_thread = c->kind == CRASH_PANIC || (sp >= bottom && sp <= top);
    if (main_thread) return 1;
    no_threads = 1;
    if (c->kind != CRASH_HANG || !main_ctx) return 0;
    // The main thread isn't running, so its context holds its own saved stack pointer. __ndsabi_coro_yield pushed
    // r4-r11 and lr there: lr is where it resumes, inside the cothread_yield_*() call it waits in.
    uint32_t msp = main_ctx->arm_sp;
    if (msp < bottom || msp + 36 > top) return 0;
    const uint32_t *saved = (const uint32_t *)(uintptr_t)msp;
    for (int i = 0; i < 16; i++) c->regs[i] = 0;
    for (int i = 0; i < 8; i++) c->regs[4 + i] = saved[i];
    uint32_t lr = saved[8];
    c->regs[13] = msp + 36;
    c->regs[14] = lr;
    c->regs[15] = lr & ~1u;
    c->cpsr = 0x1F | (lr & 1 ? 0x20 : 0); // system mode, Thumb if it returns to Thumb code
    c->flags |= CRASH_MAIN_BLOCKED;
    // How long the ARM7 has been silent, and the ARM9's IPC FIFO control register (bit 0: send FIFO empty, 1: full;
    // 8: receive FIFO empty, 9: full; 14: error).
    uint32_t quiet = (vblanks - arm7_seen) / 6; // tenths of a second
    if (quiet > 9999) quiet = 9999;
    snprintf(c->msg, sizeof c->msg, "main waits arm7 %u.%us ipc %04x", (unsigned short)(quiet / 10),
             (unsigned)(quiet % 10), (unsigned short)REG_IPC_FIFO_CR);
    return 0;
}

// ---------------------------------------------------------------------------------------------------------------
// Files, keys, screen, reboot

int dsidbg__plat_fs_init(void)
{
    // The agent only uses "sd:/". NAND is never mounted by it; keep NAND writes blocked even if the app mounts it.
    if (isDSiMode()) nand_WriteProtect(true);
    return fatInitDefault() ? 0 : -1;
}

int dsidbg__plat_path(const char *sd_path, char *out, size_t cap)
{
    if (strlen(sd_path) >= cap) return -1;
    strcpy(out, sd_path);
    return 0;
}

uint32_t dsidbg__plat_hw_keys(void)
{
    scanKeys();
    return keysHeld();
}

// Captures the main engine's output (what the top screen shows, unless the app swapped screens) into `bank`.
int dsidbg__plat_capture(int screen, uint16_t *pixels, int bank)
{
    if (screen != DSIDBG_SCREEN_TOP || bank < 0 || bank > 3) return -1;
    vu8 *cr = &VRAM_A_CR + bank;
    u8 old = *cr;
    *cr = VRAM_ENABLE; // LCDC mapping (MST 0): required for capture
    REG_DISPCAPCNT = DCAP_ENABLE | DCAP_MODE(DCAP_MODE_A) | DCAP_SRC_A(DCAP_SRC_A_COMPOSITED) |
                     DCAP_SIZE(DCAP_SIZE_256x192) | DCAP_OFFSET(0) | DCAP_BANK(bank) | DCAP_A(16);
    int ok = 0;
    for (int i = 0; i < 4; i++) {
        dsidbg__plat_wait_frame();
        if (!(REG_DISPCAPCNT & DCAP_ENABLE)) { ok = 1; break; }
    }
    if (ok) memcpy(pixels, (const void *)(0x06800000 + bank * 0x20000), 256 * 192 * 2);
    REG_DISPCAPCNT = 0;
    *cr = old;
    return ok ? 0 : -1;
}

// Unlaunch boots the file named in its "AutoLoadInfo" block at 0x02000800 after a reset (same layout TWiLight
// Menu++ uses). Only RAM is written; the reset itself is libnds' systemReboot() (an ARM7 power-management request).
#define UNLAUNCH_INFO      0x02000800
#define UNLAUNCH_INFO_SIZE 0x438

static void unlaunch_build(u8 *base, const char *sd_path)
{
    static const char id[12] = {'A', 'u', 't', 'o', 'L', 'o', 'a', 'd', 'I', 'n', 'f', 'o'};
    memcpy(base, id, 12);
    *(u16 *)(base + 0x0C) = 0x3F0;                 // length covered by the CRC
    *(u16 *)(base + 0x0E) = 0;                     // CRC16, filled below
    *(u32 *)(base + 0x10) = BIT(0) | BIT(1);       // load the title at +0x38; use the colours at +0x14
    *(u16 *)(base + 0x14) = 0x0000;                // top screen background while loading
    *(u16 *)(base + 0x16) = 0x0000;                // bottom screen background
    memset(base + 0x18, 0, 0x20 + 0x208 + 0x1C0);
    u16 *name = (u16 *)(base + 0x38);
    const char *p = sd_path + 3; // "sd:" -> "sdmc:"
    int i = 0;
    for (const char *s = "sdmc:"; *s; s++) name[i++] = (u8)*s;
    for (; *p && i < 0x103; p++) name[i++] = (u8)*p;
    name[i] = 0;
    *(u16 *)(base + 0x0E) = swiCRC16(0xFFFF, base + 0x10, 0x3F0);
}

// Programs built with the stock BlocksDS memory map start at 0x02000000, so the Unlaunch block overwrites part of
// the running program. This copy therefore runs from ITCM with interrupts off and never returns to main RAM: it
// writes the block, writes back the data cache, and waits for the ARM7 to reset the console.
ITCM_CODE ARM_CODE __attribute__((noinline, noreturn)) static void unlaunch_commit(const u32 *block)
{
    REG_IME = 0;
    vu32 *dst = (vu32 *)UNLAUNCH_INFO;
    for (int i = 0; i < UNLAUNCH_INFO_SIZE / 4; i++) dst[i] = block[i];
    for (u32 seg = 0; seg < 4; seg++)       // clean + invalidate the 4 KiB data cache by set/way
        for (u32 line = 0; line < 32; line++)
            asm volatile("mcr p15, 0, %0, c7, c14, 2" ::"r"((seg << 30) | (line << 5)));
    asm volatile("mcr p15, 0, %0, c7, c10, 4" ::"r"(0)); // drain the write buffer
    for (;;) {}
}

void dsidbg__plat_reboot(const char *sd_path)
{
    u32 block[UNLAUNCH_INFO_SIZE / 4]; // on the stack (DTCM), away from the block's destination
    uintptr_t b = (uintptr_t)block;
    int set_boot = sd_path && isDSiMode() &&
                   (b + sizeof block <= UNLAUNCH_INFO || b >= UNLAUNCH_INFO + UNLAUNCH_INFO_SIZE);
    if (set_boot) unlaunch_build((u8 *)block, sd_path);
    DC_FlushAll();
    // The ARM7's reset takes far longer (I2C writes) than the copy below. If it ever won the race, Unlaunch would
    // just boot its default app.
    systemReboot();
    if (set_boot) unlaunch_commit(block);
    for (;;) swiWaitForVBlank();
}

void dsidbg__plat_info(char *buf, size_t cap)
{
    snprintf(buf, cap, "mode=%s\nirq_hook=%d\n", isDSiMode() ? "dsi" : "ds", hooked);
}

static int stdout_echo;

static ssize_t stdout_fn(const char *ptr, size_t len)
{
    dsidbg_write(ptr, len);
    if (stdout_echo)
        for (size_t i = 0; i < len; i++) consolePrintChar(ptr[i]);
    return (ssize_t)len;
}

void dsidbg__plat_hook_stdout(int mode)
{
    stdout_echo = mode == 2;
    consoleSetCustomStdout(mode ? stdout_fn : NULL);
    consoleSetCustomStderr(mode ? stdout_fn : NULL);
}

void dsidbg__plat_console_write(const char *text, size_t len)
{
    for (size_t i = 0; i < len; i++) consolePrintChar(text[i]);
}
