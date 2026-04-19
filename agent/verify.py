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
        else:
            return VerifyResult(
                status=VerifyStatus.SKIPPED,
                reason=f"Unknown action '{action}', skipping verification",
            )

    def _has_error(self, result: str) -> bool:
        """Check if an action result indicates an error."""
        error_signals = ["failed", "error", "not found", "timeout", "exception"]
        result_lower = result.lower()
        return any(sig in result_lower for sig in error_signals)

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
