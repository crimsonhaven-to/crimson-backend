from core.config import get_settings



def test_manga_preferences_have_sane_defaults():
    settings = get_settings()
    assert settings.manga_enabled is True
    assert settings.manga_languages[0] == "en"
    assert "safe" in settings.manga_content_rating
