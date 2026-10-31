# debug-dsi

Remote debugging for DSi homebrew on real hardware, over Wi-Fi. An agent library inside your app connects to a
daemon on your computer. From the command line (or an AI agent) you can then:

- press buttons and touch the screen, timed to the frame
- take screenshots (PNG)
- read the app's logs and `printf` output
- run a sampling profiler (1 kHz, symbolized with the app's ELF)
- copy files to and from the SD card, push a new build and reboot into it
- get crash reports (data abort, undefined instruction, hang watchdog, panics) with registers, a symbolized
  backtrace guess and the stack. A crash leaves the agent running, so you can push a fix and relaunch from the
  crash screen.

It is built with [BlocksDS](https://blocksds.skylyrac.net/) and targets DSi mode (DSi Wi-Fi supports WPA2).

> Status: 0.1.0, tested on a DSi XL (hiyaCFW + Unlaunch, home Wi-Fi) and in a melonDS development build:
> Wi-Fi, input, screenshots, logs, file transfer (about 150 KiB/s), pushing and booting a build through Unlaunch,
> the profiler, and crash and hang reports with recovery from the crash screen. The daemon, CLI and protocol also
> have end-to-end tests against a simulator that runs the same agent code (`make test`). The daemon serves one
> device at a time: the newest connection replaces the previous one.

## Safety

- **The agent only touches the SD card.** Every device path must start with `sd:/` (no `..`); the CLI and the
  agent both refuse anything else. The agent never mounts NAND, and it turns NAND write protection on at start-up.
- **Reboots** use libnds' `systemReboot()` (the same power-management reset the DSi menu uses). To boot a specific
  file, the agent first writes Unlaunch's "AutoLoadInfo" block to RAM at 0x02000800, as TWiLight Menu++ does.
  Nothing is written to NAND or firmware.
- **Network:** the DSi connects out to your computer. Both sides prove they know a shared key (HMAC-SHA1
  challenge-response) before any command runs. Traffic is not encrypted: use it on your own network only.

## Downloads

Each [release](../../releases) has:

| File | Contents |
| --- | --- |
| `dsidbg-macos.zip` | `dsidbg.app` (menu bar app, CLI, daemon, MCP server; Apple silicon) and the loader |
| `dsidbg-windows.zip` | `dsidbg\dsidbg.exe` (tray app when opened, CLI from a terminal) and the loader |
| `dsidbg-linux.tar.gz` | `dsidbg/dsidbg` (tray app when run without arguments, CLI otherwise) and the loader |
| `dsidbg-loader.nds` | the loader on its own |
| `dsidbg-nds.zip` | loader, demo, `libdsidbg.a`, `dsidbg.h` and ELFs, for adding the agent to your own app |

The apps include Python and the tray's packages, and "Set Up SD Card..." copies their bundled loader. They aren't
signed: on macOS, right-click the app and choose Open the first time; on Windows, choose More info > Run anyway.
Move `dsidbg.app` to Applications before using "Install MCP", since the MCP configuration points at the app.

## Setup

Requirements: a DSi with Unlaunch and an SD card, Docker (for the BlocksDS toolchain), Python 3.

1. Build: `tools/docker.sh make nds`. This produces `build/dsidbg-loader.nds` and `build/dsidbg-demo.nds`.
2. Start the daemon: `bin/dsidbg serve`. It listens on TCP port 7710; allow incoming connections for Python if the
   macOS firewall asks.
3. Write the DSi's settings: put the SD card in your computer and run `bin/dsidbg setup --sd /Volumes/YOURCARD`.
   This writes `sd:/dsidbg/config.ini` (your computer's address, port and key) and copies the loader to
   `sd:/dsidbg/dsidbg-loader.nds`. Without `--sd` it prints the file for you to copy. If the loader starts without
   a `config.ini`, it writes a blank one (empty `host` and `key`) and says so on the top screen.
   If this computer's address changes later, the DSi finds it anyway: when the configured address doesn't answer
   (or `host=` is empty or `auto`), it broadcasts a discovery request on the local network and connects to the
   daemon that answers with a reply signed with its key. That costs a few seconds per boot; set up the card again to
   make it fast. Discovery needs the DSi and computer on the same network segment (it doesn't work through
   melonDS's network emulation).
4. Wi-Fi: by default the DSi uses its saved connections (System Settings > Internet; WPA2 networks go in the
   advanced slots 4-6). Or pass `--ssid NAME --pass PASSWORD` to `setup`.
5. Make the loader start after every reboot, so a crash never needs you to touch the console. Two options:
   - **SD only (recommended):** in TWiLight Menu++ settings, turn on "Last played ROM on startup", then launch
     `dsidbg-loader.nds` once from TWiLight. From then on a reboot goes Unlaunch > TWiLight > loader. Hold B while
     booting to get the TWiLight menu instead. This setting lives on the SD card.
   - **Unlaunch default:** set the loader as Unlaunch's no-button boot file. Note that Unlaunch stores its own
     options on NAND when you change them.
6. Check: turn the DSi on. The loader shows `connected to ...` and `bin/dsidbg status` shows `dsidbg-loader`.

Try the demo: `bin/dsidbg push build/dsidbg-demo.nds --launch --wait 30`, then `bin/dsidbg shot`.

### Emulators

melonDS 1.1 (the latest release, November 2025) can't run the agent:

- **SD writes:** single-sector writes from BlocksDS's SD driver never complete. The first file the loader creates
  hangs (it shows "Checking the settings file..." and stops). Fixed upstream in melonDS PR 2438 (January 2026).
- **DSi-mode Wi-Fi:** dswifi can't start the Wi-Fi chip. The agent then stays at "starting wifi" while the app keeps
  running. Fixed upstream in melonDS PR 2596 (March 2026).

A melonDS development build works (tested with the macOS build of commit 1e654443, October 2026). Settings that
matter:

- **Keep it separate from a release install:** put a folder named `portable` next to `melonDS.app` and it keeps its
  settings (and can point at its own SD card image) there.
- **Use your real DSi BIOS dumps:** set `ExternalBIOSEnable = true` under `[DSi]`. With the built-in BIOS, every
  CPU exception hangs inside the BIOS, so crash reports never arrive.
- **Wi-Fi:** melonDS's access point is the open network `melonAP`. Put `ssid=melonAP` and an empty `pass=` in
  `config.ini` unless your firmware's saved connections include it. The DSi then reaches your computer through
  melonDS's network emulation.
- **Reboots:** with direct boot, a reboot restarts the loaded ROM. With a full NAND boot and no Unlaunch in the
  NAND, it goes to the DSi Menu. Booting a specific file after a reboot needs Unlaunch, so test that on hardware.

## Command line

`bin/dsidbg -h` lists everything. Most device commands take `--timeout N` to wait for a device to connect first.

| Command | What it does |
| --- | --- |
| `serve`, `stop` | start / stop the daemon (state in `~/.dsidbg`, or `$DSIDBG_HOME`) |
| `setup [--sd DIR] [--ssid S --pass P]` | write the DSi's `config.ini` |
| `status`, `wait [--app NAME]`, `ping` | connection state, wait for a device, round trip |
| `press SPEC...` | buttons: `A`, `A+B+10` (10 frames), `START@60+5` (in 60 frames, for 5); `--replace` hides real input |
| `touch X Y`, `release` | touch the bottom screen; cancel pending presses |
| `shot [-o FILE] [--bottom]` | screenshot to PNG |
| `push FILE [--to sd:/...] [--launch] [--wait S] [--elf ELF]` | copy a build to the SD card and boot it |
| `launch sd:/x.nds`, `reboot` | boot a file / boot Unlaunch's default |
| `get`, `ls`, `rm` | SD card files |
| `log [-f] [--for S]` | device logs (also saved in `~/.dsidbg/logs/`) |
| `crash [--elf ELF] [--wait S]` | last crash report, symbolized |
| `prof run SECONDS [--elf ELF]`, `prof start`/`stop` | sampling profiler, top functions |
| `cmd TEXT` | app-defined command |
| `data CHANNEL` | app-defined binary data sent with `dsidbg_send()` |
| `watchdog SECONDS` | change the hang watchdog (0 = off) |
| `elf APP PATH` | remember an app's ELF so crashes and profiles are symbolized automatically |

## Menu bar / system tray app

`bin/dsidbg tray` runs the daemon behind a menu bar (macOS) or system tray (Windows, Linux) icon. The icon's light
shows the state: green with a DSi connected, orange waiting for one, red when the daemon isn't running. The menu has:

- **Status:** the connected DSi's address and app, and the daemon's address and port (not clickable).
- **Set Up SD Card...:** asks for the card's root folder, then writes `dsidbg/config.ini` (this computer's address
  and key, keeping Wi-Fi settings already on the card) and copies the loader.
- **Install MCP:** registers the MCP server with Claude Code, Codex and Claude Desktop, and says for each whether it
  was installed, already installed, or not found.
- **Open Terminal:** a terminal in this repo with `dsidbg` on the PATH.
- **Quit:** stops the daemon and the icon.

The tray needs pystray and Pillow (plus PyObjC on macOS). `bin/dsidbg tray --setup` installs them once into a private
environment, `~/.dsidbg/venv`; the rest of dsidbg keeps using only the standard library. `--foreground` keeps it
attached to the terminal. With a menu bar manager (Bartender, Ice, ...), the icon may start out hidden behind it.

## MCP tools for LLM agents

`bin/dsidbg mcp` is an MCP server (stdio, Python standard library only) that gives an LLM agent the same control as
the command line, as tools: `status`, `wait_for_device`, `press`, `touch`, `release_input`, `screenshot` (returned
as an image), `read_log` (only new lines by default), `push_file` (optionally launching it), `launch`, `reboot`,
`list_files`, `get_file`, `delete_file`, `app_command`, `profile`, `last_crash`, `set_watchdog` and `read_data`. It
starts the daemon if it isn't running, and refuses device paths outside `sd:/` like the CLI does. The daemon keeps
running across updates of this repo; the MCP server and `dsidbg serve` replace one that was started from older code
(the DSi reconnects by itself).

Register it with the harnesses on this computer:

```bash
bin/dsidbg mcp-install
```

| Harness | How |
| --- | --- |
| Claude Code (CLI, and the desktop app's Code tab) | `claude mcp add -s user dsidbg -- <repo>/bin/dsidbg mcp` |
| Codex (CLI, IDE extension and desktop app share `~/.codex/config.toml`) | `codex mcp add dsidbg -- <repo>/bin/dsidbg mcp` |
| Claude Desktop (chat) | adds `mcpServers.dsidbg` to `claude_desktop_config.json`, keeping a `.bak` copy |

Options: `--target claude-code|codex|claude-desktop` (default: every one found), `--scope user|project|local`
(Claude Code), `--skip-existing`, `--remove`, `--dry-run`. Claude Desktop rewrites its config from memory, so the
installer leaves it alone while it is running: quit Claude (the Code tab is part of the same app), then install, for
example from the tray icon. Running sessions need a restart to see the tools; Claude Desktop needs to be
quit and reopened. Other MCP clients can run `<repo>/bin/dsidbg mcp` as a stdio server.

## Using the library in your app

Link `build/nds/libdsidbg.a` with dswifi, use an ARM7 core with Wi-Fi (`arm7_dswifi.elf` or
`arm7_dswifi_maxmod.elf`), and add `include/` to the include path:

```c
#include <dsidbg.h>

dsidbg_config cfg;
dsidbg_config_defaults(&cfg);
cfg.app_name = "mygame";
cfg.build_id = GIT_HASH;
cfg.capture_bank = 3;          // VRAM bank D may be overwritten by screenshots
cfg.command = my_command;      // optional: `dsidbg cmd ...`
dsidbg_init(&cfg);             // after fatInitDefault(); reads sd:/dsidbg/config.ini
dsidbg_capture_stdout(1);      // optional: printf to the daemon (1 = also to the console)

while (1) {
    dsidbg_scan_keys();        // instead of scanKeys(); then dsidbg_keys_held()/down()/up()
    ...
    dsidbg_frame();            // once per frame: network, injected input, watchdog
    swiWaitForVBlank();
}
```

Libraries: `-Wl,--start-group -ldsidbg -ldswifi9 -lnds9 -lc -Wl,--end-group` (see the `Makefile`).

Things to know:

- **Call `dsidbg_frame()` every frame.** dswifi runs in a cooperative thread, so the network only moves when the
  app yields. The agent yields there. During long loads that don't call it, call `dsidbg_heartbeat()` or raise
  the watchdog (default 15 s).
- **Apps that read keys their own way** can call `dsidbg_keys(held)` and `dsidbg_touch(&x, &y)` instead of
  `dsidbg_scan_keys()`.
- **Screenshots:**
  - The top screen comes from display capture of the main engine into `capture_bank`. That bank must hold
    nothing the frame needs (capture writes 96 KiB into it).
  - A `screenshot` hook in the config can provide the image instead, and is the only way to capture the bottom
    screen.
- **Resources** (measured on a DSi XL with the demo connected):
  - About 14 KiB of code and 13 KiB of static memory; 8 KiB of that is a stack reserved for crash reports.
  - `dsidbg_init()` allocates a 16 KiB send queue (`tx_queue_kib`) and a 12 KiB receive buffer. The heap in use
    with the agent and dswifi running is about 80-90 KiB (`dsidbg status` shows it).
  - Uploads and the profiler borrow 32 KiB and 16 KiB while they run. Screenshots borrow 96 KiB.
  - CPU: about 1% of the ARM9 while connected and idle.
  - An app that never calls `dsidbg_init()` pays only the static part.
  - dswifi uses ARM9 timer 3. The agent itself uses no timers: the profiler samples on dswifi's 1 kHz timer
    interrupt.
- **Blocking commands:** screenshots (about 0.2 s), file transfers and the profiler's final flush run inside
  `dsidbg_frame()`, so the app pauses for them. Everything else is queued and sent without blocking.
- **Tracing:** `DSIDBG_TRACE` logs the agent's own connection and transfer steps, and `DSIDBG_ECHO_LOG` also prints
  every log line on the libnds console. If a freeze stops the network, the last line on screen shows where the
  agent was.
- **IRQ hook:** the agent installs a small hook in front of libnds' interrupt dispatcher. It records the
  interrupted address (profiler, watchdog) and counts VBlanks. `DSIDBG_NO_IRQ_HOOK` turns it off, and the
  profiler and watchdog with it. `DSIDBG_NO_CRASH_HANDLER` keeps your own exception handler.
- **Crashes:**
  - The agent shows the dump on the bottom screen and sends it if the network still works.
  - If it can't send within 8 s, it saves the dump to `sd:/dsidbg/crash/` and the next connection uploads it.
  - A hang with interrupts disabled can't be detected. Press the power button and the loader comes back up.

## Layout

```
include/dsidbg.h     public API
src/agent.c          portable agent: connection, commands, logs, input, profiler buffer, crash reports
src/platform_nds.c   DSi: dswifi, SD card, display capture, reboot, exception handler, IRQ hook
src/irq_nds.s        IRQ entry hook and panic mode switch
src/platform_host.c  host platform for the simulator
loader/              recovery/updater app (boot target after reboots)
demo/                demo app exercising every feature
sim/                 simulator: the agent on the host, for tests
host/dsidbg/         daemon, command line, MCP server, installer and tray app (Python 3; the tray also uses pystray)
tests/               end-to-end tests (make test)
docs/protocol.md     wire protocol
```

## Releases

`.github/workflows/release.yml` builds the DSi files in the BlocksDS container, runs the tests, and packages the app
on macOS, Windows and Linux with PyInstaller (`packaging/dsidbg.spec`, `packaging/package.py`). Pushing a tag
`vX.Y.Z` that matches `DSIDBG_VERSION` (in `include/dsidbg.h` and `host/dsidbg/__init__.py`) publishes a release; a
manual run keeps the files as workflow artifacts.

To build the app locally (after `tools/docker.sh make nds`):

```bash
python3 -m pip install pyinstaller pystray pillow
python3 -m PyInstaller --noconfirm --distpath dist --workpath build/pyinstaller packaging/dsidbg.spec
python3 packaging/package.py macos
```

## Development

- `tools/docker.sh make nds` builds the DSi targets in the BlocksDS container.
- `make test` builds the simulator with the host compiler and runs the tests.

The Python code needs only the standard library (3.9+). For file:line in crash reports, install
`arm-none-eabi-addr2line` on the host.

License: MIT.
