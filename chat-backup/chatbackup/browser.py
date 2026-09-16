"""Drives the real, logged-in Chromium.

The browser runs headed (it needs a display; entrypoint.sh starts a virtual
one) because headless browsers get challenged by Cloudflare. It keeps a
persistent profile in data/profile so it looks like the same visitor every
time, and it is seeded once from the storage_state.json you export on your
laptop.

API calls run inside the chatgpt.com page with fetch(), so they carry the
site's cookies and the app's own bearer token, exactly like the app's requests.
"""

from __future__ import annotations

import base64
import json
import logging
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, Request, sync_playwright

from .archive import write_atomic
from .chatgpt import LOGIN_URL_MARKERS, ApiError, classify_response
from .config import Config

log = logging.getLogger(__name__)

CHATGPT_ORIGIN = "https://chatgpt.com"
SESSION_COOKIE = "__Secure-next-auth.session-token"
# Cloudflare ties these to the machine and network that earned them; a copy from the laptop is useless here.
CLOUDFLARE_COOKIES = {"cf_clearance", "__cf_bm", "_cfuvid"}
TOKEN_WAIT_SECONDS = 30
NAVIGATION_TIMEOUT_MS = 60_000
FETCH_TIMEOUT_MS = 60_000
DOWNLOAD_TIMEOUT_MS = 180_000
FETCH_FAILURE_MARKERS = ("failed to fetch", "aborterror", "timeouterror", "networkerror", "load failed")

# Runs inside the page. Returns status, headers and body text so Python can decide what happened.
JS_FETCH_TEXT = """
async ({ url, headers, timeout }) => {
  const response = await fetch(url, { credentials: "include", headers, signal: AbortSignal.timeout(timeout) });
  const responseHeaders = {};
  response.headers.forEach((value, key) => { responseHeaders[key] = value; });
  return { status: response.status, headers: responseHeaders, text: await response.text() };
}
"""

# Runs inside the page. Returns the file as base64 (the only practical way to move bytes out of a page).
JS_FETCH_BYTES = """
async ({ url, maxBytes, timeout }) => {
  const response = await fetch(url, { credentials: "include", signal: AbortSignal.timeout(timeout) });
  if (!response.ok) return { error: "http", status: response.status };
  const blob = await response.blob();
  if (blob.size > maxBytes) return { error: "too_large", size: blob.size };
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  return {
    status: response.status,
    contentType: response.headers.get("content-type") || "",
    base64: String(dataUrl).split(",", 2)[1] || "",
  };
}
"""


def seed_cookies(storage_state: dict) -> list[dict]:
    """The cookies worth importing from an exported session: everything except Cloudflare's."""
    return [cookie for cookie in storage_state.get("cookies") or [] if cookie.get("name") not in CLOUDFLARE_COOKIES]


def is_fetch_failure(error: Exception) -> bool:
    """Did fetch() itself fail (network, timeout, cross-origin) rather than the browser?"""
    text = str(error).lower()
    return any(marker in text for marker in FETCH_FAILURE_MARKERS)


def is_playwright_error(error: BaseException) -> bool:
    return isinstance(error, PlaywrightError)


class Browser:
    def __init__(self, config: Config):
        self.config = config
        self.profile_dir = config.data_dir / "profile"
        self.seed_path = config.data_dir / "storage_state.json"
        self.imported_seed_path = config.data_dir / "storage_state.imported.json"
        self.state_path = config.data_dir / "state.json"
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._token: str | None = None
        self._extra_headers: dict[str, str] = {}

    @property
    def page(self) -> Page:
        assert self._page is not None, "browser is not started"
        return self._page

    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, "browser is not started"
        return self._context

    def seed_available(self) -> bool:
        return self.seed_path.is_file()

    # --- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """Launch Chromium with the saved profile, import a new login if one was provided, open chatgpt.com.

        Raises ApiError("login_required") when nobody is logged in; the browser stays open in that case.
        """
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--window-size=1920,1080"],
            ignore_default_args=["--enable-automation"],
            no_viewport=True,
            locale=self.config.locale,
            timezone_id=self.config.timezone,
        )
        self._context.on("request", self._remember_token)
        used_seed = self._import_cookies()
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._open_chatgpt()
        if used_seed:
            # Keep the file, but under another name, so it is not imported again on every start.
            self.seed_path.replace(self.imported_seed_path)
            log.info("login imported; %s renamed to %s", self.seed_path.name, self.imported_seed_path.name)

    def stop(self) -> None:
        try:
            self.save_state()
        except Exception as error:      # noqa: BLE001 - shutting down must not fail
            log.warning("could not save the browser state: %s", error)
        for step in (self._close_context, self._stop_playwright):
            try:
                step()
            except Exception as error:  # noqa: BLE001
                log.warning("error while closing the browser: %s", error)
        self._context = self._page = self._playwright = None
        self._token = None
        self._extra_headers = {}

    def restart(self) -> None:
        self.stop()
        self.start()

    def reload(self) -> None:
        """Open chatgpt.com again: refreshes the bearer token, lets a Cloudflare check run, notices a dead login."""
        self._open_chatgpt()

    def save_state(self) -> None:
        """Keep a copy of the cookies outside the profile (Chromium sometimes drops session cookies on restart)."""
        if self._context is None:
            return
        state = self._context.storage_state()
        write_atomic(self.state_path, json.dumps(state).encode("utf-8"))

    def _close_context(self) -> None:
        if self._context is not None:
            self._context.close()

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()

    # --- login ------------------------------------------------------------------------------------

    def _import_cookies(self) -> bool:
        """Import a freshly exported login if there is one; otherwise repair the profile from our own copy."""
        if self.seed_path.is_file():
            state = json.loads(self.seed_path.read_text(encoding="utf-8"))
            self.context.add_cookies(seed_cookies(state))
            log.info("seeding cookies from %s", self.seed_path.name)
            return True
        if not self._has_session_cookie() and self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.context.add_cookies(seed_cookies(state))
            log.info("restoring cookies from %s (the profile had lost them)", self.state_path.name)
        return False

    def _has_session_cookie(self) -> bool:
        # NextAuth splits a large session cookie into ".0", ".1", ... pieces, so match the prefix.
        return any((cookie.get("name") or "").startswith(SESSION_COOKIE) for cookie in self.context.cookies(CHATGPT_ORIGIN))

    def _open_chatgpt(self) -> None:
        self._token = None
        self.page.goto(CHATGPT_ORIGIN + "/", wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        self._wait_for_token()
        if self._on_challenge_page():
            log.warning("Cloudflare challenge page; giving it 15 seconds to clear")
            self.page.wait_for_timeout(15_000)
            if self._on_challenge_page():
                self.page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                self._wait_for_token()
        self._verify_logged_in()

    def _wait_for_token(self) -> None:
        deadline = TOKEN_WAIT_SECONDS * 2
        while self._token is None and deadline > 0:
            self.page.wait_for_timeout(500)
            deadline -= 1

    def _on_challenge_page(self) -> bool:
        try:
            title = (self.page.title() or "").lower()
        except PlaywrightError:
            return False
        return "just a moment" in title or "attention required" in title

    def _verify_logged_in(self) -> None:
        if self._on_challenge_page():
            raise ApiError("challenge", detail="Cloudflare challenge page when opening chatgpt.com")
        if any(marker in self.page.url for marker in LOGIN_URL_MARKERS):
            raise ApiError("login_required", detail=f"browser is on {self.page.url}")
        if self._token:
            log.info("token captured from the app's own requests")
            return
        # The app made no authorized request we could see. Ask the session endpoint instead.
        session = self._session_info()
        if session.get("accessToken"):
            self._token = str(session["accessToken"])
            log.info("token obtained from /api/auth/session")
            return
        if session.get("user"):
            log.info("logged in, but no bearer token is available; relying on cookies")
            return
        raise ApiError("login_required", detail="chatgpt.com reports no logged-in user")

    def _session_info(self) -> dict:
        try:
            result = self._evaluate(JS_FETCH_TEXT, {"url": CHATGPT_ORIGIN + "/api/auth/session",
                                                     "headers": {"Accept": "application/json"},
                                                     "timeout": FETCH_TIMEOUT_MS})
            value = json.loads(result["text"]) if result["status"] == 200 else {}
        except (ApiError, ValueError, KeyError):
            return {}
        return value if isinstance(value, dict) else {}

    def _remember_token(self, request: Request) -> None:
        """The app sends its bearer token on every API call; keep the latest one and its oai-* headers."""
        if not request.url.startswith(CHATGPT_ORIGIN + "/backend-api/"):
            return
        headers = request.headers
        auth = headers.get("authorization") or ""
        if not auth.lower().startswith("bearer ") or len(auth) < 20:
            return
        token = auth[7:]
        if token != self._token:
            self._token = token
            self._extra_headers = {key: value for key, value in headers.items() if key.startswith("oai-")}
            log.debug("captured a bearer token")

    # --- API calls --------------------------------------------------------------------------------

    def api_get(self, path: str):
        """GET a chatgpt.com path from inside the page and return the decoded JSON."""
        result = self._evaluate(JS_FETCH_TEXT, {"url": CHATGPT_ORIGIN + path, "headers": self._request_headers(),
                                                 "timeout": FETCH_TIMEOUT_MS})
        log.debug("GET %s -> %s %s", path, result["status"], result["text"][:300].replace("\n", " "))
        error = classify_response(result["status"], result["headers"], result["text"], self.page.url)
        if error is not None:
            raise error
        try:
            return json.loads(result["text"])
        except ValueError:
            raise ApiError("bad_body", result["status"], detail=result["text"][:200]) from None

    def download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Fetch a file's bytes. Returns (data, content type)."""
        try:
            result = self._evaluate(JS_FETCH_BYTES, {"url": url, "maxBytes": max_bytes, "timeout": DOWNLOAD_TIMEOUT_MS})
        except ApiError as error:
            if error.kind != "network":
                raise
            # A download URL on another domain cannot be fetched from the page; Playwright's
            # request client shares the browser's cookies and can fetch it from outside.
            log.debug("in-page download failed (%s); retrying outside the page", error.detail)
            return self._download_outside_page(url, max_bytes)
        if result.get("error") == "too_large":
            raise ApiError("too_large", detail=f"{result.get('size')} bytes is above the limit")
        if result.get("error") == "http":
            raise classify_response(result["status"], {}, "", self.page.url) or ApiError("invalid", result["status"])
        return base64.b64decode(result["base64"]), result.get("contentType") or ""

    def _download_outside_page(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        response = self.context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
        if not response.ok:
            raise classify_response(response.status, response.headers, "", self.page.url) or ApiError("invalid", response.status)
        data = response.body()
        if len(data) > max_bytes:
            raise ApiError("too_large", detail=f"{len(data)} bytes is above the limit")
        return data, response.headers.get("content-type", "")

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self._extra_headers}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _evaluate(self, script: str, argument: dict):
        try:
            return self.page.evaluate(script, argument)
        except PlaywrightError as error:
            if is_fetch_failure(error):
                raise ApiError("network", detail=str(error).splitlines()[0][:200]) from error
            raise
