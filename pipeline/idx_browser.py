import base64
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

IDX_HOME = "https://www.idx.co.id/id"
NOVNC_URL = os.getenv("PEAD_NOVNC_URL", "http://localhost:6080/vnc.html")
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
CHROME_START_TIMEOUT_SECONDS = 60
BROWSER_CONCURRENCY = 3
BROWSER_REQUEST_DELAY_MS = 400
MAX_CHALLENGE_RETRIES = 2

BROWSER_BATCH_JS = """
async ([urls, concurrency, delayMs]) => {
    const results = new Array(urls.length);
    let next = 0;
    async function worker() {
        while (true) {
            const i = next++;
            if (i >= urls.length) return;
            try {
                const response = await fetch(urls[i], { credentials: 'include' });
                let b64 = null;
                if (response.status === 200) {
                    const bytes = new Uint8Array(await response.arrayBuffer());
                    let binary = '';
                    const chunk = 0x8000;
                    for (let k = 0; k < bytes.length; k += chunk) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(k, k + chunk));
                    }
                    b64 = btoa(binary);
                }
                results[i] = { status: response.status, b64: b64 };
            } catch (e) {
                results[i] = { status: 0, b64: null };
            }
            await new Promise(r => setTimeout(r, delayMs));
        }
    }
    await Promise.all(Array.from({ length: concurrency }, worker));
    return results;
}
"""


class ChallengeError(RuntimeError):
    pass


def cdp_is_open(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2):
            return True
    except Exception:
        return False


def start_container() -> None:
    if not COMPOSE_FILE.exists():
        raise RuntimeError(f"docker-compose.yml not found at {COMPOSE_FILE}")
    logger.info("Starting pead-chrome container from %s", COMPOSE_FILE)
    try:
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("docker command not found, install Docker or start the container manually") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"docker compose up failed with exit code {exc.returncode}") from exc


def ensure_chrome_running(cdp_url: str) -> None:
    if cdp_is_open(cdp_url):
        logger.info("Chrome already listening at %s, attaching", cdp_url)
        return

    start_container()

    deadline = time.time() + CHROME_START_TIMEOUT_SECONDS
    while time.time() < deadline:
        if cdp_is_open(cdp_url):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome did not open {cdp_url} within {CHROME_START_TIMEOUT_SECONDS}s. "
        "Check: docker logs pead-chrome"
    )


class IdxBrowser:
    def __init__(self, cdp_url: str | None = None):
        self.cdp_url = (cdp_url or os.getenv("PEAD_CHROME_CDP") or DEFAULT_CDP_URL).rstrip("/")
        self._pw = None
        self._browser = None
        self.page = None
        self.scraper_blocked = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def ensure_open(self) -> None:
        if self.page is None:
            self.open()

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        ensure_chrome_running(self.cdp_url)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self.page = context.pages[0] if context.pages else context.new_page()

        if "idx.co.id" not in self.page.url or self._is_challenge():
            self._open_home()
        if self._is_challenge():
            self.wait_for_verification()

    def close(self) -> None:
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            logger.exception("Playwright stop failed")
        self._browser = None
        self._pw = None
        self.page = None

    def _open_home(self) -> None:
        try:
            self.page.goto(IDX_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            logger.warning("Navigation to IDX home did not finish cleanly: %s", exc)

    def _is_challenge(self) -> bool:
        try:
            title = (self.page.title() or "").lower()
        except Exception:
            return False
        return "just a moment" in title or "attention required" in title

    def wait_for_verification(self) -> None:
        if not sys.stdin or not sys.stdin.isatty():
            raise ChallengeError(
                "Cloudflare verification required and no interactive terminal is available. "
                f"Open {NOVNC_URL}, make sure the IDX website loads, then run again."
            )
        self._open_home()
        print()
        print("Cloudflare verification may be required.")
        print(f"Open {NOVNC_URL} in your browser and make sure the IDX website loads normally.")
        answer = input("Press Enter to continue, or type q to quit: ").strip().lower()
        if answer == "q":
            raise ChallengeError("Stopped by user at the verification prompt.")

    def fetch_batch(self, urls: list[str]) -> list[tuple[int, bytes | None]]:
        raw = self.page.evaluate(BROWSER_BATCH_JS, [urls, BROWSER_CONCURRENCY, BROWSER_REQUEST_DELAY_MS])
        results = []
        for item in raw:
            content = base64.b64decode(item["b64"]) if item.get("b64") else None
            results.append((item["status"], content))
        return results

    def fetch_with_verification(self, urls: list[str]) -> list[tuple[int, bytes | None]]:
        results = self.fetch_batch(urls)
        for _ in range(MAX_CHALLENGE_RETRIES):
            blocked = [
                i for i, (status, content) in enumerate(results)
                if status in (403, 429, 503) or (status == 200 and (content or b"")[:2] != b"PK")
            ]
            if not blocked:
                break
            logger.warning("%d request(s) blocked, handling verification", len(blocked))
            self.wait_for_verification()
            retried = self.fetch_batch([urls[i] for i in blocked])
            for i, item in zip(blocked, retried):
                results[i] = item
        return results