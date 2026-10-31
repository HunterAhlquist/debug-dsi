# Changelog

Versions follow semantic versioning. The library version is `DSIDBG_VERSION` (include/dsidbg.h and
host/dsidbg/__init__.py); the wire protocol has its own `PROTO_VERSION`, and the daemon and the DSi agent must use the
same one. `dsidbg_config` may gain fields: always start from `dsidbg_config_defaults()` and rebuild the app when the
library changes (it is a static library).

## 0.1.0 (unreleased)

First version.

- DSi agent library: Wi-Fi connection with an HMAC handshake, discovery of the computer when its address changes,
  input injection, screenshots, logs and stdout, app commands and data channels, SD card file transfer, reboot into a
  file through Unlaunch, a 1 kHz sampling profiler, crash and hang reports with recovery from the crash screen.
- Loader (recovery app) and demo.
- Daemon and command line, MCP server with an installer for Claude Code, Codex and Claude Desktop, menu bar / tray
  app, packaged apps for macOS, Windows and Linux.
- Only `sd:/` paths; NAND is never mounted and write protection is turned on.
- Protocol version 1.
