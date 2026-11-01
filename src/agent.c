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

#include <unistd.h>

#ifdef DSIDBG_NDS
#define sock_close closesocket
#else
#include <poll.h>
#define sock_close close
#endif

#include "internal.h"

#define DEFAULT_PORT 7710
#define TXQ_DEFAULT_KIB 16 // send queue, allocated by dsidbg_init()
#define LOGLINE_MAX  512
#define MAX_INJECT   32
#define PROF_N       4096 // power of two
#define CONNECT_TIMEOUT_MS 4000
#define DISCOVER_MS        5000 // one discovery round
#define DISCOVER_RESEND_MS 1000
#define SEND_TIMEOUT_MS    10000
#define CRASH_SAVE_DELAY_MS 8000

enum { ST_OFF, ST_DOWN, ST_DISCOVER, ST_CONNECTING, ST_HANDSHAKE, ST_READY };

static dsidbg_config cfg;
static char host[64], key[128], ssid[33], pass[65];
static size_t key_len;
static uint16_t port;

// Where to connect. The configured host is tried first; after a failure (or with no host configured) the agent
// broadcasts a discovery request and connects to the daemon that answers with a valid signature.
static uint32_t cfg_addr, cur_addr;   // network byte order; 0 = none
static uint16_t cur_port;
static int connect_fails, discovered, udp = -1;
static uint8_t disc_nonce[16];
static uint32_t disc_until, disc_next;
static int inited, st = ST_OFF, sock = -1;
static uint32_t st_time, retry_at, last_status, frames_at_status;
static uint8_t nonce_d[16];
static uint32_t frame;
static char status_text[160], off_reason[96] = "not started";
static uint32_t log_drops;
static int crash_files_done;

// Outgoing queue for asynchronous messages (logs, status, data, profiler). Replies bypass it via send_direct(),
// which drains it first so the stream stays in order. It and the receive buffer are allocated by dsidbg_init(), so
// an app that never starts the agent doesn't pay for them.
static uint8_t *txq;
static uint32_t txq_size; // power of two
static uint32_t txq_r, txq_w;

static char logline[LOGLINE_MAX];
static size_t loglen;

#define RX_CAP (PROTO_HEADER + PROTO_MAX_RX)

// Buffers the SD card driver reads into or writes from by DMA must own whole cache lines (32 bytes on the DSi):
// a line shared with other data (heap bookkeeping, say) can be written back over freshly DMA'd bytes.
#define DMA_ALIGN 32
static void *dma_alloc(size_t n) { return aligned_alloc(DMA_ALIGN, (n + DMA_ALIGN - 1) & ~(size_t)(DMA_ALIGN - 1)); }

#define GET_CHUNK 4096           // download message size
#define GET_CHUNKS_PER_FRAME 3   // up to 12 KiB per frame, if the send queue has room
static FILE *get_f;              // the running download, if any
static uint8_t *get_buf;
static uint16_t get_seq;
static uint32_t get_size, get_crc;
static void get_close(void);

#define PUT_BUFFER (32 * 1024) // uploads go to the card in pieces this big (fewer, faster SD writes)
static uint8_t *put_buf;       // allocated during an upload only
static size_t put_fill;
static uint8_t *rxbuf;
static size_t rxlen;

typedef struct { uint32_t start, end, keys, flags; int16_t x, y; } inject_t;
static inject_t inj[MAX_INJECT];
static int ninj;
static uint32_t held, down, up, prev_held;

static volatile uint32_t *prof_buf; // allocated while profiling
static volatile uint32_t prof_w, prof_r;
static volatile int prof_on;

// Uploads use the file descriptor API with their own aligned buffer: stdio would split them into small writes.
static int put_fd = -1;
static int put_flush(void)
{
    size_t done = 0;
    while (done < put_fill) {
        ssize_t w = write(put_fd, put_buf + done, put_fill - done);
        if (w <= 0) return -1;
        done += (size_t)w;
    }
    put_fill = 0;
    return 0;
}
static int put_close(void)
{
    int r = 0;
    if (put_fd >= 0) r = close(put_fd);
    put_fd = -1;
    free(put_buf);
    put_buf = NULL;
    put_fill = 0;
    return r;
}
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
    return dsidbg__plat_path(sd_path, out, cap);
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

static void txq_put(const void *src, size_t n)
{
    const uint8_t *p = src;
    while (n) {
        uint32_t at = txq_w & (txq_size - 1);
        size_t take = txq_size - at < n ? txq_size - at : n;
        memcpy(txq + at, p, take);
        txq_w += (uint32_t)take;
        p += take;
        n -= take;
    }
}

static size_t txq_free(void) { return txq ? txq_size - txq_used() : 0; }

// Queues a whole message (header + a + b), or nothing if it doesn't fit. Returns 0 or -1.
static int queue_msg_seq(uint16_t type, uint16_t seq, const void *a, size_t alen, const void *b, size_t blen)
{
    if (PROTO_HEADER + alen + blen > txq_free()) return -1;
    uint8_t hdr[PROTO_HEADER];
    put32(hdr, (uint32_t)(alen + blen));
    put16(hdr + 4, type);
    put16(hdr + 6, seq);
    txq_put(hdr, sizeof hdr);
    if (alen) txq_put(a, alen);
    if (blen) txq_put(b, blen);
    return 0;
}

static int queue_msg(uint16_t type, const void *a, size_t alen, const void *b, size_t blen)
{
    return queue_msg_seq(type, 0, a, alen, b, blen);
}

// Agent steps, logged with DSIDBG_TRACE (and shown on screen with DSIDBG_ECHO_LOG): after a freeze the last line
// says where the agent was.
#define TRACE(...) do { if (cfg.flags & DSIDBG_TRACE) { dsidbg_log("dsidbg: " __VA_ARGS__); log_flush(); } } while (0)
static void log_flush(void);

static void shot_cancel(void);

static void drop_conn(const char *why)
{
    TRACE("drop: %s (state %d, upload %s)", why, st, put_fd >= 0 ? "open" : "none");
    if (sock >= 0) sock_close(sock);
    sock = -1;
    // The queue is only sent once authenticated. After that, a partly sent message can't be resumed on a new
    // connection, so drop what's queued; before that (failed connects), keep it: it holds the boot logs.
    if (st == ST_READY) txq_r = txq_w;
    st = ST_DOWN;
    retry_at = dsidbg__plat_ms() + 2000;
    rxlen = 0;
    if (get_f) get_close();
    shot_cancel(); // its reply can't reach the host any more (a pending hook just isn't asked again)
    if (put_fd >= 0) {
        put_close();
        TRACE("drop: upload file closed");
        remove(put_tmp);
        TRACE("drop: partial upload removed");
    }
    snprintf(off_reason, sizeof off_reason, "%s", why);
}

// Sends all of p. With block=0 it stops when the socket is full. Returns bytes sent, or -1 (connection dropped).
static long raw_send(const void *p, size_t n, int block)
{
    const uint8_t *b = p;
    size_t done = 0;
    uint32_t t0 = dsidbg__plat_ms();
    while (done < n) {
        long r = send(sock, b + done, n - done, 0);
        if (r > 0) { done += (size_t)r; t0 = dsidbg__plat_ms(); continue; }
        if (r < 0 && would_block()) {
            if (!block) break;
            if (dsidbg__plat_ms() - t0 > SEND_TIMEOUT_MS) { drop_conn("send timeout"); return -1; }
            dsidbg__plat_heartbeat();
            dsidbg__plat_yield();
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
        uint32_t at = txq_r & (txq_size - 1);
        uint32_t chunk = txq_used();
        if (chunk > txq_size - at) chunk = txq_size - at;
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
    if (cfg.flags & DSIDBG_ECHO_LOG) dsidbg__plat_console_write(text, len);
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
                     "frame=%lu\nfps=%lu.%lu\nuptime_ms=%lu\nlog_drops=%lu\nprof=%d\npanic=%d\nheap_kib=%lu\n",
                     (unsigned long)frame, (unsigned long)(fps10 / 10), (unsigned long)(fps10 % 10),
                     (unsigned long)now, (unsigned long)log_drops, prof_on, dsidbg__in_panic,
                     (unsigned long)(dsidbg__plat_heap_used() / 1024));
    queue_msg(MSG_STATUS, text, (size_t)n, NULL, 0);
    last_status = now;
    frames_at_status = frame;
}

static void send_info(void)
{
    char text[512], plat[256] = "";
    struct in_addr ip;
    ip.s_addr = dsidbg__plat_ip();
    dsidbg__plat_info(plat, sizeof plat);
    int n = snprintf(text, sizeof text,
                     "lib=%s\nproto=%d\napp=%s\nbuild=%s\nip=%s\nhost_from=%s\ncapture_bank=%d\nwatchdog=%u\n"
                     "prof_hz=%lu\n%s",
                     DSIDBG_VERSION, PROTO_VERSION, cfg.app_name, cfg.build_id, inet_ntoa(ip),
                     discovered ? "discovery" : "config", cfg.capture_bank,
                     cfg.watchdog_seconds, (unsigned long)dsidbg__plat_prof_hz(), plat);
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
// The samples go out straight from the ring (no copy): both the DSi and the simulator's hosts are little-endian.
static uint32_t prof_flush(uint32_t max, int direct)
{
    if (!prof_buf) return 0;
    uint32_t n = prof_w - prof_r, at = prof_r & (PROF_N - 1);
    if (n > max) n = max;
    if (n > PROF_N - at) n = PROF_N - at; // contiguous part of the ring
    if (!n) return 0;
    uint8_t hz[4];
    put32(hz, dsidbg__plat_prof_hz());
    const void *pcs = (const void *)&prof_buf[at];
    int r = direct ? send_direct(MSG_PROF, 0, hz, 4, pcs, n * 4) : queue_msg(MSG_PROF, hz, 4, pcs, n * 4);
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
        uint8_t *stack = malloc(CRASH_STACK_MAX);
        size_t got = fread(&c, 1, sizeof c, f);
        size_t sl = got == sizeof c && stack ? fread(stack, 1, CRASH_STACK_MAX, f) : 0;
        fclose(f);
        if (got != sizeof c) { free(stack); continue; }
        c.flags |= CRASH_FROM_FILE;
        int sent_ok = send_crash(&c, stack, sl) == 0;
        free(stack);
        if (!sent_ok) break;
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
    // Interrupting the thread scheduler or a network thread leaves no way to switch threads: the network and the
    // card are out of reach, so only the screen can show the report. (Before the stack copy: for a hang in the
    // scheduler this switches the registers to the main thread's wait point.)
    int threads = dsidbg__plat_panic_on_main_thread(c);
    uint32_t sp = c->regs[13], n = CRASH_STACK_MAX;
    while (n && !dsidbg__plat_valid_ram(sp, n)) n /= 2;
    if (n) memcpy(crash_stack, (const void *)(uintptr_t)sp, n);
    c->stack_base = sp;
    c->stack_len = n;
    crash_pending = 1;
    const char *stuck = "not sendable: press power";
    log_flush();
    dsidbg__plat_crash_show(c, threads ? dsidbg_status() : stuck);

    uint32_t t0 = dsidbg__plat_ms(), shown = t0;
    int saved = 0;
    for (;;) {
        if (inited && threads) service();
        uint32_t now = dsidbg__plat_ms();
        if (threads && crash_pending && !saved && (cfg.flags & DSIDBG_SAVE_CRASHES) && now - t0 > CRASH_SAVE_DELAY_MS) {
            save_crash_file();
            saved = 1;
        }
        if (now - shown > 1000) {
            dsidbg__plat_crash_show(c, !threads ? stuck : crash_pending ? dsidbg_status() : "reported to host");
            shown = now;
        }
        dsidbg__plat_wait_frame();
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

// A screenshot whose hook answered DSIDBG_SHOT_PENDING: the hook is asked again from later frames (shot_step).
#define SHOT_PENDING_MS 3000
static uint16_t *shot_px;
static uint16_t shot_seq;
static int shot_screen;
static uint32_t shot_deadline;

static void shot_cancel(void)
{
    free(shot_px);
    shot_px = NULL;
}

// Asks the hook (again), falls back to the display capture, and replies; or leaves the shot pending.
static void shot_finish(void)
{
    uint16_t *px = shot_px;
    int screen = shot_screen;
    uint16_t seq = shot_seq;
    int r = -1;
    if (cfg.screenshot && !dsidbg__in_panic) r = cfg.screenshot(screen, px);
    if (r == DSIDBG_SHOT_PENDING) {
        if ((int32_t)(dsidbg__plat_ms() - shot_deadline) < 0) return; // asked again on a later frame
        shot_cancel();
        ack(seq, -1, "screenshot timed out: the app's screenshot hook stayed pending");
        return;
    }
    shot_px = NULL; // this function replies and frees it
    if (r != 0 && screen == DSIDBG_SCREEN_TOP && cfg.capture_bank >= 0)
        r = dsidbg__plat_capture(screen, px, cfg.capture_bank);
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

static void cmd_shot(uint16_t seq, const uint8_t *p, size_t n)
{
    if (shot_px) { ack(seq, -1, "a screenshot is already in progress"); return; }
    shot_px = malloc(256 * 192 * 2);
    if (!shot_px) { ack(seq, -1, "out of memory"); return; }
    shot_screen = n ? p[0] : DSIDBG_SCREEN_TOP;
    shot_seq = seq;
    shot_deadline = dsidbg__plat_ms() + SHOT_PENDING_MS;
    shot_finish();
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
    dsidbg__plat_reboot(path[0] ? path : NULL);
}

static void cmd_put_begin(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256];
    if (put_fd >= 0) { put_close(); remove(put_tmp); }
    put_err = 0;
    if (n < 9) { ack(seq, -1, "bad request"); return; }
    put_size = get32(p);
    put_crc = get32(p + 4);
    payload_str(p + 8, n - 8, path, sizeof path);
    if (local_path(path, put_local, sizeof put_local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    snprintf(put_tmp, sizeof put_tmp, "%s.part", put_local);
    mkdirs_for(put_tmp);
    put_buf = dma_alloc(PUT_BUFFER);
    if (!put_buf) { ack(seq, -1, "out of memory"); return; }
    put_fd = open(put_tmp, O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (put_fd < 0) { put_close(); ack(seq, -1, "can't create %s (errno %d)", path, errno); return; }
    put_got = 0;
    TRACE("upload: %s, %lu bytes", path, (unsigned long)put_size);
    put_crc_now = 0;
    ack(seq, 0, "ok");
}

static void cmd_put_data(const uint8_t *p, size_t n)
{
    if (put_fd < 0 || put_err) return;
    put_got += (uint32_t)n;
    put_crc_now = dsidbg__crc32(put_crc_now, p, n);
    while (n) {
        size_t take = PUT_BUFFER - put_fill;
        if (take > n) take = n;
        memcpy(put_buf + put_fill, p, take);
        put_fill += take;
        p += take;
        n -= take;
        if (put_fill == PUT_BUFFER && put_flush() < 0) { put_err = errno ? errno : EIO; return; }
        dsidbg__plat_heartbeat(); // a slow upload is not a hang
    }
}

// The host sends an upload in windows and waits for this ACK after each one, so no Wi-Fi traffic runs while the card
// is written. On the DSi, the ARM7 does both, and an SD write during a stream of Wi-Fi packets has hung it (the
// ARM9 then waits forever for the write to finish).
static void cmd_put_sync(uint16_t seq)
{
    if (put_fd < 0) { ack(seq, -1, "no transfer in progress"); return; }
    if (!put_err && put_fill && put_flush() < 0) put_err = errno ? errno : EIO;
    dsidbg__plat_heartbeat();
    if (put_err) { ack(seq, -1, "write failed (errno %d)", put_err); return; }
    ack(seq, 0, "%lu", (unsigned long)put_got);
}

static void cmd_put_end(uint16_t seq)
{
    if (put_fd < 0) { ack(seq, -1, "no transfer in progress"); return; }
    if (!put_err && put_flush() < 0) put_err = errno ? errno : EIO;
    int close_err = put_close();
    if (put_err || close_err) { remove(put_tmp); ack(seq, -1, "write failed (errno %d)", put_err); return; }
    if (put_got != put_size || put_crc_now != put_crc) {
        remove(put_tmp);
        ack(seq, -1, "size/crc mismatch: got %lu bytes crc %08lx", (unsigned long)put_got, (unsigned long)put_crc_now);
        return;
    }
    remove(put_local);
    if (rename(put_tmp, put_local) != 0) { ack(seq, -1, "rename failed (errno %d)", errno); return; }
    TRACE("upload: done");
    ack(seq, 0, "%lu bytes", (unsigned long)put_got);
}

// Downloads are sent a few chunks per dsidbg_frame() (see get_step()), so a big file doesn't stop the app for the
// whole transfer.
static void cmd_get(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256], local[264];
    payload_str(p, n, path, sizeof path);
    if (get_f) { ack(seq, -1, "a download is already running"); return; }
    if (local_path(path, local, sizeof local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    FILE *f = fopen(local, "rb");
    if (!f) { ack(seq, -1, "%s not found", path); return; }
    get_buf = dma_alloc(GET_CHUNK);
    if (!get_buf) { fclose(f); ack(seq, -1, "out of memory"); return; }
    get_f = f;
    get_seq = seq;
    get_size = get_crc = 0;
    TRACE("download: %s", path);
}

static void get_close(void)
{
    if (get_f) fclose(get_f);
    get_f = NULL;
    free(get_buf);
    get_buf = NULL;
}

// Queues up to GET_CHUNKS_PER_FRAME chunks of the running download, then the end message.
// Chunks go into the send queue only when there is room, and the queue drains without blocking (flush_tx(0)), so
// the network sets the download speed and the app keeps its frame rate.
static void get_step(void)
{
    for (int i = 0; get_f && i < GET_CHUNKS_PER_FRAME; i++) {
        // A chunk at most a quarter of the queue, and a quarter kept free for logs and status: a small configured
        // queue still downloads, and the download never crowds out the log.
        size_t chunk = txq_size / 4 < GET_CHUNK ? txq_size / 4 : GET_CHUNK;
        if (txq_free() < PROTO_HEADER + chunk + txq_size / 4) return; // the network hasn't caught up yet
        size_t got = fread(get_buf, 1, chunk, get_f);
        if (got > 0) {
            queue_msg_seq(MSG_FILE_DATA, get_seq, get_buf, got, NULL, 0);
            get_size += (uint32_t)got;
            get_crc = dsidbg__crc32(get_crc, get_buf, got);
            if (got == chunk) continue;
        }
        uint8_t end[12];
        put32(end, (uint32_t)(ferror(get_f) ? -1 : 0));
        put32(end + 4, get_size);
        put32(end + 8, get_crc);
        uint16_t seq = get_seq;
        get_close();
        TRACE("download: done");
        queue_msg_seq(MSG_FILE_END, seq, end, sizeof end, NULL, 0); // fits: a chunk's worth of room was free
    }
}

static void cmd_list(uint16_t seq, const uint8_t *p, size_t n)
{
    char path[256], local[264];
    payload_str(p, n, path, sizeof path);
    if (!path[0]) strcpy(path, "sd:/");
    if (local_path(path, local, sizeof local) < 0) { ack(seq, -1, "path must be on sd:/"); return; }
    DIR *d = opendir(local);
    if (!d) { ack(seq, -1, "can't open %s", path); return; }
    enum { OUT_CAP = 16384 };
    char *out = malloc(OUT_CAP);
    if (!out) { closedir(d); ack(seq, -1, "out of memory"); return; }
    size_t len = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL && len < OUT_CAP - 300) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
        char full[600];
        struct stat sb;
        snprintf(full, sizeof full, "%s%s%s", local, local[strlen(local) - 1] == '/' ? "" : "/", e->d_name);
        int ok = stat(full, &sb) == 0;
        len += (size_t)snprintf(out + len, OUT_CAP - len, "%c\t%lu\t%s\n", ok && S_ISDIR(sb.st_mode) ? 'D' : 'F',
                                ok ? (unsigned long)sb.st_size : 0ul, e->d_name);
    }
    closedir(d);
    ack_text(seq, 0, out, len);
    free(out);
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
    enum { REPLY_CAP = 4096 };
    char line[1024];
    payload_str(p, n, line, sizeof line);
    if (dsidbg__in_panic) { ack(seq, -1, "the app has crashed"); return; }
    if (!cfg.command) { ack(seq, -1, "the app has no command handler"); return; }
    char *reply = malloc(REPLY_CAP);
    if (!reply) { ack(seq, -1, "out of memory"); return; }
    reply[0] = 0;
    int r = cfg.command(line, reply, REPLY_CAP);
    reply[REPLY_CAP - 1] = 0;
    ack_text(seq, r, reply, strlen(reply));
    free(reply);
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
            connect_fails++;
            retry_at = dsidbg__plat_ms() + 10000;
            return;
        }
        uint8_t msg2[10 + 32];
        memcpy(msg2, "dsidbg-dev", 10);
        memcpy(msg2 + 10, p, 16);
        memcpy(msg2 + 26, nonce_d, 16);
        dsidbg__hmac_sha1(key, key_len, msg2, sizeof msg2, mac);
        if (send_direct(MSG_PROOF, 0, mac, 20, NULL, 0) < 0) return;
        st = ST_READY;
        TRACE("authenticated");
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
    case MSG_PUT_SYNC: cmd_put_sync(seq); break;
    case MSG_GET: cmd_get(seq, p, n); break;
    case MSG_LIST: cmd_list(seq, p, n); break;
    case MSG_DELETE: cmd_delete(seq, p, n); break;
    case MSG_PROF_START:
        if (!prof_buf && !(prof_buf = malloc(PROF_N * sizeof *prof_buf))) { ack(seq, -1, "out of memory"); break; }
        prof_r = prof_w;
        prof_on = 1; // after the buffer exists: the IRQ hook checks only this
        ack(seq, 0, "%lu Hz", (unsigned long)dsidbg__plat_prof_hz());
        break;
    case MSG_PROF_STOP:
        prof_on = 0;
        while (prof_flush(1024, 1)) {}
        free((void *)prof_buf);
        prof_buf = NULL;
        ack(seq, 0, "stopped");
        break;
    case MSG_APP_CMD: cmd_app(seq, p, n); break;
    case MSG_WATCHDOG:
        if (n < 4) { ack(seq, -1, "bad request"); break; }
        cfg.watchdog_seconds = get32(p);
        dsidbg__plat_set_watchdog(cfg.watchdog_seconds);
        ack(seq, 0, "watchdog %u s", cfg.watchdog_seconds);
        break;
    default: ack(seq, -1, "unknown message 0x%04x", type); break;
    }
}

// Bytes received per dsidbg_frame() at most. Without a limit an upload keeps this loop busy (the SD writes let the
// network thread refill the socket), and the app gets only a few frames a second. 24 KiB a frame is far more than
// the Wi-Fi delivers.
#define RX_PER_FRAME (24 * 1024)

static void pump_rx(void)
{
    size_t budget = RX_PER_FRAME;
    while (budget > 0) {
        size_t room = RX_CAP - rxlen;
        long r = recv(sock, rxbuf + rxlen, room < budget ? room : budget, 0);
        if (r == 0) { drop_conn("host closed the connection"); return; }
        if (r < 0) {
            if (!would_block()) drop_conn("receive failed");
            return;
        }
        rxlen += (size_t)r;
        budget -= (size_t)r;
        while (rxlen >= PROTO_HEADER) {
            uint32_t len = get32(rxbuf);
            if (len > PROTO_MAX_RX) { drop_conn("message too large"); return; }
            if (rxlen < PROTO_HEADER + len) break;
            handle((uint16_t)get16(rxbuf + 4), (uint16_t)get16(rxbuf + 6), rxbuf + PROTO_HEADER, len);
            if (sock < 0) return;
            rxlen -= PROTO_HEADER + len;
            memmove(rxbuf, rxbuf + PROTO_HEADER + len, rxlen);
        }
        if (rxlen == RX_CAP) { drop_conn("receive buffer full"); return; }
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Connection

static void make_nonce(void)
{
    static uint32_t counter;
    uint32_t seed[7] = {dsidbg__plat_ms(), frame, ++counter, dsidbg__plat_ip(), (uint32_t)(uintptr_t)&seed,
                        dsidbg__plat_hw_keys(), dsidbg__plat_entropy()};
    uint8_t mac[20];
    dsidbg__hmac_sha1(key, key_len, seed, sizeof seed, mac);
    memcpy(nonce_d, mac, 16);
}

static void on_connected(void)
{
    TRACE("tcp connected");
    uint8_t hello[4 + 16 + 160];
    size_t n = 0;
    st = ST_HANDSHAKE;
    st_time = dsidbg__plat_ms();
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
    a.sin_port = htons(cur_port);
    a.sin_addr.s_addr = cur_addr;
    TRACE("connect: %s:%u", inet_ntoa(a.sin_addr), cur_port);
    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) { retry_at = dsidbg__plat_ms() + 2000; return; }
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
    local.sin_port = htons((uint16_t)(49152 + dsidbg__plat_entropy() % 16000));
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    bind(sock, (struct sockaddr *)&local, sizeof local); // on failure the stack picks a port
    fcntl(sock, F_SETFL, O_NONBLOCK);
    st = ST_CONNECTING;
    st_time = dsidbg__plat_ms();
    if (connect(sock, (struct sockaddr *)&a, sizeof a) == 0) on_connected();
    else if (!would_block()) { drop_conn("connect failed"); connect_fails++; }
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
            { drop_conn("host not reachable"); connect_fails++; }
        return;
    }
    if (r < 0) { drop_conn("poll failed"); connect_fails++; return; }
    if (dsidbg__plat_ms() - st_time > CONNECT_TIMEOUT_MS) { drop_conn("connect timeout"); connect_fails++; }
}

// ---------------------------------------------------------------------------------------------------------------
// Discovery

static void stop_discovery(void)
{
    if (udp >= 0) sock_close(udp);
    udp = -1;
}

static void start_discovery(void)
{
    TRACE("discovery: broadcasting");
    udp = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp < 0) { retry_at = dsidbg__plat_ms() + 2000; return; }
    int one = 1;
    setsockopt(udp, SOL_SOCKET, SO_BROADCAST, &one, sizeof one);
    fcntl(udp, F_SETFL, O_NONBLOCK);
    uint32_t seed[4] = {dsidbg__plat_entropy(), dsidbg__plat_ms(), frame, (uint32_t)connect_fails};
    uint8_t mac[20];
    dsidbg__hmac_sha1(key, key_len, seed, sizeof seed, mac);
    memcpy(disc_nonce, mac, 16);
    st = ST_DISCOVER;
    disc_until = dsidbg__plat_ms() + DISCOVER_MS;
    disc_next = 0;
}

static void poll_discovery(uint32_t now)
{
    if ((int32_t)(now - disc_next) >= 0) {
        uint8_t req[24];
        memcpy(req, "DSDBGQ1", 8); // includes the terminating NUL: 8 bytes
        memcpy(req + 8, disc_nonce, 16);
        uint32_t targets[4];
        int n = dsidbg__plat_discovery_targets(targets, 4);
        for (int i = 0; i < n; i++) {
            struct sockaddr_in to;
            memset(&to, 0, sizeof to);
            to.sin_family = AF_INET;
            to.sin_port = htons(port);
            to.sin_addr.s_addr = targets[i];
            sendto(udp, req, sizeof req, 0, (struct sockaddr *)&to, sizeof to);
        }
        disc_next = now + DISCOVER_RESEND_MS;
    }
    for (;;) {
        uint8_t ans[64];
        struct sockaddr_in from;
        socklen_t from_len = sizeof from;
        long r = recvfrom(udp, ans, sizeof ans, 0, (struct sockaddr *)&from, &from_len);
        if (r < 0) break;
        if (r != 32 || memcmp(ans, "DSDBGA1", 8) != 0) continue;
        // Check the signature: HMAC(key, "dsidbg-discover" nonce port)
        uint8_t msg[15 + 16 + 2], mac[20];
        memcpy(msg, "dsidbg-discover", 15);
        memcpy(msg + 15, disc_nonce, 16);
        memcpy(msg + 31, ans + 8, 2);
        dsidbg__hmac_sha1(key, key_len, msg, sizeof msg, mac);
        uint8_t diff = 0;
        for (int i = 0; i < 20; i++) diff |= mac[i] ^ ans[12 + i];
        if (diff) continue; // a daemon with another key
        cur_addr = from.sin_addr.s_addr;
        cur_port = (uint16_t)get16(ans + 8);
        discovered = 1;
        connect_fails = 0;
        stop_discovery();
        st = ST_DOWN;
        retry_at = now;
        dsidbg_log("dsidbg: found the computer at %s:%u by discovery", inet_ntoa(from.sin_addr), cur_port);
        return;
    }
    if ((int32_t)(now - disc_until) >= 0) {
        stop_discovery();
        st = ST_DOWN;
        snprintf(off_reason, sizeof off_reason, "no computer answered discovery");
        TRACE("discovery: no answer");
        if (cfg_addr) { cur_addr = cfg_addr; cur_port = port; discovered = 0; connect_fails = 0; }
        retry_at = now + 2000;
    }
}

static void service(void)
{
    if (st == ST_OFF) return;
    uint32_t now = dsidbg__plat_ms();
    if (!dsidbg__plat_net_poll()) {
        if (sock >= 0) drop_conn("wifi lost");
        if (udp >= 0) { stop_discovery(); st = ST_DOWN; }
        return;
    }
    switch (st) {
    case ST_DOWN:
        if ((int32_t)(now - retry_at) < 0) break;
        if (!cur_addr || connect_fails > 0) start_discovery();
        else start_connect();
        break;
    case ST_DISCOVER:
        poll_discovery(now);
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
        if (get_f) get_step();
        if (shot_px && st == ST_READY) shot_finish();
        if (st != ST_READY) break;
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

    dsidbg__plat_crash_install(cfg.flags);
    dsidbg__plat_set_watchdog(cfg.watchdog_seconds);
    inited = 1;

    if (dsidbg__plat_fs_init() == 0) load_config(cfg.config_path);
    if (cfg.host) snprintf(host, sizeof host, "%s", cfg.host);
    if (cfg.port) port = cfg.port;
    if (cfg.key) snprintf(key, sizeof key, "%s", cfg.key);
    if (cfg.ssid) snprintf(ssid, sizeof ssid, "%s", cfg.ssid);
    if (cfg.pass) snprintf(pass, sizeof pass, "%s", cfg.pass);
    if (!port) port = DEFAULT_PORT;
    key_len = strlen(key);

    if (!key_len) {
        snprintf(off_reason, sizeof off_reason, "no key: see %s", cfg.config_path);
        return -1;
    }
    unsigned kib = cfg.tx_queue_kib ? cfg.tx_queue_kib : TXQ_DEFAULT_KIB;
    for (txq_size = 4096; txq_size < kib * 1024u && txq_size < (1u << 24);) txq_size <<= 1;
    txq = malloc(txq_size);
    rxbuf = dma_alloc(RX_CAP);
    if (!txq || !rxbuf) {
        free(txq);
        free(rxbuf);
        txq = rxbuf = NULL;
        snprintf(off_reason, sizeof off_reason, "out of memory");
        return -1;
    }
    // host= may be empty or "auto": then the agent finds the computer by discovery.
    struct in_addr ha;
    if (host[0] && strcmp(host, "auto") != 0 && inet_pton(AF_INET, host, &ha) == 1) cfg_addr = ha.s_addr;
    cur_addr = cfg_addr;
    cur_port = port;
    if (dsidbg__plat_init(&cfg, ssid[0] ? ssid : NULL, pass) != 0) {
        snprintf(off_reason, sizeof off_reason, "wifi init failed");
        return -1;
    }
    st = ST_DOWN;
    retry_at = 0;
    last_status = dsidbg__plat_ms();
    return 0;
}

void dsidbg_heartbeat(void) { dsidbg__plat_heartbeat(); }

void dsidbg_frame(void)
{
    frame++;
    dsidbg__plat_heartbeat();
    int k = 0;
    for (int i = 0; i < ninj; i++)
        if (inj[i].end > frame) inj[k++] = inj[i];
    ninj = k;
    log_flush();
    service();
    dsidbg__plat_yield();
}

int dsidbg_connected(void) { return st == ST_READY; }

static const char *cur_text(void)
{
    static char buf[32];
    struct in_addr a;
    a.s_addr = cur_addr;
    if (!cur_addr) return "the computer";
    snprintf(buf, sizeof buf, "%s:%u", inet_ntoa(a), cur_port);
    return buf;
}

const char *dsidbg_status(void)
{
    switch (st) {
    case ST_OFF: snprintf(status_text, sizeof status_text, "off: %s", off_reason); break;
    case ST_DOWN:
        if (!dsidbg__plat_net_poll()) snprintf(status_text, sizeof status_text, "wifi: %s", dsidbg__plat_net_state());
        else snprintf(status_text, sizeof status_text, "waiting for %s (%.60s)", cur_text(), off_reason);
        break;
    case ST_DISCOVER: snprintf(status_text, sizeof status_text, "searching for the computer"); break;
    case ST_CONNECTING: snprintf(status_text, sizeof status_text, "connecting to %s", cur_text()); break;
    case ST_HANDSHAKE: snprintf(status_text, sizeof status_text, "authenticating"); break;
    case ST_READY: snprintf(status_text, sizeof status_text, "connected to %s", cur_text()); break;
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
    held = dsidbg_keys(dsidbg__plat_hw_keys());
    down = held & ~prev_held;
    up = prev_held & ~held;
    prev_held = held;
}

uint32_t dsidbg_keys_held(void) { return held; }
uint32_t dsidbg_keys_down(void) { return down; }
uint32_t dsidbg_keys_up(void) { return up; }

void dsidbg_capture_stdout(int echo) { dsidbg__plat_hook_stdout(echo ? 2 : 1); }

void dsidbg_reboot(const char *sd_path)
{
    char local[264];
    if (sd_path && local_path(sd_path, local, sizeof local) == 0) remember_launch(sd_path);
    else sd_path = NULL;
    log_flush();
    if (st == ST_READY) flush_tx(1);
    dsidbg__plat_reboot(sd_path);
}
