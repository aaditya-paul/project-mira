"""
Context Awareness Module for Mira Agent.
Gathers system context: active window, installed apps, running processes, screen info.
"""
import os
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("mira.context")

# ──────────────────────────────────────────
# Known app map: app keyword -> (exe names, display name, web fallback URL)
# ──────────────────────────────────────────
APP_REGISTRY = {
    "whatsapp": {
        "exe_names": ["whatsapp.exe", "whatsapp.root"],
        "display_name": "WhatsApp",
        "web_fallback": "https://web.whatsapp.com",
        "start_menu_keywords": ["whatsapp"],
        "appx_keywords": ["whatsapp"],
    },
    "telegram": {
        "exe_names": ["telegram.exe"],
        "display_name": "Telegram",
        "web_fallback": "https://web.telegram.org",
        "start_menu_keywords": ["telegram"],
        "appx_keywords": ["telegram"],
    },
    "discord": {
        "exe_names": ["discord.exe", "update.exe"],
        "display_name": "Discord",
        "web_fallback": "https://discord.com/app",
        "start_menu_keywords": ["discord"],
        "appx_keywords": ["discord"],
    },
    "slack": {
        "exe_names": ["slack.exe"],
        "display_name": "Slack",
        "web_fallback": "https://app.slack.com",
        "start_menu_keywords": ["slack"],
        "appx_keywords": ["slack"],
    },
    "spotify": {
        "exe_names": ["spotify.exe"],
        "display_name": "Spotify",
        "web_fallback": "https://open.spotify.com",
        "start_menu_keywords": ["spotify"],
        "appx_keywords": ["spotify"],
    },
    "chrome": {
        "exe_names": ["chrome.exe"],
        "display_name": "Google Chrome",
        "web_fallback": None,
        "start_menu_keywords": ["google chrome", "chrome"],
        "appx_keywords": [],
    },
    "firefox": {
        "exe_names": ["firefox.exe"],
        "display_name": "Mozilla Firefox",
        "web_fallback": None,
        "start_menu_keywords": ["firefox"],
        "appx_keywords": ["firefox"],
    },
    "edge": {
        "exe_names": ["msedge.exe"],
        "display_name": "Microsoft Edge",
        "web_fallback": None,
        "start_menu_keywords": ["edge"],
        "appx_keywords": ["microsoftedge"],
    },
    "vscode": {
        "exe_names": ["code.exe"],
        "display_name": "Visual Studio Code",
        "web_fallback": "https://vscode.dev",
        "start_menu_keywords": ["visual studio code", "vs code"],
        "appx_keywords": [],
    },
    "notepad": {
        "exe_names": ["notepad.exe", "notepad++.exe"],
        "display_name": "Notepad",
        "web_fallback": None,
        "start_menu_keywords": ["notepad"],
        "appx_keywords": [],
    },
    "brave": {
        "exe_names": ["brave.exe"],
        "display_name": "Brave Browser",
        "web_fallback": None,
        "start_menu_keywords": ["brave"],
        "appx_keywords": [],
    },
    "utorrent": {
        "exe_names": ["utorrent.exe"],
        "display_name": "uTorrent",
        "web_fallback": None,
        "start_menu_keywords": ["utorrent", "torrent"],
        "appx_keywords": [],
    },
    "youtube_music": {
        "exe_names": [],
        "display_name": "YouTube Music",
        "web_fallback": "https://music.youtube.com",
        "start_menu_keywords": ["youtube music"],
        "appx_keywords": [],
    },
    "youtube": {
        "exe_names": [],
        "display_name": "YouTube",
        "web_fallback": "https://youtube.com",
        "start_menu_keywords": ["youtube"],
        "appx_keywords": [],
    },
    "duckduckgo": {
        "exe_names": [],
        "display_name": "DuckDuckGo",
        "web_fallback": "https://duckduckgo.com",
        "start_menu_keywords": ["duckduckgo"],
        "appx_keywords": [],
    },
}


def get_active_window() -> dict:
    """Returns info about the currently focused/foreground window using PowerShell."""
    try:
        # Use PowerShell to get foreground window info (no pywin32 dependency needed)
        ps_script = """
        Add-Type @"
        using System;
        using System.Runtime.InteropServices;
        using System.Text;
        public class WinAPI {
            [DllImport("user32.dll")]
            public static extern IntPtr GetForegroundWindow();
            [DllImport("user32.dll")]
            public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
            [DllImport("user32.dll")]
            public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
        }
"@
        $hwnd = [WinAPI]::GetForegroundWindow()
        $sb = New-Object System.Text.StringBuilder 256
        [WinAPI]::GetWindowText($hwnd, $sb, 256) | Out-Null
        $title = $sb.ToString()
        $pid = 0
        [WinAPI]::GetWindowThreadProcessId($hwnd, [ref]$pid) | Out-Null
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        Write-Output "$title|||$($proc.ProcessName)|||$($proc.MainWindowTitle)"
        """
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|||")
            return {
                "window_title": parts[0] if len(parts) > 0 else "Unknown",
                "process_name": parts[1] if len(parts) > 1 else "Unknown",
            }
    except Exception as e:
        logger.warning(f"Failed to get active window: {e}")
    
    return {"window_title": "Unknown", "process_name": "Unknown"}


def get_running_processes() -> list[str]:
    """Returns a deduplicated list of ALL running process names."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Select-Object -ExpandProperty ProcessName -Unique | Sort-Object"],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            return [p.strip().lower() for p in result.stdout.strip().split("\n") if p.strip()]
    except Exception as e:
        logger.warning(f"Failed to get running processes: {e}")
    return []


def get_visible_apps() -> list[dict]:
    """Returns list of apps with visible windows (processes with a MainWindowTitle).
    This is the most reliable way to detect what's actually running and visible to the user.
    Uses a .ps1 script file to avoid PowerShell escaping issues with $_.
    """
    # Write the PS script to a temp file in the project dir to avoid $_ escaping issues
    ps_script_path = Path(__file__).parent.parent / "_get_visible_apps.ps1"
    ps_script_content = """Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | ForEach-Object {
    Write-Output "$($_.ProcessName)|||$($_.MainWindowTitle)"
}
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
        
        if result.returncode == 0:
            apps = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if "|||" in line:
                    parts = line.split("|||", 1)
                    apps.append({
                        "process": parts[0].strip().lower(),
                        "title": parts[1].strip()
                    })
            return apps
    except Exception as e:
        logger.warning(f"Failed to get visible apps: {e}")
    return []


def get_screen_resolution() -> tuple[int, int]:
    """Returns the primary screen resolution."""
    try:
        import pyautogui
        size = pyautogui.size()
        return (size.width, size.height)
    except Exception:
        return (1920, 1080)


def _scan_start_menu() -> list[str]:
    """Scans Start Menu for installed app shortcuts."""
    shortcuts = []
    start_menu_paths = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    for base in start_menu_paths:
        if base.exists():
            for item in base.rglob("*.lnk"):
                shortcuts.append(item.stem.lower())
    return shortcuts


def _scan_program_files() -> list[str]:
    """Quick scan of Program Files directories for known app folders."""
    found = []
    paths_to_scan = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    ]
    for base in paths_to_scan:
        if base.exists():
            try:
                for item in base.iterdir():
                    if item.is_dir():
                        found.append(item.name.lower())
            except PermissionError:
                continue
    return found


# Cache for Store apps (slow PowerShell call, only run once per session)
_store_apps_cache = None

def _scan_store_apps() -> list[str]:
    """Scans Microsoft Store (AppX) installed apps via PowerShell Get-AppxPackage.
    Many modern Windows apps (WhatsApp, Telegram, Spotify) are installed this way.
    Results are cached for the session since this call takes ~2-3 seconds.
    """
    global _store_apps_cache
    if _store_apps_cache is not None:
        return _store_apps_cache
    
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-AppxPackage | ForEach-Object { $_.Name.ToLower() }"],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            _store_apps_cache = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            logger.info(f"Store apps scan found {len(_store_apps_cache)} packages")
            return _store_apps_cache
    except Exception as e:
        logger.warning(f"Failed to scan Store apps: {e}")
    
    _store_apps_cache = []
    return _store_apps_cache


def check_app_installed(app_key: str) -> dict:
    """
    Checks if a specific app is installed and/or running.
    Returns: {installed: bool, running: bool, display_name: str, web_fallback: str|None}
    """
    app_key = app_key.lower().strip()
    
    # Find matching app in registry
    app_info = None
    for key, info in APP_REGISTRY.items():
        if key in app_key or app_key in key:
            app_info = info
            break
    
    if not app_info:
        return {
            "installed": "unknown",
            "running": False,
            "display_name": app_key.title(),
            "web_fallback": None,
            "suggestion": f"App '{app_key}' is not in my known app database. Try opening it via Start Menu.",
        }
    
    # Check if running
    running_procs = get_running_processes()
    is_running = any(
        exe.replace(".exe", "").lower() in running_procs
        for exe in app_info["exe_names"]
    )
    
    # Check if installed (Start Menu + Program Files + Microsoft Store)
    start_menu_apps = _scan_start_menu()
    program_files_apps = _scan_program_files()
    store_apps = _scan_store_apps()
    all_installed = start_menu_apps + program_files_apps
    
    is_installed = any(
        keyword in " ".join(all_installed)
        for keyword in app_info["start_menu_keywords"]
    )
    
    # Also check Microsoft Store / AppX packages
    if not is_installed and app_info.get("appx_keywords"):
        is_installed = any(
            any(kw in pkg for pkg in store_apps)
            for kw in app_info["appx_keywords"]
        )
    
    result = {
        "installed": is_installed,
        "running": is_running,
        "display_name": app_info["display_name"],
        "web_fallback": app_info["web_fallback"],
    }
    
    # Generate smart suggestion
    if is_running:
        result["suggestion"] = f"{app_info['display_name']} is already running. Switch to it with Alt+Tab or click its taskbar icon."
    elif is_installed:
        result["suggestion"] = f"{app_info['display_name']} is installed. Open it via Start Menu (press Win, type '{app_info['display_name']}', press Enter)."
    elif app_info["web_fallback"]:
        result["suggestion"] = f"{app_info['display_name']} is NOT installed on this PC. Use the web version instead: {app_info['web_fallback']}"
    else:
        result["suggestion"] = f"{app_info['display_name']} does not appear to be installed."
    
    return result


def build_context_snapshot() -> str:
    """
    Builds a comprehensive context string about the current desktop state.
    This is injected into the agent's first message.
    """
    logger.info("Building context snapshot...")
    
    # 1. Active window
    active = get_active_window()
    
    # 2. Screen resolution
    res = get_screen_resolution()
    
    # 3. Get visible apps (windows with titles) — this is the ground truth
    visible_apps = get_visible_apps()
    running = get_running_processes()  # For app status checks below
    
    # Build human-readable list from visible windows
    # Filter out system noise (background processes with generic titles)
    NOISE_FILTERS = {"textinputhost", "applicationframehost", "nvidia overlay", "systemsettings"}
    
    seen_names = set()
    known_running = []
    for app in visible_apps:
        proc = app["process"].lower()
        title = app["title"]
        
        # Skip noise
        if proc in NOISE_FILTERS:
            continue
        
        # Try to match against APP_REGISTRY for a clean display name
        matched = False
        for key, info in APP_REGISTRY.items():
            exe_names_clean = [e.replace(".exe", "").lower() for e in info["exe_names"]]
            if proc in exe_names_clean or key in proc:
                display_name = info["display_name"]
                if display_name not in seen_names:
                    known_running.append(f"{display_name} ({title})")
                    seen_names.add(display_name)
                matched = True
                break
        
        # If not in registry, use the process name + title directly
        if not matched and proc not in seen_names:
            # Capitalize the process name for readability
            display = proc.replace(".", " ").title()
            known_running.append(f"{display} ({title})")
            seen_names.add(proc)
    
    # 4. Quick check of key messaging apps
    messaging_apps = ["whatsapp", "telegram", "discord", "slack"]
    app_status_lines = []
    for app in messaging_apps:
        info = APP_REGISTRY.get(app, {})
        exe_names = info.get("exe_names", [])
        # Check running via both process list AND visible windows
        visible_procs = [a["process"] for a in visible_apps]
        is_running = (
            any(e.replace(".exe", "").lower() in running for e in exe_names) or
            any(e.replace(".exe", "").lower() in visible_procs for e in exe_names) or
            any(app in " ".join(visible_procs) for app in info.get("start_menu_keywords", []))
        )
        
        # Install check via Start Menu + Microsoft Store
        start_menu = _scan_start_menu()
        store_apps = _scan_store_apps()
        is_installed = any(kw in " ".join(start_menu) for kw in info.get("start_menu_keywords", []))
        # Also check Store/AppX
        if not is_installed and info.get("appx_keywords"):
            is_installed = any(
                any(kw in pkg for pkg in store_apps)
                for kw in info["appx_keywords"]
            )
        
        status = "RUNNING" if is_running else ("INSTALLED" if is_installed else "NOT INSTALLED")
        fallback = f" -> Web fallback: {info.get('web_fallback', 'N/A')}" if status == "NOT INSTALLED" and info.get("web_fallback") else ""
        app_status_lines.append(f"  - {info.get('display_name', app.title())}: {status}{fallback}")
    
    snapshot = f"""=== SYSTEM CONTEXT (auto-gathered) ===
Active Window: "{active['window_title']}"
Active Process: {active['process_name']}
Screen Resolution: {res[0]}x{res[1]}
Currently Running Apps: {', '.join(known_running) if known_running else 'None detected'}

Key App Status:
{chr(10).join(app_status_lines)}
=== END CONTEXT ==="""
    
    logger.info(f"Context snapshot built:\n{snapshot}")
    return snapshot
