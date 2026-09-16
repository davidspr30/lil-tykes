"""Like tools/export_session.py, but for a remote screen: no Enter key needed.

Opens ChatGPT in a headed Chromium, waits until a login session cookie appears
(NextAuth splits a large session cookie into ".0", ".1", ... pieces, so any name
starting with SESSION_COOKIE counts), waits a little longer so the login settles,
then writes /data/storage_state.json and exits.

Run it with tools/remote-login/run.sh, which prints the address and password.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

OUTPUT = "/data/storage_state.json"
PROFILE_DIR = "/tmp/login-profile"   # thrown away with the container
SESSION_COOKIE = "__Secure-next-auth.session-token"
TIMEOUT_SECONDS = 30 * 60
SETTLE_SECONDS = 15
LOG_EVERY_SECONDS = 30


def chatgpt_cookie_names(context) -> list[str]:
    return sorted(c.get("name", "") for c in context.cookies("https://chatgpt.com"))


def main() -> int:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized", "--window-position=0,0"],
            ignore_default_args=["--enable-automation"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://chatgpt.com/")
        print("browser open, waiting for login", flush=True)

        deadline = time.monotonic() + TIMEOUT_SECONDS
        next_log = 0.0
        while True:
            names = chatgpt_cookie_names(context)
            if any(name.startswith(SESSION_COOKIE) for name in names):
                break
            if time.monotonic() >= next_log:
                print("cookie names so far:", ", ".join(names) or "(none)", flush=True)
                next_log = time.monotonic() + LOG_EVERY_SECONDS
            if time.monotonic() > deadline:
                print("gave up: no login within 30 minutes", flush=True)
                return 1
            time.sleep(2)

        print(f"session cookie seen, waiting {SETTLE_SECONDS}s for the login to settle", flush=True)
        time.sleep(SETTLE_SECONDS)
        print("cookie names:", ", ".join(chatgpt_cookie_names(context)), flush=True)
        temp_path = OUTPUT + ".tmp"
        context.storage_state(path=temp_path)
        os.replace(temp_path, OUTPUT)
        print(f"OK: session cookie found. Saved {OUTPUT}", flush=True)
        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
