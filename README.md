# Project Mira: Autonomous Desktop Agent

Project Mira is a high-performance, autonomous desktop automation agent designed to execute complex tasks using a **Plan-Execute** architecture. Unlike reactive agents that rely on expensive vision loops for every action, Mira generates a granular JSON task plan and executes it directly using non-vision verification (window title/process checks) to ensure speed and predictability.

## 🚀 Core Features

- **Plan-Execute Architecture**: Decouples high-level reasoning from low-level execution. Generates a multi-step plan in one LLM call, then executes it directly.
- **Smart Navigation**: Uses Win32 APIs (via PowerShell) to instantly switch between apps or launch closed apps (supporting Store/AppX and Start Menu shortcuts).
- **Vision Fallback**: Only uses vision analysis (Gemini/Ollama) as a diagnostic tool when a step fails or for critical state verification.
- **Privacy & Performance Focus**: Optimized for local-first operations with support for a fallback chain of providers (Groq, Ollama, Gemini, NVIDIA).
- **Keyboard-First Interaction**: Prioritizes robust keyboard shortcuts (Ctrl+F for search, Win key for launching) over fragile mouse-based interactions.
- **Automatic State Verification**: Performs instant, zero-latency window title and process checks after every action to ensure the agent is in the right place.

## 🛠️ Architecture

Mira operates in two distinct phases:

1.  **Phase 1: The Planner**: Takes the user's task and current system context (running apps, resolution, etc.) and generates a precise JSON array of atomic steps.
2.  **Phase 2: The Executor**: A direct Python-based execution engine that parses the JSON and calls primitive tools (`switch_to_app`, `type_keyboard`, etc.) without per-step LLM overhead.

## ⚙️ Configuration

The agent is configured via `config.json`:

```json
{
  "fallback_chain": ["groq", "ollama", "gemini", "nvidia"],
  "vision_analyzer": {
    "provider": "ollama",
    "model": "gemma4:e2b",
    "fallback": "gemini",
    "fallback_model": "gemini-2.5-flash"
  },
  "providers": {
    "ollama": { "url": "http://localhost:11434/v1" },
    "gemini": { "model": "gemini-2.5-flash" },
    "groq": { "model": "meta-llama/llama-4-scout-17b-16e-instruct" }
  }
}
```

## 📦 Default App Logic

Mira comes with pre-defined defaults for common tasks:
- **Texting**: WhatsApp
- **Browsing**: Brave Browser
- **Music**: YouTube Music
- **Media**: YouTube
- **Search**: DuckDuckGo

## 🏗️ Getting Started

1.  **Environment Setup**:
    ```bash
    python -m venv venv
    .\venv\Scripts\activate
    pip install -r requirements.txt
    ```

2.  **API Keys**:
    Set your environment variables for `GROQ_API_KEY`, `GEMINI_API_KEY`, etc.

3.  **Run Mira**:
    ```bash
    python mira.py
    ```

## 📜 Primitives

- `switch_to_app(app_name)`: Instantly focus a window by title or process name.
- `launch_app(app_name)`: Command-line launching of desktop and Store apps.
- `type_keyboard(text, hotkey)`: High-fidelity typing and shortcut execution.
- `vision()`: Diagnostic screenshot analysis with coordinate-grid overlay.

---
*Built for Advanced Agentic Coding.*
