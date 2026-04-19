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

logger = logging.getLogger("mira.browser")

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────
CDP_PORT = 9222
STARTUP_TIMEOUT = 15  # seconds to wait for browser to start in debug mode
SETUP_FLAG_FILE = Path(__file__).parent.parent / ".playwright_setup_done"

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
            console.print(f"[bold red]✗ Failed to launch {self._browser_name} in debug mode.[/bold red]")
            return False

        # Step 3: Connect Playwright via CDP
        try:
            from playwright.sync_api import sync_playwright

            if self._playwright is None:
                self._playwright = sync_playwright().start()

            self._browser = self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            self._connected = True

            console.print(f"[bold green]✓ Connected to {self._browser_name} via CDP (port {CDP_PORT})[/bold green]")
            logger.info(f"Playwright connected to browser via CDP on port {CDP_PORT}")
            return True

        except Exception as e:
            logger.error(f"Playwright CDP connection failed: {e}")
            console.print(f"[bold red]✗ CDP connection failed:[/bold red] {str(e)[:200]}")
            self._connected = False
            return False

    def get_active_page(self):
        """Get the currently active (last focused) browser page/tab."""
        if not self.ensure_connected():
            return None

        pages = self._context.pages
        if not pages:
            # No tabs open — create one
            return self._context.new_page()

        # Return the last page (most recently active)
        return pages[-1]

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

def browser_navigate(url: str) -> str:
    """Navigate the active tab to a URL. Waits for the page to load."""
    try:
        controller = BrowserController.get_instance()
        page = controller.get_active_page()
        if not page:
            return "ERROR: Cannot connect to browser."

        # Normalize URL
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        title = page.title()
        final_url = page.url
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

        page.wait_for_load_state("domcontentloaded", timeout=10000)

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


def _type_into_candidates(candidates: list[tuple[str, object]], text: str, clear_first: bool) -> tuple[str, int]:
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
                if clear_first:
                    target.fill(text, timeout=5000)
                else:
                    target.type(text, timeout=5000)
                return desc, i
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
        page.wait_for_load_state("domcontentloaded", timeout=10000)

        # First pass: use normal locator strategy.
        first_pass_error = None
        try:
            used_desc, used_index = _type_into_candidates(_build_type_locators(page, selector), text, clear_first)
            logger.info(f"browser_type succeeded with {used_desc}[{used_index}]")
            return f"Typed '{text[:50]}...' into {selector}" if len(text) > 50 else f"Typed '{text}' into {selector}"
        except Exception as e:
            first_pass_error = e

        # Site-specific fallback: open YouTube Music search UI, then retry.
        revealed = _reveal_youtube_music_search_if_needed(page, selector)
        if revealed:
            try:
                used_desc, used_index = _type_into_candidates(_build_type_locators(page, selector), text, clear_first)
                logger.info(f"browser_type succeeded after search reveal with {used_desc}[{used_index}]")
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
        if url:
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
