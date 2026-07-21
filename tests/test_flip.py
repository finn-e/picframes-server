import random
from image.pipeline import _flip_bitstream, _flip_bitstream_13in3


def test_flip_roundtrip_800x480():
    data = bytes(range(256)) * (192000 // 256)
    assert _flip_bitstream(_flip_bitstream(data)) == data


def test_flip_roundtrip_13in3():
    rng = random.Random(42)
    data = bytes(rng.getrandbits(8) for _ in range(960000))
    assert _flip_bitstream_13in3(_flip_bitstream_13in3(data)) == data


def test_flip_changes_data():
    data = bytes(range(256)) * (192000 // 256)
    assert _flip_bitstream(data) != data


def test_flip_13in3_changes_data():
    data = bytes([0xAB] * 960000)
    # All same nibbles: 0xAB → reversed nibbles = 0xBA; reversed order stays same
    # Result differs from input
    flipped = _flip_bitstream_13in3(data)
    assert flipped != data or all(b == 0xAA or b == 0xBB for b in data[:10])
