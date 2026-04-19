"""
Playbook System — Pre-written detailed procedures for common desktop actions.

Instead of asking the LLM to generate plans from scratch (which produces garbage),
playbooks provide battle-tested, edge-case-aware step sequences. The LLM's only
job is to MATCH user intent to a playbook and EXTRACT variables.

Architecture:
  User: "check my emails"
    → Intent Matcher (LLM): playbook="open_url", variables={url: "gmail.com"}
    → Renderer: fills {url} into the playbook template
    → Returns concrete plan: switch_to_app → ctrl+l → type gmail.com → enter
"""
import os
import json
import logging
from pathlib import Path

import yaml
from openai import OpenAI

logger = logging.getLogger("mira.playbooks")


class PlaybookEngine:
    """Loads, matches, and renders playbooks from YAML files."""

    def __init__(self, playbooks_dir: str = None, config: dict = None):
        if playbooks_dir is None:
            playbooks_dir = str(Path(__file__).parent.parent / "prompts" / "playbooks")
        self.playbooks_dir = Path(playbooks_dir)
        self.playbooks: dict[str, dict] = {}
        self.config = config or {}
        self._load_playbooks()

    def _load_playbooks(self):
        """Load all YAML playbooks from the playbooks directory."""
        if not self.playbooks_dir.exists():
            logger.warning(f"Playbooks directory not found: {self.playbooks_dir}")
            return

        for yaml_file in self.playbooks_dir.glob("*.yaml"):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    playbook = yaml.safe_load(f)
                name = playbook.get("name", yaml_file.stem)
                self.playbooks[name] = playbook
                logger.info(f"Loaded playbook: {name} ({len(playbook.get('steps', []))} steps)")
            except Exception as e:
                logger.error(f"Failed to load playbook {yaml_file}: {e}")

        logger.info(f"Loaded {len(self.playbooks)} playbooks: {list(self.playbooks.keys())}")

    def reload(self):
        """Reload all playbooks from disk. Used after a new playbook is created."""
        self.playbooks.clear()
        self._load_playbooks()

    def reload_single(self, playbook_name: str) -> bool:
        """Load a single newly-created playbook by name without reloading everything."""
        filepath = self.playbooks_dir / f"{playbook_name}.yaml"
        if not filepath.exists():
            logger.error(f"Playbook file not found: {filepath}")
            return False
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                playbook = yaml.safe_load(f)
            name = playbook.get("name", filepath.stem)
            self.playbooks[name] = playbook
            logger.info(f"Hot-loaded new playbook: {name} ({len(playbook.get('steps', []))} steps)")
            return True
        except Exception as e:
            logger.error(f"Failed to hot-load playbook {filepath}: {e}")
            return False

    def get_playbook_summary(self) -> str:
        """Build a compact summary of all available playbooks for the intent matcher."""
        lines = []
        for name, pb in self.playbooks.items():
            triggers = pb.get("triggers", [])
            variables = pb.get("variables", {})
            var_names = list(variables.keys()) if isinstance(variables, dict) else []

            # Build variable descriptions
            var_descs = []
            for vname in var_names:
                vinfo = variables[vname]
                if isinstance(vinfo, dict):
                    desc = vinfo.get("description", "")
                    required = vinfo.get("required", False)
                    default = vinfo.get("default", None)
                    compose = vinfo.get("compose", False)
                    parts = [f"{vname}"]
                    if required:
                        parts.append("(REQUIRED)")
                    if default is not None:
                        parts.append(f"[default: {default}]")
                    if compose:
                        parts.append("[compose natural message]")
                    if desc:
                        parts.append(f"— {desc}")
                    var_descs.append(" ".join(parts))
                else:
                    var_descs.append(f"{vname} — {vinfo}")

            # Include URL aliases for open_url
            aliases = pb.get("url_aliases", {})
            alias_examples = ""
            if aliases:
                examples = list(aliases.items())[:8]
                alias_examples = "\n    URL aliases: " + ", ".join(f'"{k}"→{v}' for k, v in examples)

            lines.append(
                f"  PLAYBOOK: {name}\n"
                f"    Description: {pb.get('description', '')}\n"
                f"    Triggers: {', '.join(triggers[:5])}\n"
                f"    Variables: {'; '.join(var_descs) if var_descs else 'none'}"
                f"{alias_examples}"
            )
        return "\n\n".join(lines)

    def match_playbook(self, task: str, context: str, clients: dict, providers: dict, fallback_chain: list) -> tuple[str | None, dict]:
        """
        Use a lightweight LLM call to match a user task to a playbook and extract variables.

        Returns:
            (playbook_name, variables) if matched
            (None, {}) if no playbook matches
        """
        playbook_summary = self.get_playbook_summary()

        # Get user profile from config for variable substitution
        user_profile = self.config.get("user_profile", {})
        profile_str = ""
        if user_profile:
            profile_str = "\n\nUSER PROFILE (use this info to fill variables):\n"
            for k, v in user_profile.items():
                profile_str += f"  {k}: {v}\n"

        match_prompt = f"""You are an INTENT MATCHER for a desktop automation agent.

Given a user's task, determine which playbook to use and extract the required variables.

AVAILABLE PLAYBOOKS:
{playbook_summary}
{profile_str}
SYSTEM CONTEXT:
{context}

USER TASK: "{task}"

RULES:
1. Match the task to the BEST playbook. Consider triggers and description.
2. Extract ALL required variables from the task text.
3. For URL aliases (like "emails" → "gmail.com"), resolve them automatically.
4. For "compose" variables (like messages), write a natural, conversational message as if YOU are the user speaking to their friend. Include greetings, proper tone, and the key information.
5. Use the USER PROFILE data when relevant (e.g., default browser).
6. Treat "open_app" and "open_url" as generic playbooks. Use them ONLY for pure open/switch/navigation requests with no additional objective.
7. If the task includes a secondary objective (send/message/DM/play/search/etc.) and no specialized playbook matches, set playbook to "none" so a new playbook can be created.
8. If the task doesn't match ANY playbook well, set playbook to "none".

OUTPUT FORMAT: Return ONLY valid JSON. No markdown, no explanation.
{{
  "playbook": "playbook_name_or_none",
  "variables": {{"var1": "value1", "var2": "value2"}},
  "reasoning": "one line explaining why this playbook matches"
}}

EXAMPLES:
Task: "check my emails" → {{"playbook": "open_url", "variables": {{"url": "gmail.com"}}, "reasoning": "User wants to check emails, which maps to gmail.com via url_aliases"}}
Task: "tell june I'll be late" → {{"playbook": "send_whatsapp", "variables": {{"contact": "June", "message": "Hey June, just wanted to let you know I'll be running a bit late. I'll be there soon!"}}, "reasoning": "User wants to send a message to June via WhatsApp"}}
Task: "search for the best restaurants in NYC" → {{"playbook": "search_web", "variables": {{"query": "best restaurants in NYC"}}, "reasoning": "User wants to search the web for restaurant info"}}
Task: "open spotify" → {{"playbook": "open_app", "variables": {{"app": "Spotify"}}, "reasoning": "User wants to open Spotify application"}}
Task: "open instagram and send a dm to rajdeep" → {{"playbook": "none", "variables": {{}}, "reasoning": "Task has a messaging objective; use a specialized DM playbook, not generic open_app/open_url"}}
Task: "deploy my kubernetes cluster" → {{"playbook": "none", "variables": {{}}, "reasoning": "No playbook covers Kubernetes deployment"}}"""

        for provider in fallback_chain:
            try:
                client = clients.get(provider)
                if not client:
                    continue
                model = providers.get(provider, {}).get("model", "")

                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are an intent matcher. Output ONLY valid JSON. No markdown."},
                        {"role": "user", "content": match_prompt}
                    ],
                    temperature=0.1,
                )

                text = resp.choices[0].message.content.strip()
                # Clean markdown fences
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

                result = json.loads(text)
                playbook_name = result.get("playbook", "none")
                variables = result.get("variables", {})
                reasoning = result.get("reasoning", "")

                if playbook_name and playbook_name != "none" and playbook_name in self.playbooks:
                    logger.info(f"Matched playbook '{playbook_name}': {reasoning}")
                    return playbook_name, variables
                else:
                    logger.info(f"No playbook matched (LLM said: {playbook_name}). Reason: {reasoning}")
                    return None, {}

            except Exception as e:
                logger.error(f"Intent matching failed with {provider}: {e}")
                continue

        logger.warning("Intent matching exhausted all providers")
        return None, {}

    def render_playbook(self, playbook_name: str, variables: dict) -> list[dict]:
        """
        Render a playbook template into a concrete plan by substituting variables.

        Args:
            playbook_name: Name of the playbook to render
            variables: Dict of variable values to substitute

        Returns:
            List of plan step dicts ready for the executor
        """
        playbook = self.playbooks.get(playbook_name)
        if not playbook:
            logger.error(f"Playbook '{playbook_name}' not found")
            return []

        # Apply defaults from playbook variable definitions
        pb_vars = playbook.get("variables", {})
        for var_name, var_info in pb_vars.items():
            if var_name not in variables or not variables[var_name]:
                if isinstance(var_info, dict) and "default" in var_info:
                    variables[var_name] = var_info["default"]

        # Apply user profile defaults (e.g., default_browser → browser)
        user_profile = self.config.get("user_profile", {})
        if "browser" not in variables or not variables.get("browser"):
            variables["browser"] = user_profile.get("default_browser", "Brave Browser")

        # Resolve URL aliases for open_url playbook
        if playbook_name == "open_url" and "url" in variables:
            url_aliases = playbook.get("url_aliases", {})
            url_val = variables["url"].lower().strip()
            if url_val in url_aliases:
                variables["url"] = url_aliases[url_val]
                logger.info(f"Resolved URL alias: '{url_val}' → '{variables['url']}'")

        # Render steps
        steps = playbook.get("steps", [])
        rendered = []
        step_num = 0

        for step_template in steps:
            # Check conditions
            condition = step_template.get("condition", "")
            if condition:
                if condition == "song_specified" and not variables.get("song", ""):
                    continue  # Skip this step

            step_num += 1

            # Substitute variables in params and description
            params = {}
            for k, v in step_template.get("params", {}).items():
                if isinstance(v, str):
                    params[k] = self._substitute(v, variables)
                else:
                    params[k] = v

            rendered.append({
                "step": step_num,
                "action": step_template.get("action", ""),
                "params": params,
                "description": self._substitute(step_template.get("description", ""), variables),
                "expect": step_template.get("expect", ""),
                "risk_level": step_template.get("risk_level", "low"),
                "wait_for": step_template.get("wait_for", None),
                # Include notes for debugging/logging but strip from execution
                "_notes": step_template.get("notes", ""),
            })

        logger.info(f"Rendered playbook '{playbook_name}' into {len(rendered)} steps with variables: {variables}")
        return rendered

    def _substitute(self, template: str, variables: dict) -> str:
        """Replace {variable} placeholders in a template string."""
        result = template
        for key, value in variables.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result
