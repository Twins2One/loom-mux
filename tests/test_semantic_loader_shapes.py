"""Exercise real semantic._load with offline ORT/tokenizer module seams."""
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from loom.study import semantic


@pytest.fixture
def loader(monkeypatch, tmp_path):
    model = tmp_path / 'paired model.onnx'
    tokenizer = tmp_path / 'tokenizer.json'
    model.write_bytes(b'offline seam model')
    tokenizer.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('LOOM_EMBED_MODEL', str(model))
    monkeypatch.setenv('LOOM_EMBED_TOKENIZER', str(tokenizer))
    monkeypatch.setenv('LOOM_EMBED_BATCH', '16')
    monkeypatch.setattr(semantic, '_SESS', None)
    monkeypatch.setattr(semantic, '_TOK', None)
    monkeypatch.setattr(semantic, '_LAST_ERR', '')
    monkeypatch.setitem(semantic._PROGRESS, 'on', False)
    state = SimpleNamespace(runs=[], sessions=[], tokenizers=[], shape='2d', sequence=3, token_types=False)

    class Session:
        def __init__(self, path, *, sess_options, providers):
            assert path == str(model)
            assert providers == ['CPUExecutionProvider']
            state.sessions.append(self)
        def get_inputs(self):
            names = ['input_ids', 'attention_mask'] + (['token_type_ids'] if state.token_types else [])
            return [SimpleNamespace(name=name) for name in names]
        def run(self, outputs, feeds):
            assert outputs is None
            state.runs.append({key: value.copy() for key, value in feeds.items()})
            ids = feeds['input_ids']
            batch, seq = ids.shape
            base = np.stack([ids[:, 0], np.ones(batch)], axis=1).astype(np.float32)
            if state.shape == '2d':
                return [base]
            if state.shape == '3d':
                hidden = np.repeat(base[:, None, :], seq, axis=1)
                hidden[:, :, 1] = np.arange(1, seq + 1)
                return [hidden]
            return [state.shape(batch, seq)]

    class Tokenizer:
        @classmethod
        def from_file(cls, path):
            assert path == str(tokenizer)
            instance = cls()
            state.tokenizers.append(instance)
            return instance
        def enable_truncation(self, *, max_length):
            assert max_length == 512
        def encode(self, text):
            value = int(text)
            length = state.sequence - (value % 2 if state.shape == '3d' else 0)
            return SimpleNamespace(ids=[value] * length)

    ort = ModuleType('onnxruntime')
    ort.SessionOptions = type('SessionOptions', (), {})
    ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL='sequential')
    ort.InferenceSession = Session
    tok = ModuleType('tokenizers')
    tok.Tokenizer = Tokenizer
    monkeypatch.setitem(sys.modules, 'onnxruntime', ort)
    monkeypatch.setitem(sys.modules, 'tokenizers', tok)
    return state


@pytest.mark.parametrize('sequence', [2, 3], ids=['batch_equals_sequence', 'different_sequence'])
@pytest.mark.parametrize('token_types', [False, True])
def test_two_dimensional_sentence_vectors_do_not_mix_samples(loader, sequence, token_types):
    loader.sequence = sequence
    loader.token_types = token_types
    result = semantic.embed(['1', '2'])
    expected = np.array([[1, 1], [2, 1]], dtype=np.float32)
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    np.testing.assert_allclose(result, expected, rtol=1e-6)
    assert not np.allclose(result[0], result[1])
    assert len(loader.sessions) == len(loader.tokenizers) == 1
    assert ('token_type_ids' in loader.runs[0]) == token_types
    if token_types:
        assert not loader.runs[0]['token_type_ids'].any()


def test_seventeen_samples_preserve_order_across_batches(loader):
    result = semantic.embed([str(i) for i in range(1, 18)])
    expected = np.array([[i, 1] for i in range(1, 18)], dtype=np.float32)
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    np.testing.assert_allclose(result, expected, rtol=1e-6)
    assert result.shape == (17, 2)
    assert [run['input_ids'].shape[0] for run in loader.runs] == [16, 1]
    assert len(loader.sessions) == len(loader.tokenizers) == 1


def test_three_dimensional_outputs_keep_masked_mean(loader):
    loader.shape = '3d'
    result = semantic.embed(['1', '2'])
    # Row 1 has two tokens, row 2 has three; padded token must not enter mean.
    expected = np.array([[1, 1.5], [2, 2]], dtype=np.float32)
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    np.testing.assert_allclose(result, expected, rtol=1e-6)


@pytest.mark.parametrize('output', [
    lambda b, s: np.ones((b + 1, 2)),
    lambda b, s: np.ones((b, s + 1, 2)),
    lambda b, s: np.ones((b, s, 2, 1)),
    lambda b, s: np.full((b, 2), np.nan),
    lambda b, s: np.full((b, 2), np.inf),
    lambda b, s: np.zeros((b, 2)),
    lambda b, s: np.empty((b, 0)),
], ids=['wrong_batch', 'wrong_sequence', 'wrong_rank', 'nan', 'inf', 'zero', 'empty_dimension'])
def test_invalid_output_is_rejected_with_diagnostic(loader, output):
    loader.shape = output
    assert semantic.embed(['1', '2']) is None
    assert semantic._LAST_ERR.startswith('ValueError:')
    assert len(loader.sessions) == len(loader.tokenizers) == 1
