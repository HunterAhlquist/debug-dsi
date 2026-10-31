# Test plan

What has been verified so far, what is known to be wrong or limited, and what still needs testing before dsidbg is
announced anywhere beyond its GitHub repository. Update the "Verified" tables when a test passes, with the date, the
setup and the commit.

Reference setup used so far:

- **Console:** DSi XL with hiyaCFW and Unlaunch. TWiLight Menu++ is Unlaunch's default, set to start the last played
  ROM (the loader).
- **Network:** home Wi-Fi (security type not recorded), saved in the DSi's own settings.
- **Computer:** macOS on Apple silicon, Python 3.9, on the same network.
- **Emulator:** melonDS development build (commit 1e654443, October 2026), DSi mode, real DSi BIOS, `melonAP`.

## Hardware test runner

`tests/hardware/run.py` runs the scriptable hardware tests against a DSi connected to the daemon (the loader is
enough). It copies this build's demo to the card, reboots the DSi several times, and writes
`build/hwtest/<time>/report.md` and `report.json` with screenshots. Run it after every change to the agent:

```bash
python3 tests/hardware/run.py                      # quick: about 1.5 minutes
python3 tests/hardware/run.py --suite full         # about 15 to 20 minutes
python3 tests/hardware/run.py --suite soak --soak-minutes 480
python3 tests/hardware/run.py --suite discovery    # changes the card's config.ini, then restores it
python3 tests/hardware/run.py --suite stress --rounds 10   # 16 MiB uploads in a row, about 3 minutes each
python3 tests/hardware/run.py --list
```

| Suite | Tests |
| --- | --- |
| quick | setup, ping, screenshot, input, commands (and data channel), files (14 sizes across chunk and sector boundaries, up to 1 MiB), paths (`sd:/` only), profiler, crash_abort, crash_undef, hang, reboot, churn (3 daemon restarts during uploads), transfer_fps (the app keeps at least 30 fps during a 1 MiB upload and download) |
| full | quick, but 20 restarts instead of 3, plus files_large (16 MiB), fuzz (13 malformed messages, 200 random ones, an oversized one), log_flood (a line every frame for 10 s) |
| soak | stays connected for `--soak-minutes`: status every second, screenshot and ping every minute; checks heap growth and disconnects |
| discovery | puts an unreachable address in the card's config, checks that the DSi finds the computer, restores the config |
| stress | `--rounds` uploads of 16 MiB in a row (the ARM7 hang below) |

Still manual: anything needing the power button, the router or another console (interrupted uploads, Wi-Fi loss,
other networks), the host apps on Windows and Linux, and the FF9 integration.

## 1. Verified

### Automated (`make test`, 27 tests)

These run against the simulator (`sim/`), which runs the same `agent.c` as the DSi. They don't touch hardware, the
dswifi stack, the DSi platform code (`platform_nds.c`, `irq_nds.s`) or the GUI.

| Area | Tests |
| --- | --- |
| Connection, handshake, status | `test_01`, `test_02` (wrong key rejected), `test_14` |
| Input, screenshot, files, `sd:/`-only paths | `test_03` to `test_06` |
| App commands, data channel, profiler | `test_07`, `test_08` |
| Crash report, reboot from panic, hang watchdog, saved crash upload | `test_09` to `test_11` |
| Reconnects: boot logs kept, silent device dropped | `test_12`, `test_13` |
| Discovery: no host, stale host, other daemon's key ignored | `test_15` to `test_17` |
| MCP server session and errors | `test_mcp_session`, `test_mcp_errors` |
| MCP installer (fake `claude`/`codex`, temporary home) | 4 `Install` tests |
| SD setup (config merge, card layout), stale daemon replaced | `SdSetup`, `StaleDaemon` |
| Symbolization (needs the DSi build) | `test_lookup_and_crash_text` |

They pass on macOS (Python 3.9) and on Linux (Python 3.12, in a container).

### On hardware (DSi XL), 2026-10-30 to 2026-10-31

| Feature | Result | Notes |
| --- | --- | --- |
| Wi-Fi in DSi mode from the console's saved connections | pass | ping 19 to 40 ms |
| Handshake with the shared key | pass | |
| Screenshot, 2D (loader, bank B) and 3D (demo, bank D) | pass | the app pauses about 0.2 s per screenshot |
| Button and touch injection, checked through the demo's log | pass | |
| Logs, captured `printf`, app commands, data channel | pass | |
| File upload and download | pass | three 1 MiB round trips identical after the DMA alignment fix (c423acc). With 32 KiB upload windows: up 77 to 97 KiB/s, down about 235 KiB/s |
| 16 MiB uploads in a row (stress suite) | pass | 10 of 10 with upload windows; before them, 3 of 4 hung (ARM7) |
| App frame rate during transfers (transfer_fps) | pass | demo at 56.8 fps uploading, 53.9 fps downloading (was 18.8 downloading) |
| Hang screen with every thread waiting (`waithang`) | pass | shows the main thread's wait point (main.c:89), "arm7 0.0s"; nothing written to the card |
| Full hardware suite | pass | 17 of 17 on 2026-10-31, after clearing a `.part` left by an earlier hang |
| `push --launch` and `launch` through Unlaunch | pass | connected about 6 s after the reboot |
| `reboot` to Unlaunch's default (loader through TWiLight) | pass | |
| Profiler, symbolized with the ELF | pass | |
| Data abort and undefined instruction reports, relaunch from the crash screen | pass | |
| Hang watchdog report, relaunch from the hang screen | pass | after the IRQ stack overflow fix, in the first commit (4a71b01) |
| Discovery with a stale address on the card | pass | found the computer, connected about 6 s after boot |
| Daemon restarted during an upload | pass | 15 times, including two daemons racing |
| Template-built app (BlocksDS `rom_arm9_only` + README lines) | pass | DSi mode, connected, screenshot, command |
| MCP tools from Claude Code (status, screenshot, launch, press, touch, logs, profile, crash) | pass | |
| Overhead | measured | about 1% CPU idle; 13 KiB static, about 80 to 90 KiB heap with dswifi |

### Host apps (macOS only)

| Feature | Result |
| --- | --- |
| Menu bar app: icon, live status, Install MCP message, folder picker | pass (Open Terminal and Quit not clicked) |
| Tray restarts itself after a code update | pass |
| Packaged `dsidbg.app`: CLI, SD setup with the bundled loader, MCP server starting its daemon, tray opened from Finder, zip keeps the code signature | pass |
| `mcp-install` into Claude Code, Codex and Claude Desktop | pass (Claude Desktop must be closed, see below) |

## 2. Known issues and limits

- **Fixed 2026-10-31, ARM7 hang during large uploads:** 16 MiB uploads hung the DSi three times out of four, after
  7 to 12 MiB (red hang screen, nothing sent). The hang screen, extended for this (main thread's wait point, ARM7
  silence, backtrace), showed: `put_flush` → `write` → `f_write` → `disk_write` → `fifoWaitValue32Async`, the
  ARM7 silent for 15 s, and words left in the ARM9's send FIFO. On the DSi the ARM7 does every SD access (inside its
  FIFO interrupt handler) and runs Wi-Fi; the ARM9 sends it a FIFO message per transmitted packet. An SD write while
  upload packets streamed in stopped the ARM7 for good (the exact failure is inside libnds' ARM7 code, not
  identified). Fix: uploads go in 32 KiB windows (`PUT_SYNC`), and nothing is in flight while the card is written.
  Older agents without `PUT_SYNC` still receive uploads, without windows. Earlier in the day the same hang had been
  taken for a false watchdog report; the upload heartbeat added then is still useful.
- **Fixed 2026-10-31, downloads paused the app:** a 16 MiB download stopped the app for about a minute. Downloads
  are now queued only as fast as the network sends them (the demo stays above 53 fps).
- **Unexplained freeze (probably the ARM7 hang above):** the DSi froze once, without a crash screen, while two
  daemons were being started and stopped during uploads. Not reproduced since; it matches the ARM7 hang (uploads,
  the ARM7 stopping while the ARM9 waits for it). If it happens again, photograph both screens.
- **melonDS crash:** the melonDS development build crashed (in its ARM7 emulation) when the daemon restarted during
  an upload. Possibly an emulator bug; the console didn't do this in the 15 hardware runs.
- **Undetectable hangs:** a hang with interrupts disabled can't be detected; the power button is the fallback.
- **melonDS 1.1** (release) can't run the agent (SD writes, DSi Wi-Fi). Use a development build.
- **Claude Desktop** rewrites its config from memory, so the installer skips it while Claude is running.
- **Blocking commands:** screenshots (about 0.2 s) and the profiler's final flush pause the app. File transfers
  no longer do (see 2026-10-31 below).
- **Crash reports from other threads:** a crash or hang that stops a thread other than the main one (or the main
  thread inside `cothread_yield_irq()`) leaves the agent no working thread for the network. The dump stays on
  screen with "not sendable: press power". Saving it to the card is opt-in (`DSIDBG_SAVE_CRASHES`), because the
  crash may have interrupted a file operation.
- **Discovery** only works within one network segment, and not through melonDS's network emulation.

## 3. To test

Each test lists its steps and what counts as a pass. "Demo" means `dsidbg-demo.nds` from the same build.

### 3.1 Release pipeline

| Test | Steps | Pass |
| --- | --- | --- |
| Workflow, manual run | Push the repo, run the Release workflow manually | all jobs green; artifacts contain the three apps, `dsidbg-lib.zip`, loader and demo |
| Workflow, tag | Push a test tag on a fork or a private repo | release created with all files; a tag that doesn't match `DSIDBG_VERSION` fails the test job |
| Library zip | Unzip `dsidbg-lib.zip` into `$BLOCKSDSEXT`, build a template app with the README lines | builds; runs on the DSi as in section 1 |

### 3.2 Windows (packaged app)

| Test | Steps | Pass |
| --- | --- | --- |
| First start | Unzip, open `dsidbg.exe` | SmartScreen can be passed; tray icon appears; no console window stays open |
| Status | Turn on a configured DSi | icon turns green; menu shows the DSi's and the daemon's addresses |
| Set Up SD Card | Choose a card's root | `dsidbg\config.ini` and the loader written; Wi-Fi lines kept |
| Install MCP | Click it with Claude Code / Codex installed | each harness reported; the tools work in a new session |
| Open Terminal | Click it | Windows Terminal or cmd opens with `dsidbg status` working |
| CLI and MCP | `dsidbg.exe status`, the MCP server through Claude Code | work; nothing written outside `%USERPROFILE%\.dsidbg` |
| Firewall | First connection from the DSi | Windows Firewall prompt; DSi connects after allowing |
| Quit | Click it | daemon stops; icon disappears |

### 3.3 Linux (packaged app)

Same table as Windows, on at least one X11 desktop with a system tray (for example Xfce or KDE). Also: GNOME with an
AppIndicator extension, and a desktop without zenity/kdialog (dialogs should fail with a readable message, not a
crash). Check the app on an older distribution (glibc of Ubuntu 22.04 or older).

### 3.4 macOS

| Test | Steps | Pass |
| --- | --- | --- |
| Downloaded app | Download the release zip with a browser, open the app | Gatekeeper's right-click > Open works; Install MCP refuses until the app is in Applications |
| Intel Mac | Run the app on an Intel Mac | it doesn't run (the build is Apple silicon only): either add an Intel build or document it |
| Open Terminal, Quit | Click them | Terminal opens with `dsidbg status` working (macOS may ask to allow control of Terminal); Quit stops the daemon |

### 3.5 Consoles and networks

| Test | Pass |
| --- | --- |
| Another DSi (not XL), and a DSi with a different launcher setup (no TWiLight) | everything in section 1 works; reboot behaviour documented for each launcher |
| 3DS running the app in DSi mode (TWiLight) | document what works: Wi-Fi, SD, reboot (there is no Unlaunch on 3DS) |
| DS / DS Lite (DS mode) | Wi-Fi only on open/WEP networks; reboot unsupported: the agent must fail cleanly, not crash |
| Networks: WPA2, WPA2/WPA3 mixed, open; a router with client isolation; DSi and computer on different subnets | connects where the DSi supports the network; isolation and different subnets give a clear "waiting" status (discovery can't cross them) |
| Two computers running daemons with different keys on one network | the DSi connects only to the one with its key |

### 3.6 Robustness on hardware

| Test | Steps | Pass |
| --- | --- | --- |
| Soak | Leave the demo connected for 8 hours with status every second, a screenshot every minute | still connected; heap (`dsidbg status`) doesn't grow; no log drops while connected |
| Large upload | Push a 16 MiB and a 32 MiB file | identical on download; speed stable |
| Interrupted upload | Turn the DSi off during an upload, then on | no `.part` file left that breaks the next upload; card still mounts on the computer |
| Card nearly full | Upload more than the free space | clear error; no corrupted files |
| Wi-Fi loss | Turn the router off for 1 minute during a session, then on | reconnects without a reboot |
| Computer address change | Change the computer's IP while the DSi runs | discovery finds it within one retry cycle |
| Daemon churn | 50 daemon restarts during uploads (scriptable) | reconnects every time; no freeze (see the open issue) |
| Log flood | App logs every frame for 10 minutes | app keeps 60 fps; drops counted, no crash |
| Watchdog false positives | App that blocks 10 s while loading without `dsidbg_heartbeat()` | reported as a hang (expected); with `dsidbg_heartbeat()` it isn't |

### 3.7 Crashes and hangs

| Case | Pass |
| --- | --- |
| Data abort in ARM code and in Thumb code | report with the right file:line |
| Prefetch abort (jump to an invalid address) | report; address explained in the docs if it's off by an instruction |
| Crash inside an interrupt handler | report or documented limitation |
| Crash inside dswifi/lwIP | report or documented limitation (network may be gone: dump saved on SD, uploaded on the next connection) |
| Stack overflow | report or documented limitation |
| Hang with interrupts disabled | documented limitation (power button) |
| App with its own exception handler (`DSIDBG_NO_CRASH_HANDLER`) | agent leaves it alone |

### 3.8 Host side

| Test | Pass |
| --- | --- |
| CLI, MCP server and tray used at the same time | no interference; commands serialize |
| MCP in Codex (CLI and desktop) and Claude Desktop, not only Claude Code | tools listed and working |
| Protocol fuzzing: malformed messages to the daemon and to the DSi agent (simulator first, then hardware) | no crashes; connection dropped with a reason |
| Upgrade: install a new version over an old one (repo and packaged app) | daemon replaced; DSi reconnects |

### 3.9 Integration in a real app (FF9 port)

| Test | Pass |
| --- | --- |
| Agent linked into ff9-ds, initialized in dev builds only | release build without the agent unchanged in size and behaviour |
| Screenshot with a VRAM bank FF9 can spare (or a screenshot hook) | correct image, no texture corruption |
| Profiler during battle and field scenes | symbolized hotspots plausible against the existing on-screen timings |
| Watchdog during disc loading | no false hang reports (heartbeats where needed) |
| Crash in FF9 code | report points at the right decomp function |

## 4. Before announcing beyond GitHub

- Sections 3.1 to 3.4 pass on all three platforms, or untested platforms are clearly marked as such.
- Section 3.5 run on at least two different consoles and two networks.
- Section 3.6 soak, large upload, interrupted upload and daemon churn pass.
- The freeze in section 2 is either explained and fixed, or not reproduced in the daemon churn test.
- Every known limitation is in the README.
- Tested BlocksDS version stated in the release notes.
