"""
The catalogue of models an operator may select, with pricing and capabilities.

Why a hand-maintained table
---------------------------
Two reasons. First, cost: the usage ledger records an estimated price per call,
and that needs published per-million rates the API itself does not return.
Second, capability skew: the request shape is not uniform across models even
within one provider (Haiku 4.5 rejects the effort parameter that Sonnet 5 wants;
prompt caching has a different minimum prefix on every model), so providers.py
needs somewhere to look those facts up rather than branching on model id strings
scattered through the call path.

Keeping this list short is deliberate. It is an operator dropdown, not a mirror
of every model each vendor sells. The three per provider span budget, default and
quality so there is an obvious answer at each price point.

Prices are USD per million tokens, current as of 2026-08-08. They only affect the
cost ESTIMATE shown in the dashboard; a stale number here never changes what a
provider actually bills, so this table drifting is a reporting bug rather than a
billing one.
"""

from __future__ import annotations

from typing import Dict, List, Optional

ANTHROPIC = "anthropic"
GEMINI = "gemini"

PROVIDERS = (ANTHROPIC, GEMINI)


class ChatModel:
    """One selectable model.

    ``cache_min_tokens`` is the smallest prefix the provider will actually cache.
    A prefix below it is not an error, it simply never becomes a cache entry, so
    this is the difference between caching working and silently doing nothing.
    Lumi's stable prefix (persona plus tool schemas) is roughly 1.8k tokens,
    which clears every model here except Haiku 4.5.

    ``supports_effort`` and ``supports_thinking`` gate the two request fields
    that hard-error when sent to a model that does not take them.
    """

    def __init__(
        self,
        model_id: str,
        provider: str,
        label: str,
        input_per_mtok: float,
        output_per_mtok: float,
        cached_input_per_mtok: float,
        *,
        cache_min_tokens: int = 1024,
        supports_effort: bool = False,
        supports_thinking: bool = False,
        note: str = "",
    ):
        self.model_id = model_id
        self.provider = provider
        self.label = label
        self.input_per_mtok = input_per_mtok
        self.output_per_mtok = output_per_mtok
        self.cached_input_per_mtok = cached_input_per_mtok
        self.cache_min_tokens = cache_min_tokens
        self.supports_effort = supports_effort
        self.supports_thinking = supports_thinking
        self.note = note

    def cost_micros(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> int:
        """Estimated cost of one call in USD millionths.

        Cached tokens are billed at the provider's reduced read rate and are
        counted separately from ``input_tokens``, matching how both providers
        report usage (the cached count is not included in the uncached count).
        """
        fresh = max(0, input_tokens)
        dollars = (
            fresh * self.input_per_mtok
            + max(0, cached_tokens) * self.cached_input_per_mtok
            + max(0, output_tokens) * self.output_per_mtok
        ) / 1_000_000.0
        return int(round(dollars * 1_000_000))

    def public(self) -> Dict:
        """Shape sent to the admin dashboard for the model dropdown."""
        return {
            "id": self.model_id,
            "provider": self.provider,
            "label": self.label,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "note": self.note,
        }


# Anthropic. Sonnet 5 is the default for the whole feature: persona fidelity and
# correct tool arguments are exactly where the cheaper tiers get sloppy, and at
# a handful of users the difference between these three is a few dollars a month.
_ANTHROPIC_MODELS = [
    ChatModel(
        "claude-sonnet-5", ANTHROPIC, "Claude Sonnet 5",
        3.00, 15.00, 0.30,
        cache_min_tokens=1024, supports_effort=True, supports_thinking=True,
        note="Recommended. Best balance of persona fidelity and tool accuracy.",
    ),
    ChatModel(
        "claude-opus-5", ANTHROPIC, "Claude Opus 5",
        5.00, 25.00, 0.50,
        cache_min_tokens=512, supports_effort=True, supports_thinking=True,
        note="Strongest reasoning. Noticeably pricier for little gain in chat.",
    ),
    ChatModel(
        "claude-haiku-4-5", ANTHROPIC, "Claude Haiku 4.5",
        1.00, 5.00, 0.10,
        # 4096 is genuinely the minimum here, and Lumi's prefix does not reach it,
        # so prompt caching never engages on this model. Flagged in the note
        # because it makes Haiku less of a saving than the sticker price implies.
        cache_min_tokens=4096, supports_effort=False, supports_thinking=False,
        note="Cheapest. Prompt caching does not engage at Lumi's prompt size.",
    ),
]

# Gemini. Model ids verified against ai.google.dev on 2026-08-08. The 2.0 family
# is shut down and is deliberately absent.
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
    """Every selectable model grouped by provider, for the dashboard dropdown."""
    return {p: [m.public() for m in models_for(p)] for p in PROVIDERS}


def resolve(provider: str, model_id: Optional[str]) -> ChatModel:
    """The model to actually use, falling back to the provider default.

    Guards the case where an operator switches provider while a model id from the
    other provider is still stored: rather than sending an Anthropic id to Google
    and getting an opaque 404, fall back to the new provider's default.
    """
    model = MODELS.get(model_id or "")
    if model is None or model.provider != provider:
        model = MODELS[DEFAULT_MODEL[provider]]
    return model
