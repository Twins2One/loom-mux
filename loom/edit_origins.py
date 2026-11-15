"""Content-bound provenance for explicit editor saves; unknown changes stay quiet."""
import hashlib
import json
import threading
import os
from pathlib import Path
from .fsutil import atomic_write_text

_LOCK = threading.RLock()

def _path(root):
    return Path(root) / '.loom-edit-origins.json'

def load(root):
    try:
        data = json.loads(_path(root).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

_UNAVAILABLE = set()

def record(root, rel, origin, *, content=None):
    key = str(Path(root).resolve())
    # Match atomic_write_text's platform newline conversion, without rereading
    # a file that may already contain a concurrent writer's next version.
    raw = (str(content).replace('\n', os.linesep).encode('utf-8') if content is not None
           else (Path(root) / rel).read_bytes())
    with _LOCK:
        data = load(root)
        data[rel] = {'origin': 'manual' if origin == 'manual' else 'agent',
                     'hash': hashlib.sha256(raw).hexdigest()[:16]}
        try:
            atomic_write_text(_path(root), json.dumps(data, ensure_ascii=False))
        except OSError:
            _UNAVAILABLE.add(key)
            return False
        _UNAVAILABLE.discard(key)
        return True

def manual_hints(root, result):
    from . import chains
    if str(Path(root).resolve()) in _UNAVAILABLE:
        return {**result, 'hints': [], 'origin_warning': '来源记录暂不可用，联动提示已暂停。'}
    data = {k: v for k, v in load(root).items() if isinstance(v, dict)}
    maps = {'settings': chains.settings_map, 'final': chains.final_map,
            'card': chains.card_map, 'outline': chains.outline_map}
    hints = []
    for hint in result.get('hints', []):
        current = maps[hint['key']](root)
        from .state import load_state
        baseline = ((load_state(root).get('chain_seen') or {}).get(hint['key']) or {}).get('files') or {}
        files = [rel for rel, sig in current.items()
                 if baseline.get(rel) != sig
                 and data.get(rel, {}).get('origin') == 'manual'
                 and data[rel].get('hash') == sig]
        if files:
            item = dict(hint)
            if hint['key'] == 'settings':
                item['files'], item['count'] = files, len(files)
            hints.append(item)
    return {**result, 'hints': hints}
