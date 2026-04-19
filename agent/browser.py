"""
Browser Controller — Playwright-based browser automation via CDP.

Architecture:
  1. ONE-TIME SETUP: On first import, installs Playwright Chromium if needed.
  2. LAUNCH: Starts the default browser (Brave) with --remote-debugging-port so
     Playwright can connect via Chrome DevTools Protocol (CDP).
  3. CONNECT: Playwright connects to the running browser and exposes a Page object.
  4. PRIMITIVES: High-level functions (navigate, click, type, etc.) that the agent's
     brain can call as tools — far more reliable than PyAutoGUI for web tasks.

The browser uses the user's REAL profile directory so all cookies, saved passwords,
and extensions are available — no cold login needed.
"""

import os
import sys
import json
import time
import shutil
import socket
import logging
import subprocess
import atexit
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("mira.browser")

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────
CDP_PORT = 9222
STARTUP_TIMEOUT = 15  # seconds to wait for browser to start in debug mode
CONNECT_RETRIES = 3
SETUP_FLAG_FILE = Path(__file__).parent.parent / ".playwright_setup_done"
ACTION_SETTLE_MS = 120
TYPE_DELAY_MS = 18

# Known browser paths on Windows (checked in order)
BROWSER_PATHS = {
    "Brave Browser": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        Path("C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"),
    ],
    "Google Chrome": [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ],
    "Microsoft Edge": [
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ],
}

# User data directories (for session persistence — cookies, passwords, etc.)
USER_DATA_DIRS = {
    "Brave Browser": Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data",
    "Google Chrome": Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
    "Microsoft Edge": Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data",
}


# ──────────────────────────────────────────
# One-Time Setup
# ──────────────────────────────────────────

def _run_playwright_setup():
    """
    One-time setup: installs Playwright's Chromium browser bundle.
    Only runs if the setup flag file doesn't exist.
    """
    if SETUP_FLAG_FILE.exists():
        logger.info("Playwright setup already completed (flag file exists).")
        return True

    logger.info("Running one-time Playwright setup...")
    try:
        from agent.display import console
        console.print("[bold yellow]🔧 One-time Playwright setup — installing browser drivers...[/bold yellow]")

        # Install only chromium (smallest download, and we connect via CDP anyway)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

        if result.returncode == 0:
            # Write flag file so we never do this again
            SETUP_FLAG_FILE.write_text(f"setup_completed_at={time.time()}\n", encoding="utf-8")
            console.print("[bold green]✓ Playwright setup complete![/bold green]")
            logger.info("Playwright setup completed successfully.")
            return True
        else:
            logger.error(f"Playwright setup failed: {result.stderr}")
            console.print(f"[bold red]✗ Playwright setup failed:[/bold red] {result.stderr[:300]}")
            return False

    except Exception as e:
        logger.error(f"Playwright setup exception: {e}")
        return False


# ──────────────────────────────────────────
# Browser Detection & Launch
# ──────────────────────────────────────────

def _find_browser_exe(browser_name: str) -> Optional[Path]:
    """Find the executable path for a browser by name."""
    candidates = BROWSER_PATHS.get(browser_name, [])
    for path in candidates:
        if path.exists():
            logger.info(f"Found {browser_name} at: {path}")
            return path

    # Fallback: try all known browsers
    for name, paths in BROWSER_PATHS.items():
        for path in paths:
            if path.exists():
                logger.info(f"Fallback: found {name} at: {path}")
                return path

    return None


def _is_port_open(port: int) -> bool:
    """Check if a TCP port is already in use (i.e., browser debug port is active)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _launch_browser_debug_mode(browser_name: str = "Brave Browser") -> bool:
    """
    Launch the browser with --remote-debugging-port so Playwright can connect via CDP.

    Uses the user's REAL profile directory so cookies/sessions are preserved.
    If the browser is already running with the debug port, this is a no-op.
    """
    if _is_port_open(CDP_PORT):
        logger.info(f"CDP port {CDP_PORT} already open — browser is running in debug mode.")
        return True

    exe_path = _find_browser_exe(browser_name)
    if not exe_path:
        logger.error(f"Cannot find browser executable for '{browser_name}'")
        return False

    user_data_dir = USER_DATA_DIRS.get(browser_name, "")

    # Build launch args
    args = [
        str(exe_path),
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ]

    # Use the real user profile if it exists (preserves all sessions)
    if user_data_dir and Path(user_data_dir).exists():
        args.append(f"--user-data-dir={user_data_dir}")
        logger.info(f"Using user data dir: {user_data_dir}")

    logger.info(f"Launching browser in debug mode: {' '.join(args)}")

    try:
        # Launch detached (browser stays open after Python exits)
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | subprocess.DETACHED_PROCESS
        )
    except Exception as e:
        logger.error(f"Failed to launch browser: {e}")
        return False

    # Wait for the CDP port to become available
    for i in range(STARTUP_TIMEOUT * 2):  # check every 0.5s
        if _is_port_open(CDP_PORT):
            logger.info(f"Browser CDP port ready after {(i + 1) * 0.5:.1f}s")
            return True
        time.sleep(0.5)

    logger.error(f"Browser did not open CDP port {CDP_PORT} within {STARTUP_TIMEOUT}s")
    return False


# ──────────────────────────────────────────
# Playwright Connection Manager (Singleton)
# ──────────────────────────────────────────

class BrowserController:
    """
    Singleton controller that manages the Playwright ↔ Browser CDP connection.

    Usage:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        page.goto("https://example.com")
    """
    _instance: Optional["BrowserController"] = None

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._connected = False
        self._agent_page = None
        self._browser_name = "Brave Browser"

        # Load browser preference from config
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            self._browser_name = config.get("user_profile", {}).get("default_browser", "Brave Browser")
        except Exception:
            pass

    @classmethod
    def get_instance(cls) -> "BrowserController":
        """Get or create the singleton BrowserController."""
        if cls._instance is None:
            cls._instance = BrowserController()
        return cls._instance

    def ensure_connected(self) -> bool:
        """
        Ensure we have a live Playwright connection to the browser.
        Handles the full lifecycle: setup → launch → connect.
        """
        if self._connected and self._browser and self._browser.is_connected():
            return True

        from agent.display import console

        # Step 1: One-time Playwright setup
        if not SETUP_FLAG_FILE.exists():
            if not _run_playwright_setup():
                console.print("[bold red]✗ Playwright setup failed. Cannot use browser automation.[/bold red]")
                return False

        # Step 2: Launch browser in debug mode (if not already running)
        console.print(f"[dim cyan]🌐 Ensuring {self._browser_name} is running in debug mode (port {CDP_PORT})...[/dim cyan]")
        if not _launch_browser_debug_mode(self._browser_name):
            # Startup can be racy on systems where an existing browser takes time
            # to expose CDP. Give a short grace period before hard failing.
            for _ in range(20):
                if _is_port_open(CDP_PORT):
                    break
                time.sleep(0.5)

            if not _is_port_open(CDP_PORT):
                console.print(f"[bold red]✗ Failed to launch {self._browser_name} in debug mode.[/bold red]")
                return False

        # Step 3: Connect Playwright via CDP
        last_error = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                from playwright.sync_api import sync_playwright

                if self._playwright is None:
                    self._playwright = sync_playwright().start()

                self._browser = self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
                self._connected = True
                self._agent_page = None

                console.print(f"[bold green]✓ Connected to {self._browser_name} via CDP (port {CDP_PORT})[/bold green]")
                logger.info(f"Playwright connected to browser via CDP on port {CDP_PORT}")
                return True

            except Exception as e:
                last_error = e
                logger.warning(
                    "Playwright CDP connection attempt %s/%s failed: %s",
                    attempt,
                    CONNECT_RETRIES,
                    e,
                )
                time.sleep(0.6)

        logger.error(f"Playwright CDP connection failed after retries: {last_error}")
        console.print(f"[bold red]✗ CDP connection failed:[/bold red] {str(last_error)[:200]}")
        self._connected = False
        return False

    def _is_page_usable(self, page) -> bool:
        try:
            return page is not None and not page.is_closed()
        except Exception:
            return False

    def get_active_page(self):
        """Get the currently active (last focused) browser page/tab."""
        if not self.ensure_connected():
            return None

        # Prefer a sticky "agent-controlled" tab to avoid drifting across tabs.
        if self._is_page_usable(self._agent_page):
            return self._agent_page

        pages = self._context.pages
        if not pages:
            # No tabs open — create one
            self._agent_page = self._context.new_page()
            return self._agent_page

        # Prefer visible tabs (prevents acting on hidden/background startup tabs).
        for page in reversed(pages):
            try:
                visibility = page.evaluate("() => document.visibilityState")
                if visibility == "visible":
                    self._agent_page = page
                    return page
            except Exception:
                continue

        # Return the last page (most recently active)
        self._agent_page = pages[-1]
        return self._agent_page

    def set_agent_page(self, page):
        if page is None:
            self._agent_page = None
            return
        if self._is_page_usable(page):
            self._agent_page = page

    def get_all_pages(self) -> list:
        """Get all open pages/tabs."""
        if not self.ensure_connected():
            return []
        return self._context.pages

    def close(self):
        """Cleanly disconnect Playwright (does NOT close the browser)."""
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error during Playwright cleanup: {e}")
        finally:
            self._browser = None
            self._context = None
            self._playwright = None
            self._connected = False
            self._agent_page = None
            BrowserController._instance = None


# Register cleanup on exit
def _cleanup():
    if BrowserController._instance:
        BrowserController._instance.close()

atexit.register(_cleanup)


# ──────────────────────────────────────────
# High-Level Browser Primitives
# ──────────────────────────────────────────
# These are the functions that brain.py exposes as agent tools.

def _stabilize_page(page, wait_for_load: bool = True):
    """Best-effort tab stabilization to reduce startup focus glitches."""
    try:
        page.bring_to_front()
    except Exception:
        pass

    if wait_for_load:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

    try:
        focus_info = page.evaluate(
            """() => ({
                hasFocus: typeof document.hasFocus === 'function' ? document.hasFocus() : true,
                visibility: document.visibilityState || 'visible'
            })"""
        )
    except Exception:
        focus_info = {}

    if isinstance(focus_info, dict) and not focus_info.get("hasFocus", True):
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            page.wait_for_timeout(ACTION_SETTLE_MS)
        except Exception:
            pass


def _target_contains_text(target, expected_text: str) -> bool:
    """Verify typed content actually landed in the target element."""
    probe = (expected_text or "").strip().lower()
    if not probe:
        return True

    # Keep comparisons cheap and robust for long messages.
    probe = probe[:120]

    try:
        current = target.input_value(timeout=500) or ""
        if probe in current.lower():
            return True
    except Exception:
        pass

    try:
        current = target.evaluate(
            """(el) => {
                if (!el) return "";
                if (typeof el.value === "string") return el.value;
                return typeof el.textContent === "string" ? el.textContent : "";
            }"""
        ) or ""
        if probe in current.lower():
            return True
    except Exception:
        pass

    return False


def _normalize_url(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        return ""
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    return candidate


def _clean_host(host: str) -> str:
    host = (host or "").lower().strip()
    return host[4:] if host.startswith("www.") else host


def _url_matches_expected(actual_url: str, expected_url: str) -> bool:
    expected_norm = _normalize_url(expected_url)
    if not expected_norm:
        return True

    try:
        actual = urlparse(actual_url)
        expected = urlparse(expected_norm)
    except Exception:
        return False

    actual_host = _clean_host(actual.hostname or "")
    expected_host = _clean_host(expected.hostname or "")
    if expected_host:
        if actual_host != expected_host and not actual_host.endswith(f".{expected_host}"):
            return False

    expected_path = (expected.path or "").rstrip("/")
    actual_path = (actual.path or "").rstrip("/")
    if expected_path and expected_path != "/":
        if not actual_path.startswith(expected_path):
            return False

    return True


def _is_page_visible(page) -> bool:
    try:
        visibility = page.evaluate("() => document.visibilityState || 'visible'")
        return visibility == "visible"
    except Exception:
        return True

def browser_navigate(url: str) -> str:
    """Navigate the active tab to a URL. Waits for the page to load."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        _stabilize_page(page, wait_for_load=False)

        # Normalize URL
        url = _normalize_url(url)
        if not url:
            return "Navigation failed: URL is empty."

        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        _stabilize_page(page, wait_for_load=True)
        controller.set_agent_page(page)
        title = page.title()
        final_url = page.url

        if not _url_matches_expected(final_url, url):
            return (
                f"Navigation verification failed: requested '{url}' but active tab is '{final_url}'"
            )

        if not _is_page_visible(page):
            return (
                f"Navigation verification failed: tab loaded at '{final_url}' but is not visible"
            )

        logger.info(f"Navigated to: {final_url} (title: {title})")
        return f"Navigated to '{title}' ({final_url})"

    except Exception as e:
        logger.error(f"browser_navigate failed: {e}")
        return f"Navigation failed: {str(e)[:200]}"


def _looks_like_css_selector(selector: str) -> bool:
    """Heuristic: distinguish CSS selectors from plain visible text queries."""
    css_markers = ("#", ".", "[", "]", ">", ":", "=", "*", "+", "~", "(", ")")
    return any(marker in selector for marker in css_markers)


def _click_candidates(candidates: list[tuple[str, object]], max_per_candidate: int = 6) -> tuple[str, int]:
    """Try a sequence of locator candidates and click the first actionable match."""
    last_error = None
    for desc, locator in candidates:
        try:
            count = locator.count()
        except Exception as e:
            last_error = e
            continue

        if count == 0:
            continue

        for i in range(min(count, max_per_candidate)):
            target = locator.nth(i)
            try:
                target.wait_for(state="visible", timeout=1500)
                target.scroll_into_view_if_needed(timeout=1200)
                target.click(timeout=3500)
                return desc, i
            except Exception as click_error:
                last_error = click_error
                try:
                    target.click(timeout=2500, force=True)
                    return desc, i
                except Exception as force_error:
                    last_error = force_error
                    continue

    if last_error:
        raise last_error
    raise RuntimeError("No matching clickable element found.")


def _click_youtube_music_first_result(page, selector: str) -> bool:
    """Fallback for YouTube Music results where container rows are not directly clickable."""
    url = (page.url or "").lower()
    selector_lower = (selector or "").lower()
    if "music.youtube.com" not in url:
        return False
    if not any(token in selector_lower for token in (
        "result", "watch", "song", "ytmusic-card-shelf-renderer", "ytmusic-responsive-list-item-renderer"
    )):
        return False

    try:
        page.locator(
            "ytmusic-card-shelf-renderer a[href*='watch?v='], "
            "ytmusic-responsive-list-item-renderer a[href*='watch?v=']"
        ).first.wait_for(state="attached", timeout=10000)
    except Exception:
        return False

    click_targets = [
        (
            "ytmusic-top-result",
            page.locator("ytmusic-card-shelf-renderer a[href*='watch?v=']"),
        ),
        (
            "ytmusic-top-play-button",
            page.locator("ytmusic-card-shelf-renderer ytmusic-play-button-renderer"),
        ),
        (
            "ytmusic-top-play-button-button",
            page.locator("ytmusic-card-shelf-renderer ytmusic-play-button-renderer button"),
        ),
        (
            "ytmusic-song-list",
            page.locator("ytmusic-responsive-list-item-renderer a[href*='watch?v=']"),
        ),
        (
            "ytmusic-song-list-play-button",
            page.locator("ytmusic-responsive-list-item-renderer ytmusic-play-button-renderer button"),
        ),
        (
            "ytmusic-any-watch-link",
            page.locator("a[href*='watch?v=']"),
        ),
    ]

    try:
        _click_candidates(click_targets, max_per_candidate=3)
        return True
    except Exception:
        return False


def browser_click(selector: str) -> str:
    """Click an element by CSS selector or text content."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        selector = (selector or "").strip()
        if not selector:
            return "Click failed: selector is empty."

        _stabilize_page(page, wait_for_load=True)
        if not _is_page_visible(page):
            return "Click failed: active browser tab is not visible."

        # Pass 1: CSS locator strategy.
        last_error = None
        try:
            used_desc, used_index = _click_candidates([(f"css:{selector}", page.locator(selector))], max_per_candidate=20)
            logger.info(f"browser_click succeeded with {used_desc}[{used_index}]")
            return f"Clicked element: {selector}"
        except Exception as e:
            last_error = e

        # Site-specific fallback for YouTube Music result rows.
        if _click_youtube_music_first_result(page, selector):
            logger.info("browser_click succeeded via YouTube Music fallback")
            return f"Clicked element: {selector}"

        # Pass 2: Text/role strategy only if the selector looks like user-visible text.
        if not _looks_like_css_selector(selector):
            try:
                used_desc, used_index = _click_candidates(
                    [
                        (f"button:{selector}", page.get_by_role("button", name=selector, exact=False)),
                        (f"link:{selector}", page.get_by_role("link", name=selector, exact=False)),
                        (f"text:{selector}", page.get_by_text(selector, exact=False)),
                    ]
                )
                logger.info(f"browser_click succeeded with {used_desc}[{used_index}]")
                return f"Clicked element with text: '{selector}'"
            except Exception as e:
                last_error = e

        if last_error:
            raise last_error
        raise RuntimeError("No matching clickable element found.")

    except Exception as e:
        logger.error(f"browser_click failed: {e}")
        return f"Click failed for '{selector}': {str(e)[:200]}"


def _build_type_locators(page, selector: str) -> list[tuple[str, object]]:
    """
    Build candidate locators for typing, ordered from specific to broad.
    """
    selector_clean = (selector or "").strip()
    selector_lower = selector_clean.lower()
    candidates: list[tuple[str, object]] = []

    if selector_clean:
        candidates.append((f"css:{selector_clean}", page.locator(selector_clean)))
        candidates.append((f"placeholder:{selector_clean}", page.get_by_placeholder(selector_clean, exact=False)))
        candidates.append((f"label:{selector_clean}", page.get_by_label(selector_clean, exact=False)))
        candidates.append((f"textbox-name:{selector_clean}", page.get_by_role("textbox", name=selector_clean, exact=False)))
        candidates.append((f"searchbox-name:{selector_clean}", page.get_by_role("searchbox", name=selector_clean, exact=False)))

    # If the selector looks like a generic or search-related input, add broad fallbacks.
    generic_input_terms = {
        "input", "textbox", "text box", "text field", "field", "search", "search box", "searchbar"
    }
    if (
        selector_lower in generic_input_terms
        or "search" in selector_lower
        or selector_lower.startswith("input")
    ):
        candidates.append(("role=searchbox", page.get_by_role("searchbox")))
        candidates.append(("input[type='search']:visible", page.locator("input[type='search']:visible")))
        candidates.append(("input[aria-label*='Search' i]:visible", page.locator("input[aria-label*='Search' i]:visible")))
        candidates.append(("input[placeholder*='Search' i]:visible", page.locator("input[placeholder*='Search' i]:visible")))
        candidates.append(("input:visible", page.locator("input:visible")))

    return candidates


def _type_into_candidates(page, candidates: list[tuple[str, object]], text: str, clear_first: bool) -> tuple[str, int]:
    """
    Try typing into candidate locators and return the winning strategy.
    """
    last_error = None
    for desc, locator in candidates:
        try:
            count = locator.count()
        except Exception as e:
            last_error = e
            continue

        if count == 0:
            continue

        for i in range(min(count, 8)):
            target = locator.nth(i)
            try:
                target.wait_for(state="visible", timeout=1200)
                target.scroll_into_view_if_needed(timeout=1200)
                try:
                    target.click(timeout=1200)
                except Exception:
                    pass
                try:
                    target.focus(timeout=1200)
                except Exception:
                    pass

                typed = False
                if clear_first:
                    try:
                        target.fill(text, timeout=5000)
                        typed = True
                    except Exception as fill_error:
                        last_error = fill_error

                if not typed:
                    try:
                        if clear_first:
                            try:
                                target.press("Control+A", timeout=1200)
                            except Exception:
                                pass
                        target.type(text, timeout=7000, delay=TYPE_DELAY_MS)
                        typed = True
                    except Exception as type_error:
                        last_error = type_error

                # Last-resort DOM assignment when key events are flaky on startup.
                if not typed:
                    try:
                        dom_assigned = target.evaluate(
                            """(el, value) => {
                                if (!el) return false;
                                const canSetValue = typeof el.value === "string";
                                if (!canSetValue && !el.isContentEditable) return false;
                                if (canSetValue) {
                                    el.focus();
                                    el.value = value;
                                } else {
                                    el.focus();
                                    el.textContent = value;
                                }
                                el.dispatchEvent(new Event("input", { bubbles: true }));
                                el.dispatchEvent(new Event("change", { bubbles: true }));
                                return true;
                            }""",
                            text,
                        )
                        typed = bool(dom_assigned)
                    except Exception as dom_error:
                        last_error = dom_error

                if typed and _target_contains_text(target, text):
                    return desc, i

                # Give React/Vue controlled inputs one more moment before re-checking.
                page.wait_for_timeout(ACTION_SETTLE_MS)
                if typed and _target_contains_text(target, text):
                    return desc, i

                last_error = RuntimeError(f"Input did not retain typed text for candidate {desc}[{i}]")
            except Exception as e:
                last_error = e
                continue

    if last_error:
        raise last_error
    raise RuntimeError("No matching input field found for typing.")


def _reveal_youtube_music_search_if_needed(page, selector: str) -> bool:
    """
    YouTube Music often hides the actual search input until the search icon is opened.
    """
    url = (page.url or "").lower()
    selector_lower = (selector or "").lower()
    if "music.youtube.com" not in url:
        return False
    if not any(token in selector_lower for token in ("search", "input", "textbox")):
        return False

    button_selectors = [
        "button[aria-label='Search']",
        "button[aria-label*='Search' i]",
        "ytmusic-search-box button",
        "tp-yt-paper-icon-button[title*='Search' i]",
    ]

    for button_selector in button_selectors:
        try:
            button = page.locator(button_selector).first
            if button.is_visible(timeout=800):
                button.click(timeout=2000)
                return True
        except Exception:
            continue

    return False


def _type_youtube_music_via_keyboard(page, selector: str, text: str) -> bool:
    """
    Last-resort fallback for YouTube Music: use '/' to focus search and type.
    """
    url = (page.url or "").lower()
    selector_lower = (selector or "").lower()
    if "music.youtube.com" not in url:
        return False
    if not any(token in selector_lower for token in ("search", "input", "textbox")):
        return False

    try:
        page.keyboard.press("/")
        page.wait_for_timeout(250)
        page.keyboard.type(text)
        return True
    except Exception:
        return False


def browser_type(selector: str, text: str, clear_first: bool = True) -> str:
    """Type text into an input field identified by selector or placeholder text."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        selector = (selector or "").strip() or "input"
        _stabilize_page(page, wait_for_load=True)
        if not _is_page_visible(page):
            return "Type failed: active browser tab is not visible."

        # First pass: use normal locator strategy.
        first_pass_error = None
        try:
            used_desc, used_index = _type_into_candidates(page, _build_type_locators(page, selector), text, clear_first)
            logger.info(f"browser_type succeeded with {used_desc}[{used_index}]")
            controller.set_agent_page(page)
            return f"Typed '{text[:50]}...' into {selector}" if len(text) > 50 else f"Typed '{text}' into {selector}"
        except Exception as e:
            first_pass_error = e

        # Site-specific fallback: open YouTube Music search UI, then retry.
        revealed = _reveal_youtube_music_search_if_needed(page, selector)
        if revealed:
            try:
                used_desc, used_index = _type_into_candidates(page, _build_type_locators(page, selector), text, clear_first)
                logger.info(f"browser_type succeeded after search reveal with {used_desc}[{used_index}]")
                controller.set_agent_page(page)
                return f"Typed '{text[:50]}...' into {selector}" if len(text) > 50 else f"Typed '{text}' into {selector}"
            except Exception as e:
                first_pass_error = e

        # Final fallback for YouTube Music: shortcut focus + keyboard typing.
        if _type_youtube_music_via_keyboard(page, selector, text):
            logger.info("browser_type succeeded via YouTube Music keyboard fallback")
            return f"Typed '{text[:50]}...' into {selector}" if len(text) > 50 else f"Typed '{text}' into {selector}"

        if first_pass_error:
            raise first_pass_error
        raise RuntimeError("Typing failed: no candidate input accepted the text.")

    except Exception as e:
        logger.error(f"browser_type failed: {e}")
        return f"Type failed for '{selector}': {str(e)[:200]}"


def browser_press_key(key: str) -> str:
    """Press a keyboard key on the active page (e.g., 'Enter', 'Tab', 'Escape')."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        _stabilize_page(page, wait_for_load=False)
        page.keyboard.press(key)
        return f"Pressed key: {key}"

    except Exception as e:
        logger.error(f"browser_press_key failed: {e}")
        return f"Key press failed for '{key}': {str(e)[:200]}"


def browser_get_text() -> str:
    """Get the visible text content of the current page (truncated for context window)."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        title = page.title()
        url = page.url

        # Get inner text of body (visible text only)
        text = page.inner_text("body", timeout=5000)

        # Truncate to avoid blowing up the context window
        max_chars = 3000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... [truncated, {len(text)} total chars]"

        return f"Page: {title}\nURL: {url}\n\n{text}"

    except Exception as e:
        logger.error(f"browser_get_text failed: {e}")
        return f"Failed to get page text: {str(e)[:200]}"


def browser_get_state() -> str:
    """Get a summary of the current browser state: active tab URL/title, number of tabs."""
    try:
        controller = BrowserController.get_instance()
        if not controller.ensure_connected():
            return "Browser not connected."

        pages = controller.get_all_pages()
        active = controller.get_active_page()

        tabs_info = []
        for i, p in enumerate(pages):
            try:
                tabs_info.append(f"  [{i + 1}] {p.title()} — {p.url}")
            except Exception:
                tabs_info.append(f"  [{i + 1}] (unreachable)")

        active_title = active.title() if active else "N/A"
        active_url = active.url if active else "N/A"

        return (
            f"Active Tab: {active_title} ({active_url})\n"
            f"Open Tabs ({len(pages)}):\n" + "\n".join(tabs_info)
        )

    except Exception as e:
        logger.error(f"browser_get_state failed: {e}")
        return f"Failed to get browser state: {str(e)[:200]}"


def browser_new_tab(url: str = "") -> str:
    """Open a new tab, optionally navigating to a URL."""
    try:
        controller = BrowserController.get_instance()
        if not controller.ensure_connected():
            return "ERROR: Cannot connect to browser."

        page = controller._context.new_page()
        controller.set_agent_page(page)
        if url:
            url = _normalize_url(url)
            if not url:
                return "Failed to open new tab: URL is empty."
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if not _url_matches_expected(page.url, url):
                return f"Failed to open requested URL in new tab. Active URL: {page.url}"
            return f"Opened new tab: {page.title()} ({page.url})"
        return "Opened new blank tab."

    except Exception as e:
        logger.error(f"browser_new_tab failed: {e}")
        return f"Failed to open new tab: {str(e)[:200]}"


def browser_close_tab() -> str:
    """Close the currently active tab."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        title = page.title()
        page.close()
        controller.set_agent_page(None)
        return f"Closed tab: {title}"

    except Exception as e:
        logger.error(f"browser_close_tab failed: {e}")
        return f"Failed to close tab: {str(e)[:200]}"


def browser_wait_for(selector: str = "", text: str = "", timeout: int = 10) -> str:
    """Wait for an element or text to appear on the page."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        _stabilize_page(page, wait_for_load=True)

        timeout_ms = timeout * 1000

        if selector:
            locator = page.locator(selector)
            start = time.time()
            stable_presence_hits = 0
            last_count = 0

            while (time.time() - start) * 1000 < timeout_ms:
                try:
                    count = locator.count()
                except Exception:
                    count = 0

                last_count = count
                if count > 0:
                    stable_presence_hits += 1
                    visible_count = 0
                    for i in range(min(count, 8)):
                        try:
                            if locator.nth(i).is_visible(timeout=120):
                                visible_count += 1
                        except Exception:
                            continue

                    if visible_count > 0:
                        return f"Element '{selector}' appeared ({count} match(es), {visible_count} visible)."

                    # Some sites render list items in ways where the first nodes are hidden.
                    # If presence is stable for a short period, treat it as ready.
                    if stable_presence_hits >= 3:
                        return f"Element '{selector}' present ({count} match(es)); proceeding."
                else:
                    stable_presence_hits = 0

                page.wait_for_timeout(200)

            raise TimeoutError(f"Selector '{selector}' not ready after {timeout}s (last_count={last_count}).")
        elif text:
            locator = page.get_by_text(text, exact=False)
            start = time.time()
            stable_presence_hits = 0
            last_count = 0

            while (time.time() - start) * 1000 < timeout_ms:
                try:
                    count = locator.count()
                except Exception:
                    count = 0

                last_count = count
                if count > 0:
                    stable_presence_hits += 1
                    visible_count = 0
                    for i in range(min(count, 8)):
                        try:
                            if locator.nth(i).is_visible(timeout=120):
                                visible_count += 1
                        except Exception:
                            continue

                    if visible_count > 0:
                        return f"Text '{text}' appeared on page ({visible_count} visible match(es))."

                    if stable_presence_hits >= 3:
                        return f"Text '{text}' present on page ({count} match(es)); proceeding."
                else:
                    stable_presence_hits = 0

                page.wait_for_timeout(200)

            raise TimeoutError(f"Text '{text}' not ready after {timeout}s (last_count={last_count}).")
        else:
            return "No selector or text provided to wait for."

    except Exception as e:
        logger.error(f"browser_wait_for failed: {e}")
        return f"Wait timed out: {str(e)[:200]}"


def browser_scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll the page up or down."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        _stabilize_page(page, wait_for_load=False)

        pixels = amount * 300  # ~300px per "scroll tick"
        if direction == "up":
            pixels = -pixels

        page.mouse.wheel(0, pixels)
        return f"Scrolled {direction} by {abs(pixels)}px"

    except Exception as e:
        logger.error(f"browser_scroll failed: {e}")
        return f"Scroll failed: {str(e)[:200]}"


def browser_screenshot() -> str:
    """Take a screenshot of the current page and return it as base64 PNG."""
    try:
        import base64
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        screenshot_bytes = page.screenshot(full_page=False)
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Also save a debug copy
        debug_path = Path(__file__).parent.parent / "debug_browser_screenshot.png"
        with open(debug_path, "wb") as f:
            f.write(screenshot_bytes)

        return b64

    except Exception as e:
        logger.error(f"browser_screenshot failed: {e}")
        return f"Screenshot failed: {str(e)[:200]}"
