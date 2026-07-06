# Unit tests for db.sanitize_title
from db import sanitize_title


def test_whitespace_becomes_underscore():
    assert sanitize_title('Beach Day') == 'Beach_Day'


def test_specials_stripped():
    assert sanitize_title('Beach Day!') == 'Beach_Day'


def test_multiple_spaces_collapse():
    assert sanitize_title('a   b\tc') == 'a_b_c'


def test_allowed_chars_kept():
    assert sanitize_title('Photo_2-final') == 'Photo_2-final'


def test_empty_becomes_untitled():
    assert sanitize_title('   ') == 'untitled'
    assert sanitize_title('!!!') == 'untitled'


def test_uniqueness_suffixing():
    existing = {'Beach_Day'}
    assert sanitize_title('Beach Day!', existing) == 'Beach_Day_2'
    existing = {'Beach_Day', 'Beach_Day_2'}
    assert sanitize_title('Beach Day', existing) == 'Beach_Day_3'


def test_unique_when_not_colliding():
    assert sanitize_title('Sunset', {'Beach_Day'}) == 'Sunset'


def test_non_string_input():
    assert sanitize_title(42) == '42'
