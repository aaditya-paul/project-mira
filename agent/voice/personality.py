import copy
from pathlib import Path

DEFAULT_COMPOSE_INSTRUCTION = (
    "Write as if the user is speaking naturally to their contact. Keep the key intent, "
    "add friendly context, and avoid robotic phrasing."
)

DEFAULT_PERSONALITIES = {
    "friendly": {
        "display_name": "Friendly",
        "system_prompt_file": "prompts/personalities/friendly_system_prompt.txt",
        "compose_instruction": (
            "Use a warm, friendly tone with natural wording and short supportive phrasing. "
            "Keep the message clear and human."
        ),
        "voice": {
            "preset": "friendly",
            "clone_profile": "",
            "rate_multiplier": 1.0,
            "volume": 1.0,
        },
    },
    "hype": {
        "display_name": "Hype",
        "system_prompt_file": "prompts/personalities/hype_system_prompt.txt",
        "compose_instruction": (
            "Use energetic language and upbeat momentum while staying concise and clear. "
            "Avoid overlong messages."
        ),
        "voice": {
            "preset": "hype",
            "clone_profile": "",
            "rate_multiplier": 1.08,
            "volume": 1.0,
        },
    },
    "calm_mentor": {
        "display_name": "Calm mentor",
        "system_prompt_file": "prompts/personalities/calm_mentor_system_prompt.txt",
        "compose_instruction": (
            "Use a calm, thoughtful, and reassuring tone. Keep language gentle, direct, and practical."
        ),
        "voice": {
            "preset": "calm_mentor",
            "clone_profile": "",
            "rate_multiplier": 0.94,
            "volume": 0.95,
        },
    },
}

PERSONALITY_ALIASES = {
    "friendly": "friendly",
    "friend": "friendly",
    "hype": "hype",
    "energetic": "hype",
    "calm": "calm_mentor",
    "mentor": "calm_mentor",
    "calm mentor": "calm_mentor",
    "calm_mentor": "calm_mentor",
}


def normalize_personality_name(value: str) -> str:
    key = (value or "").strip().lower().replace("-", "_")
    return PERSONALITY_ALIASES.get(key, key)


def _merged_personalities(config: dict) -> dict:
    merged = copy.deepcopy(DEFAULT_PERSONALITIES)
    custom = config.get("personalities", {}) if isinstance(config, dict) else {}
    if not isinstance(custom, dict):
        return merged

    for key, value in custom.items():
        canonical = normalize_personality_name(key)
        if not isinstance(value, dict):
            continue
        if canonical in merged:
            merged[canonical].update(value)
            if isinstance(merged[canonical].get("voice"), dict) and isinstance(value.get("voice"), dict):
                merged[canonical]["voice"].update(value["voice"])
        else:
            merged[canonical] = copy.deepcopy(value)
    return merged


def resolve_personality(config: dict, requested: str | None = None) -> tuple[str, dict, dict]:
    personalities = _merged_personalities(config)
    user_selected = requested or config.get("user_profile", {}).get("personality", "friendly")
    selected = normalize_personality_name(user_selected)

    if selected not in personalities:
        selected = "friendly" if "friendly" in personalities else next(iter(personalities.keys()), "friendly")

    return selected, personalities.get(selected, {}), personalities


def load_system_prompt(config: dict, requested: str | None = None) -> str:
    selected, profile, _ = resolve_personality(config, requested)

    base_path = Path("prompts") / "system_prompt.txt"
    base_prompt = ""
    if base_path.exists():
        base_prompt = base_path.read_text(encoding="utf-8")

    style_path_str = profile.get("system_prompt_file", "")
    style_text = ""
    if style_path_str:
        style_path = Path(style_path_str)
        if style_path.exists():
            style_text = style_path.read_text(encoding="utf-8").strip()

    if not style_text:
        return base_prompt

    overlay = (
        f"PERSONALITY OVERLAY ({selected}):\n"
        f"{style_text}\n\n"
        "Do not break core planning and safety constraints from the main system prompt."
    )
    return f"{overlay}\n\n{base_prompt}".strip()


def get_compose_instruction(config: dict, requested: str | None = None) -> str:
    _, profile, _ = resolve_personality(config, requested)
    instruction = profile.get("compose_instruction", "") if isinstance(profile, dict) else ""
    return instruction.strip() if instruction else DEFAULT_COMPOSE_INSTRUCTION
