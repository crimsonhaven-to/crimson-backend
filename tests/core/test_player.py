from core.player import is_safe_src, render_player


def test_src_cannot_close_the_script_element():
    html = render_player("/x</script><script>alert(1)</script>")
    assert "<script>alert(1)" not in html
    assert html.count("</script>") == 2


def test_protocol_relative_src_is_refused():
    assert is_safe_src("/jellyfin_proxy/a.m3u8")
    assert not is_safe_src("//evil.example/a.m3u8")
    assert not is_safe_src("/\\evil.example/a.m3u8")
    assert not is_safe_src("https://evil.example/a.m3u8")
