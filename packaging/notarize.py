# SPDX-License-Identifier: MIT
"""Notarizes the signed macOS app and staples the ticket to it, so Gatekeeper opens it without asking (offline too).

    python packaging/notarize.py [dist/dsidbg.app]

The release workflow runs it after building with DSIDBG_CODESIGN_IDENTITY set (see dsidbg.spec) and before
packaging/package.py macos. Credentials: an App Store Connect API key, given as DSIDBG_NOTARY_KEY (path to the .p8),
DSIDBG_NOTARY_KEY_ID and DSIDBG_NOTARY_ISSUER; the workflow sets them from its MACOS_NOTARY_* secrets.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def run(args, **kw):
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(args, check=True, **kw)


def credentials():
    key, key_id, issuer = (os.environ.get(n) for n in ("DSIDBG_NOTARY_KEY", "DSIDBG_NOTARY_KEY_ID",
                                                        "DSIDBG_NOTARY_ISSUER"))
    if key and key_id and issuer:
        return ["--key", key, "--key-id", key_id, "--issuer", issuer]
    raise SystemExit("no notary credentials: set DSIDBG_NOTARY_KEY, DSIDBG_NOTARY_KEY_ID and DSIDBG_NOTARY_ISSUER")


def main(app):
    if not os.path.isdir(app):
        raise SystemExit("%s doesn't exist: build it first" % app)
    creds = credentials()
    # Notarization needs a Developer ID signature with the hardened runtime; check before uploading.
    info = subprocess.run(["codesign", "-dvv", app], capture_output=True, text=True).stderr
    if "Authority=Developer ID Application" not in info or "runtime" not in info:
        raise SystemExit("%s isn't signed with a Developer ID and the hardened runtime: rebuild it with "
                         "DSIDBG_CODESIGN_IDENTITY set\n%s" % (app, info))
    run(["codesign", "--verify", "--deep", "--strict", app])

    with tempfile.TemporaryDirectory() as tmp:
        upload = os.path.join(tmp, "dsidbg-notarize.zip")
        run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", app, upload])
        out = subprocess.run(["xcrun", "notarytool", "submit", upload, "--wait", "--output-format", "json"] + creds,
                             capture_output=True, text=True)
    print(out.stdout + out.stderr, flush=True)
    try:
        result = json.loads(out.stdout)
    except ValueError:
        raise SystemExit("notarytool failed (exit %d)" % out.returncode)
    if result.get("status") != "Accepted":
        if result.get("id"):  # Apple's log says which file and why
            subprocess.run(["xcrun", "notarytool", "log", result["id"]] + creds)
        raise SystemExit("notarization: %s" % result.get("status"))

    run(["xcrun", "stapler", "staple", app])
    run(["xcrun", "stapler", "validate", app])
    run(["spctl", "--assess", "--type", "execute", "-vv", app])  # what Gatekeeper decides on a user's Mac
    print("notarized and stapled: %s" % app)


if __name__ == "__main__":
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1].startswith("-")):
        raise SystemExit(__doc__)
    main(sys.argv[1] if len(sys.argv) == 2 else os.path.join(ROOT, "dist", "dsidbg.app"))
