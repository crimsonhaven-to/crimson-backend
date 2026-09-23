"""The model catalogue and the cost arithmetic the dashboard bills against.
"""







from chat_engine import models


def test_no_shutdown_gemini_models_are_offered():
    """Gemini 2.0 is shut down; offering it would be a 404 at request time."""
    for model_id in models.MODELS:
        assert not model_id.startswith("gemini-2.0")


def test_every_provider_default_exists_and_matches_its_provider():
    for provider, model_id in models.DEFAULT_MODEL.items():
        model = models.get_model(model_id)
        assert model is not None
        assert model.provider == provider


def test_resolve_falls_back_when_the_model_belongs_to_the_other_provider():
    """Guards a provider switch that left the other vendor's model id stored."""
    model = models.resolve("gemini", "claude-sonnet-5")
    assert model.provider == "gemini"
    assert model.model_id == models.DEFAULT_MODEL["gemini"]


def test_resolve_keeps_a_valid_pairing():
    assert models.resolve("anthropic", "claude-opus-5").model_id == "claude-opus-5"


def test_cost_arithmetic():
    model = models.get_model("claude-sonnet-5")
    # 1M input at $3 plus 1M output at $15 is $18, or 18,000,000 micros.
    assert model.cost_micros(1_000_000, 1_000_000) == 18_000_000
    # Cached reads bill at the reduced rate, not the full input rate.
    assert model.cost_micros(0, 0, 1_000_000) == 300_000
    assert model.cost_micros(0, 0, 0) == 0


def test_negative_token_counts_cannot_produce_a_credit():
    model = models.get_model("claude-sonnet-5")
    assert model.cost_micros(-5000, -5000, -5000) == 0
