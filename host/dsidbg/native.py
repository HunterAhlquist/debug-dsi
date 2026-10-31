# SPDX-License-Identifier: MIT
"""Native dialogs and terminal launching for the tray app, without GUI toolkits: AppleScript on macOS, PowerShell
and the Win32 API on Windows, zenity/kdialog on Linux."""
import os
import shutil
import subprocess
import sys

MAC, WINDOWS = sys.platform == "darwin", sys.platform.startswith("win")


class NativeError(Exception):
    pass


def _osascript(lines, *args):
    """Runs AppleScript with arguments passed separately (no quoting problems). -> (returncode, stdout)."""
    cmd = ["osascript"]
    for line in ["on run argv"] + lines + ["end run"]:
        cmd += ["-e", line]
    r = subprocess.run(cmd + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def pick_folder(prompt, start=None):
    """Asks for a folder. Returns its path, or None if the user cancelled."""
    if MAC:
        start = start or ("/Volumes" if os.path.isdir("/Volumes") else os.path.expanduser("~"))
        rc, out = _osascript(["activate",
                              "return POSIX path of (choose folder with prompt (item 1 of argv) "
                              "default location (POSIX file (item 2 of argv)))"], prompt, start)
        if rc != 0 or not out:
            return None
        return out.rstrip("/") or "/"
    if WINDOWS:
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "$d.Description = $env:DSIDBG_PROMPT; $d.ShowNewFolderButton = $false;"
                  "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True,
                           env=dict(os.environ, DSIDBG_PROMPT=prompt))
        return r.stdout.strip() or None
    if shutil.which("zenity"):
        r = subprocess.run(["zenity", "--file-selection", "--directory", "--title", prompt], capture_output=True,
                           text=True)
    elif shutil.which("kdialog"):
        r = subprocess.run(["kdialog", "--getexistingdirectory", start or os.path.expanduser("~"), "--title", prompt],
                           capture_output=True, text=True)
    else:
        raise NativeError("no folder picker found: install zenity or kdialog")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def message(title, text, error=False):
    """Shows a message box and waits until it is dismissed."""
    if MAC:
        _osascript(["activate", "display dialog (item 2 of argv) with title (item 1 of argv) buttons {\"OK\"} "
                    "default button \"OK\" with icon %s" % ("stop" if error else "note")], title, text)
    elif WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)
    elif shutil.which("zenity"):
        subprocess.run(["zenity", "--error" if error else "--info", "--title", title, "--text", text, "--no-markup"])
    elif shutil.which("kdialog"):
        subprocess.run(["kdialog", "--title", title, "--error" if error else "--msgbox", text])
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, text])
    else:
        print("%s: %s" % (title, text), file=sys.stderr)


def open_terminal(directory, path_dir, command="dsidbg status"):
    """Opens the system's terminal in `directory`, with `path_dir` on PATH, running `command` first."""
    if MAC:
        # Terminal.app is the macOS default; `do script` opens a window running a shell command.
        script = "cd %s && export PATH=%s:\"$PATH\" && %s" % (_sh(directory), _sh(path_dir), command)
        rc, out = _osascript(["tell application \"Terminal\"", "activate", "do script (item 1 of argv)",
                              "end tell"], script)
        if rc != 0:
            raise NativeError("couldn't open Terminal: %s" % out)
        return
    env = dict(os.environ, PATH=path_dir + os.pathsep + os.environ.get("PATH", ""))
    if WINDOWS:
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-d", directory, "cmd", "/k", command], env=env)
        else:
            subprocess.Popen(["cmd", "/k", command], cwd=directory, env=env,
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    keep = "%s; exec \"${SHELL:-sh}\"" % command
    for term, args in (("x-terminal-emulator", ["-e", "sh", "-c", keep]),
                       ("gnome-terminal", ["--", "sh", "-c", keep]),
                       ("konsole", ["-e", "sh", "-c", keep]),
                       ("xfce4-terminal", ["-x", "sh", "-c", keep]),
                       ("xterm", ["-e", "sh", "-c", keep])):
        if shutil.which(term):
            subprocess.Popen([term] + args, cwd=directory, env=env, start_new_session=True)
            return
    raise NativeError("no terminal emulator found")


def _sh(s):
    return "'" + s.replace("'", "'\\''") + "'"
