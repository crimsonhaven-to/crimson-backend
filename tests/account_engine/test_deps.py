
import pytest



from account_engine.deps import parse_bearer





@pytest.mark.parametrize("header,expected", [
    ("Bearer abc", "abc"),
    ("bearer abc", "abc"),
    ("BEARER  abc ", "abc"),
    ("Basic abc", None),
    ("abc", None),
    (None, None),
    ("", None),
])
def test_bearer_token_parsing(header, expected):
    assert parse_bearer(header) == expected
