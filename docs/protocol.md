# dsidbg wire protocol (version 1)

The DSi connects over TCP to the daemon (default port 7710). Constants live in `src/internal.h` and
`host/dsidbg/proto.py`; keep the three in sync.

## Framing

Every message is an 8-byte little-endian header followed by the payload:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | u32 | payload length |
| 4 | u16 | message type |
| 6 | u16 | seq: set by the host on commands, echoed on their replies; 0 on unsolicited messages |

The device accepts payloads up to 12 KiB, so the host sends files in 8 KiB chunks.

## Discovery (UDP)

When the configured host doesn't answer, or none is configured, the device looks for the daemon on its local network.
It sends to the daemon's port number (UDP) on the limited broadcast address and on the subnet's directed broadcast
address, once a second for 5 seconds:

| Direction | Bytes |
| --- | --- |
| request (device, broadcast) | `"DSDBGQ1\0"`, nonce[16] |
| answer (daemon, to the sender) | `"DSDBGA1\0"`, u16 TCP port, u16 0, HMAC-SHA1(key, "dsidbg-discover" ‖ nonce ‖ port as u16 LE) |

The device ignores answers whose signature doesn't match its key, and connects to the first valid answer's source
address and TCP port. Without an answer it tries the configured host again.

## Handshake

Both sides share a key (`key=` in `sd:/dsidbg/config.ini`, `~/.dsidbg/key` on the host). Each side proves it knows
the key before the other trusts it:

1. Device → `HELLO` (0x0001): u16 version, u16 0, nonce_d[16], app name NUL, build id NUL.
2. Host → `CHALLENGE` (0x0002): nonce_h[16], HMAC-SHA1(key, "dsidbg-host" ‖ nonce_d ‖ nonce_h).
   The device checks it and disconnects on mismatch (retrying 10 s later).
3. Device → `PROOF` (0x0003): HMAC-SHA1(key, "dsidbg-dev" ‖ nonce_h ‖ nonce_d). The host checks it.
4. Device → `INFO`, then normal traffic.

## Host → device

Each command gets an `ACK` with the same seq unless noted.

| Type | Name | Payload | Reply |
| --- | --- | --- | --- |
| 0x0100 | PING | any | `PONG` with the same payload |
| 0x0101 | INPUT | n × {u32 delay, u16 frames, u16 flags, u32 keys, i16 x, i16 y} | ACK |
| 0x0102 | INPUT_CLEAR | – | ACK |
| 0x0103 | SHOT | u8 screen (0 top, 1 bottom) | `SHOT_DATA`, or ACK on error |
| 0x0104 | REBOOT | optional "sd:/path.nds" | ACK, then the device reboots |
| 0x0105 | PUT_BEGIN | u32 size, u32 crc32, path | ACK |
| 0x0106 | PUT_DATA | bytes | none |
| 0x0107 | PUT_END | – | ACK (checks size and CRC, then renames `.part` into place) |
| 0x0108 | GET | path | `FILE_DATA`…, `FILE_END`, or ACK on error |
| 0x0109 | LIST | path | ACK, text `D\|F <tab> size <tab> name` lines |
| 0x010A | DELETE | path (files only) | ACK |
| 0x010B | PROF_START | – | ACK |
| 0x010C | PROF_STOP | – | remaining `PROF` messages, then ACK |
| 0x010D | APP_CMD | text | ACK carrying the app's reply |
| 0x010E | WATCHDOG | u32 seconds | ACK |
| 0x010F | PUT_SYNC | – | ACK after writing the data received so far to the card; text: bytes received |

Uploads: `PUT_BEGIN`, then the file in windows of 32 KiB (`PUT_DATA` messages of up to 8 KiB), each window
followed by `PUT_SYNC`; the host waits for its ACK before sending the next window, then sends `PUT_END`. While the
DSi writes a window to the card, no data is in flight: on the DSi the ARM7 runs both the SD card and Wi-Fi, and an SD
write during a stream of Wi-Fi packets has hung it.

Input records: frames are counted in `dsidbg_frame()` calls. A record is active from `now + delay` for `frames`
frames. Flag 1 (`REPLACE`) hides the real buttons while it is active. Key bits are libnds' `KEY_*`; `KEY_TOUCH`
(bit 12) with x, y injects a touch.

Paths must start with `sd:/` and must not contain `..`; anything else is refused.

## Device → host

| Type | Name | Payload |
| --- | --- | --- |
| 0x0200 | PONG | the PING payload |
| 0x0201 | ACK | i32 status (negative = error), text |
| 0x0202 | LOG | text (a line, or part of one) |
| 0x0203 | INFO | `key=value` lines: lib, proto, app, build, ip, capture_bank, watchdog, prof_hz, mode |
| 0x0204 | STATUS | `key=value` lines, about once a second: frame, fps, uptime_ms, log_drops, prof, panic |
| 0x0205 | SHOT_DATA | u8 screen, u8 format (0 = RGB555), u16 width, u16 height, u16 0, pixels |
| 0x0206 | FILE_DATA | bytes |
| 0x0207 | FILE_END | i32 status, u32 size, u32 crc32 |
| 0x0208 | PROF | u32 sample rate (Hz), u32 pc[] |
| 0x0209 | CRASH | 128-byte crash header, then stack bytes |
| 0x020A | DATA | u16 channel, u16 0, bytes |

Crash header (`dsidbg_crash`):

| Offset | Field |
| --- | --- |
| 0 | u32 kind: 1 data abort, 2 undefined instruction, 3 hang (watchdog), 4 panic |
| 4 | u32 r0..r15 (r15 = faulting or interrupted instruction) |
| 68 | u32 cpsr of the crashed code (bit 5 = Thumb) |
| 72 | u32 fault address (data aborts: the address accessed) |
| 76 | u32 frame |
| 80 | u32 stack base (r13 at the crash) |
| 84 | u32 stack length (bytes that follow the header, up to 1024) |
| 88 | u32 flags: bit 0 = sent from a dump saved on the SD card |
| 92 | char message[36] |
