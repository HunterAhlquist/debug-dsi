// SPDX-License-Identifier: MIT
// Portable agent: connection, handshake, commands, input injection, logs, profiler buffer and crash reports.
// Platform code lives in platform_nds.c (the DSi) and platform_host.c (the simulator used by the tests).
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>

#ifdef DSIDBG_NDS
#define sock_close closesocket
#else
#include <poll.h>
#include <unistd.h>
#define sock_close close
#endif

#include "internal.h"

#define DEFAULT_PORT 7710
#define TXQ_SIZE     65536 // power of two
#define LOGLINE_MAX  512
#define MAX_INJECT   32
#define PROF_N       4096 // power of two
#define CONNECT_TIMEOUT_MS 4000
#define SEND_TIMEOUT_MS    10000
#define CRASH_SAVE_DELAY_MS 8000

enum { ST_OFF, ST_DOWN, ST_CONNECTING, ST_HANDSHAKE, ST_READY };

static dsidbg_config cfg;
static char host[64], key[128], ssid[33], pass[65];
static size_t key_len;
static uint16_t port;
static int inited, st = ST_OFF, sock = -1;
static uint32_t st_time, retry_at, last_status, frames_at_status;
static uint8_t nonce_d[16];
static uint32_t frame;
static char status_text[160], off_reason[96] = "not started";
static uint32_t log_drops;
static int crash_files_done;

// Outgoing queue for asynchronous messages (logs, status, data, profiler). Replies bypass it via send_direct(),
// which drains it first so the stream stays in order.
static uint8_t txq[TXQ_SIZE];
static uint32_t txq_r, txq_w;

static char logline[LOGLINE_MAX];
static size_t loglen;

static uint8_t rxbuf[PROTO_HEADER + PROTO_MAX_RX];
static size_t rxlen;

typedef struct { uint32_t start, end, keys, flags; int16_t x, y; } inject_t;
static inject_t inj[MAX_INJECT];
static int ninj;
static uint32_t held, down, up, prev_held;

static volatile uint32_t prof_buf[PROF_N];
static volatile uint32_t prof_w, prof_r;
static volatile int prof_on;

static FILE *put_f;
static char put_local[256], put_tmp[264];
static uint32_t put_size, put_crc, put_got, put_crc_now;
static int put_err;

dsidbg_crash dsidbg__crash;
volatile int dsidbg__in_panic;
static uint8_t crash_stack[CRASH_STACK_MAX];
static int crash_pending;

static void service(void);

// ---------------------------------------------------------------------------------------------------------------
// Little helpers

static void put16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v); put16(p + 2, v >> 16); }
static uint32_t get16(const uint8_t *p) { return p[0] | (uint32_t)p[1] << 8; }
static uint32_t get32(const uint8_t *p) { return get16(p) | get16(p + 2) << 16; }

static int would_block(void)
{
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINPROGRESS || errno == EALREADY || errno == EINTR;
}

// Copies a payload string (not necessarily NUL-terminated) into buf.
static void payload_str(const uint8_t *p, size_t n, char *buf, size_t cap)
{
    if (n >= cap) n = cap - 1;
    memcpy(buf, p, n);
    buf[n] = 0;
}

// Only "sd:/..." paths without ".." are allowed: the agent never touches NAND or other devices.
static int local_path(const char *sd_path, char *out, size_t cap)
{
    if (strncmp(sd_path, "sd:/", 4) != 0 || strstr(sd_path, "..") || strlen(sd_path) > 240)
        return -1;
    return plat_path(sd_path, out, cap);
}

static void mkdirs_for(const char *local)
{
    char tmp[264];
    size_t n = strlen(local);
    if (n >= sizeof tmp) return;
    memcpy(tmp, local, n + 1);
    for (size_t i = 1; i < n; i++) {
        if (tmp[i] != '/' || tmp[i - 1] == ':') continue;
        tmp[i] = 0;
        mkdir(tmp, 0777);
        tmp[i] = '/';
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Sending

static uint32_t txq_used(void) { return txq_w - txq_r; }

static int queue_msg(uint16_t type, const void *a, size_t alen, const void *b, size_t blen)
{
    size_t total = PROTO_HEADER + alen + blen;
    if (total > TXQ_SIZE - txq_used()) return -1;
    uint8_t hdr[PROTO_HEADER];
    put32(hdr, (uint32_t)(alen + blen));
    put16(hdr + 4, type);
    put16(hdr + 6, 0);
    const uint8_t *parts[3] = {hdr, a, b};
    size_t lens[3] = {PROTO_HEADER, alen, blen};
    for (int i = 0; i < 3; i++)
        for (size_t j = 0; j < lens[i]; j++)
            txq[(txq_w++) & (TXQ_SIZE - 1)] = parts[i][j];
    return 0;
}

static void drop_conn(const char *why)
{
    if (sock >= 0) sock_close(sock);
    sock = -1;
    // The queue is only sent once authenticated. After that, a partly sent message can't be resumed on a new
    // connection, so drop what's queued; before that (failed connects), keep it: it holds the boot logs.
    if (st == ST_READY) txq_r = txq_w;
    st = ST_DOWN;
    retry_at = plat_ms() + 2000;
    rxlen = 0;
    if (put_f) { fclose(put_f); put_f = NULL; remove(put_tmp); }
    snprintf(off_reason, sizeof off_reason, "%s", why);
}

// Sends all of p. With block=0 it stops when the socket is full. Returns bytes sent, or -1 (connection dropped).
static long raw_send(const void *p, size_t n, int block)
{
    const uint8_t *b = p;
    size_t done = 0;
    uint32_t t0 = plat_ms();
    while (done < n) {
        long r = send(sock, b + done, n - done, 0);
        if (r > 0) { done += (size_t)r; t0 = plat_ms(); continue; }
        if (r < 0 && would_block()) {
            if (!block) break;
            if (plat_ms() - t0 > SEND_TIMEOUT_MS) { drop_conn("send timeout"); return -1; }
            plat_heartbeat();
            plat_yield();
            continue;
        }
        drop_conn("send failed");
        return -1;
    }
    return (long)done;
}

static int flush_tx(int block)
{
    while (sock >= 0 && txq_used()) {
        uint32_t at = txq_r & (TXQ_SIZE - 1);
        uint32_t chunk = txq_used();
        if (chunk > TXQ_SIZE - at) chunk = TXQ_SIZE - at;
        long r = raw_send(txq + at, chunk, block);
        if (r < 0) return -1;
        txq_r += (uint32_t)r;
        if ((uint32_t)r < chunk) break;
    }
    return 0;
}

// Sends a message now, after everything already queued. Returns 0 or -1.
static int send_direct(uint16_t type, uint16_t seq, const void *a, size_t alen, const void *b, size_t blen)
{
    if (sock < 0 || (st != ST_READY && st != ST_HANDSHAKE)) return -1;
    if (st == ST_READY && flush_tx(1) < 0) return -1;
    uint8_t hdr[PROTO_HEADER];
    put32(hdr, (uint32_t)(alen + blen));
    put16(hdr + 4, type);
    put16(hdr + 6, seq);
    if (raw_send(hdr, sizeof hdr, 1) < 0) return -1;
    if (alen && raw_send(a, alen, 1) < 0) return -1;
    if (blen && raw_send(b, blen, 1) < 0) return -1;
    return 0;
}

static void ack(uint16_t seq, int32_t status, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
static void ack(uint16_t seq, int32_t status, const char *fmt, ...)
{
    char text[256];
    uint8_t st32[4];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    put32(st32, (uint32_t)status);
    send_direct(MSG_ACK, seq, st32, 4, text, strlen(text));
}

static void ack_text(uint16_t seq, int32_t status, const char *text, size_t n)
{
    uint8_t st32[4];
    put32(st32, (uint32_t)status);
    send_direct(MSG_ACK, seq, st32, 4, text, n);
}

// ---------------------------------------------------------------------------------------------------------------
// Logs, data, status

static void log_flush(void)
{
    if (!loglen) return;
    if (queue_msg(MSG_LOG, logline, loglen, NULL, 0) < 0) log_drops++;
    loglen = 0;
}

void dsidbg_write(const char *text, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        logline[loglen++] = text[i];
        if (text[i] == '\n' || loglen == LOGLINE_MAX) log_flush();
    }
}

void dsidbg_vlog(const char *fmt, va_list args)
{
    char buf[LOGLINE_MAX];
    int n = vsnprintf(buf, sizeof buf, fmt, args);
    if (n < 0) return;
    if ((size_t)n >= sizeof buf) n = sizeof buf - 1;
    dsidbg_write(buf, (size_t)n);
    if (n == 0 || buf[n - 1] != '\n') dsidbg_write("\n", 1);
}

void dsidbg_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    dsidbg_vlog(fmt, ap);
    va_end(ap);
}

int dsidbg_send(uint16_t channel, const void *data, size_t len)
{
    uint8_t h[4];
    put16(h, channel);
    put16(h + 2, 0);
    return queue_msg(MSG_DATA, h, 4, data, len);
}

static void send_status(uint32_t now)
{
    char text[256];
    uint32_t dt = now - last_status, frames = frame - frames_at_status;
    uint32_t fps10 = dt ? frames * 10000 / dt : 0;
    int n = snprintf(text, sizeof text,
                     "frame=%lu\nfps=%lu.%lu\nuptime_ms=%lu\nlog_drops=%lu\nprof=%d\npanic=%d\n",
                     (unsigned long)frame, (unsigned long)(fps10 / 10), (unsigned long)(fps10 % 10),
                     (unsigned long)now, (unsigned long)log_drops, prof_on, dsidbg__in_panic);
    queue_msg(MSG_STATUS, text, (size_t)n, NULL, 0);
    last_status = now;
    frames_at_status = frame;
}

static void send_info(void)
{
    char text[512], plat[256] = "";
    struct in_addr ip;
    ip.s_addr = plat_ip();
    plat_info(plat, sizeof plat);
    int n = snprintf(text, sizeof text,
                     "lib=%s\nproto=%d\napp=%s\nbuild=%s\nip=%s\ncapture_bank=%d\nwatchdog=%u\nprof_hz=%lu\n%s",
                     DSIDBG_VERSION, PROTO_VERSION, cfg.app_name, cfg.build_id, inet_ntoa(ip), cfg.capture_bank,
                     cfg.watchdog_seconds, (unsigned long)plat_prof_hz(), plat);
    send_direct(MSG_INFO, 0, text, (size_t)n, NULL, 0);
}

// ---------------------------------------------------------------------------------------------------------------
// Profiler

void dsidbg__prof_sample(uint32_t pc)
{
    if (!prof_on) return;
    uint32_t w = prof_w;
    if (w - prof_r >= PROF_N) return; // full: drop the sample
    prof_buf[w & (PROF_N - 1)] = pc;
    prof_w = w + 1;
}

// Sends up to `max` buffered samples. direct=1 sends now, otherwise through the queue. Returns samples sent.
static uint32_t prof_flush(uint32_t max, int direct)
{
    static uint32_t pcs[1 + 1024]; // static: app stacks (DTCM) are small
    uint32_t n = prof_w - prof_r;
    if (n > max) n = max;
    if (n > 1024) n = 1024;
    if (!n) return 0;
    uint8_t *p = (uint8_t *)pcs;
    put32(p, plat_prof_hz());
    for (uint32_t i = 0; i < n; i++) put32(p + 4 + i * 4, prof_buf[(prof_r + i) & (PROF_N - 1)]);
    int r = direct ? send_direct(MSG_PROF, 0, p, 4 + n * 4, NULL, 0) : queue_msg(MSG_PROF, p, 4 + n * 4, NULL, 0);
    if (r < 0) return 0;
    prof_r += n;
    return n;
}

// ---------------------------------------------------------------------------------------------------------------
// Crashes

static int send_crash(const dsidbg_crash *c, const uint8_t *stack, size_t n)
{
    return send_direct(MSG_CRASH, 0, c, sizeof *c, stack, n);
}

static void save_crash_file(void)
{
    char local[264], sd[64];
    for (int i = 0; i < 1000; i++) {
        snprintf(sd, sizeof sd, "sd:/dsidbg/crash/%03d.bin", i);
        if (local_path(sd, local, sizeof local) < 0) return;
        FILE *f = fopen(local, "rb");
        if (f) { fclose(f); continue; }
        char sent[272];
        snprintf(sent, sizeof sent, "%s.sent", local);
        f = fopen(sent, "rb");
        if (f) { fclose(f); continue; }
        mkdirs_for(local);
        f = fopen(local, "wb");
        if (!f) return;
        fwrite(&dsidbg__crash, sizeof dsidbg__crash, 1, f);
        fwrite(crash_stack, 1, dsidbg__crash.stack_len, f);
        fclose(f);
        return;
    }
}

// Sends dumps saved on the SD card by earlier crashes, then renames them to *.sent.
static void send_crash_files(void)
{
    char dir_local[264];
    if (local_path("sd:/dsidbg/crash/", dir_local, sizeof dir_local) < 0) return;
    DIR *d = opendir(dir_local);
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        size_t n = strlen(e->d_name);
        if (n < 5 || strcmp(e->d_name + n - 4, ".bin") != 0) continue;
        char path[300], sent[310];
        snprintf(path, sizeof path, "%s%s", dir_local, e->d_name);
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        dsidbg_crash c;
        static uint8_t stack[CRASH_STACK_MAX];
        size_t got = fread(&c, 1, sizeof c, f);
        size_t sl = got == sizeof c ? fread(stack, 1, sizeof stack, f) : 0;
        fclose(f);
        if (got != sizeof c) continue;
        c.flags |= CRASH_FROM_FILE;
        if (send_crash(&c, stack, sl) < 0) break;
        snprintf(sent, sizeof sent, "%s.sent", path);
        remove(sent);
        rename(path, sent);
    }
    closedir(d);
}

void dsidbg__panic_main(void)
{
    dsidbg__in_panic = 1;
    prof_on = 0;
    dsidbg_crash *c = &dsidbg__crash;
    c->frame = frame;
    if (c->kind == CRASH_HANG && !c->msg[0])
        snprintf(c->msg, sizeof c->msg, "no dsidbg_frame() for %u s", cfg.watchdog_seconds);
    uint32_t sp = c->regs[13], n = CRASH_STACK_MAX;
    while (n && !plat_valid_ram(sp, n)) n /= 2;
    if (n) memcpy(crash_stack, (const void *)(uintptr_t)sp, n);
    c->stack_base = sp;
    c->stack_len = n;
    crash_pending = 1;
    log_flush();
    plat_crash_show(c, dsidbg_status());

    uint32_t t0 = plat_ms(), shown = t0;
    int saved = 0;
    for (;;) {
        if (inited) service();
        uint32_t now = plat_ms();
        if (crash_pending && !saved && now - t0 > CRASH_SAVE_DELAY_MS) {
            save_crash_file();
            saved = 1;
        }
        if (now - shown > 1000) {
            plat_crash_show(c, crash_pending ? dsidbg_status() : "reported to host");
            shown = now;
        }
        plat_wait_frame();
    }
}

void dsidbg_panic(const char *message)
{
    dsidbg_crash *c = &dsidbg__crash;
    memset(c, 0, sizeof *c);
    c->kind = CRASH_PANIC;
    c->regs[14] = c->regs[15] = (uint32_t)(uintptr_t)__builtin_return_address(0);
    c->regs[13] = (uint32_t)(uintptr_t)__builtin_frame_address(0);
    snprintf(c->msg, sizeof c->msg, "%s", message ? message : "panic");
    dsidbg__panic_main();
}

// ---------------------------------------------------------------------------------------------------------------
// Commands

static void cmd_input(uint16_t seq, const uint8_t *p, size_t n)
{
    if (n % 16) { ack(seq, -1, "bad input record size"); return; }
    for (size_t i = 0; i < n; i += 16) {
        if (ninj == MAX_INJECT) { ack(seq, -1, "too many pending inputs (max %d)", MAX_INJECT); return; }
        inject_t *r = &inj[ninj++];
        r->start = frame + get32(p + i);
        r->end = r->start + get16(p + i + 4);
        r->flags = get16(p + i + 6);
        r->keys = get32(p + i + 8);
        r->x = (int16_t)get16(p + i + 12);
        r->y = (int16_t)get16(p + i + 14);
    }
    ack(seq, 0, "frame %lu", (unsigned long)frame);
}

static void cmd_shot(uint16_t seq, const uint8_t *p, size_t n)
{
    int screen = n ? p[0] : DSIDBG_SCREEN_TOP;
    uint16_t *px = malloc(256 * 192 * 2);
    if (!px) { ack(seq, -1, "out of memory"); return; }
    int r = -1;
    if (cfg.screenshot && !dsidbg__in_panic) r = cfg.screenshot(screen, px);
    if (r != 0 && screen == DSIDBG_SCREEN_TOP && cfg.capture_bank >= 0) r = plat_capture(screen, px, cfg.capture_bank);
    if (r != 0) {
        free(px);
        ack(seq, -1, screen == DSIDBG_SCREEN_TOP ? "no capture source: set capture_bank or a screenshot hook"
                                                 : "the bottom screen needs the app's screenshot hook");
        return;
    }
    uint8_t h[8] = {(uint8_t)screen, 0};
    put16(h + 2, 256);
    put16(h + 4, 192);
    put16(h + 6, 0);
    send_direct(MSG_SHOT_DATA, seq, h, 8, px, 256 * 192 * 2);
    free(px);
}

static void remember_launch(const char *sd_path)
{
    char local[264];
    if (local_path("sd:/dsidbg/last.txt", local, sizeof local) < 0) return;
    mkdirs_for(local);
    FILE *f = fopen(local, "wb");
    if (!f) return;
    fputs(sd_path, f);
    fclose(f);
}

static void cmd_reboot(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256], local[264];
    payload_str(p, n, path, sizeof path);
    if (path[0]) {
        if (local_path(path, local, sizeof local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
        FILE *f = fopen(local, "rb");
        if (!f) { ack(seq, -1, "%s not found", path); return; }
        fclose(f);
        remember_launch(path);
    }
    ack(seq, 0, "rebooting");
    log_flush();
    flush_tx(1);
    plat_reboot(path[0] ? path : NULL);
}

static void cmd_put_begin(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256];
    if (put_f) { fclose(put_f); put_f = NULL; remove(put_tmp); }
    put_err = 0;
    if (n < 9) { ack(seq, -1, "bad request"); return; }
    put_size = get32(p);
    put_crc = get32(p + 4);
    payload_str(p + 8, n - 8, path, sizeof path);
    if (local_path(path, put_local, sizeof put_local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    snprintf(put_tmp, sizeof put_tmp, "%s.part", put_local);
    mkdirs_for(put_tmp);
    put_f = fopen(put_tmp, "wb");
    if (!put_f) { ack(seq, -1, "can't create %s (errno %d)", path, errno); return; }
    put_got = 0;
    put_crc_now = 0;
    ack(seq, 0, "ok");
}

static void cmd_put_data(const uint8_t *p, size_t n)
{
    if (!put_f || put_err) return;
    if (fwrite(p, 1, n, put_f) != n) put_err = errno ? errno : EIO;
    put_got += (uint32_t)n;
    put_crc_now = dsidbg__crc32(put_crc_now, p, n);
}

static void cmd_put_end(uint16_t seq)
{
    if (!put_f) { ack(seq, -1, "no transfer in progress"); return; }
    int close_err = fclose(put_f);
    put_f = NULL;
    if (put_err || close_err) { remove(put_tmp); ack(seq, -1, "write failed (errno %d)", put_err); return; }
    if (put_got != put_size || put_crc_now != put_crc) {
        remove(put_tmp);
        ack(seq, -1, "size/crc mismatch: got %lu bytes crc %08lx", (unsigned long)put_got, (unsigned long)put_crc_now);
        return;
    }
    remove(put_local);
    if (rename(put_tmp, put_local) != 0) { ack(seq, -1, "rename failed (errno %d)", errno); return; }
    ack(seq, 0, "%lu bytes", (unsigned long)put_got);
}

static void cmd_get(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256], local[264];
    payload_str(p, n, path, sizeof path);
    if (local_path(path, local, sizeof local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    FILE *f = fopen(local, "rb");
    if (!f) { ack(seq, -1, "%s not found", path); return; }
    static uint8_t buf[8192];
    uint32_t size = 0, crc = 0;
    size_t got;
    while ((got = fread(buf, 1, sizeof buf, f)) > 0) {
        if (send_direct(MSG_FILE_DATA, seq, buf, got, NULL, 0) < 0) { fclose(f); return; }
        size += (uint32_t)got;
        crc = dsidbg__crc32(crc, buf, got);
        plat_heartbeat();
    }
    int err = ferror(f);
    fclose(f);
    uint8_t end[12];
    put32(end, (uint32_t)(err ? -1 : 0));
    put32(end + 4, size);
    put32(end + 8, crc);
    send_direct(MSG_FILE_END, seq, end, sizeof end, NULL, 0);
}

static void cmd_list(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256], local[264];
    payload_str(p, n, path, sizeof path);
    if (!path[0]) strcpy(path, "sd:/");
    if (local_path(path, local, sizeof local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    DIR *d = opendir(local);
    if (!d) { ack(seq, -1, "can't open %s", path); return; }
    static char out[16384];
    size_t len = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL && len < sizeof out - 300) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
        char full[600];
        struct stat sb;
        snprintf(full, sizeof full, "%s%s%s", local, local[strlen(local) - 1] == '/' ? "" : "/", e->d_name);
        int ok = stat(full, &sb) == 0;
        len += (size_t)snprintf(out + len, sizeof out - len, "%c\t%lu\t%s\n", ok && S_ISDIR(sb.st_mode) ? 'D' : 'F',
                                ok ? (unsigned long)sb.st_size : 0ul, e->d_name);
    }
    closedir(d);
    ack_text(seq, 0, out, len);
}

static void cmd_delete(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256], local[264];
    struct stat sb;
    payload_str(p, n, path, sizeof path);
    if (local_path(path, local, sizeof local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    if (stat(local, &sb) != 0) { ack(seq, -1, "%s not found", path); return; }
    if (S_ISDIR(sb.st_mode)) { ack(seq, -1, "only files can be deleted"); return; }
    if (remove(local) != 0) { ack(seq, -1, "delete failed (errno %d)", errno); return; }
    ack(seq, 0, "deleted");
}

static void cmd_app(uint16_t seq, const uint8_t *p, size_t n)
{
    char line[1024];
    static char reply[4096];
    payload_str(p, n, line, sizeof line);
    if (dsidbg__in_panic) { ack(seq, -1, "the app has crashed"); return; }
    if (!cfg.command) { ack(seq, -1, "the app has no command handler"); return; }
    reply[0] = 0;
    int r = cfg.command(line, reply, sizeof reply);
    reply[sizeof reply - 1] = 0;
    ack_text(seq, r, reply, strlen(reply));
}

static void handle(uint16_t type, uint16_t seq, const uint8_t *p, size_t n)
{
    if (st == ST_HANDSHAKE) {
        if (type != MSG_CHALLENGE || n != 36) { drop_conn("bad handshake"); return; }
        uint8_t msg[11 + 32], mac[20];
        memcpy(msg, "dsidbg-host", 11);
        memcpy(msg + 11, nonce_d, 16);
        memcpy(msg + 27, p, 16);
        dsidbg__hmac_sha1(key, key_len, msg, sizeof msg, mac);
        uint8_t diff = 0;
        for (int i = 0; i < 20; i++) diff |= mac[i] ^ p[16 + i];
        if (diff) {
            drop_conn("host failed authentication: check the key");
            retry_at = plat_ms() + 10000;
            return;
        }
        uint8_t msg2[10 + 32];
        memcpy(msg2, "dsidbg-dev", 10);
        memcpy(msg2 + 10, p, 16);
        memcpy(msg2 + 26, nonce_d, 16);
        dsidbg__hmac_sha1(key, key_len, msg2, sizeof msg2, mac);
        if (send_direct(MSG_PROOF, 0, mac, 20, NULL, 0) < 0) return;
        st = ST_READY;
        crash_files_done = 0;
        send_info();
        return;
    }
    switch (type) {
    case MSG_PING: send_direct(MSG_PONG, seq, p, n, NULL, 0); break;
    case MSG_INPUT: cmd_input(seq, p, n); break;
    case MSG_INPUT_CLEAR: ninj = 0; ack(seq, 0, "cleared"); break;
    case MSG_SHOT: cmd_shot(seq, p, n); break;
    case MSG_REBOOT: cmd_reboot(seq, p, n); break;
    case MSG_PUT_BEGIN: cmd_put_begin(seq, p, n); break;
    case MSG_PUT_DATA: cmd_put_data(p, n); break;
    case MSG_PUT_END: cmd_put_end(seq); break;
    case MSG_GET: cmd_get(seq, p, n); break;
    case MSG_LIST: cmd_list(seq, p, n); break;
    case MSG_DELETE: cmd_delete(seq, p, n); break;
    case MSG_PROF_START:
        prof_r = prof_w;
        prof_on = 1;
        ack(seq, 0, "%lu Hz", (unsigned long)plat_prof_hz());
        break;
    case MSG_PROF_STOP:
        prof_on = 0;
        while (prof_flush(1024, 1)) {}
        ack(seq, 0, "stopped");
        break;
    case MSG_APP_CMD: cmd_app(seq, p, n); break;
    case MSG_WATCHDOG:
        if (n < 4) { ack(seq, -1, "bad request"); break; }
        cfg.watchdog_seconds = get32(p);
        plat_set_watchdog(cfg.watchdog_seconds);
        ack(seq, 0, "watchdog %u s", cfg.watchdog_seconds);
        break;
    default: ack(seq, -1, "unknown message 0x%04x", type); break;
    }
}

static void pump_rx(void)
{
    for (;;) {
        long r = recv(sock, rxbuf + rxlen, sizeof rxbuf - rxlen, 0);
        if (r == 0) { drop_conn("host closed the connection"); return; }
        if (r < 0) {
            if (!would_block()) drop_conn("receive failed");
            return;
        }
        rxlen += (size_t)r;
        while (rxlen >= PROTO_HEADER) {
            uint32_t len = get32(rxbuf);
            if (len > PROTO_MAX_RX) { drop_conn("message too large"); return; }
            if (rxlen < PROTO_HEADER + len) break;
            handle((uint16_t)get16(rxbuf + 4), (uint16_t)get16(rxbuf + 6), rxbuf + PROTO_HEADER, len);
            if (sock < 0) return;
            rxlen -= PROTO_HEADER + len;
            memmove(rxbuf, rxbuf + PROTO_HEADER + len, rxlen);
        }
        if (rxlen == sizeof rxbuf) { drop_conn("receive buffer full"); return; }
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Connection

static void make_nonce(void)
{
    static uint32_t counter;
    uint32_t seed[7] = {plat_ms(), frame, ++counter, plat_ip(), (uint32_t)(uintptr_t)&seed, plat_hw_keys(),
                        plat_entropy()};
    uint8_t mac[20];
    dsidbg__hmac_sha1(key, key_len, seed, sizeof seed, mac);
    memcpy(nonce_d, mac, 16);
}

static void on_connected(void)
{
    uint8_t hello[4 + 16 + 160];
    size_t n = 0;
    st = ST_HANDSHAKE;
    st_time = plat_ms();
    make_nonce();
    put16(hello, PROTO_VERSION);
    put16(hello + 2, 0);
    memcpy(hello + 4, nonce_d, 16);
    n = 20;
    n += (size_t)snprintf((char *)hello + n, 80, "%s", cfg.app_name) + 1;
    n += (size_t)snprintf((char *)hello + n, 80, "%s", cfg.build_id) + 1;
    send_direct(MSG_HELLO, 0, hello, n, NULL, 0);
}

static void start_connect(void)
{
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    if (inet_pton(AF_INET, host, &a.sin_addr) != 1) {
        snprintf(off_reason, sizeof off_reason, "bad host address '%s'", host);
        retry_at = plat_ms() + 5000;
        return;
    }
    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) { retry_at = plat_ms() + 2000; return; }
    int one = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
#ifdef SO_NOSIGPIPE
    setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
    // A random local port: after a reboot the IP stack would otherwise reuse the same port, and a NAT (melonDS's,
    // for one) can map the new connection onto the old one.
    struct sockaddr_in local;
    memset(&local, 0, sizeof local);
    local.sin_family = AF_INET;
    local.sin_port = htons((uint16_t)(49152 + plat_entropy() % 16000));
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    bind(sock, (struct sockaddr *)&local, sizeof local); // on failure the stack picks a port
    fcntl(sock, F_SETFL, O_NONBLOCK);
    st = ST_CONNECTING;
    st_time = plat_ms();
    if (connect(sock, (struct sockaddr *)&a, sizeof a) == 0) on_connected();
    else if (!would_block()) drop_conn("connect failed");
}

static void check_connect(void)
{
    // The socket becomes writable when the pending connect() finishes; SO_ERROR says whether it worked. (Calling
    // connect() again to ask is not portable: lwIP rejects it.)
    struct pollfd pfd;
    memset(&pfd, 0, sizeof pfd);
    pfd.fd = sock;
    pfd.events = POLLOUT;
    int r = poll(&pfd, 1, 0);
    if (r > 0) {
        int err = 0;
        socklen_t len = sizeof err;
        if (getsockopt(sock, SOL_SOCKET, SO_ERROR, &err, &len) == 0 && err == 0 && !(pfd.revents & POLLERR))
            on_connected();
        else
            drop_conn("host not reachable");
        return;
    }
    if (r < 0) { drop_conn("poll failed"); return; }
    if (plat_ms() - st_time > CONNECT_TIMEOUT_MS) drop_conn("connect timeout");
}

static void service(void)
{
    if (st == ST_OFF) return;
    uint32_t now = plat_ms();
    if (!plat_net_poll()) {
        if (sock >= 0) drop_conn("wifi lost");
        return;
    }
    switch (st) {
    case ST_DOWN:
        if ((int32_t)(now - retry_at) >= 0) start_connect();
        break;
    case ST_CONNECTING:
        check_connect();
        break;
    case ST_HANDSHAKE:
        pump_rx();
        if (st == ST_HANDSHAKE && now - st_time > 5000) drop_conn("handshake timeout");
        break;
    case ST_READY:
        pump_rx();
        if (st != ST_READY) break;
        if (crash_pending && send_crash(&dsidbg__crash, crash_stack, dsidbg__crash.stack_len) == 0) crash_pending = 0;
        if (!crash_files_done) { crash_files_done = 1; send_crash_files(); }
        if (prof_w - prof_r >= 512) prof_flush(1024, 0);
        if (now - last_status >= 1000) send_status(now);
        flush_tx(0);
        break;
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Public API

void dsidbg_config_defaults(dsidbg_config *c)
{
    memset(c, 0, sizeof *c);
    c->app_name = "app";
    c->build_id = "";
    c->config_path = "sd:/dsidbg/config.ini";
    c->capture_bank = -1;
    c->watchdog_seconds = 15;
}

static void trim(char *s)
{
    char *e = s + strlen(s);
    while (e > s && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t')) *--e = 0;
}

static void load_config(const char *sd_path)
{
    char local[264], line[256];
    if (!sd_path || local_path(sd_path, local, sizeof local) < 0) return;
    FILE *f = fopen(local, "rb");
    if (!f) return;
    while (fgets(line, sizeof line, f)) {
        trim(line);
        char *s = line;
        while (*s == ' ' || *s == '\t') s++;
        if (*s == '#' || *s == ';' || !*s) continue;
        char *eq = strchr(s, '=');
        if (!eq) continue;
        *eq = 0;
        char *v = eq + 1;
        trim(s);
        while (*v == ' ' || *v == '\t') v++;
        if (!strcmp(s, "host")) snprintf(host, sizeof host, "%s", v);
        else if (!strcmp(s, "port")) port = (uint16_t)atoi(v);
        else if (!strcmp(s, "key")) snprintf(key, sizeof key, "%s", v);
        else if (!strcmp(s, "ssid")) snprintf(ssid, sizeof ssid, "%s", v);
        else if (!strcmp(s, "pass")) snprintf(pass, sizeof pass, "%s", v);
    }
    fclose(f);
}

int dsidbg_init(const dsidbg_config *in)
{
    if (inited) return 0;
    if (in) cfg = *in;
    else dsidbg_config_defaults(&cfg);
    if (!cfg.app_name) cfg.app_name = "app";
    if (!cfg.build_id) cfg.build_id = "";
    if (!cfg.config_path) cfg.config_path = "sd:/dsidbg/config.ini";

    plat_crash_install(cfg.flags);
    plat_set_watchdog(cfg.watchdog_seconds);
    inited = 1;

    if (plat_fs_init() == 0) load_config(cfg.config_path);
    if (cfg.host) snprintf(host, sizeof host, "%s", cfg.host);
    if (cfg.port) port = cfg.port;
    if (cfg.key) snprintf(key, sizeof key, "%s", cfg.key);
    if (cfg.ssid) snprintf(ssid, sizeof ssid, "%s", cfg.ssid);
    if (cfg.pass) snprintf(pass, sizeof pass, "%s", cfg.pass);
    if (!port) port = DEFAULT_PORT;
    key_len = strlen(key);

    if (!host[0] || !key_len) {
        snprintf(off_reason, sizeof off_reason, "no host/key: see %s", cfg.config_path);
        return -1;
    }
    if (plat_init(&cfg, ssid[0] ? ssid : NULL, pass) != 0) {
        snprintf(off_reason, sizeof off_reason, "wifi init failed");
        return -1;
    }
    st = ST_DOWN;
    retry_at = 0;
    last_status = plat_ms();
    return 0;
}

void dsidbg_heartbeat(void) { plat_heartbeat(); }

void dsidbg_frame(void)
{
    frame++;
    plat_heartbeat();
    int k = 0;
    for (int i = 0; i < ninj; i++)
        if (inj[i].end > frame) inj[k++] = inj[i];
    ninj = k;
    log_flush();
    service();
    plat_yield();
}

int dsidbg_connected(void) { return st == ST_READY; }

const char *dsidbg_status(void)
{
    switch (st) {
    case ST_OFF: snprintf(status_text, sizeof status_text, "off: %s", off_reason); break;
    case ST_DOWN:
        if (!plat_net_poll()) snprintf(status_text, sizeof status_text, "wifi: %s", plat_net_state());
        else snprintf(status_text, sizeof status_text, "waiting for %s:%u (%.60s)", host, port, off_reason);
        break;
    case ST_CONNECTING: snprintf(status_text, sizeof status_text, "connecting to %s:%u", host, port); break;
    case ST_HANDSHAKE: snprintf(status_text, sizeof status_text, "authenticating"); break;
    case ST_READY: snprintf(status_text, sizeof status_text, "connected to %s:%u", host, port); break;
    }
    return status_text;
}

uint32_t dsidbg_keys(uint32_t real)
{
    uint32_t keys = 0;
    for (int i = 0; i < ninj; i++) {
        if (frame < inj[i].start || frame >= inj[i].end) continue;
        if (inj[i].flags & INPUT_REPLACE) real = 0;
        keys |= inj[i].keys;
    }
    return real | keys;
}

int dsidbg_touch(int16_t *x, int16_t *y)
{
    for (int i = 0; i < ninj; i++) {
        if (frame < inj[i].start || frame >= inj[i].end || !(inj[i].keys & (1u << 12))) continue;
        *x = inj[i].x;
        *y = inj[i].y;
        return 1;
    }
    return 0;
}

void dsidbg_scan_keys(void)
{
    held = dsidbg_keys(plat_hw_keys());
    down = held & ~prev_held;
    up = prev_held & ~held;
    prev_held = held;
}

uint32_t dsidbg_keys_held(void) { return held; }
uint32_t dsidbg_keys_down(void) { return down; }
uint32_t dsidbg_keys_up(void) { return up; }

void dsidbg_capture_stdout(int echo) { plat_hook_stdout(echo ? 2 : 1); }

void dsidbg_reboot(const char *sd_path)
{
    char local[264];
    if (sd_path && local_path(sd_path, local, sizeof local) == 0) remember_launch(sd_path);
    else sd_path = NULL;
    log_flush();
    if (st == ST_READY) flush_tx(1);
    plat_reboot(sd_path);
}
