"""
The catalogue of models an operator may select, with pricing and capabilities.

Hand-maintained for two reasons. Cost: the usage ledger records an estimated
price per call, which needs published per-million rates the API does not return.
And capability skew: the request shape is not uniform even within one provider,
so providers.py needs somewhere to look those facts up rather than branching on
model id strings scattered through the call path.

The list is deliberately short. It is an operator dropdown, not a mirror of every
model a vendor sells, and the three per provider span budget, default and quality.

Prices are USD per million tokens, current as of 2026-08-08. They affect only the
dashboard's estimate, never what a provider bills, so drift here is a reporting
bug rather than a billing one.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import Dict, List, Optional

ANTHROPIC = "anthropic"
GEMINI = "gemini"

PROVIDERS = (ANTHROPIC, GEMINI)


@dataclass(frozen=True, slots=True)
class ChatModel:
    """One selectable model. ``supports_effort`` and ``supports_thinking`` gate
    the two request fields that hard-error on a model that does not take them."""

    model_id: str
    provider: str
    label: str
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float
    _: KW_ONLY
    supports_effort: bool = False
    supports_thinking: bool = False
    note: str = ""

    def cost_micros(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> int:
        """Estimated USD millionths. A per-million rate times tokens is already in
        micros. Cached tokens are reported apart from ``input_tokens`` by both
        providers and bill at the read rate."""
        return round(
            max(0, input_tokens) * self.input_per_mtok
            + max(0, cached_tokens) * self.cached_input_per_mtok
            + max(0, output_tokens) * self.output_per_mtok
        )

    def public(self) -> Dict:
        """Shape sent to the dashboard's model dropdown."""
        return {
            "id": self.model_id,
            "provider": self.provider,
            "label": self.label,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "note": self.note,
        }


# Sonnet 5 is the default: persona fidelity and correct tool arguments are exactly
# where the cheaper tiers get sloppy, and at this scale the difference between the
# three is a few dollars a month.
_ANTHROPIC_MODELS = [
    ChatModel(
        "claude-sonnet-5", ANTHROPIC, "Claude Sonnet 5",
        3.00, 15.00, 0.30,
        supports_effort=True, supports_thinking=True,
        note="Recommended. Best balance of persona fidelity and tool accuracy.",
    ),
    ChatModel(
        "claude-opus-5", ANTHROPIC, "Claude Opus 5",
        5.00, 25.00, 0.50,
        supports_effort=True, supports_thinking=True,
        note="Strongest reasoning. Noticeably pricier for little gain in chat.",
    ),
    ChatModel(
        "claude-haiku-4-5", ANTHROPIC, "Claude Haiku 4.5",
        1.00, 5.00, 0.10,
        # Haiku's 4096-token cache minimum is above Lumi's ~1.8k prefix, so prompt
        # caching never engages and it saves less than its sticker price implies.
        note="Cheapest. Prompt caching does not engage at Lumi's prompt size.",
    ),
]

# Model ids verified against ai.google.dev on 2026-08-08. The 2.0 family is shut
# down and deliberately absent.
_GEMINI_MODELS = [
    ChatModel(
        "gemini-3.6-flash", GEMINI, "Gemini 3.6 Flash",
        1.50, 7.50, 0.15,
        note="Recommended on Gemini. Current stable Flash.",
    ),
    ChatModel(
        "gemini-3.1-pro-preview", GEMINI, "Gemini 3.1 Pro Preview",
        2.00, 12.00, 0.20,
        note="Strongest Gemini reasoning. Preview, so behaviour may shift.",
    ),
    ChatModel(
        "gemini-3.5-flash-lite", GEMINI, "Gemini 3.5 Flash-Lite",
        0.30, 2.50, 0.03,
        note="Cheapest option overall. Weaker at multi-step tool use.",
    ),
]

MODELS: Dict[str, ChatModel] = {m.model_id: m for m in _ANTHROPIC_MODELS + _GEMINI_MODELS}

DEFAULT_MODEL = {
    ANTHROPIC: "claude-sonnet-5",
    GEMINI: "gemini-3.6-flash",
}


def get_model(model_id: str) -> Optional[ChatModel]:
    return MODELS.get(model_id)


def models_for(provider: str) -> List[ChatModel]:
    return [m for m in MODELS.values() if m.provider == provider]


def catalogue() -> Dict[str, List[Dict]]:
    """Every selectable model, grouped by provider, for the dropdown."""
    return {p: [m.public() for m in models_for(p)] for p in PROVIDERS}


def resolve(provider: str, model_id: Optional[str]) -> ChatModel:
    """The model to use, falling back to the provider default.

    Guards an operator switching provider while the other vendor's model id is
    still stored: better the new provider's default than an opaque 404.
    """
    model = MODELS.get(model_id or "")
    if model is None or model.provider != provider:
        model = MODELS[DEFAULT_MODEL[provider]]
    return model
