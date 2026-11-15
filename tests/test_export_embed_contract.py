"""Offline exporter call contract only; these are not real ONNX exports."""
from contextlib import nullcontext
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from loom.vendor import export_embed_onnx as exporter


@pytest.mark.parametrize('token_types', [False, True])
def test_export_call_and_paired_tokenizer_contract(monkeypatch, tmp_path, token_types):
    calls = {}
    class Tokenizer:
        def __call__(self, texts, **kwargs):
            calls['sample'] = (texts, kwargs)
            sample = {'input_ids': object(), 'attention_mask': object()}
            if token_types:
                sample['token_type_ids'] = object()
            calls['tensors'] = sample
            return sample
        def save_pretrained(self, directory):
            calls['tokenizer_dir'] = directory
            (Path(directory) / 'tokenizer.json').write_text('{}', encoding='utf-8')
    tokenizer = Tokenizer()
    class Model:
        def eval(self):
            calls['eval'] = True
            return self
    model = Model()
    def load_tokenizer(model_id):
        calls['tokenizer_model'] = model_id
        return tokenizer
    def load_model(model_id):
        calls['model'] = model_id
        return model
    def fake_export(wrapped, inputs, output, **kwargs):
        calls['export'] = (wrapped, inputs, output, kwargs)
        Path(output).write_bytes(b'offline contract fixture, not an ONNX graph')
    torch = ModuleType('torch')
    torch.nn = SimpleNamespace(Module=type('Module', (), {}))
    torch.no_grad = nullcontext
    torch.onnx = SimpleNamespace(export=fake_export)
    transformers = ModuleType('transformers')
    transformers.AutoModel = SimpleNamespace(from_pretrained=load_model)
    transformers.AutoTokenizer = SimpleNamespace(from_pretrained=load_tokenizer)
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    output = tmp_path / 'paired output' / 'model.onnx'

    assert exporter.export(output, 'offline/custom-model', 123, 17) == 0

    assert calls['model'] == calls['tokenizer_model'] == 'offline/custom-model'
    assert calls['eval'] is True
    assert calls['sample'][1] == {'padding': True, 'truncation': True, 'max_length': 123, 'return_tensors': 'pt'}
    wrapped, inputs, written, kwargs = calls['export']
    assert wrapped.inner is model
    expected_names = ['input_ids', 'attention_mask'] + (['token_type_ids'] if token_types else [])
    assert inputs == tuple(calls['tensors'][name] for name in expected_names)
    assert written == str(output)
    assert kwargs['input_names'] == expected_names
    assert kwargs['output_names'] == ['sentence_embedding']
    assert kwargs['dynamo'] is False
    assert kwargs['opset_version'] == 17
    assert kwargs['do_constant_folding'] is True
    assert kwargs['dynamic_axes'] == {
        **{name: {0: 'batch', 1: 'seq'} for name in expected_names},
        'sentence_embedding': {0: 'batch'},
    }
    assert calls['tokenizer_dir'] == str(output.parent)
    assert (output.parent / 'tokenizer.json').is_file()


@pytest.mark.parametrize('missing', ['torch', 'transformers'])
def test_missing_dependency_returns_two_without_output(monkeypatch, tmp_path, capsys, missing):
    monkeypatch.setitem(sys.modules, 'torch', ModuleType('torch'))
    monkeypatch.setitem(sys.modules, 'transformers', ModuleType('transformers'))
    monkeypatch.setitem(sys.modules, missing, None)
    output = tmp_path / 'model.onnx'
    assert exporter.export(output, 'offline/model', 512, 17) == 2
    assert not output.exists()
    assert 'pip install torch transformers onnx' in capsys.readouterr().err
