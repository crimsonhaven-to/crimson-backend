"""Lumi's house-style rules are a product requirement that no other check catches,
and the prompt must stay frozen so it remains cacheable.
"""







from chat_engine import persona


# Em and en dashes are banned in Lumi's output. The rule is stated in the prompt,
# but a prompt that itself contains the character it forbids is both a mixed
# signal to the model and a sign someone edited the file without reading it, so
# the prompt is checked as well as the rule's presence.
EM_DASH = "\u2014"


EN_DASH = "\u2013"


def test_system_prompt_states_the_dash_ban():
    assert "em dash" in persona.SYSTEM_PROMPT.lower()


def test_system_prompt_contains_no_dashes_itself():
    # The ban characters appear exactly where the rule quotes them, and nowhere
    # else. Quoting them is what makes the instruction unambiguous.
    rule_line = next(
        line for line in persona.SYSTEM_PROMPT.splitlines() if "NEVER use em dashes" in line
    )
    body = persona.SYSTEM_PROMPT.replace(rule_line, "")
    assert EM_DASH not in body
    assert EN_DASH not in body


def test_greetings_carry_no_dashes():
    for line in persona.GREETINGS:
        assert EM_DASH not in line and EN_DASH not in line


def test_system_prompt_is_frozen_and_cacheable():
    """It is the cached prefix, so it must not vary between reads.

    Anything interpolated per request would change the prompt bytes on every
    call and silently turn every cache read into a cache write.
    """
    assert persona.SYSTEM_PROMPT == persona.SYSTEM_PROMPT
    assert "{" not in persona.SYSTEM_PROMPT.replace("{}", "")


def test_context_block_is_none_without_facts():
    assert persona.build_context_block(username=None, recent=[]) is None


def test_context_block_summarises_what_it_has():
    block = persona.build_context_block(
        username="Ramon",
        recent=[{"title": "Overlord"}, {"title": "Frieren"}],
    )
    assert "Ramon" in block
    assert "Overlord" in block
