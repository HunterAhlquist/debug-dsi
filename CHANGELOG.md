# Changelog

Versions follow semantic versioning. The library version is `DSIDBG_VERSION` (include/dsidbg.h and
host/dsidbg/__init__.py); the wire protocol has its own `PROTO_VERSION`, and the daemon and the DSi agent must use the
same one. `dsidbg_config` may gain fields: always start from `dsidbg_config_defaults()` and rebuild the app when the
library changes (it is a static library).

## 0.1.3 (2026-11-01)

- A screenshot hook can return `DSIDBG_SHOT_PENDING` when the image needs frames the app hasn't drawn yet: the agent
  calls it again from later `dsidbg_frame()` calls, for up to 3 seconds. ff9-ds uses it to free a texture bank
  for the capture in its GX renderer. Any other non-zero value still falls back to `capture_bank`.
- `push --elf`, `prof --elf` and `crash --elf` take paths relative to the current folder (they were looked up in
  the daemon's folder).

## 0.1.2 (2026-11-01)

- The library's internal functions are named `dsidbg__plat_*` instead of `plat_*`, which clashed with an app's own
  functions of the same name (found linking it into ff9-ds). The build now fails if the library defines a global
  symbol without the `dsidbg` prefix.
- Two installs (the app and a repo checkout, say) no longer replace each other's daemon in turn: a daemon is only
  replaced by newer code of the same install, or by a newer version. The daemon's status reports its version and
  install.
- Crash reports get file:line again when the daemon was started by the app (from Finder or at login): addr2line is
  also looked for in the usual toolchain folders, not only on PATH.

## 0.1.1 (2026-11-01)

- The macOS app is signed with a Developer ID and notarized by Apple in the release workflow, so it opens without a
  Gatekeeper warning.
- Release archives hold only the app: the macOS zip unzips to `dsidbg.app`, the Windows and Linux archives to the
  `dsidbg` folder. The loader is inside the app ("Set Up SD Card..." copies it); releases still attach
  `dsidbg-loader.nds` on its own.

## 0.1.0 (2026-10-31)

First version.

- DSi agent library: Wi-Fi connection with an HMAC handshake, discovery of the computer when its address changes,
  input injection, screenshots, logs and stdout, app commands and data channels, SD card file transfer, reboot into a
  file through Unlaunch, a 1 kHz sampling profiler, crash and hang reports with recovery from the crash screen.
- Loader (recovery app) and demo.
- Daemon and command line, MCP server with an installer for Claude Code, Codex and Claude Desktop, menu bar / tray
  app, packaged apps for macOS, Windows and Linux.
- Only `sd:/` paths; NAND is never mounted and write protection is turned on.
- Protocol version 1.
