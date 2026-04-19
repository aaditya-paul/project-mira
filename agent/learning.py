"""
Playbook Auto-Creator — Generates new playbooks when no existing one matches.

When Mira encounters a task it has never seen before, instead of falling back to
flaky dynamic LLM planning, it first creates a permanent, reusable playbook.
The playbook is saved to disk so Mira never has to "figure out" the same task twice.

Architecture:
  User: "install a VS Code extension"
    → Intent Matcher: no playbook matches
    → PlaybookArchitect: generates new YAML playbook from scratch
    → Saves to prompts/playbooks/install_vscode_extension.yaml
    → PlaybookEngine reloads & renders the new playbook
    → Executor runs the concrete steps
"""
import json
import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger("mira.learning")


# A real playbook example to show the LLM exactly what format we expect.
# This is the highest-quality reference — open_url with all its edge-case notes.
EXAMPLE_PLAYBOOK = '''name: open_url
description: "Open a website or URL in the default web browser"

triggers:
  - "open {url}"
  - "go to {url}"
  - "check my emails"
  - "open gmail"

url_aliases:
  emails: "gmail.com"
  gmail: "gmail.com"

variables:
  url:
    description: "The URL or website name to navigate to"
    required: true
  browser:
    description: "Which browser to use"
    default: "Brave Browser"

steps:
  - action: switch_to_app
    params:
      app_name: "{browser}"
    description: "Bring the browser to foreground"
    expect: "browser_foreground"
    risk_level: "medium"
    notes: >
      If the browser is NOT RUNNING, the executor will automatically launch it.
      The browser might open showing ANY page — that is perfectly fine.

  - action: type_keyboard
    params:
      hotkey: "ctrl,l"
    description: "Focus the address bar with Ctrl+L (NOT Ctrl+F!)"
    expect: "address_bar_focused"
    risk_level: "medium"
    notes: >
      CRITICAL: ctrl+l = ADDRESS BAR. ctrl+f = FIND IN PAGE (wrong!).
      After pressing ctrl+l, the current URL becomes highlighted/selected.

  - action: type_keyboard
    params:
      text: "{url}"
    description: "Type the URL into the address bar"
    expect: "url_typed"
    risk_level: "low"
    notes: >
      The old content was auto-selected by ctrl+l, so typing REPLACES it.
      Do NOT add "https://" — the browser adds it automatically.

  - action: type_keyboard
    params:
      hotkey: "enter"
    description: "Press Enter to navigate to the URL"
    expect: "page_loading"
    risk_level: "low"
    notes: >
      The window title will change to reflect the new page.
'''


class PlaybookArchitect:
    """
    Generates new playbook YAML files by teaching the LLM the exact format
    and style, then having it design a procedure for an unseen task.
    """

    def __init__(self, playbooks_dir: str = None):
        if playbooks_dir is None:
            playbooks_dir = str(Path(__file__).parent.parent / "prompts" / "playbooks")
        self.playbooks_dir = Path(playbooks_dir)

    def create_playbook(
        self,
        task: str,
        context: str,
        clients: dict,
        providers: dict,
        fallback_chain: list,
    ) -> str | None:
        """
        Generate a new playbook YAML for a task that has no existing playbook.

        Returns:
            The playbook name (filename stem) if created successfully, None on failure.
        """

        architect_prompt = f"""You are a PLAYBOOK ARCHITECT for a desktop automation agent on Windows 10/11.

Your job: design a detailed, step-by-step YAML playbook for a task that the agent hasn't seen before.

## REFERENCE EXAMPLE
Here is a PERFECT playbook. Study its format, detail level, and style EXACTLY:

```yaml
{EXAMPLE_PLAYBOOK}
```

## SYSTEM CONTEXT (current state of the user's desktop)
{context}

## USER'S TASK
"{task}"

## AVAILABLE ACTIONS (the ONLY actions the executor can perform)
- switch_to_app(app_name): Bring a running app window to foreground. Auto-launches if not running.
- type_keyboard(text): Type a string of text into the currently focused field.
- type_keyboard(hotkey): Press a keyboard shortcut. Format: "ctrl,l" or "alt,f4" or "enter".
  Only ONE of text or hotkey per step — NEVER both.
- click_mouse(x, y, button): Click at screen coordinates. button = "left" or "right".
- scroll_mouse(clicks): Scroll. Positive = up, negative = down.

## CRITICAL KEYBOARD SHORTCUTS (memorize — getting these wrong breaks everything)
- ctrl+l = FOCUS ADDRESS BAR in browsers. Use to type a URL.
- ctrl+f = FIND IN PAGE / SEARCH in apps. NEVER use for URL navigation.
- ctrl+t = NEW TAB in browsers.
- ctrl+w = CLOSE TAB in browsers.
- ctrl+n = NEW WINDOW in most apps.
- ctrl+s = SAVE in most apps.
- ctrl+a = SELECT ALL.
- ctrl+c / ctrl+v = COPY / PASTE.
- alt+tab = SWITCH between apps.
- alt+f4 = CLOSE current window.
- win = OPEN START MENU. Then type app name + enter to launch.
- tab = MOVE FOCUS to next UI element.
- escape = CLOSE popup / CANCEL.

## PLAYBOOK DESIGN RULES

1. **Name**: Short, snake_case, descriptive. Example: "install_vscode_extension", "send_discord_message"
2. **Triggers**: 5-10 natural language phrases users might say. Include {{variable}} placeholders.
3. **Variables**: Define ALL dynamic parts. Mark required/optional. Add defaults where sensible.
4. **Steps**: Write as if explaining to a 5-year-old who has never used a computer:
   - One action per step. NEVER combine two actions.
   - DETAILED notes for every step explaining WHY, edge cases, what could go wrong.
   - Use the CORRECT keyboard shortcuts (ctrl+l for address bar, NOT ctrl+f).
   - Include expect (short verification tag) and risk_level (low/medium/high).
   - If a step is conditional, add `condition: "condition_name"`.
5. **Default browser is Brave Browser** (NOT Edge, NOT Chrome).
6. **NEVER generate login/password steps**. Assume user is already logged in.
7. **NEVER use placeholder values** like "username" or "user@example.com".

## OUTPUT FORMAT
Return ONLY valid YAML. No markdown fences, no explanation, no preamble. Just the raw YAML content.
Start with "name:" on the first line."""

        for provider in fallback_chain:
            try:
                client = clients.get(provider)
                if not client:
                    continue
                model = providers.get(provider, {}).get("model", "")

                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a playbook architect. Output ONLY valid YAML. "
                                "No markdown fences, no explanation. Start with 'name:' on line 1."
                            ),
                        },
                        {"role": "user", "content": architect_prompt},
                    ],
                    temperature=0.2,
                )

                raw = resp.choices[0].message.content.strip()

                # Strip markdown fences if the LLM wrapped it
                raw = self._strip_fences(raw)

                # Validate it's parseable YAML
                playbook = yaml.safe_load(raw)
                if not isinstance(playbook, dict):
                    logger.error(f"LLM returned non-dict YAML: {type(playbook)}")
                    continue

                # Validate required fields
                if not playbook.get("name"):
                    logger.error("Generated playbook missing 'name' field")
                    continue
                if not playbook.get("steps"):
                    logger.error("Generated playbook missing 'steps' field")
                    continue

                # Sanitize the name for filesystem use
                pb_name = self._sanitize_name(playbook["name"])
                playbook["name"] = pb_name

                # Validate steps have required fields
                for i, step in enumerate(playbook["steps"]):
                    if "action" not in step:
                        logger.error(f"Step {i+1} missing 'action' field")
                        continue
                    if "params" not in step:
                        step["params"] = {}
                    if "description" not in step:
                        step["description"] = f"Step {i+1}"
                    if "expect" not in step:
                        step["expect"] = ""
                    if "risk_level" not in step:
                        step["risk_level"] = "low"

                # Save to disk
                filepath = self.playbooks_dir / f"{pb_name}.yaml"
                with open(filepath, "w", encoding="utf-8") as f:
                    # Write the raw LLM output (already valid YAML) rather than
                    # yaml.dump() which loses comments and formatting
                    f.write(raw)

                logger.info(
                    f"Created new playbook '{pb_name}' with {len(playbook['steps'])} steps "
                    f"at {filepath}"
                )
                return pb_name

            except yaml.YAMLError as e:
                logger.error(f"Generated YAML is invalid ({provider}): {e}")
                continue
            except Exception as e:
                logger.error(f"Playbook creation failed with {provider}: {e}")
                continue

        logger.error("Playbook creation exhausted all providers")
        return None

    def _strip_fences(self, text: str) -> str:
        """Remove markdown code fences from LLM output."""
        # Handle ```yaml ... ``` wrapping
        if text.startswith("```"):
            # Remove first line (```yaml or ```)
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    def _sanitize_name(self, name: str) -> str:
        """Convert a playbook name to a safe filesystem-friendly snake_case string."""
        # Lowercase, replace spaces/hyphens with underscores
        name = name.lower().strip()
        name = re.sub(r"[^a-z0-9_]", "_", name)
        # Collapse multiple underscores
        name = re.sub(r"_+", "_", name)
        name = name.strip("_")
        return name or "unknown_playbook"
