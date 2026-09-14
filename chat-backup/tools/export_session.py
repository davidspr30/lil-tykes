#!/usr/bin/env python3
"""Run this on your own computer to log in to ChatGPT once and save the session.

    pip install playwright==1.62.0
    playwright install chromium
    python tools/export_session.py

A browser window opens. Log in as usual (two-factor and all), then press Enter
in this terminal. The script writes storage_state.json in the current folder;
copy that file into chat-backup/data/ on the backup machine.

Do not log out in the window afterwards: logging out cancels the session you
just saved. Just close the window.
"""

import sys

from playwright.sync_api import sync_playwright

OUTPUT = "storage_state.json"
SESSION_COOKIE = "__Secure-next-auth.session-token"


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--window-size=1280,900"],
            ignore_default_args=["--enable-automation"],
        )
        # A fresh, empty profile: this login is its own "device" and won't disturb your normal browser.
        context = browser.new_context(no_viewport=True)
        page = context.new_page()
        page.goto("https://chatgpt.com/")
        print("A browser window has opened. Log in to ChatGPT there.")
        print("When you can see your chat list, come back here and press Enter.")
        input()
        state = context.storage_state(path=OUTPUT)
        cookie_names = {cookie.get("name") for cookie in state.get("cookies", [])}
        context.close()
        browser.close()

    # NextAuth splits a large session cookie into ".0", ".1", ... pieces, so match the prefix.
    if not any((name or "").startswith(SESSION_COOKIE) for name in cookie_names):
        print(f"WARNING: no ChatGPT session cookie was found, so {OUTPUT} will not work. Were you logged in?")
        return 1
    print(f"OK: session cookie found. Saved {OUTPUT}.")
    print("Next: copy it into chat-backup/data/ on the backup machine, for example:")
    print(f"    scp {OUTPUT} you@backup-box:lil-tykes/chat-backup/data/")
    print("The service picks it up within 10 minutes (or right away after: docker compose restart).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
