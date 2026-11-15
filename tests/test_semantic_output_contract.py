import types
import numpy as np
from loom.study import semantic


class Tokenizer:
    def encode(self, text):
        return types.SimpleNamespace(ids=[1, 2, 3])


class Session:
    def __init__(self, output):
        self.output = output
    def get_inputs(self):
        return [types.SimpleNamespace(name='input_ids'), types.SimpleNamespace(name='attention_mask')]
    def run(self, names, feeds):
        return [self.output[:len(feeds['input_ids'])]]


def test_exported_sentence_vectors_keep_batch_identity(monkeypatch):
    rows = np.array([[3., 4.], [4., 3.]], dtype=np.float32)
    monkeypatch.setattr(semantic, '_load', lambda: (Session(rows), Tokenizer()))
    out = semantic.embed(['first', 'second'])
    assert out is not None
    np.testing.assert_allclose(out, rows / 5)
    assert out.shape == (2, 2)


def test_token_vectors_keep_existing_masked_pooling(monkeypatch):
    rows = np.array([[[3., 4.]] * 3, [[4., 3.]] * 3], dtype=np.float32)
    monkeypatch.setattr(semantic, '_load', lambda: (Session(rows), Tokenizer()))
    out = semantic.embed(['first', 'second'])
    np.testing.assert_allclose(out, np.array([[.6, .8], [.8, .6]], dtype=np.float32))


def test_invalid_output_batch_degrades_explicitly(monkeypatch):
    monkeypatch.setattr(semantic, '_load', lambda: (Session(np.ones((1, 2))), Tokenizer()))
    assert semantic.embed(['first', 'second']) is None
    assert semantic._LAST_ERR
