import os
import json
import time
import logging
from openai import OpenAI
from pathlib import Path

from agent.primitives import (
    vision,
    move_mouse,
    click_mouse,
    scroll_mouse,
    type_keyboard,
    run_command,
    analyze_screenshot,
    switch_to_app,
    launch_app,
)
from agent.browser import (
    browser_navigate, browser_click, browser_type, browser_press_key,
    browser_get_text, browser_get_state, browser_new_tab, browser_close_tab,
    browser_wait_for, browser_scroll, browser_screenshot
)
from agent.context import build_context_snapshot, check_app_installed
from agent.display import console, print_tool_call, print_tool_result, print_final_answer, print_error, print_thought
from agent.state import AgentState, StepStatus, RiskLevel
from agent.verify import Verifier, VerifyStatus
from agent.playbooks import PlaybookEngine
from agent.learning import PlaybookArchitect
from agent.voice.personality import load_system_prompt, resolve_personality

# Tool schema mapping
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "vision",
            "description": "Takes a screenshot and analyzes it with a specialized vision AI. Returns a structured description of the screen: active app, UI elements, text content, and suggested click coordinates. USE THIS FIRST to see the screen, and use it repeatedly to check state after actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Before calling this, explain exactly what you expect to see and why you need to look at the screen."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_mouse",
            "description": "Moves the mouse cursor to absolute X, Y coordinates on the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Before calling this, explain exactly what you currently see on the screen and why you are choosing to use this tool."
                    },
                    "x": {"type": "integer", "description": "Absolute X coordinate (pixels from left edge, e.g. 0 to 1920)"},
                    "y": {"type": "integer", "description": "Absolute Y coordinate (pixels from top edge, e.g. 0 to 1080)"}
                },
                "required": ["thought_process", "x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "click_mouse",
            "description": "Moves the cursor to X, Y and clicks the mouse there.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Before calling this, explain exactly what you currently see on the screen and why you are choosing to use this tool."
                    },
                    "x": {"type": "integer", "description": "Absolute X coordinate (pixels from left edge, e.g. 0 to 1920)"},
                    "y": {"type": "integer", "description": "Absolute Y coordinate (pixels from top edge, e.g. 0 to 1080)"},
                    "button": {
                        "type": "string", 
                        "enum": ["left", "right", "double"],
                        "description": "Which button to click. Default is left."
                    }
                },
                "required": ["thought_process", "x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_ui_coordinates",
            "description": "Perception Sub-Agent: Use this tool FIRST if you need to click something! Give it a physical description of the target. It will analyze the red grid image and return the precise X/Y integer coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Before calling this, explain exactly what you currently see on the screen and why you are choosing to use this tool."
                    },
                    "target_description": {"type": "string", "description": "What UI element to find. E.g. 'The search magnifying glass icon' or 'Rajdeep chat button'"}
                },
                "required": ["thought_process", "target_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_mouse",
            "description": "Scrolls the mouse wheel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Before calling this, explain exactly what you currently see on the screen and why you are choosing to use this tool."
                    },
                    "clicks": {
                        "type": "integer", 
                        "description": "Number of scroll ticks. Positive to scroll UP, negative to scroll DOWN."
                    }
                },
                "required": ["thought_process", "clicks"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_keyboard",
            "description": "Types text or sends a hotkey. Provide either text OR hotkey.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Before calling this, explain exactly what you currently see on the screen and why you are choosing to type these specific keys."
                    },
                    "text": {"type": "string", "description": "Actual text string to type. Like 'Hello World'."},
                    "hotkey": {"type": "string", "description": "Keys to press as hotkey format, e.g. 'ctrl,c', 'enter', 'win', 'space'."}
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_app",
            "description": "Checks if a specific app is installed on this PC and whether it is currently running. Returns install status, running status, and a suggested action (e.g., 'Use web.whatsapp.com instead').",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you need to check this app."
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Name of the app to check. E.g., 'whatsapp', 'chrome', 'discord', 'telegram', 'spotify'."
                    }
                },
                "required": ["thought_process", "app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_to_app",
            "description": "Directly switches to a running app window by name. Uses Win32 API to bring the window to foreground instantly — no alt+tab cycling needed. Use this INSTEAD of alt+tab. The app must already be running.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain which app you want to switch to and why."
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Name of the app to switch to. Matches against window title and process name. E.g., 'WhatsApp', 'Chrome', 'Discord'."
                    }
                },
                "required": ["thought_process", "app_name"]
            }
        }
    },
    # ── Browser Tools (Playwright CDP) ──
    # These are PREFERRED over PyAutoGUI for ALL web/browser tasks.
    # They operate programmatically on the DOM — no coordinate guessing needed.
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the browser to a URL. Automatically connects to the browser via CDP. PREFERRED over ctrl+l/type for all URL navigation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain what page you want to navigate to and why."
                    },
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to. No 'https://' prefix needed. E.g., 'gmail.com', 'music.youtube.com'."
                    }
                },
                "required": ["thought_process", "url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element in the browser by CSS selector or visible text. Much more reliable than coordinate-based clicking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Describe the element you want to click."
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (e.g., 'button.submit', '#search-icon') or visible text (e.g., 'Sign In', 'Next')."
                    }
                },
                "required": ["thought_process", "selector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into a browser input field. Identifies the field by CSS selector, placeholder text, or label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Describe which input field you're targeting and what you're typing."
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector, placeholder text, or label of the input field. E.g., 'input[name=q]', 'Search', '#email'."
                    },
                    "text": {
                        "type": "string",
                        "description": "The text to type into the field."
                    },
                    "clear_first": {
                        "type": "boolean",
                        "description": "Whether to clear the field before typing. Default true."
                    }
                },
                "required": ["thought_process", "selector", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": "Press a keyboard key in the browser. E.g., 'Enter', 'Tab', 'Escape', 'ArrowDown'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you need to press this key."
                    },
                    "key": {
                        "type": "string",
                        "description": "The key to press. E.g., 'Enter', 'Tab', 'Escape', 'ArrowDown', 'Backspace'."
                    }
                },
                "required": ["thought_process", "key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_text",
            "description": "Get the visible text content of the current browser page. Useful for reading page content, checking what's displayed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you need to read the page content."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_state",
            "description": "Get the current browser state: active tab title/URL, list of all open tabs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you need to check the browser state."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_new_tab",
            "description": "Open a new browser tab, optionally navigating to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you need a new tab."
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional URL to open in the new tab."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close_tab",
            "description": "Close the currently active browser tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you're closing this tab."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Wait for an element or text to appear on the browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain what you're waiting for and why."
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to wait for. E.g., '#results', '.search-box'."
                    },
                    "text": {
                        "type": "string",
                        "description": "Text content to wait for on the page."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait. Default 10."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the browser page up or down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why you need to scroll."
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Scroll direction."
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Number of scroll ticks (default 3)."
                    }
                },
                "required": ["thought_process"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a local shell command (PowerShell or cmd) and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "MANDATORY. Explain why this command is needed."
                    },
                    "command": {
                        "type": "string",
                        "description": "The command text to execute."
                    },
                    "shell": {
                        "type": "string",
                        "enum": ["auto", "powershell", "cmd"],
                        "description": "Shell selection. Default is auto."
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 30)."
                    }
                },
                "required": ["thought_process", "command"]
            }
        }
    }
]

def execute_tool(name: str, args: dict) -> str:
    """Executes the mapped python primitive."""
    if name == "vision":
        return vision()
    elif name == "move_mouse":
        return move_mouse(args.get("x", 0), args.get("y", 0))
    elif name == "click_mouse":
        return click_mouse(args.get("x", -1), args.get("y", -1), args.get("button", "left"))
    elif name == "scroll_mouse":
        return scroll_mouse(args.get("clicks", 0))
    elif name == "type_keyboard":
        return type_keyboard(args.get("text", ""), args.get("hotkey", ""))
    elif name == "run_command":
        return run_command(
            args.get("command", ""),
            args.get("shell", "auto"),
            args.get("timeout_seconds", args.get("timeout", 30)),
        )
    elif name == "check_app":
        result = check_app_installed(args.get("app_name", ""))
        return json.dumps(result, indent=2)
    elif name == "switch_to_app":
        return switch_to_app(args.get("app_name", ""))
    # ── Browser tools (Playwright CDP) ──
    elif name == "browser_navigate":
        return browser_navigate(args.get("url", ""))
    elif name == "browser_click":
        return browser_click(args.get("selector", ""))
    elif name == "browser_type":
        return browser_type(args.get("selector", ""), args.get("text", ""), args.get("clear_first", True))
    elif name == "browser_press_key":
        return browser_press_key(args.get("key", ""))
    elif name == "browser_get_text":
        return browser_get_text()
    elif name == "browser_get_state":
        return browser_get_state()
    elif name == "browser_new_tab":
        return browser_new_tab(args.get("url", ""))
    elif name == "browser_close_tab":
        return browser_close_tab()
    elif name == "browser_wait_for":
        return browser_wait_for(args.get("selector", ""), args.get("text", ""), args.get("timeout", 10))
    elif name == "browser_scroll":
        return browser_scroll(args.get("direction", "down"), args.get("amount", 3))
    else:
        return f"Error: Tool '{name}' not found."

logger = logging.getLogger("mira.brain")

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────
MAX_RETRIES = 3           # Max recovery attempts per step
SETTLE_DELAY = 0.3        # Seconds to wait after action for UI to settle
APP_SWITCH_DELAY = 1.0    # Seconds to wait after app switch for window to render
WAIT_POLL_INTERVAL = 0.5  # Seconds between polls when waiting for a condition
WAIT_DEFAULT_TIMEOUT = 15 # Default timeout for wait_for conditions
VOICE_CONTROL_POLL_SECONDS = 0.2


class AgentBrain:
    def __init__(self, voice_coordinator=None):
        with open("config.json", "r", encoding="utf-8") as f:
            self.config = json.load(f)
            
        self.voice_coordinator = voice_coordinator
        self._voice_paused = False
        self._runtime_interrupt = None
        self._current_task_prompt = ""
        self.personality_name = "friendly"
        self.personality_profile = {}

        self.fallback_chain = self.config.get("fallback_chain", [])
        self.show_thoughts = self.config.get("show_thoughts", True)
        self.providers = self.config.get("providers", {})
        self.system_prompt = load_system_prompt(self.config)
        self.personality_name, self.personality_profile, _ = resolve_personality(self.config)

        self.clients = self._init_clients()
        self.verifier = Verifier()
        self.playbook_engine = PlaybookEngine(config=self.config)
        self.playbook_architect = PlaybookArchitect()
        
    def _init_clients(self):
        clients = {}
        for provider, info in self.providers.items():
            if provider == "ollama":
                clients["ollama"] = OpenAI(base_url=info.get("url", "http://localhost:11434/v1"), api_key="ollama")
            elif provider == "gemini":
                # Google AI Studio API is natively OpenAI compatible via this endpoint
                clients["gemini"] = OpenAI(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=os.environ.get("GEMINI_API_KEY", "")
                )
            elif provider == "groq":
                clients["groq"] = OpenAI(
                    base_url="https://api.groq.com/openai/v1",
                    api_key=os.environ.get("GROQ_API_KEY", "")
                )
            elif provider == "nvidia":
                clients["nvidia"] = OpenAI(
                    base_url="https://integrate.api.nvidia.com/v1",
                    api_key=os.environ.get("NVIDIA_API_KEY", "")
                )
        return clients

    def attach_voice_coordinator(self, voice_coordinator):
        self.voice_coordinator = voice_coordinator

    def save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def set_personality(self, requested_name: str) -> str:
        resolved, profile, _ = resolve_personality(self.config, requested_name)
        self.config.setdefault("user_profile", {})["personality"] = resolved
        self.personality_name = resolved
        self.personality_profile = profile
        self.system_prompt = load_system_prompt(self.config, resolved)
        self.playbook_engine.config = self.config
        return resolved

    def _emit_voice_event(self, event_type: str, **payload):
        if not self.voice_coordinator:
            return
        try:
            self.voice_coordinator.emit_event(event_type, **payload)
        except Exception as e:
            logger.debug("Voice event emit failed: %s", e)

    def _update_voice_snapshot(self, phase: str, task_prompt: str = "", state: AgentState | None = None):
        if not self.voice_coordinator:
            return

        snapshot = {
            "phase": phase,
            "task": task_prompt,
            "personality": self.personality_name,
        }

        if state and state.current_step:
            snapshot["current_step"] = state.current_step.step_num
            snapshot["current_action"] = state.current_step.action
            snapshot["current_description"] = state.current_step.description

        try:
            self.voice_coordinator.update_state(**snapshot)
        except Exception as e:
            logger.debug("Voice state update failed: %s", e)

    def _handle_single_voice_command(self, command: dict, task_prompt: str = "", state: AgentState | None = None) -> tuple[str, str | None]:
        action = str(command.get("action", "")).strip().lower()

        if action == "heard_ignored":
            return "continue", None

        if action == "pause_task":
            self._voice_paused = True
            self._emit_voice_event("status_report", message="Pausing task at the next safe checkpoint.")
            return "continue", None

        if action == "resume_task":
            self._voice_paused = False
            self._emit_voice_event("status_report", message="Resuming task execution.")
            return "continue", None

        if action == "cancel_task":
            self._voice_paused = False
            self._emit_voice_event("task_cancelled", reason="Cancelled by voice command.")
            return "cancel", None

        if action == "set_personality":
            requested = str(command.get("personality", "")).strip()
            if requested:
                selected = self.set_personality(requested)
                try:
                    self.save_config()
                except Exception as e:
                    logger.debug("Could not persist personality change: %s", e)
                self._emit_voice_event(
                    "personality_changed",
                    personality=self.personality_profile.get("display_name", selected),
                    personality_key=selected,
                )
            return "continue", None

        if action == "status":
            if state and state.current_step:
                message = (
                    f"I am on step {state.current_step.step_num}: "
                    f"{state.current_step.description}"
                )
            elif task_prompt:
                message = f"I am preparing the task: {task_prompt}"
            else:
                message = "I am idle and ready for your next task."
            self._emit_voice_event("status_report", message=message)
            return "continue", None

        if action == "run_task_now":
            requested_task = str(command.get("task", "")).strip()
            if requested_task:
                self._voice_paused = False
                return "interrupt", requested_task
            return "continue", None

        return "continue", None

    def _print_voice_transcript(self, command: dict):
        raw_text = str(command.get("raw_text", "")).strip()
        action = str(command.get("action", "")).strip().lower()
        if not raw_text:
            return

        if action == "heard_ignored":
            console.print(f"[dim yellow]Voice Heard (wake not matched):[/dim yellow] {raw_text}")
        else:
            console.print(f"[bold magenta]Voice Heard:[/bold magenta] {raw_text}")

    def _process_voice_commands(self, task_prompt: str = "", state: AgentState | None = None) -> tuple[str, str | None]:
        if not self.voice_coordinator:
            return "continue", None

        while True:
            command = self.voice_coordinator.poll_command()
            if not command:
                break
            self._print_voice_transcript(command)
            control, next_task = self._handle_single_voice_command(command, task_prompt, state)
            if control != "continue":
                return control, next_task

        while self._voice_paused:
            self._update_voice_snapshot("paused", task_prompt, state)
            time.sleep(VOICE_CONTROL_POLL_SECONDS)
            command = self.voice_coordinator.poll_command()
            if not command:
                continue
            self._print_voice_transcript(command)
            control, next_task = self._handle_single_voice_command(command, task_prompt, state)
            if control != "continue":
                return control, next_task

        return "continue", None

    def process_idle_voice_commands(self) -> str | None:
        control, next_task = self._process_voice_commands(task_prompt="", state=None)
        if control == "interrupt" and next_task:
            return next_task
        return None

    def is_vision_model(self, model_name: str, provider: str) -> bool:
        """Determines if a model is vision-capable based on name or provider."""
        model_name = model_name.lower()
        if provider == "gemini":
            return True # Gemini 1.5/2.0 are vision native
        if "vision" in model_name or "vl" in model_name or "pixtral" in model_name:
            return True
        if "gemma4:e2b" in model_name:
            return True
        if "nemotron" in model_name and "vl" in model_name:
            return True
        return False

    def query_llm(self, messages, provider="ollama"):
        client = self.clients.get(provider)
        if not client:
            raise ValueError(f"Provider {provider} not initialized.")
            
        model_info = self.providers.get(provider, {})
        model = model_info.get("model", "")
        vision_capable = self.is_vision_model(model, provider)
        
        # Find the absolute latest visual state (either from tool call or autonomous injection)
        last_vision_index = -1
        for i, m in enumerate(messages):
            is_tool_vision = (m.get("role") == "tool" and m.get("name") == "vision")
            is_auto_vision = (m.get("role") == "user" and m.get("_is_vision_result", False))
            if is_tool_vision or is_auto_vision:
                last_vision_index = i

                
        # Format vision messages for OpenAI standard
        openai_messages = [{"role": "system", "content": self.system_prompt}]
        for i, m in enumerate(messages):
            if m.get("role") == "tool" and m.get("name") == "vision":
                is_latest = (i == last_vision_index)
                
                # The tool result content is the vision analysis text (not raw base64 anymore)
                analysis_text = m.get("_analysis", "")
                base64_img = m.get("_raw_image", "")
                
                # Vision tool response: always include the structured analysis
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": analysis_text if analysis_text else json.dumps({"visual_state": "Screenshot captured." if is_latest else "Old screenshot pruned."})
                })
                
                # For vision-capable models, ALSO attach the latest image for direct pixel reasoning
                if vision_capable and is_latest and base64_img:
                    openai_messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "LATEST screenshot (raw image for coordinate reference):"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{base64_img}"
                                }
                            }
                        ]
                    })
                    
            elif m.get("role") == "user" and m.get("_is_vision_result", False):
                is_latest = (i == last_vision_index)
                analysis_text = m.get("_analysis", "")
                base64_img = m.get("content", "")
                
                if is_latest and analysis_text:
                    # Always inject the analysis text (works for ALL models)
                    openai_messages.append({
                        "role": "user",
                        "content": f"AUTONOMOUS VERIFICATION — Vision Analysis of Current State:\n\n{analysis_text}"
                    })
                    # For vision-capable models, also attach the raw image
                    if vision_capable and base64_img:
                        openai_messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Raw screenshot (for coordinate reference):"},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}}
                            ]
                        })
                elif is_latest:
                    openai_messages.append({"role": "user", "content": "[Screenshot Provided - Latest State]"})
                # If not latest, we prune it by not adding it at all
            else:
                # Filter out internal metadata keys before sending
                clean_msg = {k: v for k, v in m.items() if not k.startswith("_") and k != "step_logic"}
                openai_messages.append(clean_msg)

        # Allow provider to override tool_choice (e.g. for NVIDIA NIM compatibility)
        t_choice = model_info.get("tool_choice", "auto")
        
        args = {
            "model": model,
            "messages": openai_messages,
            "tools": TOOLS,
            "temperature": 0.1
        }
        
        if t_choice is not None:
            args["tool_choice"] = t_choice
            
        # Support Ollama-specific thinking mode via extra_body
        if provider == "ollama" and "think" in model_info:
            args["extra_body"] = {"think": model_info["think"]}

        return client.chat.completions.create(**args)

    def _is_multi_intent_task(self, task_prompt: str) -> bool:
        """
        Detect whether a task asks for more than a simple open/switch/navigation action.
        """
        task = f" {(task_prompt or '').lower().strip()} "
        if not task.strip():
            return False

        secondary_intent_tokens = (
            " send ", " message ", " dm ", " text ", " chat ",
            " play ", " search ", " find ", " watch ", " listen ",
            " call ", " post ", " upload ", " download ", " reply ",
            " book ", " order ", " buy ", " stream ", " email "
        )
        connectors = (" and ", " then ", " to ")

        has_secondary_intent = any(token in task for token in secondary_intent_tokens)
        has_connector = any(token in task for token in connectors)
        token_count = len(task_prompt.split()) if task_prompt else 0

        # Keep simple tasks like "open gmail" on generic playbooks.
        return has_secondary_intent and (has_connector or token_count > 4)

    def _should_force_playbook_creation(self, task_prompt: str, playbook_name: str) -> bool:
        """
        If a generic playbook matched a multi-intent request, force creation of
        a specialized playbook so the engine does not stop at an underfit plan.
        """
        if playbook_name not in {"open_app", "open_url"}:
            return False

        if not self._is_multi_intent_task(task_prompt):
            return False

        logger.info(
            "Generic playbook '%s' matched a multi-intent task. Forcing playbook creation.",
            playbook_name,
        )
        return True

    def _is_web_browser_task(self, task_prompt: str) -> bool:
        """Heuristic to detect tasks that should use browser_* actions."""
        task = (task_prompt or "").lower()
        web_markers = (
            "http://", "https://", ".com", ".org", ".net",
            "browser", "website", "web", "url",
            "instagram", "reddit", "twitter", " x ", "linkedin", "facebook",
            "youtube", "gmail", "duckduckgo"
        )
        return any(marker in task for marker in web_markers)

    def _generate_plan(self, task_prompt: str, context_snapshot: str) -> list[dict]:
        """
        Phase 1: Generate a step-by-step plan.
        
        Strategy:
        1. Try to match a PLAYBOOK first (pre-written, battle-tested steps)
        2. If no playbook matches, fall back to dynamic LLM generation
        
        Playbooks are vastly superior because:
        - Every step is pre-tested and edge-case-aware
        - No risk of hallucinated shortcuts (ctrl+f vs ctrl+l)
        - Variables are extracted once, then substituted deterministically
        - The LLM only classifies intent — it doesn't generate steps
        """
        
        # ── Try 1: Playbook matching ──
        force_create_playbook = False

        if self.playbook_engine.playbooks:
            console.print("  [dim cyan]Checking playbooks...[/dim cyan]")
            pb_name, pb_vars = self.playbook_engine.match_playbook(
                task_prompt, context_snapshot,
                self.clients, self.providers, self.fallback_chain
            )
            
            if pb_name:
                if self._should_force_playbook_creation(task_prompt, pb_name):
                    force_create_playbook = True
                    console.print(
                        "  [dim yellow]Generic playbook matched, but task needs a richer workflow. "
                        "Creating a specialized playbook...[/dim yellow]"
                    )
                else:
                    plan = self.playbook_engine.render_playbook(pb_name, pb_vars)
                    if plan:
                        console.print(f"  [bold green]📖 Using playbook: {pb_name}[/bold green] [dim](variables: {pb_vars})[/dim]")
                        return plan
                    else:
                        console.print(f"  [dim yellow]Playbook '{pb_name}' matched but rendered empty. Falling back.[/dim yellow]")
            else:
                console.print("  [dim yellow]No playbook matched.[/dim yellow]")
        
        # ── Try 2: Auto-create a new playbook ──
        if force_create_playbook:
            console.print("  [bold magenta]🧠 Creating specialized playbook for this task...[/bold magenta]")
        else:
            console.print("  [bold magenta]🧠 Creating new playbook for this task...[/bold magenta]")
        new_pb_name = self.playbook_architect.create_playbook(
            task_prompt, context_snapshot,
            self.clients, self.providers, self.fallback_chain
        )
        
        if new_pb_name:
            # Hot-load the newly created playbook
            if self.playbook_engine.reload_single(new_pb_name):
                console.print(f"  [bold green]📝 New playbook created: {new_pb_name}[/bold green]")
                
                # Now match it — the LLM needs to extract variables for this new playbook
                pb_name2, pb_vars2 = self.playbook_engine.match_playbook(
                    task_prompt, context_snapshot,
                    self.clients, self.providers, self.fallback_chain
                )
                
                if pb_name2:
                    plan = self.playbook_engine.render_playbook(pb_name2, pb_vars2)
                    if plan:
                        console.print(f"  [bold green]📖 Using new playbook: {pb_name2}[/bold green] [dim](variables: {pb_vars2})[/dim]")
                        return plan
                
                # If matcher failed, try rendering with empty variables (use defaults)
                plan = self.playbook_engine.render_playbook(new_pb_name, {})
                if plan:
                    console.print(f"  [bold green]📖 Using new playbook: {new_pb_name}[/bold green] [dim](default variables)[/dim]")
                    return plan
            else:
                console.print(f"  [dim red]Failed to load new playbook '{new_pb_name}'.[/dim red]")
        else:
            console.print("  [dim yellow]Playbook creation failed. Using dynamic LLM planning.[/dim yellow]")
        
        # ── Try 3: Dynamic LLM generation (last resort) ──
        return self._generate_plan_dynamic(task_prompt, context_snapshot)

    def _generate_plan_dynamic(self, task_prompt: str, context_snapshot: str) -> list[dict]:
        """Fallback: Generate a plan dynamically via LLM when no playbook matches."""
        is_web_task = self._is_web_browser_task(task_prompt)

        planning_prompt = f"""{context_snapshot}

USER TASK: {task_prompt}

You are a TASK PLANNER for a desktop automation agent. Generate a precise step-by-step plan.

AVAILABLE ACTIONS:
- switch_to_app(app_name): Bring a running app window to foreground
- type_keyboard(text, hotkey): Type text OR press keyboard shortcut
- click_mouse(x, y, button): Click at screen coordinates
- scroll_mouse(clicks): Scroll up/down
    - browser_navigate(url): Navigate directly using Playwright CDP (preferred for all web tasks)
    - browser_click(selector): Click by selector/text in browser DOM
    - browser_type(selector, text, clear_first): Type into browser inputs
    - browser_press_key(key): Press key in browser context
    - browser_wait_for(selector, text, timeout): Wait for DOM element/text in browser
    - browser_scroll(direction, amount): Scroll the browser page

CRITICAL KEYBOARD SHORTCUTS (memorize these — getting them wrong breaks everything):
  ctrl+l = FOCUS ADDRESS BAR in browsers. Use this to type a URL.
  ctrl+f = FIND IN PAGE. Searches for text ON the current page. NEVER use this to navigate to a URL.
  ctrl+k = Search bar in some apps.
  ctrl+t = New tab in browsers.
  ctrl+w = Close current tab.

RULES:
1. Break the task into the SMALLEST possible atomic steps — one action per step
2. Do NOT include vision/screenshot/verify steps. System handles verification automatically.
3. DEFAULT APPS:
   - Texting/Messaging: WhatsApp (search shortcut: Ctrl+F)
   - Emails, Web, & Browsing: Brave Browser (CRITICAL: NOT Edge, NOT Chrome)
   - Music: YouTube Music (music.youtube.com in browser)
   - Search: DuckDuckGo (duckduckgo.com in browser)
4. When composing messages: write a proper, natural message. Don't copy raw task text.
5. NEVER use placeholders like "username" or "user@example.com". If you don't know a specific value, skip that step.
6. For ALL web/social/media tasks, prefer browser_* actions over keyboard/mouse UI control.
7. For web tasks, NEVER use click_mouse coordinates.
8. For social DM tasks, prefer inbox/messages URL flows over homepage search.
9. To search in WhatsApp desktop: switch_to_app → ctrl+f → type name → enter

OUTPUT FORMAT: Return ONLY a valid JSON array. No markdown, no explanation, no code fences.
[
    {{"step": 1, "action": "browser_navigate", "params": {{"url": "instagram.com/direct/inbox/"}}, "description": "Open Instagram inbox", "expect": "messages_open", "risk_level": "low"}},
    {{"step": 2, "action": "browser_type", "params": {{"selector": "input[aria-label*='Search' i]", "text": "rajdeep"}}, "description": "Search recipient", "expect": "recipient_typed", "risk_level": "low"}}
]"""

        for provider in self.fallback_chain:
            try:
                client = self.clients.get(provider)
                if not client:
                    continue
                model = self.providers.get(provider, {}).get("model", "")
                
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a precise task planner. Output ONLY valid JSON arrays. No markdown, no explanation, no code fences."},
                        {"role": "user", "content": planning_prompt}
                    ],
                    temperature=0.1
                )
                
                plan_text = resp.choices[0].message.content.strip()
                if plan_text.startswith("```"):
                    plan_text = plan_text.split("\n", 1)[1] if "\n" in plan_text else plan_text[3:]
                if plan_text.endswith("```"):
                    plan_text = plan_text[:-3]
                plan_text = plan_text.strip()
                
                plan = json.loads(plan_text)

                if not isinstance(plan, list):
                    raise ValueError("Dynamic planner did not return a JSON array")
                
                plan = [s for s in plan if s.get("action") != "vision"]

                if is_web_task:
                    has_browser_actions = any(str(s.get("action", "")).startswith("browser_") for s in plan)
                    has_coordinate_actions = any(s.get("action") in {"click_mouse", "move_mouse"} for s in plan)

                    if not has_browser_actions:
                        logger.warning(
                            "Rejected dynamic plan from %s for web task: no browser_* actions.",
                            provider,
                        )
                        continue
                    if has_coordinate_actions:
                        logger.warning(
                            "Rejected dynamic plan from %s for web task: contains coordinate actions.",
                            provider,
                        )
                        continue

                for i, step in enumerate(plan):
                    step["step"] = i + 1
                    if "expect" not in step:
                        step["expect"] = ""
                    if "risk_level" not in step:
                        action = step.get("action", "")
                        if action in ("click_mouse", "move_mouse"):
                            step["risk_level"] = "high"
                        elif action in ("switch_to_app", "launch_app"):
                            step["risk_level"] = "medium"
                        else:
                            step["risk_level"] = "low"
                
                logger.info(f"Dynamic plan generated with {len(plan)} steps via {provider}")
                return plan
            except Exception as e:
                logger.error(f"Dynamic planning failed with {provider}: {e}")
                continue
        
        return []

    def _launch_and_wait(self, app_name: str) -> bool:
        """Launches an app via PowerShell commands and waits for it to appear. No GUI interaction."""
        from agent.context import get_active_window
        
        console.print(f"  [dim yellow]App not running. Launching '{app_name}' via system command...[/dim yellow]")
        
        # Use launch_app (PowerShell: AppX -> Start Menu shortcut -> Start-Process)
        result = launch_app(app_name)
        console.print(f"  [dim cyan]{result}[/dim cyan]")
        
        if "Failed" in result:
            console.print(f"  [bold red]✗ Could not launch {app_name}[/bold red]")
            return False
        
        # Wait for the app to appear (poll for up to 10 seconds)
        for attempt in range(10):
            time.sleep(1.0)
            # Try to switch to it
            switch_result = switch_to_app(app_name)
            if "Switched to" in switch_result:
                console.print(f"  [bold green]✓ {app_name} is ready! {switch_result}[/bold green]")
                return True
            console.print(f"  [dim]  Waiting for {app_name} to open... ({attempt + 1}/10)[/dim]")
        
        console.print(f"  [bold red]✗ {app_name} launched but window not found after 10s[/bold red]")
        return False

    def _execute_action(self, action: str, params: dict) -> str:
        """Execute a single plan action. Extracted for reuse in recovery."""
        if action == "switch_to_app":
            return switch_to_app(params.get("app_name", ""))
        elif action == "type_keyboard":
            return type_keyboard(
                params.get("text", ""), 
                params.get("hotkey", params.get("key", "")), 
                params.get("repeat", 1)
            )
        elif action == "click_mouse":
            return click_mouse(params.get("x", -1), params.get("y", -1), params.get("button", "left"))
        elif action == "scroll_mouse":
            return scroll_mouse(params.get("clicks", 0))
        elif action == "move_mouse":
            return move_mouse(params.get("x", 0), params.get("y", 0))
        elif action == "run_command":
            return run_command(
                params.get("command", ""),
                params.get("shell", "auto"),
                params.get("timeout_seconds", params.get("timeout", 30)),
            )
        # ── Browser actions (Playwright CDP) ──
        elif action == "browser_navigate":
            return browser_navigate(params.get("url", ""))
        elif action == "browser_click":
            return browser_click(params.get("selector", ""))
        elif action == "browser_type":
            return browser_type(params.get("selector", ""), params.get("text", ""), params.get("clear_first", True))
        elif action == "browser_press_key":
            return browser_press_key(params.get("key", ""))
        elif action == "browser_get_text":
            return browser_get_text()
        elif action == "browser_get_state":
            return browser_get_state()
        elif action == "browser_new_tab":
            return browser_new_tab(params.get("url", ""))
        elif action == "browser_close_tab":
            return browser_close_tab()
        elif action == "browser_wait_for":
            return browser_wait_for(params.get("selector", ""), params.get("text", ""), params.get("timeout", 10))
        elif action == "browser_scroll":
            return browser_scroll(params.get("direction", "down"), params.get("amount", 3))
        else:
            return f"Unknown action: {action}"

    def _wait_for_condition(self, wait_for: dict, pre_title: str) -> bool:
        """
        Dynamically wait for a condition by polling cheap OS signals.
        No hardcoded sleeps — polls until the condition is met or timeout.

        Supported conditions (from playbook YAML `wait_for` field):
          title_contains: "keyword"   — wait until window title includes this keyword
          title_changed: true         — wait until window title differs from pre_title
          timeout: N                  — max seconds to wait (default: 15)

        Returns True if condition was met, False if timed out.
        """
        from agent.context import get_active_window

        timeout = wait_for.get("timeout", WAIT_DEFAULT_TIMEOUT)
        title_contains = wait_for.get("title_contains", "").lower().strip()
        title_changed = wait_for.get("title_changed", False)

        if not title_contains and not title_changed:
            return True  # No condition specified, nothing to wait for

        condition_desc = (
            f"title contains '{title_contains}'" if title_contains
            else f"title changes from '{pre_title[:50]}'"
        )
        console.print(f"  [dim cyan]⏳ Waiting for {condition_desc} (up to {timeout}s)...[/dim cyan]")

        start = time.time()
        polls = 0
        while (time.time() - start) < timeout:
            control, next_task = self._process_voice_commands(task_prompt=self._current_task_prompt, state=None)
            if control in {"cancel", "interrupt"}:
                self._runtime_interrupt = {
                    "control": control,
                    "next_task": next_task,
                }
                console.print("  [yellow]Voice control requested while waiting. Exiting wait loop.[/yellow]")
                return False

            time.sleep(WAIT_POLL_INTERVAL)
            polls += 1

            active = get_active_window()
            current_title = active.get("window_title", "").lower()

            if title_contains and title_contains in current_title:
                elapsed = time.time() - start
                console.print(f"  [green]✓ Condition met:[/green] [dim]title contains '{title_contains}' ({elapsed:.1f}s, {polls} polls)[/dim]")
                return True

            if title_changed and current_title != pre_title.lower():
                elapsed = time.time() - start
                console.print(f"  [green]✓ Condition met:[/green] [dim]title changed to '{current_title[:60]}' ({elapsed:.1f}s, {polls} polls)[/dim]")
                return True

        elapsed = time.time() - start
        console.print(f"  [yellow]⚠ Wait timed out after {elapsed:.1f}s ({polls} polls). Proceeding anyway.[/yellow]")
        return False

    def _get_recovery_action(self, state: AgentState, step: dict, verify_reason: str, vision_analysis: str = "") -> dict | None:
        """
        Ask the LLM for a single corrective action when a step fails verification.
        
        This is a lightweight call — no full re-planning, just one fix action.
        """
        recovery_context = state.get_recovery_context()
        
        recovery_prompt = f"""{recovery_context}

Verification Failure: {verify_reason}
{"Vision Analysis: " + vision_analysis[:500] if vision_analysis else "No vision data available."}

You are a RECOVERY AGENT. The step above failed verification. Generate EXACTLY ONE corrective action to fix the situation.

AVAILABLE ACTIONS:
- switch_to_app(app_name): Bring an app to foreground
- type_keyboard(text, hotkey): Type text or press keys
- click_mouse(x, y, button): Click at coordinates (use ONLY if vision analysis provides coordinates)
- browser_navigate(url): Navigate browser to URL with Playwright
- browser_click(selector): Click browser DOM element by selector/text
- browser_type(selector, text, clear_first): Type into browser input by selector
- browser_press_key(key): Press a key in browser context
- browser_wait_for(selector, text, timeout): Wait for browser DOM/text state
- browser_get_state(): Return active browser tab URL/title for diagnosis

RULES:
1. Output ONLY a single JSON object — no markdown, no explanation
2. If the wrong window is active, switch_to_app to the correct one
3. If an element wasn't found, try an alternative approach (different hotkey, etc.)
4. For browser verification failures, prefer browser_get_state or browser_navigate/browser_wait_for before desktop coordinates.
5. If you can't determine a fix, output: {{"action": "abort", "reason": "..."}}

Example outputs:
{{"action": "switch_to_app", "params": {{"app_name": "WhatsApp"}}, "description": "Refocus WhatsApp"}}
{{"action": "type_keyboard", "params": {{"hotkey": "escape"}}, "description": "Close popup and retry"}}
{{"action": "abort", "reason": "App is not installed and no web fallback available"}}"""

        for provider in self.fallback_chain:
            try:
                client = self.clients.get(provider)
                if not client:
                    continue
                model = self.providers.get(provider, {}).get("model", "")
                
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a recovery agent. Output ONLY valid JSON. No markdown."},
                        {"role": "user", "content": recovery_prompt}
                    ],
                    temperature=0.1
                )
                
                text = resp.choices[0].message.content.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
                
                recovery = json.loads(text)
                logger.info(f"Recovery action from {provider}: {recovery}")
                return recovery
                
            except Exception as e:
                logger.error(f"Recovery planning failed with {provider}: {e}")
                continue
        
        return None

    def run_agentic_loop(self, task_prompt: str):
        """
        Main execution loop: Plan → Act → Verify → Correct.

        Returns a status payload so the caller can handle voice interruptions and
        immediately start injected follow-up tasks.
        """
        logger.info(f"--- Starting Agentic Loop for Task: {task_prompt} ---")

        self._runtime_interrupt = None
        self._voice_paused = False
        self._current_task_prompt = task_prompt

        self._emit_voice_event("task_start", task=task_prompt)
        self._update_voice_snapshot("running", task_prompt=task_prompt, state=None)

        # Respect runtime voice controls before expensive planning.
        control, injected_task = self._process_voice_commands(task_prompt=task_prompt, state=None)
        if control == "cancel":
            self._update_voice_snapshot("idle", task_prompt="", state=None)
            return {"status": "cancelled", "next_task": None, "summary": "Cancelled before planning."}
        if control == "interrupt" and injected_task:
            self._update_voice_snapshot("idle", task_prompt="", state=None)
            return {"status": "interrupted", "next_task": injected_task, "summary": "Interrupted before planning."}

        # Phase 0: Gather context
        console.print("[dim cyan]Gathering system context...[/dim cyan]")
        context_snapshot = build_context_snapshot()
        console.print(f"[dim]{context_snapshot}[/dim]")

        # Phase 1: Generate plan
        console.print("\n[bold yellow]📋 Phase 1: Generating step-by-step plan...[/bold yellow]")
        plan = self._generate_plan(task_prompt, context_snapshot)

        if not plan:
            print_error("Failed to generate a plan. Aborting.")
            self._emit_voice_event("task_failed", reason="Failed to generate a plan.")
            self._update_voice_snapshot("idle", task_prompt="", state=None)
            self._current_task_prompt = ""
            return {"status": "failed", "next_task": None, "summary": "Plan generation failed."}

        console.print(f"\n[bold green]✅ Plan generated ({len(plan)} steps):[/bold green]")
        for step in plan:
            step_num = step.get("step", "?")
            action = step.get("action", "?")
            desc = step.get("description", "")
            params = step.get("params", {})
            risk = step.get("risk_level", "low")
            expect = step.get("expect", "")
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())

            risk_color = {"low": "green", "medium": "yellow", "high": "red"}.get(risk, "white")
            console.print(
                f"  [dim]{step_num}. [{action}] {desc}[/dim]"
                + (f" [dim cyan]({param_str})[/dim cyan]" if param_str else "")
                + f" [{risk_color}][{risk}][/{risk_color}]"
                + (f" [dim magenta]→ {expect}[/dim magenta]" if expect else "")
            )
        console.print()

        # Phase 2: Execute + verify + recover
        console.print("[bold yellow]⚡ Phase 2: Executing with verification...[/bold yellow]\n")

        from agent.context import get_active_window

        state = AgentState(task=task_prompt)
        run_status = "completed"
        next_task = None

        for step in plan:
            control, injected_task = self._process_voice_commands(task_prompt=task_prompt, state=state)
            if control == "cancel":
                run_status = "cancelled"
                break
            if control == "interrupt" and injected_task:
                run_status = "interrupted"
                next_task = injected_task
                break

            step_num = step.get("step", "?")
            action = step.get("action", "")
            params = step.get("params", {})
            description = step.get("description", "")
            wait_for = step.get("wait_for", None)

            console.print(f"[bold cyan]── Step {step_num}: {description} ──[/bold cyan]")
            self._emit_voice_event("step_started", step=step_num, description=description, action=action)

            pre_action_window = get_active_window()
            pre_action_title = pre_action_window.get("window_title", "")

            state.begin_step(step)
            self._update_voice_snapshot("running", task_prompt=task_prompt, state=state)

            step_succeeded = False

            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    console.print(f"  [bold yellow]↻ Retry {attempt}/{MAX_RETRIES - 1}...[/bold yellow]")

                control, injected_task = self._process_voice_commands(task_prompt=task_prompt, state=state)
                if control == "cancel":
                    run_status = "cancelled"
                    break
                if control == "interrupt" and injected_task:
                    run_status = "interrupted"
                    next_task = injected_task
                    break

                try:
                    result = ""

                    if action == "switch_to_app":
                        app_name = params.get("app_name", "")
                        result = switch_to_app(app_name)

                        if "not found" in result.lower():
                            launched = self._launch_and_wait(app_name)
                            if not launched:
                                print_error(f"Cannot open {app_name}. Aborting plan.")
                                state.mark_failed(f"Cannot open {app_name}")
                                run_status = "failed"
                                break
                            result = f"Launched and switched to {app_name}"

                        print_tool_result(result)
                        state.record_result(result)

                        console.print("  [dim cyan]📷 Scanning app state (first entry)...[/dim cyan]")
                        time.sleep(APP_SWITCH_DELAY)
                        img_str = vision()
                        analysis = analyze_screenshot(img_str)
                        preview = analysis[:200] + "..." if len(analysis) > 200 else analysis
                        print_tool_result(f"Screen: {preview}")
                    else:
                        result = self._execute_action(action, params)
                        print_tool_result(result)
                        state.record_result(result)

                    if wait_for:
                        self._wait_for_condition(wait_for, pre_action_title)
                    else:
                        time.sleep(SETTLE_DELAY)

                    if self._runtime_interrupt:
                        requested_control = self._runtime_interrupt.get("control", "cancel")
                        if requested_control == "interrupt":
                            run_status = "interrupted"
                        elif requested_control == "cancel":
                            run_status = "cancelled"
                        else:
                            run_status = requested_control
                        next_task = self._runtime_interrupt.get("next_task")
                        self._runtime_interrupt = None
                        break

                    verify_result = self.verifier.verify_step(state, step, result)

                    if verify_result.passed or verify_result.skipped:
                        state.mark_verified(verify_result.reason)
                        status_icon = "✓" if verify_result.passed else "⊘"
                        status_word = "Verified" if verify_result.passed else "Skipped"
                        console.print(f"  [green]{status_icon} {status_word}:[/green] [dim]{verify_result.reason}[/dim]")

                        active = get_active_window()
                        console.print(f"  [dim]Window: {active.get('window_title', '?')} ({active.get('process_name', '?')})[/dim]")

                        self._emit_voice_event(
                            "step_verified",
                            step=step_num,
                            description=description,
                            reason=verify_result.reason,
                        )
                        step_succeeded = True
                        break

                    elif verify_result.status == VerifyStatus.NEEDS_VISION:
                        console.print(f"  [yellow]👁 Vision check needed:[/yellow] [dim]{verify_result.reason}[/dim]")
                        img_str = vision()
                        analysis = analyze_screenshot(img_str)
                        preview = analysis[:200] + "..." if len(analysis) > 200 else analysis
                        console.print(f"  [dim cyan]Vision: {preview}[/dim cyan]")

                        state.mark_verified(f"Vision confirmed: {analysis[:100]}")
                        self._emit_voice_event(
                            "step_verified",
                            step=step_num,
                            description=description,
                            reason="Vision confirmed",
                        )
                        step_succeeded = True
                        break

                    else:
                        console.print(f"  [bold red]✗ Verification failed:[/bold red] [dim]{verify_result.reason}[/dim]")
                        state.mark_failed(verify_result.reason)
                        state.increment_retry()
                        self._emit_voice_event(
                            "step_failed",
                            step=step_num,
                            description=description,
                            reason=verify_result.reason,
                        )

                        vision_analysis = ""
                        if verify_result.should_trigger_vision or attempt >= 1:
                            console.print("  [dim yellow]📷 Taking diagnostic screenshot...[/dim yellow]")
                            try:
                                img_str = vision()
                                vision_analysis = analyze_screenshot(img_str)
                                preview = vision_analysis[:200] + "..." if len(vision_analysis) > 200 else vision_analysis
                                console.print(f"  [dim cyan]Vision: {preview}[/dim cyan]")
                            except Exception as ve:
                                console.print(f"  [dim red]Vision failed: {ve}[/dim red]")

                        console.print("  [dim yellow]🧠 Asking LLM for recovery action...[/dim yellow]")
                        recovery = self._get_recovery_action(state, step, verify_result.reason, vision_analysis)

                        if recovery and recovery.get("action") != "abort":
                            recovery_action = recovery.get("action", "")
                            recovery_params = recovery.get("params", {})
                            recovery_desc = recovery.get("description", "")
                            console.print(f"  [yellow]⚡ Recovery:[/yellow] {recovery_desc} [{recovery_action}]")

                            try:
                                recovery_result = self._execute_action(recovery_action, recovery_params)
                                console.print(f"  [dim]Recovery result: {recovery_result}[/dim]")
                                time.sleep(SETTLE_DELAY)
                            except Exception as re:
                                console.print(f"  [dim red]Recovery action failed: {re}[/dim red]")
                        else:
                            reason = recovery.get("reason", "No recovery available") if recovery else "Recovery planning failed"
                            console.print(f"  [bold red]⚠ Cannot recover: {reason}[/bold red]")
                            break

                except Exception as e:
                    print_error(f"Step {step_num} exception: {str(e)}")
                    state.mark_failed(str(e))
                    self._emit_voice_event("step_failed", step=step_num, description=description, reason=str(e))

                    console.print("  [dim yellow]📷 Taking diagnostic screenshot...[/dim yellow]")
                    try:
                        img_str = vision()
                        analysis = analyze_screenshot(img_str)
                        preview = analysis[:300] + "..." if len(analysis) > 300 else analysis
                        print_tool_result(f"Vision Fallback:\n{preview}")
                    except Exception as ve:
                        print_error(f"Vision fallback also failed: {str(ve)}")
                    break

            if run_status in {"cancelled", "interrupted", "failed"}:
                break

            if not step_succeeded:
                run_status = "failed"
                console.print(f"\n[bold red]✗ Step {step_num} failed after {MAX_RETRIES} attempts. Aborting plan.[/bold red]")
                break

        summary = state.get_summary()
        console.print(f"\n[bold green]{'✅' if state.total_failures == 0 else '⚠️'} {summary}[/bold green]")

        if self.show_thoughts:
            console.print("\n[dim]Step History:[/dim]")
            for record in state.step_history:
                icon = {
                    StepStatus.VERIFIED: "[green]✓[/green]",
                    StepStatus.RECOVERED: "[yellow]↻[/yellow]",
                    StepStatus.FAILED: "[red]✗[/red]",
                    StepStatus.SKIPPED: "[dim]⊘[/dim]",
                }.get(record.status, "[dim]?[/dim]")
                retries = f" (retries: {record.recovery_attempts})" if record.recovery_attempts > 0 else ""
                console.print(f"  {icon} Step {record.step_num}: {record.action} → {record.status.value}{retries}")

        if run_status == "completed" and state.total_failures == 0:
            self._emit_voice_event("task_completed", summary=summary)
        elif run_status == "cancelled":
            self._emit_voice_event("task_cancelled", reason="Cancelled by command.")
        elif run_status == "interrupted":
            self._emit_voice_event("task_cancelled", reason="Interrupted for a new task.")
        else:
            self._emit_voice_event("task_failed", reason=summary)

        self._update_voice_snapshot("idle", task_prompt="", state=None)
        self._current_task_prompt = ""

        return {
            "status": run_status,
            "next_task": next_task,
            "summary": summary,
        }
