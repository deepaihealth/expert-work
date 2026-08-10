import re

from control_plane.member_password import _WORDS, generate_initial_password


def test_format_three_words_dash_four_digits() -> None:
    pw = generate_initial_password()
    m = re.fullmatch(r"([a-z]+)-([a-z]+)-([a-z]+)-(\d{4})", pw)
    assert m is not None
    assert all(w in _WORDS for w in m.groups()[:3])


def test_wordlist_shape() -> None:
    assert len(_WORDS) == 256
    assert len(set(_WORDS)) == 256
    assert all(re.fullmatch(r"[a-z]{4,6}", w) for w in _WORDS)


def test_not_constant() -> None:
    assert len({generate_initial_password() for _ in range(20)}) > 1
