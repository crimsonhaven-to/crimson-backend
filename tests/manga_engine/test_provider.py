"""A base build ships no manga source, so chapters resolve in the browser.
"""




def test_no_provider_in_base_build():
    # A base build ships no manga source, so the routes report "unmapped" and the
    # browser resolves chapters and pages instead.
    import manga_engine.provider as provider

    provider.get_provider.cache_clear()
    assert provider.get_provider() is None
