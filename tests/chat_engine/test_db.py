"""The chat schema lives only in its migration, so a missing column there is caught
by no import or type check.
"""




import re





def test_migration_declares_the_chat_columns():
    """The schema lives only in the migration, so a missing column there is not
    caught by any import or type check."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[2] / "migrations" / "002_lumi_chat.sql"
    ).read_text(encoding="utf-8")
    for needle in (
        "chat_enabled",
        "chat_monthly_token_budget",
        "chat_settings",
        "chat_conversations",
        "chat_messages",
        "chat_usage",
    ):
        assert needle in sql
    # Deny-by-default is the whole access model; a DEFAULT TRUE here would hand
    # every account a spending capability.
    assert re.search(r"chat_enabled\s+BOOLEAN\s+NOT NULL\s+DEFAULT FALSE", sql)
