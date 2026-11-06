#!/usr/bin/env python3
"""Connect the desk to a Photon Spectrum project: python3 local/setup_texting.py

Asks for the project id, the project secret (typed hidden) and, for a dedicated line, its number.
Checks them with read-only Spectrum API calls, writes spectrum.env (mode 600), and waits for the
running desk server to pick it up. It creates, buys and texts nothing, and never prints the secret.
"""
import getpass, json, os, sys, time, urllib.request
from pathlib import Path

import sms

STATE = Path(os.environ.get("DECISION_DESK_STATE") or Path.home() / ".local/state/decision-desk")
ENV = Path(os.environ.get("DECISION_DESK_SPECTRUM_ENV") or STATE / "spectrum.env")
PORT = int(os.environ.get("DECISION_DESK_PORT") or 8791)


def ask(prompt, hidden=False):
    v = (getpass.getpass if hidden else input)(prompt).strip()
    return v.strip("'\"")


def main():
    print("Paste the values from the Photon dashboard (your project, then Settings).")
    creds = {"SPECTRUM_PROJECT_ID": ask("Project id: "), "SPECTRUM_PROJECT_SECRET": ask("Project secret (hidden): ", hidden=True)}
    try:
        project = sms.api(creds, "GET", "/") or {}
        kind = (sms.api(creds, "GET", "/imessage/") or {}).get("type")
        lines = [l for l in (sms.api(creds, "GET", "/lines/") or {}).get("lines", []) if l.get("platform") == "imessage"]
        hooks = sms.api(creds, "GET", "/webhooks/") or []
    except sms.ApiError as e:
        print(f"Stopped: {e} Nothing was written.")
        return 1
    print(f"Project: {project.get('name') or '(unnamed)'}. iMessage: {kind or 'unknown'}.")
    line = ""
    if kind == "dedicated":
        for l in lines:
            print(f"  line ending {l['phoneNumber'][-4:]}, {l.get('status')}")
        if not lines:
            print("Stopped: this project has no dedicated line yet. Nothing was written.")
            return 1
        line = sms.norm_phone(ask("Line to text from (Enter for the only one): ")) if len(lines) > 1 else lines[0]["phoneNumber"]
        if line not in [l["phoneNumber"] for l in lines]:
            print("Stopped: that isn't one of this project's lines. Nothing was written.")
            return 1
    else:
        print("Texts come from Photon's shared pool. Connecting in the page registers your number as a user of this project.")
    if hooks:
        print(f"Careful: this project has {len(hooks)} webhook(s). Each one also gets your replies to the desk.")
        if ask("Use it anyway? [y/N] ").lower() != "y":
            print("Stopped. Nothing was written.")
            return 1
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = ENV.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"SPECTRUM_PROJECT_ID={creds['SPECTRUM_PROJECT_ID']}\nSPECTRUM_PROJECT_SECRET={creds['SPECTRUM_PROJECT_SECRET']}\nSPECTRUM_LINE={line}\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV)
    print(f"Wrote {ENV} (only you can read it).")
    for _ in range(30):  # the server checks spectrum.env every few seconds
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/sms/status", timeout=2) as r:
                if json.loads(r.read()).get("configured"):
                    print("Texting is on. Open the desk, tap Text me, and connect your phone.")
                    return 0
        except OSError:
            pass
        time.sleep(0.5)
    print("The desk server hasn't picked it up. Start or restart it (python3 local/server.py), then tap Text me.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
        sys.exit(1)
