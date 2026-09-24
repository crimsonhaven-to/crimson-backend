"""The overlay seam: a MUSIC_PROVIDER in an overlay module is found, a base
build has none."""

import sys
import types

import music_engine
from music_engine import provider


def test_a_base_build_has_no_provider():
    provider.get_provider.cache_clear()
    assert provider.get_provider() is None


def test_an_overlay_module_is_discovered(monkeypatch, tmp_path):
    (tmp_path / "fake_overlay.py").write_text("MUSIC_PROVIDER = object()\n")
    monkeypatch.setattr(music_engine, "__path__", [*music_engine.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, "music_engine.fake_overlay", raising=False)
    provider.get_provider.cache_clear()
    try:
        found = provider.get_provider()
        assert found is sys.modules["music_engine.fake_overlay"].MUSIC_PROVIDER
        assert not isinstance(found, types.ModuleType)
    finally:
        provider.get_provider.cache_clear()
