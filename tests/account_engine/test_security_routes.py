


from account_engine import security_routes






def test_export_omits_the_credential_and_the_admin_flag(monkeypatch):
    account = {
        "user_id": 7, "email": "a@b.c", "public_key": None,
        "password_hash": "pbkdf2_sha256$...", "is_admin": True, "label": "me",
    }
    monkeypatch.setattr(security_routes.store, "get_account", lambda uid: account)
    monkeypatch.setattr(security_routes.store, "get_preferences", lambda uid: {"theme": "dark"})
    monkeypatch.setattr(security_routes.store, "list_favorites", lambda uid: [{"item_key": "anilist:21"}])
    monkeypatch.setattr(security_routes.store, "list_progress", lambda uid: [{"item_key": "anilist:21:s1:e5"}])
    from notify_engine.db import store as airing_store
    monkeypatch.setattr(airing_store, "list_subscriptions", lambda uid: [])

    payload = security_routes._collect_export(7)
    assert "password_hash" not in payload["account"]
    assert "is_admin" not in payload["account"]
    assert payload["account"]["email"] == "a@b.c"
    assert payload["watchlists"] and payload["progress"]
    assert payload["preferences"] == {"theme": "dark"}
