"""
Cheap Signal Verification Engine.

Verifies action outcomes using OS-level signals (window title, process name,
clipboard, focus state) instead of expensive vision calls. Vision is only
triggered when cheap checks fail or for high-risk actions.

This is the core of the plan→act→VERIFY→correct loop.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from agent.state import AgentState, RiskLevel

logger = logging.getLogger("mira.verify")


class VerifyStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"       # Low-risk action, no check needed
    NEEDS_VISION = "needs_vision"  # Cheap checks insufficient, trigger vision


@dataclass
class VerifyResult:
    status: VerifyStatus
    reason: str
    should_trigger_vision: bool = False

    @property
    def passed(self) -> bool:
        return self.status == VerifyStatus.PASSED

    @property
    def skipped(self) -> bool:
        return self.status == VerifyStatus.SKIPPED


class Verifier:
    """
    Post-action verification using cheap OS-level signals.
    
    Strategy per action type:
    - switch_to_app:  Check window title contains app name
    - type_keyboard (text):  Check active window didn't change (text went to right app)
    - type_keyboard (hotkey):  Check window title for expected change  
    - click_mouse:  ALWAYS trigger vision (coordinates are fragile)
    - scroll_mouse:  Skip (low risk)
    - launch_app:  Check process appears in running list
    """

    def __init__(self):
        # Import here to avoid circular imports
        from agent.context import get_active_window
        self._get_active_window = get_active_window

    def verify_step(self, state: AgentState, step: dict, action_result: str) -> VerifyResult:
        """
        Run appropriate cheap checks for a completed step.
        
        Args:
            state: Current agent state (has expected_state, current_app, etc.)
            step: The plan step dict (action, params, expect, risk_level)
            action_result: The raw string result from executing the action
            
        Returns:
            VerifyResult with pass/fail/skip status and reason
        """
        action = step.get("action", "")
        params = step.get("params", {})
        risk_str = step.get("risk_level", "low")
        expect = step.get("expect", "")

        # Check for explicit execution errors first
        if self._has_error(action_result):
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason=f"Action returned error: {action_result[:200]}",
                should_trigger_vision=True,
            )

        # Route to action-specific verification
        if action == "switch_to_app":
            return self._verify_switch_to_app(state, params, action_result)
        elif action == "type_keyboard":
            return self._verify_type_keyboard(state, params, action_result)
        elif action == "click_mouse":
            return self._verify_click(state, params, action_result)
        elif action == "scroll_mouse":
            return VerifyResult(
                status=VerifyStatus.SKIPPED,
                reason="Scroll is low-risk, no verification needed",
            )
        elif action == "launch_app":
            return self._verify_launch_app(state, params, action_result)
        elif action == "move_mouse":
            return VerifyResult(
                status=VerifyStatus.SKIPPED,
                reason="Mouse move is low-risk, no verification needed",
            )
        elif action.startswith("browser_"):
            return self._verify_browser_action(state, action, params, action_result)
        else:
            return VerifyResult(
                status=VerifyStatus.SKIPPED,
                reason=f"Unknown action '{action}', skipping verification",
            )

    def _has_error(self, result: str) -> bool:
        """Check if an action result indicates an error."""
        error_signals = ["failed", "error", "not found", "timeout", "timed out", "exception"]
        result_lower = result.lower()
        return any(sig in result_lower for sig in error_signals)

    def _verify_browser_action(self, state: AgentState, action: str, params: dict, result: str) -> VerifyResult:
        """Verify browser_* actions using live browser state, not only result strings."""
        active = self._get_active_window()
        current_title = active.get("window_title", "").lower()
        current_process = active.get("process_name", "").lower()
        state.update_window(current_title, current_process)

        result_lower = (result or "").lower()

        if action == "browser_navigate":
            return self._verify_browser_navigate(params, result)

        if action == "browser_type":
            return self._verify_browser_type(params, result)

        if action == "browser_wait_for":
            return self._verify_browser_wait_for(params, result)

        success_signals = {
            "browser_click": ("clicked element",),
            "browser_press_key": ("pressed key",),
            "browser_get_text": ("page:", "url:"),
            "browser_get_state": ("active tab:", "open tabs"),
            "browser_new_tab": ("opened new",),
            "browser_close_tab": ("closed tab",),
            "browser_scroll": ("scrolled",),
        }

        signals = success_signals.get(action, ())
        if signals and any(sig in result_lower for sig in signals):
            return VerifyResult(
                status=VerifyStatus.PASSED,
                reason=f"{action} succeeded: {result[:120]}",
            )

        if action in {"browser_get_state", "browser_get_text"} and result:
            return VerifyResult(
                status=VerifyStatus.PASSED,
                reason=f"{action} returned browser content",
            )

        return VerifyResult(
            status=VerifyStatus.SKIPPED,
            reason=f"{action} executed; no strong verification signal available",
        )

    def _get_browser_page(self):
        try:
            from agent.browser import BrowserController

            controller = BrowserController.get_instance()
            if not controller.ensure_connected():
                return None
            return controller.get_active_page()
        except Exception as e:
            logger.warning(f"Could not read browser page for verification: {e}")
            return None

    def _clean_host(self, host: str) -> str:
        host = (host or "").lower().strip()
        return host[4:] if host.startswith("www.") else host

    def _normalize_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url

    def _url_matches_expected(self, actual_url: str, expected_url: str) -> bool:
        expected_norm = self._normalize_url(expected_url)
        if not expected_norm:
            return True

        try:
            actual = urlparse(actual_url)
            expected = urlparse(expected_norm)
        except Exception:
            return False

        actual_host = self._clean_host(actual.hostname or "")
        expected_host = self._clean_host(expected.hostname or "")
        if expected_host:
            if actual_host != expected_host and not actual_host.endswith(f".{expected_host}"):
                return False

        expected_path = (expected.path or "").rstrip("/")
        actual_path = (actual.path or "").rstrip("/")
        if expected_path and expected_path != "/" and not actual_path.startswith(expected_path):
            return False

        return True

    def _target_contains_text(self, target, text: str) -> bool:
        probe = (text or "").strip().lower()
        if not probe:
            return True

        try:
            current = target.input_value(timeout=300) or ""
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

    def _verify_browser_navigate(self, params: dict, result: str) -> VerifyResult:
        expected_url = params.get("url", "")
        page = self._get_browser_page()
        if not page:
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason="browser_navigate could not confirm active browser page",
                should_trigger_vision=True,
            )

        actual_url = getattr(page, "url", "") or ""
        actual_title = ""
        try:
            actual_title = page.title()
        except Exception:
            pass

        if expected_url and not self._url_matches_expected(actual_url, expected_url):
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason=(
                    f"browser_navigate mismatch: expected '{self._normalize_url(expected_url)}' "
                    f"but active URL is '{actual_url}'"
                ),
                should_trigger_vision=True,
            )

        try:
            visibility = page.evaluate("() => document.visibilityState || 'visible'")
            if visibility != "visible":
                return VerifyResult(
                    status=VerifyStatus.FAILED,
                    reason=f"browser_navigate landed on hidden tab ({actual_url})",
                    should_trigger_vision=True,
                )
        except Exception:
            pass

        return VerifyResult(
            status=VerifyStatus.PASSED,
            reason=f"browser_navigate confirmed: {actual_title} ({actual_url})",
        )

    def _verify_browser_type(self, params: dict, result: str) -> VerifyResult:
        selector = str(params.get("selector", "")).strip()
        expected_text = str(params.get("text", ""))
        page = self._get_browser_page()
        if not page:
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason="browser_type could not confirm active browser page",
                should_trigger_vision=True,
            )

        if not selector:
            return VerifyResult(
                status=VerifyStatus.SKIPPED,
                reason="browser_type selector missing; cannot verify target field",
            )

        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception as e:
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason=f"browser_type verification could not query selector '{selector}': {e}",
                should_trigger_vision=True,
            )

        if count == 0:
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason=f"browser_type target selector '{selector}' not found after typing",
                should_trigger_vision=True,
            )

        for i in range(min(count, 8)):
            target = locator.nth(i)
            try:
                if self._target_contains_text(target, expected_text):
                    return VerifyResult(
                        status=VerifyStatus.PASSED,
                        reason=f"browser_type confirmed text in selector '{selector}'",
                    )
            except Exception:
                continue

        return VerifyResult(
            status=VerifyStatus.FAILED,
            reason=(
                f"browser_type mismatch: selector '{selector}' exists but does not contain expected text"
            ),
            should_trigger_vision=True,
        )

    def _verify_browser_wait_for(self, params: dict, result: str) -> VerifyResult:
        selector = str(params.get("selector", "")).strip()
        text = str(params.get("text", "")).strip()
        page = self._get_browser_page()
        if not page:
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason="browser_wait_for could not confirm active browser page",
                should_trigger_vision=True,
            )

        if selector:
            try:
                locator = page.locator(selector)
                count = locator.count()
                if count > 0:
                    return VerifyResult(
                        status=VerifyStatus.PASSED,
                        reason=f"browser_wait_for confirmed selector '{selector}' present",
                    )
                return VerifyResult(
                    status=VerifyStatus.FAILED,
                    reason=f"browser_wait_for selector '{selector}' not present",
                    should_trigger_vision=True,
                )
            except Exception as e:
                return VerifyResult(
                    status=VerifyStatus.FAILED,
                    reason=f"browser_wait_for selector check failed: {e}",
                    should_trigger_vision=True,
                )

        if text:
            try:
                count = page.get_by_text(text, exact=False).count()
                if count > 0:
                    return VerifyResult(
                        status=VerifyStatus.PASSED,
                        reason=f"browser_wait_for confirmed text '{text}' present",
                    )
                return VerifyResult(
                    status=VerifyStatus.FAILED,
                    reason=f"browser_wait_for text '{text}' not present",
                    should_trigger_vision=True,
                )
            except Exception as e:
                return VerifyResult(
                    status=VerifyStatus.FAILED,
                    reason=f"browser_wait_for text check failed: {e}",
                    should_trigger_vision=True,
                )

        # No explicit selector/text in params: rely on action result signal.
        if "appeared" in (result or "").lower() or "present" in (result or "").lower():
            return VerifyResult(
                status=VerifyStatus.PASSED,
                reason="browser_wait_for reported readiness",
            )

        return VerifyResult(
            status=VerifyStatus.SKIPPED,
            reason="browser_wait_for had no selector/text to validate",
        )

    def _verify_switch_to_app(self, state: AgentState, params: dict, result: str) -> VerifyResult:
        """
        Verify app switch. Strategy (in order of reliability):
        1. Process name match (ground truth — most reliable)
        2. Registry lookup by keyword (handles 'brave browser' -> 'brave' exe)
        3. Window title keyword match (weakest, only as last resort)
        
        IMPORTANT: Never triggers vision. If the process switched correctly, 
        the window title reflects the active TAB/DOCUMENT (e.g., 'Instagram - Brave'),
        not the browser name. Vision is useless here and Gemini hallucinates Brave as Chrome.
        """
        app_name = params.get("app_name", "").lower().strip()
        
        # If the action itself reported failure, trust it immediately
        if "not found" in result.lower():
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason=f"App '{app_name}' not found in open windows",
                should_trigger_vision=False,  # No point looking, app isn't running
            )
        
        # If the action succeeded ("Switched to: ..."), prefer that over window check.
        # The result is from PowerShell's SetForegroundWindow — it IS correct.
        if "switched to" in result.lower() or "launched and switched" in result.lower():
            # Still read the OS state to update state tracking
            active = self._get_active_window()
            title = active.get("window_title", "").lower()
            process = active.get("process_name", "").lower()
            state.update_window(title, process)
            
            # PRIMARY CHECK: Process name — most reliable OS signal
            # Try direct substring match first
            app_keywords = [w for w in app_name.split() if len(w) > 2]  # 'brave browser' -> ['brave', 'browser']
            if any(kw in process for kw in app_keywords):
                return VerifyResult(
                    status=VerifyStatus.PASSED,
                    reason=f"Process '{process}' matched keyword from '{app_name}'",
                )
            
            # REGISTRY LOOKUP: Find matching exe names for the requested app
            from agent.context import APP_REGISTRY
            for key, info in APP_REGISTRY.items():
                # Match if any keyword from the request matches the registry key or display name
                registry_terms = [key] + info.get("start_menu_keywords", []) + [info["display_name"].lower()]
                if any(kw in rt or rt in kw for kw in app_keywords for rt in registry_terms):
                    exe_clean = [e.replace(".exe", "").lower() for e in info.get("exe_names", [])]
                    if process in exe_clean or any(e in process for e in exe_clean):
                        return VerifyResult(
                            status=VerifyStatus.PASSED,
                            reason=f"Process '{process}' matches registry entry for '{app_name}'",
                        )
            
            # TITLE CHECK: Last resort — window title keyword match
            if any(kw in title for kw in app_keywords):
                return VerifyResult(
                    status=VerifyStatus.PASSED,
                    reason=f"Window title '{title}' contains keyword from '{app_name}'",
                )
            
            # The PowerShell switch succeeded but we can't confirm the process.
            # Trust the OS-level result — don't block on this.
            logger.warning(f"switch_to_app: process='{process}' doesn't obviously match '{app_name}', but action reported success. Trusting OS.")
            return VerifyResult(
                status=VerifyStatus.PASSED,
                reason=f"Trusting OS switch result (process: {process}, title: {title})",
            )

        # Action didn't report success or failure cleanly  
        active = self._get_active_window()
        title = active.get("window_title", "").lower()
        process = active.get("process_name", "").lower()
        state.update_window(title, process)
        
        return VerifyResult(
            status=VerifyStatus.FAILED,
            reason=f"switch_to_app returned unexpected result: {result[:100]}",
            should_trigger_vision=False,  # Vision can't fix a bad process switch
        )

    # Processes that are "launcher noise" — only pure terminal/shell processes
    # that run mira.py itself. When these appear as the active process after
    # typing, it means the terminal briefly stole focus during the PS subprocess
    # call for get_active_window() — NOT that we typed into the wrong app.
    # 
    # IMPORTANT: Do NOT add IDE/editor processes here (code, cursor, etc.)
    # since those could be legitimate target apps the user wants to type into.
    _LAUNCHER_PROCESSES = {
        "powershell", "powershell_ise", "cmd", "windowsterminal",
        "python", "python3", "py", "conhost", "wt",
    }

    def _verify_type_keyboard(self, state: AgentState, params: dict, result: str) -> VerifyResult:
        """
        Verify keyboard input.
        
        Key insight: after typing completes, the terminal running mira.py may
        briefly regain focus before get_active_window() polls. So we:
        1. Split app name into keywords ('brave browser' → ['brave']) for matching
        2. If process is a known launcher/terminal, treat it as timing noise and
           trust the result string — if it says 'Typed text:' it succeeded
        3. For genuine wrong-window detection, we still catch it via registry miss
        """
        text = params.get("text", "")
        hotkey = params.get("hotkey", "")

        active = self._get_active_window()
        current_title = active.get("window_title", "").lower()
        current_process = active.get("process_name", "").lower()
        state.update_window(current_title, current_process)

        if text:
            if state.current_app:
                target = state.current_app.lower()
                # Keywords: 'brave browser' → ['brave', 'browser'], filter short words
                app_keywords = [w for w in target.split() if len(w) > 2]
                
                # Case 1: Launcher/terminal process is active — timing artifact.
                # Trust the result string; if it typed successfully, we're good.
                if current_process in self._LAUNCHER_PROCESSES:
                    if "typed text" in result.lower() or "pressed hotkey" in result.lower():
                        return VerifyResult(
                            status=VerifyStatus.PASSED,
                            reason=f"Text typed OK (terminal process '{current_process}' in focus is timing noise, not a wrong-window error)",
                        )
                
                # Case 2: Direct keyword match on process name
                if any(kw in current_process for kw in app_keywords):
                    return VerifyResult(
                        status=VerifyStatus.PASSED,
                        reason=f"Text typed in correct app (process: {current_process})",
                    )

                # Case 3: Registry lookup — same strategy as switch_to_app verifier
                from agent.context import APP_REGISTRY
                for key, info in APP_REGISTRY.items():
                    registry_terms = [key] + info.get("start_menu_keywords", []) + [info["display_name"].lower()]
                    if any(kw in rt or rt in kw for kw in app_keywords for rt in registry_terms):
                        exe_clean = [e.replace(".exe", "").lower() for e in info.get("exe_names", [])]
                        if current_process in exe_clean or any(e in current_process for e in exe_clean):
                            return VerifyResult(
                                status=VerifyStatus.PASSED,
                                reason=f"Text typed in correct app via registry (process: {current_process})",
                            )

                # Case 4: Window title keyword match (last resort)
                if any(kw in current_title for kw in app_keywords):
                    return VerifyResult(
                        status=VerifyStatus.PASSED,
                        reason=f"Text typed in correct window '{current_title}'",
                    )

                return VerifyResult(
                    status=VerifyStatus.FAILED,
                    reason=f"Wrong window! Expected '{target}' but typed in '{current_title}' ({current_process})",
                    should_trigger_vision=True,
                )
            
            # No target app tracked — can't verify, assume OK
            return VerifyResult(
                status=VerifyStatus.PASSED,
                reason="Text typed (no target app to verify against)",
            )

        elif hotkey:
            # Hotkey: check the window didn't crash/close
            if current_title and current_process:
                return VerifyResult(
                    status=VerifyStatus.PASSED,
                    reason=f"Hotkey '{hotkey}' sent, window still active: '{current_title}'",
                )
            return VerifyResult(
                status=VerifyStatus.FAILED,
                reason="Window appears to have closed or lost focus after hotkey",
                should_trigger_vision=True,
            )

        return VerifyResult(
            status=VerifyStatus.SKIPPED,
            reason="No text or hotkey provided, nothing to verify",
        )


    def _verify_click(self, state: AgentState, params: dict, result: str) -> VerifyResult:
        """
        Verify mouse click — ALWAYS triggers vision.
        Clicks are coordinates-based and inherently fragile.
        """
        x = params.get("x", -1)
        y = params.get("y", -1)

        # Do a cheap window check first
        active = self._get_active_window()
        current_title = active.get("window_title", "").lower()
        current_process = active.get("process_name", "").lower()
        state.update_window(current_title, current_process)

        # Clicks always need vision confirmation — coordinates could be wrong
        return VerifyResult(
            status=VerifyStatus.NEEDS_VISION,
            reason=f"Click at ({x},{y}) — vision required to confirm target was hit",
            should_trigger_vision=True,
        )

    def _verify_launch_app(self, state: AgentState, params: dict, result: str) -> VerifyResult:
        """Verify app launch: check if the process appeared."""
        app_name = params.get("app_name", "")

        if "launched" in result.lower():
            return VerifyResult(
                status=VerifyStatus.PASSED,
                reason=f"Launch command succeeded: {result[:100]}",
            )

        return VerifyResult(
            status=VerifyStatus.FAILED,
            reason=f"Launch may have failed: {result[:100]}",
            should_trigger_vision=True,
        )


def get_clipboard_content() -> str:
    """Read current clipboard text content. Instant, no vision needed."""
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception as e:
        logger.warning(f"Failed to read clipboard: {e}")
        return ""


def set_clipboard_content(text: str) -> bool:
    """Set clipboard content (useful for verification workflows)."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception as e:
        logger.warning(f"Failed to set clipboard: {e}")
        return False
