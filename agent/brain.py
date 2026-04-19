import os
import json
import time
import logging
from openai import OpenAI
from pathlib import Path

from agent.primitives import vision, move_mouse, click_mouse, scroll_mouse, type_keyboard, analyze_screenshot, switch_to_app, launch_app
from agent.context import build_context_snapshot, check_app_installed
from agent.display import console, print_tool_call, print_tool_result, print_final_answer, print_error, print_thought

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
    elif name == "check_app":
        result = check_app_installed(args.get("app_name", ""))
        return json.dumps(result, indent=2)
    elif name == "switch_to_app":
        return switch_to_app(args.get("app_name", ""))
    else:
        return f"Error: Tool '{name}' not found."

logger = logging.getLogger("mira.brain")

class AgentBrain:
    def __init__(self):
        with open("config.json", "r", encoding="utf-8") as f:
            self.config = json.load(f)
            
        self.fallback_chain = self.config.get("fallback_chain", [])
        self.show_thoughts = self.config.get("show_thoughts", True)
        self.providers = self.config.get("providers", {})
        
        with open(Path("prompts") / "system_prompt.txt", "r", encoding="utf-8") as f:
            self.system_prompt = f.read()

        self.clients = self._init_clients()
        
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

    def _generate_plan(self, task_prompt: str, context_snapshot: str) -> list[dict]:
        """Phase 1: Generate a step-by-step plan using the LLM (no tools, just reasoning)."""
        
        planning_prompt = f"""{context_snapshot}

USER TASK: {task_prompt}

You are a TASK PLANNER for a desktop automation agent. Based on the system context above, generate a precise step-by-step plan to accomplish the user's task.

AVAILABLE ACTIONS (the ONLY actions the executor can perform):
- switch_to_app(app_name): Instantly bring a running app window to foreground
- type_keyboard(text, hotkey): Type text OR press keyboard shortcut. Use "text" for typing strings, "hotkey" for shortcuts like "ctrl,f", "enter", "win"
- click_mouse(x, y, button): Click at screen coordinates
- scroll_mouse(clicks): Scroll up/down

RULES:
1. Break the task into the SMALLEST possible atomic steps — one action per step
2. Do NOT include any vision/screenshot/verify steps. The system handles verification automatically via window title checks.
3. Consider edge cases:
   - Is the app running? Use switch_to_app. Not running but installed? Use win key + type name + enter.
   - After searching for a contact, you MUST select them (press Enter) BEFORE typing a message
   - WhatsApp search shortcut is Ctrl+F
4. DEFAULT APPS (Use these if not specified in the task):
   - Texting/Messaging: WhatsApp
   - Emails & Browsing: Brave Browser
   - Music: YouTube Music
   - Media/Videos: YouTube
   - Search: DuckDuckGo Search (type "duckduckgo.com" in browser)
5. When composing messages: write a proper, natural message from the user's perspective.
   Do NOT copy-paste the user's raw task text as the message.
   Example: user says "tell june dont marry yet bad news" -> compose: "Hey June, please hold off on the marriage plans for now. I have some important news I need to share with you first."
6. Each step must specify the exact action and parameters — no placeholders.

OUTPUT FORMAT: Return ONLY a valid JSON array. No markdown, no explanation, no code fences.
[
  {{"step": 1, "action": "switch_to_app", "params": {{"app_name": "WhatsApp"}}, "description": "Bring WhatsApp to foreground"}},
  {{"step": 2, "action": "type_keyboard", "params": {{"hotkey": "ctrl,f"}}, "description": "Open search bar"}},
  {{"step": 3, "action": "type_keyboard", "params": {{"text": "June"}}, "description": "Search for contact"}},
  {{"step": 4, "action": "type_keyboard", "params": {{"hotkey": "enter"}}, "description": "Select contact from results"}},
  {{"step": 5, "action": "type_keyboard", "params": {{"text": "Hey June..."}}, "description": "Type the message"}},
  {{"step": 6, "action": "type_keyboard", "params": {{"hotkey": "enter"}}, "description": "Send the message"}}
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
                # Clean up markdown fences if the LLM wraps it
                if plan_text.startswith("```"):
                    plan_text = plan_text.split("\n", 1)[1] if "\n" in plan_text else plan_text[3:]
                if plan_text.endswith("```"):
                    plan_text = plan_text[:-3]
                plan_text = plan_text.strip()
                
                plan = json.loads(plan_text)
                
                # Filter out vision steps the LLM might have snuck in
                plan = [s for s in plan if s.get("action") != "vision"]
                for i, step in enumerate(plan):
                    step["step"] = i + 1
                
                logger.info(f"Plan generated with {len(plan)} steps via {provider}")
                return plan
            except Exception as e:
                logger.error(f"Planning failed with {provider}: {e}")
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

    def run_agentic_loop(self, task_prompt: str):
        logger.info(f"--- Starting Agentic Loop for Task: {task_prompt} ---")
        
        # ── Phase 0: Gather system context ──
        console.print("[dim cyan]Gathering system context...[/dim cyan]")
        context_snapshot = build_context_snapshot()
        console.print(f"[dim]{context_snapshot}[/dim]")
        
        # ── Phase 1: Generate Plan ──
        console.print("\n[bold yellow]📋 Phase 1: Generating step-by-step plan...[/bold yellow]")
        plan = self._generate_plan(task_prompt, context_snapshot)
        
        if not plan:
            print_error("Failed to generate a plan. Aborting.")
            return
        
        # Display the plan
        console.print(f"\n[bold green]✅ Plan generated ({len(plan)} steps):[/bold green]")
        for step in plan:
            step_num = step.get("step", "?")
            action = step.get("action", "?")
            desc = step.get("description", "")
            params = step.get("params", {})
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            console.print(f"  [dim]{step_num}. [{action}] {desc}[/dim]" + (f" [dim cyan]({param_str})[/dim cyan]" if param_str else ""))
        console.print()
        
        # ── Phase 2: Smart Execution ──
        console.print("[bold yellow]⚡ Phase 2: Executing plan...[/bold yellow]\n")
        
        from agent.context import get_active_window
        
        # Track which app we're targeting (for window verification)
        target_app = None
        app_confirmed = False
        
        for step in plan:
            step_num = step.get("step", "?")
            action = step.get("action", "")
            params = step.get("params", {})
            description = step.get("description", "")
            
            console.print(f"[bold cyan]── Step {step_num}: {description} ──[/bold cyan]")
            
            try:
                result = ""
                
                if action == "switch_to_app":
                    app_name = params.get("app_name", "")
                    target_app = app_name
                    result = switch_to_app(app_name)
                    
                    # If switch failed, try launching the app
                    if "not found" in result.lower():
                        launched = self._launch_and_wait(app_name)
                        if not launched:
                            print_error(f"Cannot open {app_name}. Aborting plan.")
                            return
                        result = f"Launched and switched to {app_name}"
                    
                    app_confirmed = True
                    print_tool_result(result)
                    
                    # Use ONE vision call here to see the app's UI state
                    console.print("  [dim cyan]Checking app state...[/dim cyan]")
                    time.sleep(1.0) # Let window settle
                    img_str = vision()

                    analysis = analyze_screenshot(img_str)
                    preview = analysis[:200] + "..." if len(analysis) > 200 else analysis
                    print_tool_result(f"Screen: {preview}")
                    time.sleep(0.3)
                    continue  # Done with this step
                    
                elif action == "type_keyboard":
                    text = params.get("text", "")
                    hotkey = params.get("hotkey", "")
                    
                    # Safety: before typing a long message, verify we're in the right app
                    if text and len(text) > 20 and target_app:
                        active = get_active_window()
                        window_title = active.get("window_title", "")
                        if target_app.lower() not in window_title.lower():
                            console.print(f"  [bold red]⚠ Wrong window! Expected {target_app}, got: {window_title}[/bold red]")
                            console.print("  [dim yellow]Attempting to refocus...[/dim yellow]")
                            switch_to_app(target_app)
                            time.sleep(0.5)
                            # Re-check
                            active = get_active_window()
                            if target_app.lower() not in active.get("window_title", "").lower():
                                print_error(f"Still wrong window. Aborting to prevent typing in wrong app.")
                                return
                    
                    result = type_keyboard(text, hotkey)
                elif action == "click_mouse":
                    result = click_mouse(params.get("x", -1), params.get("y", -1), params.get("button", "left"))
                elif action == "scroll_mouse":
                    result = scroll_mouse(params.get("clicks", 0))
                elif action == "move_mouse":
                    result = move_mouse(params.get("x", 0), params.get("y", 0))
                else:
                    result = f"Unknown action: {action}"
                
                print_tool_result(result)
                
                # Free instant window title check
                active = get_active_window()
                window_title = active.get("window_title", "Unknown")
                process_name = active.get("process_name", "Unknown")
                print_tool_result(f"Window: {window_title} ({process_name})")
                
                # Small delay for UI to settle
                time.sleep(0.3)
                
            except Exception as e:
                print_error(f"Step {step_num} failed: {str(e)}")
                console.print("  [dim yellow]Using vision fallback to diagnose...[/dim yellow]")
                try:
                    img_str = vision()
                    analysis = analyze_screenshot(img_str)
                    preview = analysis[:300] + "..." if len(analysis) > 300 else analysis
                    print_tool_result(f"Vision Fallback:\n{preview}")
                except Exception as ve:
                    print_error(f"Vision fallback also failed: {str(ve)}")
                break
        
        console.print("\n[bold green]✅ All plan steps executed![/bold green]")



