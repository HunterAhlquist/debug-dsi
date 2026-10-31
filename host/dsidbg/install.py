# SPDX-License-Identifier: MIT
"""`dsidbg mcp-install`: registers the dsidbg MCP server with LLM harnesses.

- Claude Code (CLI, and the Claude desktop app's Code tab, which shares its configuration): `claude mcp add`.
- Codex (CLI, IDE extension and desktop app share ~/.codex/config.toml): `codex mcp add`.
- Claude Desktop (chat): its claude_desktop_config.json, edited in place after a backup.
"""
import json
import os
import shutil
import subprocess
import sys

from .cli import REPO_DIR, CliError

TARGETS = ("claude-code", "codex", "claude-desktop")


def server_command():
    """The command a harness runs to start the server: the repo's bin/dsidbg wrapper (needs only python3)."""
    return [os.path.join(REPO_DIR, "bin", "dsidbg"), "mcp"]


def server_env():
    # Only pass DSIDBG_HOME when it isn't the default, so the server finds the same daemon as the CLI.
    home = os.environ.get("DSIDBG_HOME")
    return {"DSIDBG_HOME": os.path.abspath(home)} if home else {}


def claude_desktop_config_path():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Claude", "claude_desktop_config.json")
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


# Where these CLIs usually live. Apps started from Finder, the Start menu or at login don't get the shell's PATH.
EXTRA_BIN_DIRS = ["~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin",
                  "~/.bun/bin", "~/AppData/Roaming/npm"]


def search_path():
    return os.environ.get("PATH", "") + os.pathsep + os.pathsep.join(os.path.expanduser(d) for d in EXTRA_BIN_DIRS)


def which(name):
    return shutil.which(name, path=search_path())


def run_env():
    # The harness CLIs may be scripts that need their interpreter (node) from the same directories.
    return dict(os.environ, PATH=search_path())


class Skipped(Exception):
    """A harness that is present but can't be configured right now."""


def claude_desktop_running():
    """Claude Desktop keeps its config in memory and writes it back, dropping entries added while it runs."""
    forced = os.environ.get("DSIDBG_CLAUDE_DESKTOP_RUNNING")  # for tests
    if forced is not None:
        return forced == "1"
    try:
        if sys.platform == "darwin":
            return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True).returncode == 0
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"], capture_output=True,
                                 text=True).stdout
            return "Claude.exe" in out
        return subprocess.run(["pgrep", "-if", "claude-desktop|/claude(-desktop)?$"], capture_output=True).returncode == 0
    except OSError:
        return False


def detect():
    found = []
    if which("claude"):
        found.append("claude-code")
    if which("codex"):
        found.append("codex")
    if os.path.isdir(os.path.dirname(claude_desktop_config_path())):
        found.append("claude-desktop")
    return found


class Installer:
    def __init__(self, name="dsidbg", scope="user", dry_run=False, out=print, skip_existing=False):
        self.name, self.scope, self.dry_run, self.out = name, scope, dry_run, out
        self.skip_existing = skip_existing

    def run(self, argv, check=True):
        self.out("  $ " + " ".join(_quote(a) for a in argv))
        if self.dry_run:
            return 0
        r = subprocess.run(argv, capture_output=True, text=True, env=run_env())
        if check and r.returncode != 0:
            raise CliError("%s failed: %s" % (argv[0], (r.stderr or r.stdout).strip()))
        return r.returncode

    def exists(self, argv):
        """Whether a `... mcp get NAME` command succeeds (quietly; also in dry runs)."""
        return subprocess.run(argv, capture_output=True, text=True, env=run_env()).returncode == 0

    # -- Claude Code -------------------------------------------------------------------------------------------
    def claude_code(self, remove=False):
        claude = which("claude")
        if not claude:
            raise CliError("the `claude` command isn't on PATH")
        exists = self.exists([claude, "mcp", "get", self.name])
        if exists and self.skip_existing and not remove:
            return "already installed in Claude Code"
        if exists:
            self.run([claude, "mcp", "remove", "-s", self.scope, self.name], check=False)
        if remove:
            return "removed from Claude Code (%s scope)" % self.scope
        env = sum((["-e", "%s=%s" % kv] for kv in server_env().items()), [])
        self.run([claude, "mcp", "add", "-s", self.scope] + env + [self.name, "--"] + server_command())
        return "added to Claude Code (%s scope); new sessions get the tools" % self.scope

    # -- Codex ---------------------------------------------------------------------------------------------------
    def codex(self, remove=False):
        codex = which("codex")
        if not codex:
            raise CliError("the `codex` command isn't on PATH")
        exists = self.exists([codex, "mcp", "get", self.name])
        if exists and self.skip_existing and not remove:
            return "already installed in Codex"
        if exists:
            self.run([codex, "mcp", "remove", self.name], check=False)
        if remove:
            return "removed from Codex"
        env = sum((["--env", "%s=%s" % kv] for kv in server_env().items()), [])
        self.run([codex, "mcp", "add", self.name] + env + ["--"] + server_command())
        return "added to Codex (~/.codex/config.toml: CLI, IDE extension and desktop app); restart running sessions"

    # -- Claude Desktop ------------------------------------------------------------------------------------------
    def claude_desktop(self, remove=False):
        path = claude_desktop_config_path()
        if os.path.exists(path):
            with open(path) as f:
                text = f.read()
            config = json.loads(text) if text.strip() else {}
        else:
            text, config = None, {}
        servers = config.setdefault("mcpServers", {})
        if self.name in servers and self.skip_existing and not remove:
            return "already installed in Claude Desktop"
        if claude_desktop_running() and not self.dry_run:
            raise Skipped("Claude Desktop is running and would overwrite the change: quit it (including Claude "
                          "Code sessions in the app), then install again, e.g. from the dsidbg menu bar icon")
        if remove:
            if self.name not in servers:
                return "not in Claude Desktop's config"
            del servers[self.name]
        else:
            entry = {"command": server_command()[0], "args": server_command()[1:]}
            if server_env():
                entry["env"] = server_env()
            servers[self.name] = entry
        self.out("  edit %s (mcpServers.%s)" % (path, self.name))
        if not self.dry_run:
            if text is not None:
                with open(path + ".bak", "w") as f:  # keep the previous version
                    f.write(text)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        verb = "removed from" if remove else "added to"
        return "%s Claude Desktop (backup: %s.bak); quit and reopen Claude to load it" % (verb, os.path.basename(path))


def _quote(a):
    return a if a and all(c.isalnum() or c in "/._-=:" for c in a) else "'%s'" % a.replace("'", "'\\''")


LABELS = {"claude-code": "Claude Code", "codex": "Codex", "claude-desktop": "Claude Desktop"}


def install_all(skip_existing=True, remove=False, name="dsidbg", scope="user", dry_run=False, out=lambda s: None):
    """Installs into every harness found. -> list of (harness label, outcome, message); outcome is one of
    installed, already, removed, skipped (not found), failed."""
    found = detect()
    inst = Installer(name, scope, dry_run, out=out, skip_existing=skip_existing)
    results = []
    for t in TARGETS:
        label = LABELS[t]
        if t not in found:
            results.append((label, "skipped", "not found on this computer"))
            continue
        try:
            msg = getattr(inst, t.replace("-", "_"))(remove=remove)
            outcome = "already" if msg.startswith("already") else ("removed" if remove else "installed")
            results.append((label, outcome, msg))
        except Skipped as e:
            results.append((label, "skipped", str(e)))
        except (CliError, OSError, ValueError) as e:
            results.append((label, "failed", str(e)))
    return results


def do_mcp_install(a):
    targets = list(TARGETS) if a.target == "all" else [a.target]
    if a.target == "all":
        found = detect()
        skipped = [t for t in targets if t not in found]
        targets = [t for t in targets if t in found]
        for t in skipped:
            print("%s: not found, skipped" % t)
        if not targets:
            raise CliError("no supported harness found (Claude Code, Codex, Claude Desktop)")
    inst = Installer(a.name, a.scope, a.dry_run, skip_existing=a.skip_existing)
    failed = False
    for t in targets:
        print("%s:" % t)
        try:
            msg = getattr(inst, t.replace("-", "_"))(remove=a.remove)
            print("  %s%s" % ("(dry run) " if a.dry_run else "", msg))
        except Skipped as e:
            print("  skipped: %s" % e)
        except (CliError, OSError, ValueError) as e:
            print("  failed: %s" % e)
            failed = True
    if not a.remove and not a.dry_run:
        print("Server command: %s" % " ".join(server_command()))
    if failed:
        raise CliError("some harnesses weren't updated")
