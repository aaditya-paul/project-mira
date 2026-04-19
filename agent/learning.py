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

SUPPORTED_ACTIONS = {
    "switch_to_app",
    "type_keyboard",
    "run_command",
    "click_mouse",
    "scroll_mouse",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_get_text",
    "browser_get_state",
    "browser_new_tab",
    "browser_close_tab",
    "browser_wait_for",
    "browser_scroll",
}

BROWSER_HINT_TOKENS = {
    "browser", "web", "website", "url", "youtube", "instagram", "gmail",
    "duckduckgo", "google", "spotify", "reddit", "twitter", "x", "linkedin",
    "facebook", "search", "navigate", "open"
}

MESSAGING_HINT_TOKENS = {
    "send", "message", "dm", "chat", "text", "reply", "whatsapp", "telegram", "discord"
}

MUSIC_HINT_TOKENS = {
    "music", "song", "playlist", "listen", "play", "album", "artist"
}

WEAK_SELECTOR_PATTERNS = (
    r"^(input|a|button|textarea|div|span)$",
    r"^\[role=['\"]?(button|link|textbox|searchbox)['\"]?\]$",
)

SITE_WORKFLOW_HINTS = {
    "instagram": {
        "keywords": {"instagram", "insta"},
        "flow": "inbox_search",
        "message_urls": ["instagram.com/direct/inbox"],
        "recipient_selectors": [
            "input[aria-label*='Search' i], [role='searchbox'] input",
        ],
        "thread_selectors": [
            "a[href*='/direct/t/']",
            "[role='listbox'] [role='option']",
            "[role='grid'] [role='button']",
        ],
        "composer_selectors": [
            "textarea[placeholder*='Message' i]",
            "div[contenteditable='true'][aria-label*='Message' i]",
        ],
        "send_selectors": [
            "button[aria-label*='Send' i]",
            "button[type='submit']",
        ],
        "disallowed_selector_fragments": ["/users/"],
    },
    "twitter_x": {
        "keywords": {"twitter", "x"},
        "flow": "inbox_search",
        "message_urls": ["x.com/messages", "twitter.com/messages"],
        "recipient_selectors": [
            "input[data-testid='SearchBox_Search_Input'], input[aria-label*='Search' i]",
        ],
        "thread_selectors": [
            "a[href*='/messages/']",
            "[data-testid='cellInnerDiv'] a[href*='/messages/']",
        ],
        "composer_selectors": [
            "div[contenteditable='true'][data-testid*='dmcomposer' i]",
            "div[aria-label*='Message' i][contenteditable='true']",
        ],
        "send_selectors": [
            "button[data-testid='dmComposerSendButton']",
            "button[aria-label*='Send' i]",
        ],
        "disallowed_selector_fragments": [],
    },
    "reddit": {
        "keywords": {"reddit"},
        "flow": "compose_form",
        "message_urls": ["reddit.com/message/compose", "reddit.com/messages"],
        "recipient_selectors": [
            "input[name='to'], input[name='recipient']",
        ],
        "thread_selectors": [
            "a[href*='/message/messages']",
            "a[href*='/message/compose']",
        ],
        "composer_selectors": [
            "textarea[name='message']",
            "textarea[placeholder*='Message' i]",
        ],
        "send_selectors": [
            "button[type='submit']",
            "button[aria-label*='Send' i]",
        ],
        "disallowed_selector_fragments": [],
    },
    "linkedin": {
        "keywords": {"linkedin"},
        "flow": "inbox_search",
        "message_urls": ["linkedin.com/messaging"],
        "recipient_selectors": [
            "input[placeholder*='Search messages' i], [role='textbox'][aria-label*='Search messages' i]",
        ],
        "thread_selectors": [
            "a[href*='/messaging/thread/']",
            "[data-control-name*='message']",
        ],
        "composer_selectors": [
            "div[contenteditable='true'][role='textbox']",
            "textarea[placeholder*='message' i]",
        ],
        "send_selectors": [
            "button[type='submit']",
            "button[aria-label*='Send' i]",
        ],
        "disallowed_selector_fragments": [],
    },
}

SITE_NAME_LABELS = {
    "instagram": "Instagram",
    "twitter_x": "Twitter/X",
    "reddit": "Reddit",
    "linkedin": "LinkedIn",
}


# A real playbook example to show the LLM exactly what format we expect.
# This is the highest-quality reference — open_url with all its edge-case notes.
EXAMPLE_PLAYBOOK = '''name: open_url
description: "Open a website or URL in the default web browser"

triggers:
    - "open {url}"
    - "go to {url}"
    - "navigate to {url}"

variables:
    url:
        description: "The URL or website name to navigate to"
        required: true

steps:
    - action: browser_navigate
        params:
            url: "{url}"
        description: "Navigate to {url} via Playwright CDP"
        expect: "page_loaded"
        risk_level: "low"
        notes: >
            Use browser_navigate for web tasks instead of keyboard URL typing.
            This reuses the same browser mechanism as other browser playbooks.
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

        similar_playbooks_context = self._build_similar_playbook_context(task, max_refs=3)
        site_guidance_context = self._build_site_workflow_guidance(task)

        architect_prompt = f"""You are a PLAYBOOK ARCHITECT for a desktop automation agent on Windows 10/11.

Your job: design a detailed, step-by-step YAML playbook for a task that the agent hasn't seen before.

## REFERENCE EXAMPLE
Here is a PERFECT playbook. Study its format, detail level, and style EXACTLY:

```yaml
{EXAMPLE_PLAYBOOK}
```

## SIMILAR EXISTING PLAYBOOKS (REUSE THESE MECHANISMS)
{similar_playbooks_context}

## SITE WORKFLOW GUIDANCE (APPLY WHEN RELEVANT)
{site_guidance_context}

When creating the new playbook:
- First check the similar playbooks above and follow their mechanism style.
- If similar playbooks use browser_* actions for this task type, use browser_* actions too.
- Do NOT switch to coordinate clicking unless there is no browser mechanism available.
- For social sites (Instagram, Reddit, Twitter/X, LinkedIn), avoid homepage-only flows for DM tasks.
- Prefer direct inbox/messages URLs and specific selectors over generic tag selectors.

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
- browser_navigate(url): Navigate directly to a web URL using Playwright CDP.
- browser_click(selector): Click a DOM element by CSS selector or visible text.
- browser_type(selector, text, clear_first): Type into browser input fields.
- browser_press_key(key): Press a key in browser context.
- browser_wait_for(selector, text, timeout): Wait for browser element/text to appear.
- browser_scroll(direction, amount): Scroll the browser page.
- browser_get_state(): Inspect browser URL/title/tab state.
- browser_new_tab(url): Open a new browser tab.
- browser_close_tab(): Close active browser tab.
- browser_get_text(): Read visible text from current browser page.

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
    - Use ONLY supported actions listed above. NEVER invent actions like `wait`.
    - NEVER use weak selectors like `input`, `button`, `a`, or `textarea` by themselves.
    - Use stable attribute-rich selectors (aria-label, role+name, href fragment, data-testid).
5. **Default browser is Brave Browser** (NOT Edge, NOT Chrome).
6. **NEVER generate login/password steps**. Assume user is already logged in.
7. **NEVER use placeholder values** like "username" or "user@example.com".
8. For DM/message tasks, include a `message` variable with `compose: true` and a safe default.

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

                # Validate steps have required fields and supported actions
                invalid_action = False
                for i, step in enumerate(playbook["steps"]):
                    action = step.get("action", "")
                    if not action:
                        logger.error(f"Step {i+1} missing 'action' field")
                        invalid_action = True
                        break
                    if action not in SUPPORTED_ACTIONS:
                        logger.error(
                            "Generated playbook uses unsupported action '%s' in step %s",
                            action,
                            i + 1,
                        )
                        invalid_action = True
                        break
                    if "params" not in step:
                        step["params"] = {}
                    if "description" not in step:
                        step["description"] = f"Step {i+1}"
                    if "expect" not in step:
                        step["expect"] = ""
                    if "risk_level" not in step:
                        step["risk_level"] = "low"

                if invalid_action:
                    continue

                quality_ok, quality_reason = self._validate_playbook_quality(playbook, task)
                if not quality_ok:
                    logger.error(f"Generated playbook quality check failed: {quality_reason}")
                    continue

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

        # Final fallback: deterministic rule-based playbook generation for common web/social flows.
        fallback_name = self._create_rule_based_playbook(task)
        if fallback_name:
            logger.info(f"Created fallback rule-based playbook '{fallback_name}'")
            return fallback_name

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

    def _build_similar_playbook_context(self, task: str, max_refs: int = 3) -> str:
        """Build a prompt section containing the most similar existing playbooks."""
        similar = self._find_similar_playbooks(task, max_refs=max_refs)
        if not similar:
            return "No strong similar playbooks found."

        chunks = []
        for i, item in enumerate(similar, start=1):
            actions = ", ".join(item["actions"]) if item["actions"] else "none"
            reasons = "; ".join(item["reasons"]) if item["reasons"] else "general similarity"
            chunks.append(
                f"### Similar Playbook {i}: {item['name']}\n"
                f"Why similar: {reasons}\n"
                f"Actions used: {actions}\n"
                f"```yaml\n{item['raw_yaml'].strip()}\n```"
            )
        return "\n\n".join(chunks)

    def _find_similar_playbooks(self, task: str, max_refs: int = 3) -> list[dict]:
        """Score and return similar playbooks so new playbooks reuse existing mechanisms."""
        task_lower = (task or "").lower()
        task_tokens = self._tokenize(task_lower)

        task_is_browser = any(token in task_tokens for token in BROWSER_HINT_TOKENS)
        task_is_messaging = any(token in task_tokens for token in MESSAGING_HINT_TOKENS)
        task_is_music = any(token in task_tokens for token in MUSIC_HINT_TOKENS)

        scored = []
        for filepath in sorted(self.playbooks_dir.glob("*.yaml")):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    raw_yaml = f.read()
                playbook = yaml.safe_load(raw_yaml)
                if not isinstance(playbook, dict):
                    continue
            except Exception as e:
                logger.warning(f"Skipping playbook {filepath} during similarity scan: {e}")
                continue

            name = str(playbook.get("name", filepath.stem))
            description = str(playbook.get("description", ""))
            triggers = playbook.get("triggers", [])
            trigger_text = " ".join(t for t in triggers if isinstance(t, str))

            steps = playbook.get("steps", [])
            actions = []
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict):
                        action = str(step.get("action", "")).strip()
                        if action:
                            actions.append(action)

            has_browser_actions = any(a.startswith("browser_") for a in actions)

            # For browser tasks, only compare against browser-based playbooks so
            # the newly generated playbook adopts the same mechanism family.
            if task_is_browser and not has_browser_actions:
                continue

            pb_text = f"{name} {description} {trigger_text} {' '.join(actions)}"
            pb_tokens = self._tokenize(pb_text)

            score, reasons = self._score_similarity(
                task_tokens=task_tokens,
                playbook_tokens=pb_tokens,
                has_browser_actions=has_browser_actions,
                task_is_browser=task_is_browser,
                task_is_messaging=task_is_messaging,
                task_is_music=task_is_music,
            )

            if score <= 0:
                continue

            scored.append(
                {
                    "name": name,
                    "score": score,
                    "reasons": reasons,
                    "actions": list(dict.fromkeys(actions)),
                    "raw_yaml": raw_yaml,
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:max_refs]

    def _score_similarity(
        self,
        task_tokens: set[str],
        playbook_tokens: set[str],
        has_browser_actions: bool,
        task_is_browser: bool,
        task_is_messaging: bool,
        task_is_music: bool,
    ) -> tuple[int, list[str]]:
        """Compute a weighted similarity score between task intent and a playbook."""
        score = 0
        reasons = []

        overlap = task_tokens & playbook_tokens
        if overlap:
            top_overlap = sorted(overlap)[:4]
            score += min(len(overlap) * 3, 15)
            reasons.append(f"keyword overlap: {', '.join(top_overlap)}")

        if task_is_browser and has_browser_actions:
            score += 10
            reasons.append("browser mechanism match")

        if task_is_messaging and (playbook_tokens & MESSAGING_HINT_TOKENS):
            score += 6
            reasons.append("messaging intent match")

        if task_is_music and (playbook_tokens & MUSIC_HINT_TOKENS):
            score += 6
            reasons.append("music intent match")

        return score, reasons

    def _tokenize(self, text: str) -> set[str]:
        """Tokenize text into lowercase alphanumeric keywords for rough similarity."""
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    def _build_site_workflow_guidance(self, task: str) -> str:
        """Build task-specific website workflow hints for improved playbook accuracy."""
        task_tokens = self._tokenize(task)
        matched_sites = self._match_site_hints(task_tokens)
        is_messaging_task = any(token in task_tokens for token in MESSAGING_HINT_TOKENS)

        lines = [
            "- Use browser_* actions for web workflows.",
            "- Add explicit browser_wait_for before browser_click when waiting on dynamic content.",
            "- Prefer exact, attribute-rich selectors; avoid generic tags.",
        ]

        if is_messaging_task:
            lines.extend([
                "- This is a DM/messaging task: navigate to inbox/messages routes first, not homepage search.",
                "- Wait for thread list, then open thread, then wait for message composer before typing.",
                "- Include message variable with compose:true and a safe default.",
            ])

        if not matched_sites:
            lines.append("- No known site hint matched. Use robust generic social-web patterns.")
            return "\n".join(lines)

        for site in matched_sites:
            site_name = site["site"]
            urls = ", ".join(site["message_urls"])
            threads = ", ".join(site["thread_selectors"])
            composers = ", ".join(site["composer_selectors"])
            lines.append(f"- {site_name}: preferred message URLs -> {urls}")
            lines.append(f"- {site_name}: thread selectors -> {threads}")
            lines.append(f"- {site_name}: composer selectors -> {composers}")

        return "\n".join(lines)

    def _match_site_hints(self, task_tokens: set[str]) -> list[dict]:
        """Return website hints matching the current task tokens."""
        matched = []
        for site_name, data in SITE_WORKFLOW_HINTS.items():
            keywords = data.get("keywords", set())
            if task_tokens & keywords:
                merged = dict(data)
                merged["site"] = site_name
                matched.append(merged)
        return matched

    def _validate_playbook_quality(self, playbook: dict, task: str) -> tuple[bool, str]:
        """Reject low-accuracy generated playbooks before writing to disk."""
        steps = playbook.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return False, "No executable steps found"

        task_tokens = self._tokenize(task)
        is_browser_task = any(token in task_tokens for token in BROWSER_HINT_TOKENS)
        is_messaging_task = any(token in task_tokens for token in MESSAGING_HINT_TOKENS)
        matched_sites = self._match_site_hints(task_tokens)

        actions = [str(step.get("action", "")) for step in steps]
        has_browser_actions = any(action.startswith("browser_") for action in actions)

        if is_browser_task and not has_browser_actions:
            return False, "Browser task generated without browser_* actions"

        if is_browser_task and "browser_navigate" not in actions:
            return False, "Browser task should include browser_navigate"

        for i, step in enumerate(steps, start=1):
            action = str(step.get("action", ""))
            params = step.get("params", {}) if isinstance(step.get("params", {}), dict) else {}
            selector = str(params.get("selector", "")).strip().lower()

            if action in {"browser_click", "browser_wait_for", "browser_type"} and selector:
                if self._is_weak_selector(selector):
                    return False, f"Step {i} uses weak selector '{selector}'"

                for site in matched_sites:
                    for bad_fragment in site.get("disallowed_selector_fragments", []):
                        if bad_fragment and bad_fragment in selector:
                            return False, (
                                f"Step {i} selector '{selector}' contains disallowed fragment "
                                f"'{bad_fragment}' for site {site['site']}"
                            )

        if is_messaging_task:
            variables = playbook.get("variables", {})
            if not isinstance(variables, dict) or "message" not in variables:
                return False, "Messaging task must define a message variable"

            message_var = variables.get("message", {})
            if isinstance(message_var, dict):
                if message_var.get("compose") is not True:
                    return False, "Message variable should set compose: true"
                if "default" not in message_var:
                    return False, "Message variable should include a safe default"

            message_type_steps = [
                step for step in steps
                if step.get("action") in {"browser_type", "type_keyboard"}
                and "{message}" in str(step.get("params", {}).get("text", ""))
            ]
            if not message_type_steps:
                return False, "Messaging task missing step that types {message}"

            nav_urls = [
                str(step.get("params", {}).get("url", "")).lower()
                for step in steps
                if step.get("action") == "browser_navigate"
            ]

            for site in matched_sites:
                preferred_urls = site.get("message_urls", [])
                if preferred_urls and nav_urls:
                    if not any(
                        any(preferred in url for preferred in preferred_urls)
                        for url in nav_urls
                    ):
                        return False, (
                            f"Messaging task for {site['site']} should navigate to inbox/messages URL"
                        )

        return True, "ok"

    def _is_weak_selector(self, selector: str) -> bool:
        """Detect selectors that are too generic to be reliable across real websites."""
        selector = (selector or "").strip().lower()
        if not selector:
            return False
        return any(re.fullmatch(pattern, selector) for pattern in WEAK_SELECTOR_PATTERNS)

    def _create_rule_based_playbook(self, task: str) -> str | None:
        """Create a deterministic browser-first playbook when LLM generation fails."""
        task_tokens = self._tokenize(task)
        is_messaging_task = any(token in task_tokens for token in MESSAGING_HINT_TOKENS)
        matched_sites = self._match_site_hints(task_tokens)

        if not is_messaging_task or not matched_sites:
            return None

        site = matched_sites[0]
        site_key = site["site"]
        site_label = SITE_NAME_LABELS.get(site_key, site_key.replace("_", " ").title())
        flow = site.get("flow", "inbox_search")

        playbook_name = self._sanitize_name(f"send_{site_key}_dm")
        navigate_url = site.get("message_urls", [""])[0]
        recipient_selector = ", ".join(site.get("recipient_selectors", []))
        thread_selector = ", ".join(site.get("thread_selectors", []))
        composer_selector = ", ".join(site.get("composer_selectors", []))
        send_selector = ", ".join(site.get("send_selectors", []))

        steps = [
            {
                "action": "browser_navigate",
                "params": {"url": navigate_url},
                "description": f"Open {site_label} messages/inbox via Playwright",
                "expect": "messages_open",
                "risk_level": "low",
                "notes": "Deterministic fallback flow: browser-first and selector-driven.",
            },
        ]

        if flow == "compose_form":
            steps.extend([
                {
                    "action": "browser_wait_for",
                    "params": {"selector": recipient_selector, "timeout": 12},
                    "description": "Wait for recipient input",
                    "expect": "recipient_input_visible",
                    "risk_level": "low",
                },
                {
                    "action": "browser_type",
                    "params": {"selector": recipient_selector, "text": "{recipient}"},
                    "description": "Type recipient username",
                    "expect": "recipient_typed",
                    "risk_level": "low",
                },
                {
                    "action": "browser_wait_for",
                    "params": {"selector": composer_selector, "timeout": 12},
                    "description": "Wait for message composer",
                    "expect": "composer_visible",
                    "risk_level": "low",
                },
                {
                    "action": "browser_type",
                    "params": {"selector": composer_selector, "text": "{message}"},
                    "description": "Type message",
                    "expect": "message_typed",
                    "risk_level": "low",
                },
            ])
        else:
            steps.extend([
                {
                    "action": "browser_type",
                    "params": {"selector": recipient_selector, "text": "{recipient}"},
                    "description": "Search for recipient",
                    "expect": "recipient_typed",
                    "risk_level": "low",
                },
                {
                    "action": "browser_wait_for",
                    "params": {"selector": thread_selector, "timeout": 12},
                    "description": "Wait for recipient thread results",
                    "expect": "thread_results_visible",
                    "risk_level": "low",
                },
                {
                    "action": "browser_press_key",
                    "params": {"key": "Enter"},
                    "description": "Open top matching thread",
                    "expect": "thread_open",
                    "risk_level": "medium",
                },
                {
                    "action": "browser_wait_for",
                    "params": {"selector": composer_selector, "timeout": 12},
                    "description": "Wait for message composer",
                    "expect": "composer_visible",
                    "risk_level": "low",
                },
                {
                    "action": "browser_type",
                    "params": {"selector": composer_selector, "text": "{message}"},
                    "description": "Type message",
                    "expect": "message_typed",
                    "risk_level": "low",
                },
            ])

        if send_selector:
            steps.extend([
                {
                    "action": "browser_wait_for",
                    "params": {"selector": send_selector, "timeout": 10},
                    "description": "Wait for send control",
                    "expect": "send_control_visible",
                    "risk_level": "low",
                },
                {
                    "action": "browser_click",
                    "params": {"selector": send_selector},
                    "description": "Send message",
                    "expect": "message_sent",
                    "risk_level": "medium",
                },
            ])
        else:
            steps.append(
                {
                    "action": "browser_press_key",
                    "params": {"key": "Enter"},
                    "description": "Send message",
                    "expect": "message_sent",
                    "risk_level": "medium",
                }
            )

        playbook = {
            "name": playbook_name,
            "description": f"Send a direct message on {site_label} using browser-first fallback flow",
            "triggers": [
                f"send dm to {{recipient}} on {site_label.lower()}",
                f"message {{recipient}} on {site_label.lower()}",
                f"open {site_label.lower()} and send dm to {{recipient}}",
                f"{site_label.lower()} dm {{recipient}}",
            ],
            "variables": {
                "recipient": {
                    "description": f"{site_label} username/contact to message",
                    "required": True,
                },
                "message": {
                    "description": "Message text to send. If missing, compose a short natural message.",
                    "required": False,
                    "default": "Hey!",
                    "compose": True,
                },
            },
            "steps": steps,
        }

        quality_ok, reason = self._validate_playbook_quality(playbook, task)
        if not quality_ok:
            logger.error(f"Rule-based fallback playbook rejected by quality gate: {reason}")
            return None

        filepath = self.playbooks_dir / f"{playbook_name}.yaml"
        yaml_text = yaml.safe_dump(playbook, sort_keys=False, allow_unicode=False)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(yaml_text)

        return playbook_name
