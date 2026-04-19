"""
Windows UI Automation wrapper using pywinauto.

Provides semantic element targeting — click, type, and read UI elements by name
instead of raw pixel coordinates. This is the secret weapon that makes the agent
robust against UI layout changes, resolution shifts, and dark mode toggles.

Fallback chain: pywinauto → vision + coordinates
"""
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger("mira.accessibility")


@dataclass
class UIElement:
    """Lightweight representation of a UI element."""
    name: str
    control_type: str
    value: str = ""
    rect: dict = None  # {"left": x, "top": y, "right": x2, "bottom": y2}
    is_enabled: bool = True
    is_focused: bool = False

    @property
    def center(self) -> tuple[int, int] | None:
        if self.rect:
            cx = (self.rect["left"] + self.rect["right"]) // 2
            cy = (self.rect["top"] + self.rect["bottom"]) // 2
            return (cx, cy)
        return None


class UIAutomation:
    """
    Windows UI Automation wrapper.
    
    Uses pywinauto to interact with UI elements by name/type instead of
    coordinates. Gracefully degrades if pywinauto is unavailable.
    """

    def __init__(self):
        self._available = False
        self._app = None
        try:
            from pywinauto import Desktop
            self._desktop = Desktop(backend="uia")
            self._available = True
            logger.info("UIAutomation initialized (pywinauto available)")
        except ImportError:
            logger.warning("pywinauto not installed — accessibility features disabled")
        except Exception as e:
            logger.warning(f"UIAutomation init failed: {e}")

    @property
    def available(self) -> bool:
        return self._available

    def _get_foreground_window(self):
        """Get the foreground window wrapper."""
        if not self._available:
            return None
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            windows = desktop.windows()
            # Find the foreground window
            for w in windows:
                try:
                    if w.is_active():
                        return w
                except:
                    continue
            # Fallback: return the first window with a title
            for w in windows:
                try:
                    if w.window_text():
                        return w
                except:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get foreground window: {e}")
        return None

    def get_focused_element(self) -> UIElement | None:
        """
        Get the currently focused UI element.
        Returns: UIElement with name, type, value, and position.
        """
        if not self._available:
            return None

        try:
            from pywinauto import Desktop
            from pywinauto.uia_defines import IUIA

            # Use UIA to get the focused element directly
            import comtypes.client
            iuia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",
                interface=IUIA().ui_automation_client.IUIAutomation,
            )
            focused = iuia.GetFocusedElement()

            if focused:
                name = focused.CurrentName or ""
                ctrl_type = str(focused.CurrentControlType)
                
                # Try to get the value
                value = ""
                try:
                    from pywinauto.uia_defines import IUIA as UIA
                    value = focused.GetCurrentPropertyValue(30045) or ""  # UIA_ValueValuePropertyId
                except:
                    pass

                rect_raw = focused.CurrentBoundingRectangle
                rect = {
                    "left": rect_raw.left,
                    "top": rect_raw.top, 
                    "right": rect_raw.right,
                    "bottom": rect_raw.bottom,
                } if rect_raw else None

                return UIElement(
                    name=name,
                    control_type=ctrl_type,
                    value=str(value),
                    rect=rect,
                    is_focused=True,
                )
        except Exception as e:
            logger.debug(f"get_focused_element failed: {e}")

        # Fallback: use pywinauto's simpler approach
        try:
            window = self._get_foreground_window()
            if window:
                # Walk the control tree looking for focused element
                for ctrl in window.descendants():
                    try:
                        if ctrl.has_keyboard_focus():
                            rect_obj = ctrl.rectangle()
                            return UIElement(
                                name=ctrl.window_text() or ctrl.friendly_class_name(),
                                control_type=ctrl.friendly_class_name(),
                                value=getattr(ctrl, 'window_text', lambda: '')() or "",
                                rect={
                                    "left": rect_obj.left,
                                    "top": rect_obj.top,
                                    "right": rect_obj.right,
                                    "bottom": rect_obj.bottom,
                                },
                                is_focused=True,
                            )
                    except:
                        continue
        except Exception as e:
            logger.debug(f"Fallback get_focused_element failed: {e}")

        return None

    def find_element(self, name: str, control_type: str = None, timeout: float = 3.0) -> UIElement | None:
        """
        Find a UI element by name (and optionally control type) in the active window.
        
        Args:
            name: Text to search for (button label, input name, etc.)
            control_type: Optional filter like "Button", "Edit", "Text"
            timeout: Max seconds to search
            
        Returns:
            UIElement if found, None otherwise.
        """
        if not self._available:
            return None

        try:
            window = self._get_foreground_window()
            if not window:
                return None

            # Build search criteria
            search = {"title_re": f".*{name}.*", "enabled_only": True}
            if control_type:
                search["control_type"] = control_type

            # Try to find with timeout
            start = time.time()
            while time.time() - start < timeout:
                try:
                    ctrl = window.child_window(**search)
                    if ctrl.exists(timeout=0.5):
                        rect_obj = ctrl.rectangle()
                        return UIElement(
                            name=ctrl.window_text() or name,
                            control_type=ctrl.friendly_class_name(),
                            value="",
                            rect={
                                "left": rect_obj.left,
                                "top": rect_obj.top,
                                "right": rect_obj.right,
                                "bottom": rect_obj.bottom,
                            },
                            is_enabled=ctrl.is_enabled(),
                        )
                except Exception:
                    time.sleep(0.3)
                    continue

        except Exception as e:
            logger.debug(f"find_element('{name}') failed: {e}")

        return None

    def click_element(self, name: str, control_type: str = None) -> str:
        """
        Click a named element. No coordinates needed.
        
        Args:
            name: The element label/text to click (e.g., "Search", "Send")
            control_type: Optional type filter
            
        Returns:
            Status string describing what happened.
        """
        if not self._available:
            return f"UIAutomation unavailable — cannot click '{name}'"

        try:
            window = self._get_foreground_window()
            if not window:
                return f"No active window found — cannot click '{name}'"

            search = {"title_re": f".*{name}.*", "enabled_only": True}
            if control_type:
                search["control_type"] = control_type

            ctrl = window.child_window(**search)
            if ctrl.exists(timeout=2):
                ctrl.click_input()
                rect_obj = ctrl.rectangle()
                cx = (rect_obj.left + rect_obj.right) // 2
                cy = (rect_obj.top + rect_obj.bottom) // 2
                return f"Clicked '{name}' at ({cx}, {cy}) via accessibility"

            return f"Element '{name}' not found in active window"

        except Exception as e:
            return f"Failed to click '{name}' via accessibility: {str(e)}"

    def type_into_element(self, name: str, text: str) -> str:
        """
        Find an element by name, focus it, and type into it.
        
        Args:
            name: The element label to target
            text: Text to type
            
        Returns:
            Status string.
        """
        if not self._available:
            return f"UIAutomation unavailable — cannot type into '{name}'"

        try:
            window = self._get_foreground_window()
            if not window:
                return f"No active window found"

            ctrl = window.child_window(title_re=f".*{name}.*", enabled_only=True)
            if ctrl.exists(timeout=2):
                ctrl.set_focus()
                time.sleep(0.1)
                ctrl.type_keys(text, with_spaces=True, pause=0.05)
                return f"Typed '{text}' into '{name}' via accessibility"

            return f"Element '{name}' not found"

        except Exception as e:
            return f"Failed to type into '{name}': {str(e)}"

    def get_element_tree(self, depth: int = 2) -> list[dict]:
        """
        Shallow dump of the UI tree for the active window.
        Useful for debugging and understanding what elements are available.
        
        Args:
            depth: How deep to traverse (default 2 levels)
            
        Returns:
            List of element dicts with name, type, and position.
        """
        if not self._available:
            return []

        elements = []
        try:
            window = self._get_foreground_window()
            if not window:
                return []

            def _walk(ctrl, current_depth):
                if current_depth > depth:
                    return
                try:
                    name = ctrl.window_text() or ""
                    ctrl_type = ctrl.friendly_class_name()
                    if name or ctrl_type not in ("", "Pane", "Custom"):
                        rect_obj = ctrl.rectangle()
                        elements.append({
                            "name": name[:50],  # Truncate long names
                            "type": ctrl_type,
                            "depth": current_depth,
                            "rect": {
                                "left": rect_obj.left,
                                "top": rect_obj.top,
                                "right": rect_obj.right,
                                "bottom": rect_obj.bottom,
                            },
                        })
                except:
                    pass

                try:
                    for child in ctrl.children():
                        _walk(child, current_depth + 1)
                except:
                    pass

            _walk(window, 0)
        except Exception as e:
            logger.debug(f"get_element_tree failed: {e}")

        return elements[:50]  # Cap at 50 elements


# Module-level singleton
_ui_automation = None

def get_ui_automation() -> UIAutomation:
    """Get or create the module-level UIAutomation instance."""
    global _ui_automation
    if _ui_automation is None:
        _ui_automation = UIAutomation()
    return _ui_automation
