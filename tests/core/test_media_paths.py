"""Path tokens are in URLs already handed out, so their encoding must not drift."""

from core.media_paths import decode_token, encode_token, is_within


def test_token_encoding_is_padding_free_urlsafe_base64():
    assert encode_token("/media/Show/S01E01.mp4") == "L21lZGlhL1Nob3cvUzAxRTAxLm1wNA"


def test_token_round_trips_non_ascii_paths():
    path = "/media/Frieren/Sōsō no Frieren?.mkv"
    assert decode_token(encode_token(path)) == path


def test_garbage_token_decodes_to_none():
    assert decode_token("\xff") is None


def test_is_within_rejects_a_sibling_with_a_shared_prefix(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    assert is_within(str(root / "a.mp4"), str(root))
    assert not is_within(str(tmp_path / "lib2" / "a.mp4"), str(root))
