"""The "any app" matrix: realistic shapes from apps the engine has no special code for.

Each shape must come out as an action / incoming kind with the human text, a readable label and a
signature that is stable across objects. Shapes that don't are marked xfail with the reason, so
this file is the list of what "any app" does not cover yet. `uv run python tests/matrix_shapes.py`
prints the same thing as a table.
"""
import pytest

import matrix_shapes

KNOWN_GAPS = {
    "Telegram Web (MTProto over WS)": "payload is encrypted inside the WebSocket; there is no text to read from the network",
}


def test_matrix_has_enough_distinct_shapes():
    assert len(matrix_shapes.SHAPES) >= 12 and len({s[0] for s in matrix_shapes.SHAPES}) == len(matrix_shapes.SHAPES)
    assert set(KNOWN_GAPS) <= {s[0] for s in matrix_shapes.SHAPES}


@pytest.mark.parametrize("shape", [
    pytest.param(s, id=s[0], marks=[pytest.mark.xfail(strict=True, reason=KNOWN_GAPS[s[0]])] if s[0] in KNOWN_GAPS else [])
    for s in matrix_shapes.SHAPES])
def test_shape(shape, events):
    r = matrix_shapes.run(shape, events)
    assert not r["problems"], f"{r['name']}: {'; '.join(r['problems'])} (kind {r['sig']!r}, label {r['label']!r}, text {r['text']!r})"
    assert r["klass"] == shape[2] and shape[3].lower() in r["text"].lower()
