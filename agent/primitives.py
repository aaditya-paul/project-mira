import os
import json
import time
import base64
import logging
from io import BytesIO
import pyautogui
from PIL import ImageGrab, ImageDraw, ImageFont
from openai import OpenAI
from agent.display import console
from agent.context import APP_REGISTRY

logger = logging.getLogger("mira.primitives")

# Configure pyautogui to add a small delay to make actions visible and safe
pyautogui.PAUSE = 0.5
pyautogui.FAILSAFE = False

def _get_registry_keywords(app_name: str) -> tuple[list[str], list[str]]:
    """Resolves an app name into a list of valid process names and keywords using APP_REGISTRY."""
    keywords = [app_name.lower()]
    exe_names = [app_name.lower().replace(" ", "")]
    
    # Check registry for exact or partial matches
    from agent.context import APP_REGISTRY
    for key, info in APP_REGISTRY.items():
        match = False
        if app_name.lower() == key.lower(): match = True
        elif app_name.lower() == info["display_name"].lower(): match = True
        elif any(k in app_name.lower() for k in info["start_menu_keywords"]): match = True
        
        if match:
            keywords.extend([k.lower() for k in info["start_menu_keywords"]])
            keywords.extend([k.lower() for k in info["appx_keywords"]])
            exe_names.extend([e.lower().replace(".exe", "") for e in info.get("exe_names", [])])
            # Special case: add base name too
            keywords.append(key.lower())
    
    # De-duplicate and filter empty
    keywords = list(dict.fromkeys([k for k in keywords if k]))
    exe_names = list(dict.fromkeys([e for e in exe_names if e]))
    return keywords, exe_names

# ──────────────────────────────────────────
# Vision Analyzer — Supports fallback chain for screenshot understanding
# ──────────────────────────────────────────
_vision_chain = None  # list of (client, model) tuples

def _build_vision_client(provider: str, model: str, config: dict):
    """Build a vision client for a given provider."""
    if provider == "ollama":
        url = config.get("providers", {}).get("ollama", {}).get("url", "http://localhost:11434/v1")
        return OpenAI(base_url=url, api_key="ollama"), model
    elif provider == "gemini":
        return OpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.environ.get("GEMINI_API_KEY", "")
        ), model
    elif provider == "nvidia":
        return OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=os.environ.get("NVIDIA_API_KEY", "")
        ), model
    elif provider == "groq":
        return OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.environ.get("GROQ_API_KEY", "")
        ), model
    return None, None

def _get_vision_chain():
    """Lazy-init the vision analyzer fallback chain from config."""
    global _vision_chain
    if _vision_chain is not None:
        return _vision_chain
    
    _vision_chain = []
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        
        va_config = config.get("vision_analyzer", {})
        provider = va_config.get("provider", "gemini")
        model = va_config.get("model", "gemini-2.5-flash")
        fallback = va_config.get("fallback", "gemini")
        fallback_model = va_config.get("fallback_model", "gemini-2.5-flash")
        
        # Primary
        client, mdl = _build_vision_client(provider, model, config)
        if client:
            _vision_chain.append((client, mdl, provider))
        
        # Fallback (if different from primary)
        if fallback != provider:
            client2, mdl2 = _build_vision_client(fallback, fallback_model, config)
            if client2:
                _vision_chain.append((client2, mdl2, fallback))
        
        logger.info(f"Vision chain initialized: {[(p, m) for _, m, p in _vision_chain]}")
    except Exception as e:
        logger.error(f"Failed to init vision chain: {e}")
    
    return _vision_chain

VISION_ANALYSIS_PROMPT = """You are a precision screen-reading agent. Analyze this screenshot and provide a structured description.

Respond in this EXACT format (keep it concise but complete):

**ACTIVE APP:** [Name of the foreground application/window]
**WINDOW TITLE:** [Full title bar text]
**SCREEN STATE:** [1-2 sentence summary of what's happening on screen]

**VISIBLE UI ELEMENTS:**
- [Element type]: "[Label/text]" at approximate position (left/center/right, top/middle/bottom)
- [Continue listing key interactive elements: buttons, text fields, menus, icons, chat bubbles, etc.]

**KEY TEXT ON SCREEN:**
- [Any readable text content, chat messages, form values, error messages, etc.]

**SUGGESTED INTERACTION ZONES:**
- To [action]: click near (Xpx, Ypx) — [element description]
- [List 3-5 key clickable targets with approximate coordinates based on the red grid overlay]

Be precise with coordinates using the red grid lines as reference. The grid has spacing of 200px.
Do NOT hallucinate elements that aren't visible. If you're uncertain, say so."""


def analyze_screenshot(base64_img: str) -> str:
    """
    Sends a screenshot to the vision model chain for structured analysis.
    Tries each provider in the chain until one succeeds.
    """
    chain = _get_vision_chain()
    
    if not chain:
        return "[Vision analysis unavailable — no analyzers configured]"
    
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_ANALYSIS_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64_img}"
                    }
                }
            ]
        }
    ]
    
    for client, model, provider in chain:
        try:
            logger.info(f"Trying vision analysis with {provider}/{model}")
            console.print(f"  [dim cyan]Vision: Analyzing with {provider}...[/dim cyan]")
            
            # Add a timeout to prevent hanging on local/unresponsive providers
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
                max_tokens=1500,
                timeout=15.0  # 15 second timeout for vision
            )
            analysis = response.choices[0].message.content
            logger.info(f"Vision analysis via {provider}: success")
            return analysis
        except Exception as e:
            logger.warning(f"Vision analysis failed with {provider}/{model}: {e}")
            console.print(f"  [dim yellow]Vision: {provider} failed or timed out. Trying fallback...[/dim yellow]")
            continue

    
    return "[Vision analysis failed — all providers exhausted]"


def vision() -> str:
    """Takes a screenshot of the primary screen and returns it as a base64 encoded PNG string.
    Also runs the image through a dedicated vision analyzer for structured understanding.
    Returns: base64 image string (analysis is stored separately via vision_with_analysis).
    """
    # Hide mouse so it doesn't block UI tooltips or buttons
    try:
        pyautogui.moveTo(10, 10, duration=0)
    except:
        pass
        
    screenshot = ImageGrab.grab()
    draw = ImageDraw.Draw(screenshot)
    width, height = screenshot.size
    
    # Try using a readable font, fallback to default
    try:
        font = ImageFont.truetype("arial.ttf", 25)
    except IOError:
        font = ImageFont.load_default()
        
    grid_spacing = 200
    
    # Draw vertical lines and X coordinates
    for x in range(grid_spacing, width, grid_spacing):
        draw.line([(x, 0), (x, height)], fill=(255, 0, 0, 180), width=2)
        draw.text((x + 5, 5), f"X={x}", fill=(255, 0, 0), font=font)
        draw.text((x + 5, height // 2), f"X={x}", fill=(255, 0, 0), font=font) # Mid-screen label
        
    # Draw horizontal lines and Y coordinates
    for y in range(grid_spacing, height, grid_spacing):
        draw.line([(0, y), (width, y)], fill=(255, 0, 0, 180), width=2)
        draw.text((5, y + 5), f"Y={y}", fill=(255, 0, 0), font=font)
        draw.text((width // 2, y + 5), f"Y={y}", fill=(255, 0, 0), font=font) # Mid-screen label
    
    buffer = BytesIO()
    screenshot.save(buffer, format="PNG")
    # Save a local copy for debugging the grid accuracy
    screenshot.save("debug_vision.png", format="PNG")
    
    img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return img_str

def move_mouse(x: int, y: int) -> str:
    """Moves the mouse cursor to the absolute screen coordinates (x, y)."""
    try:
        pyautogui.moveTo(x, y, duration=0.3)
        return f"Mouse moved to ({x}, {y})"
    except Exception as e:
        return f"Failed to move mouse: {str(e)}"

def click_mouse(x: int = -1, y: int = -1, button: str = "left") -> str:
    """Moves to coordinates (if provided) and clicks the mouse.
    button options: 'left', 'right', 'double'.
    """
    try:
        if x != -1 and y != -1:
            pyautogui.moveTo(x, y, duration=0.3)
            time.sleep(0.1)
            
        if button == "left":
            pyautogui.click()
        elif button == "right":
            pyautogui.rightClick()
        elif button == "double":
            pyautogui.doubleClick()
        else:
            pyautogui.click() # Default to left
        return f"Performed {button} click at ({x}, {y})."
    except Exception as e:
        return f"Failed to click mouse: {str(e)}"

def scroll_mouse(clicks: int) -> str:
    """Scrolls the mouse wheel.
    Positive clicks scroll up, negative clicks scroll down.
    """
    try:
        import platform
        multiplier = 120 if platform.system() == "Windows" else 10
        pyautogui.scroll(clicks * multiplier)
        return f"Scrolled {clicks} clicks (scaled by {multiplier})."
    except Exception as e:
        return f"Failed to scroll: {str(e)}"

def type_keyboard(text: str = "", hotkey: str = "", repeat: int = 1) -> str:
    """Types text on the keyboard OR sends a hotkey combination.
    Provide 'text' to type a string. 
    Provide 'hotkey' to send a combo like 'ctrl,c' or 'enter' or 'win'. Provide comma separated keys.
    'repeat' specifies how many times to perform the action (only for hotkeys).
    """
    try:
        result_log = []
        
        # LLM/Playbook Auto-correction: if text accidentally contains a standalone hotkey
        common_hotkeys = {"win", "enter", "esc", "tab", "up", "down", "left", "right", "space", "backspace", "/"}
        if text and str(text).lower().strip() in common_hotkeys and not hotkey:
            hotkey = text.lower().strip()
            text = ""
            
        # Parse 'ctrl+f' into 'ctrl,f' if LLM uses standard '+' notation
        if hotkey and '+' in hotkey:
            hotkey = hotkey.replace('+', ',')
            
        if hotkey:
            keys = [k.strip() for k in hotkey.split(',')]
            for i in range(repeat):
                pyautogui.hotkey(*keys)
                if repeat > 1:
                    time.sleep(0.1) # Small gap between repeats
            
            rep_str = f" x{repeat}" if repeat > 1 else ""
            result_log.append(f"Pressed hotkey: {keys}{rep_str}")
            
            # Small pause after hotkey so the system has time to react
            if text:
                time.sleep(0.4)
        
        if text:
            pyautogui.write(text, interval=0.05)
            result_log.append(f"Typed text: '{text}'")
        
        if result_log:
            return " | ".join(result_log)
        return "No text or hotkey provided."
    except Exception as e:
        return f"Failed to type/press keys: {str(e)}"


def switch_to_app(app_name: str) -> str:
    """Directly switches to a running app window by matching its title/process name.
    Uses Win32 SetForegroundWindow API via PowerShell — no alt+tab cycling needed.
    Prioritizes process name matches over title matches, and skips helper processes.
    """
    import subprocess
    from pathlib import Path
    
    # Resolve synonyms using helper
    keywords, exe_names = _get_registry_keywords(app_name)
    
    # Prepare JSON-like strings for PowerShell
    ps_keywords = '"' + '","'.join(keywords) + '"'
    ps_exe_names = '"' + '","'.join(exe_names) + '"'
    
    # Write the PS script to a temp file to avoid $_ escaping issues
    ps_script_path = Path(__file__).parent.parent / "_switch_app.ps1"
    # Two-pass matching: 1) prefer ProcessName match 2) fallback to Title match
    # Skip known helper processes that might have the search term in their title
    ps_script_content = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinActivate {{
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);
}}
"@

$keywords = @({ps_keywords})
$exeNames = @({ps_exe_names})

# Helper processes that render content for other apps — never the main window
$helperProcesses = @("msedgewebview2", "applicationframehost", "textinputhost", "runtimebroker")

$procs = Get-Process | Where-Object {{ $_.MainWindowTitle -ne '' -and $_.MainWindowHandle -ne [IntPtr]::Zero }}

# Pass 1: Match by ProcessName (strongest signal)
$bestMatch = $null
foreach ($proc in $procs) {{
    $pname = $proc.ProcessName.ToLower()
    foreach ($kw in $exeNames) {{
        if ($pname -match $kw -and $pname -notin $helperProcesses) {{
            $bestMatch = $proc
            break
        }}
    }}
    if ($bestMatch) {{ break }}
}}

# Pass 2: If no process name match, try window title match
if ($null -eq $bestMatch) {{
    foreach ($proc in $procs) {{
        $pname = $proc.ProcessName.ToLower()
        $title = $proc.MainWindowTitle.ToLower()
        foreach ($kw in $keywords) {{
            if ($title -match $kw -and $pname -notin $helperProcesses) {{
                $bestMatch = $proc
                break
            }}
        }}
        if ($bestMatch) {{ break }}
    }}
}}

if ($null -ne $bestMatch) {{
    $hwnd = $bestMatch.MainWindowHandle
    if ([WinActivate]::IsIconic($hwnd)) {{
        [WinActivate]::ShowWindow($hwnd, 9) | Out-Null
    }}
    Start-Sleep -Milliseconds 100
    [WinActivate]::SetForegroundWindow($hwnd) | Out-Null
    Write-Output "SWITCHED|||$($bestMatch.ProcessName)|||$($bestMatch.MainWindowTitle)"
}} else {{
    Write-Output "NOT_FOUND|||$searchTerm"
}}
"""
    try:
        ps_script_path.write_text(ps_script_content, encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script_path)],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
        )
        # Clean up
        try:
            ps_script_path.unlink()
        except:
            pass
        
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout.strip()
            if output.startswith("SWITCHED"):
                parts = output.split("|||")
                proc_name = parts[1] if len(parts) > 1 else "?"
                win_title = parts[2] if len(parts) > 2 else "?"
                return f"Switched to: {win_title} (Process: {proc_name})"
            elif output.startswith("NOT_FOUND"):
                return f"App '{app_name}' not found in open windows. Try opening it first via Start Menu (win key, type name, enter)."
        
        return f"Failed to switch. PowerShell output: {result.stderr.strip() or result.stdout.strip()}"
    except Exception as e:
        return f"Failed to switch to app: {str(e)}"


def launch_app(app_name: str) -> str:
    """Launches an app using PowerShell commands — no GUI interaction needed.
    Tries: 1) AppX/Store apps  2) Start Menu shortcuts  3) Direct Start-Process
    Uses APP_REGISTRY to find valid keywords/executables for the app name.
    """
    import subprocess
    from pathlib import Path
    
    # 1. Resolve synonyms using helper
    keywords, exe_names = _get_registry_keywords(app_name)
    
    # Prepare JSON-like strings for PowerShell
    ps_keywords = '"' + '","'.join(keywords) + '"'
    ps_exe_names = '"' + '","'.join(exe_names) + '"'
    
    ps_script_path = Path(__file__).parent.parent / "_launch_app.ps1"
    # PowerShell script that tries multiple launch methods with fuzzy matching
    ps_script_content = f"""
$keywords = @({ps_keywords})
$exeNames = @({ps_exe_names})
$launched = $false

# Method 1: Try AppX (Microsoft Store apps)
foreach ($kw in $keywords) {{
    if ($launched) {{ break }}
    try {{
        $pkg = Get-AppxPackage | Where-Object {{ $_.Name -match $kw -or $_.PackageFamilyName -match $kw }} | Select-Object -First 1
        if ($pkg) {{
            $appId = (Get-AppxPackageManifest $pkg).Package.Applications.Application.Id
            # Sometimes appId is a list or missing, default to first one or app name
            $id = if ($appId -is [array]) {{ $appId[0] }} else {{ $appId }}
            if (-not $id) {{ $id = "App" }} 
            
            $familyName = $pkg.PackageFamilyName
            Start-Process "shell:AppsFolder\\$familyName!$id"
            Write-Output "LAUNCHED_APPX|||$($pkg.Name)|||$familyName"
            $launched = $true
        }}
    }} catch {{}}
}}

# Method 2: Try Start Menu shortcuts (.lnk files) with fuzzy matching
if (-not $launched) {{
    $startMenuPaths = @(
        "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs"
    )
    foreach ($path in $startMenuPaths) {{
        if ($launched) {{ break }}
        foreach ($kw in $keywords) {{
            $shortcuts = Get-ChildItem -Path $path -Filter "*.lnk" -Recurse -ErrorAction SilentlyContinue | Where-Object {{ $_.BaseName -match $kw }}
            if ($shortcuts) {{
                $shortcut = $shortcuts | Select-Object -First 1
                Start-Process $shortcut.FullName
                Write-Output "LAUNCHED_SHORTCUT|||$($shortcut.BaseName)|||$($shortcut.FullName)"
                $launched = $true
                break
            }}
        }}
    }}
}}

# Method 3: Direct Start-Process (for system apps)
if (-not $launched) {{
    foreach ($exe in $exeNames) {{
        try {{
            Start-Process $exe -ErrorAction Stop
            Write-Output "LAUNCHED_DIRECT|||$exe"
            $launched = $true
            break
        }} catch {{}}
    }}
}}

if (-not $launched) {{
    Write-Output "FAILED|||$($keywords[0])|||Could not find app to launch"
}}
"""
    try:
        ps_script_path.write_text(ps_script_content, encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script_path)],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            ps_script_path.unlink()
        except:
            pass
        
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout.strip()
            # Powershell might output multiple lines, take the one starting with LAUNCHED or FAILED
            for line in output.split('\n'):
                line = line.strip()
                if line.startswith("LAUNCHED"):
                    parts = line.split("|||")
                    method = parts[0].replace("LAUNCHED_", "").lower()
                    name = parts[1] if len(parts) > 1 else app_name
                    return f"Launched '{name}' via {method}"
                elif line.startswith("FAILED"):
                    return f"Failed to launch '{app_name}': not found via AppX, Start Menu shortcuts, or direct process."
        
        return f"Failed to launch. PowerShell: {result.stderr.strip() or result.stdout.strip()}"
    except Exception as e:
        return f"Failed to launch app: {str(e)}"


def click_element(name: str, control_type: str = None) -> str:
    """
    Click a named UI element using accessibility-first approach.
    
    Fallback chain:
    1. pywinauto (instant, no vision) — tries to find element by name
    2. vision + analyze (slow, but works for any UI) — falls back to coordinates
    
    Args:
        name: The element label/text to click (e.g., "Search", "Send")
        control_type: Optional type filter (e.g., "Button", "Edit")
        
    Returns:
        Status string describing what happened and how.
    """
    # Try 1: Accessibility (pywinauto)
    try:
        from agent.accessibility import get_ui_automation
        uia = get_ui_automation()
        if uia.available:
            result = uia.click_element(name, control_type)
            if "clicked" in result.lower() and "not found" not in result.lower():
                logger.info(f"click_element('{name}') succeeded via accessibility")
                return result
            logger.info(f"Accessibility click failed for '{name}': {result}")
    except Exception as e:
        logger.warning(f"Accessibility click error for '{name}': {e}")
    
    # Try 2: Vision fallback — take screenshot, analyze, find coordinates
    try:
        console.print(f"  [dim yellow]Accessibility failed for '{name}', falling back to vision...[/dim yellow]")
        img_str = vision()
        analysis = analyze_screenshot(img_str)
        
        # The analysis contains "SUGGESTED INTERACTION ZONES" with coordinates
        # Return the analysis so the caller (brain) can extract coordinates
        return f"VISION_FALLBACK: Element '{name}' not found via accessibility. Vision analysis:\n{analysis}"
    except Exception as e:
        return f"Failed to click '{name}': accessibility unavailable, vision failed: {str(e)}"

