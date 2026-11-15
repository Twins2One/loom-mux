"""书房语义检索：自带 ONNX向量模型，纯 numpy算余弦。

（0.8.3）：直接做语义版，自己装载向量库引擎。

三条设计：
1.不引入 torch：用 onnxruntime（打包里已有）+ tokenizers。模型实际约 279 MB（写这句时是 98 MB 的旧量化件，口径已过期）：源码树不入库，PyInstaller 安装包内含。
2.延迟加载：import不读模型、不占内存；第一次真检索才载入。
3.缺件就退化：模型没装/onnxruntime不在，返回空让上层回落字面检索。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "embed"
_LOCK = threading.Lock()
_SESS = None
_TOK = None
_LAST_ERR = ""


# ===== 深度用户：想换自己的语义模型，只改这一处 ======
#
# 换法（二选一，界面上不提供这个配置，故意留在代码里）：
#1) 把你的两个文件直接替掉 `loom/vendor/embed/` 下的
# `model.onnx` 与 `tokenizer.json`；
#2) 或者不动原文件，设环境变量指过去：
# `LOOM_EMBED_MODEL=D:\models\你的.onnx`
# `LOOM_EMBED_TOKENIZER=D:\models\你的\tokenizer.json`
#
# 新模型必须满足三个形状，否则不会报错而是静默退回字面检索（搜不到换词的东西）：
# ① **ONNX 格式**（不是 PyTorch 的 .bin/.safetensors；numpy 不能直接跑）；
# ② 输入 `input_ids` + `attention_mask`（有 `token_type_ids` 也行，本文件会自动补），
# **输出的是逐 token 隐状态 (batch, seq, dim)**，不是已经池化的 (batch, dim)；
# ③ **池化方式要对**：本文件做 mean pooling（按 mask 加权求均）。
# 你的模型若要 CLS pooling（取第一个 token）——例如 BGE 系列正式用法——
# 就把 `embed()` 里 `vec = (out * m).sum(1) / ...` 那两行换成 `vec = out[:,0]`。
# （当前自带的 bge-large-zh ONNX 就是用 mean pooling 跑的，已实测可用。）
#
# 换错了会怎样：`embed()` 捕获异常后返回 None，`rank()` 返回 []，
# 上层（`ask.search`）自动退回纯字面检索。**不会崩、不会白屏**，但你会发现“搜不到换词的东西”。
# 排查就调 `semantic.ready()` 看 `缺` 与 `last_err`。


def _paths():
	# 环境变量优先（上面那段注释说的选项2）；不设就用包内自带的，
	# 包内没有则回退到**本机下载目录**（`fetch_vendor` 的默认落点）。
	# 审查 审查记录 指出：只认包内会让"下到 ~/.loom/vendor"的用户
	# 永远用不上——脚本下了、程序却不去那儿找。
	md = os.environ.get("LOOM_EMBED_MODEL", "").strip()
	tk = os.environ.get("LOOM_EMBED_TOKENIZER", "").strip()

	def _pick(env_value: str, name: str) -> Path:
		if env_value:
			return Path(env_value)
		pkg = VENDOR / name
		if pkg.is_file():
			return pkg
		try:
			from ..config import vendor_dir as _vendor_dir
			alt = _vendor_dir() / "embed" / name
			if alt.is_file():
				return alt
		except Exception:
			pass
		return pkg          # 都没有 → 返回包内路径，让 ready() 如实报"缺"

	return _pick(md, "model.onnx"), _pick(tk, "tokenizer.json")


def perf():
    """估一下这台机器跑得动吗。**零花费、不载模型、不联网**。

    判据只用两个硬指标（都是读本机，不靠猜）：
    1. **物理内存**：模型驻留约= 文件体积 ×2.2（onnxruntime + 中间张量）；余量不够就该退化。
    2. **CPU 核数**：少于4 核时，批量编码会明显抢前台。
    """
    m, t = _paths()
    size_mb = (m.stat().st_size / 1048576) if m.is_file() else 0.0
    need_mb = size_mb * 2.2
    total_mb, cores = -1.0, -1
    try:
        import os as _os
        total_mb = (_os.cpu_count() or 0) * 0.0 # 占位，下面用 ctypes 拿真值
        cores = _os.cpu_count() or 0
    except Exception:
        pass
    try:
        import ctypes
        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        total_mb = ms.ullTotalPhys / 1048576
        avail_mb = ms.ullAvailPhys / 1048576
    except Exception:
        avail_mb = -1.0
    weak = []
    if avail_mb >0 and avail_mb < need_mb +1024:
        weak.append("内存")
    if 0 <= cores <4:
        weak.append("CPU")
    return {"ok": True, "模型MB": round(size_mb,1), "预计占用MB": round(need_mb,1),
            "可用内存MB": round(avail_mb,1), "CPU核": cores,
            "偏弱": weak, "建议退化": bool(weak)}


def ready():
	m, t = _paths()
	gaps = []
	if not m.is_file():
		gaps.append("向量模型")
	if not t.is_file():
		gaps.append("分词表")
	for mod in ("onnxruntime", "numpy", "tokenizers"):
		try:
			__import__(mod)
		except Exception:
			gaps.append(mod)
	return {"ok": True, "可语义检索": not gaps, "缺": gaps, "last_err": _LAST_ERR}


def _load():
	global _SESS, _TOK
	if _SESS is not None and _TOK is not None:
		return _SESS, _TOK
	with _LOCK:
		if _SESS is not None and _TOK is not None:
			return _SESS, _TOK
		m, t = _paths()
		import onnxruntime as ort
		from tokenizers import Tokenizer
		so = ort.SessionOptions()
		_cores = os.cpu_count() or 4
		#2026-10-31：**不再减线程**——实测减线程只会拖慢，减不了压迫感。
		#默认交给 onnxruntime（吃满全部核）；靠**降线程优先级**让路，见 _lower_self_priority。
		loom_lim = int(os.environ.get("LOOM_EMBED_THREADS", "0") or 0)
		if loom_lim > 0:
			so.intra_op_num_threads = loom_lim
			so.inter_op_num_threads = 1
		so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
		_SESS = ort.InferenceSession(str(m), sess_options=so, providers=["CPUExecutionProvider"])
		_TOK = Tokenizer.from_file(str(t))
		_TOK.enable_truncation(max_length=512)
		return _SESS, _TOK


def reload():
	"""手动「修复」：丢掉已加载的模型/分词表，重新探测依赖并试载一次。

	Loom 的语义引擎是**进程内 ONNX**，没有服务可重启——作者点「修复」时要做的
	就是重新加载模型 + 重新探测依赖（用户 2026-11-11 裁定 (a) 方案）。
	返回 ready() 形状 + attempted/loaded 两项读数；绝不抛。
	"""
	global _SESS, _TOK, _LAST_ERR
	try:
		with _LOCK:
			_SESS = None
			_TOK = None
			_LAST_ERR = ""
	except Exception:
		pass
	info = ready()
	loaded = False
	attempted = False
	try:
		if info.get("可语义检索"):
			attempted = True
			vec = embed(["探针"])     # 真跑一次前向：只探测不缓存
			loaded = vec is not None
	except Exception as e:            # noqa: BLE001 - 修复动作本身不许把界面弄崩
		_LAST_ERR = '%s: %s' % (type(e).__name__, e)
	return {**info, "attempted": attempted, "loaded": loaded, "last_err": _LAST_ERR}


def embed(texts, batch=16):
    """把一组文本变成单位向量（L2归一化，余弦=点积）。错了返回 None，不抛。

    **必须分块**（2026-10-30 实测踩到）：
    书房一册库 794 段全塞进去时，onnxruntime 报 `bad allocation`——
    要一次性分配 **490 MB**（注意力矩阵是 O(batch × heads × seq²)），
    于是语义检索一直静默失效。分块后内存峰值降到可控范围。
    """
    global _LAST_ERR
    texts = [str(x) for x in (texts or [])]
    if not texts:
        return None
    try:
        sess, tok = _load()
        import numpy as np
        import os as _os
        step = max(1, min(int(batch or 16), int(_os.environ.get('LOOM_EMBED_BATCH', '16') or 16)))
        out = []
        for start in range(0, len(texts), step):
            chunk = texts[start:start + step]
            #2026-10-31：每处理完一块报一次进度（预热时按钮要显示）
            if _PROGRESS["on"]:
                _PROGRESS["done"] = int(_PROGRESS["done"]) + len(chunk)
            encs = [tok.encode(x) for x in chunk]
            maxlen = max(len(e.ids) for e in encs)
            ids = np.zeros((len(chunk), maxlen), dtype=np.int64)
            mask = np.zeros((len(chunk), maxlen), dtype=np.int64)
            for k, e in enumerate(encs):
                n = len(e.ids)
                ids[k, :n] = e.ids
                mask[k, :n] =1
            feeds = {'input_ids': ids, 'attention_mask': mask}
            names = set(x.name for x in sess.get_inputs())
            if 'token_type_ids' in names:
                feeds['token_type_ids'] = np.zeros_like(ids)
            res = sess.run(None, feeds)[0]
            res = np.asarray(res)
            if res.ndim == 2 and res.shape[0] == len(chunk):
                # Local exporter already includes CLS pooling.
                vec = res
            elif res.ndim == 3 and res.shape[:2] == ids.shape:
                # Preserve the token-output model's existing masked mean.
                m = mask[..., None].astype(np.float32)
                vec = (res * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            else:
                raise ValueError(f"向量模型输出形状不兼容：{res.shape}，输入为 {ids.shape}")
            if vec.shape[1] == 0 or not np.isfinite(vec).all():
                raise ValueError("向量模型输出为空或包含非有限数值")
            norms = np.linalg.norm(vec, axis=1, keepdims=True)
            if np.any(norms <= 1e-9):
                raise ValueError("向量模型输出零向量")
            vec = vec / norms
            out.append(vec.astype('float32'))
            #2026-10-31：让一下 CPU。预热是后台活儿，不该抢光前台写作的手感。
            if _PROGRESS["on"]:
                import time as _t2
                _t2.sleep(0.03)
        if not out:
            return None
        return out[0] if len(out) ==1 else np.vstack(out)
    except Exception as e:
        _LAST_ERR = '%s: %s' % (type(e).__name__, e)
        return None


def rank(query, docs, top_k=10):
	"""按语义相似度给 docs排序，返回 [(相似度,原文)]。不可用时 []。"""
	q = str(query or "").strip()
	rows = [d for d in (docs or []) if str(d).strip()]
	if not q or not rows:
		return []
	allv = embed([q] + rows)
	if allv is None:
		return []
	import numpy as np
	sim = allv[1:] @ allv[0]
	order = sorted(range(len(rows)), key=lambda i: -float(sim[i]))[: max(1, int(top_k))]
	return [(float(sim[i]), rows[i]) for i in order]

# ===== 向量索引的磁盘缓存（2026-10-30）=====
#
# 为什么要存磁盘：全库 794 段用 bge-large 在 CPU 上编码一遍要 **275 秒**（实测），
# 放在每次搜索里就是不可用。存盘后只付一次成本；之后每次直接读。
#
# 失效条件：模型文件变了（比 mtime+大小）、或段数变了 → 重建。
INDEX_VERSION =1


def _index_file():
    from ..config import user_config_dir
    d = user_config_dir() / "embed-index"
    d.mkdir(parents=True, exist_ok=True)
    return d / "index.npz"




#2026-10-31：预热进度。前端拿它在按钮上显示「预热 42%」。
_PROGRESS = {"done": 0, "total": 0, "on": False}


def progress():
    """当前预热进度（百分数）。没在预热就返回 100。"""
    if not _PROGRESS["on"]:
        return 100
    total = max(1, int(_PROGRESS["total"] or 0))
    return max(0, min(100, int(_PROGRESS["done"] * 100 / total)))


def _stamp():
    """模型与版本的指纹。它一变，索引就作废。"""
    m, t = _paths()
    size = m.stat().st_size if m.is_file() else 0
    #2026-10-31：**不能用 mtime当指纹**。
    #开发版与打包版的模型是两份拷贝，mtime 必然不同；两边一切换，
    #索引就被判定失效、重新算 250 秒。只用大小+文件名就够了。
    return "%d|%d|%s" % (INDEX_VERSION, size, t.name)






def _val(z, key, default=""):
    """从 npz 里取标量。**不能直接 str()**——
    numpy 把字符串存成 0 维数组，str() 会带引号，比对永远不等。
    实测踩到过这个坑（索引建好了却总读不到）。"""
    try:
        v = z[key]
        return v.item() if hasattr(v, "item") else v
    except Exception:
        return default


def fingerprint(texts):
    """这批文本的指纹。内容变了就不能再用旧索引。"""
    import hashlib
    h = hashlib.sha1()
    for x in texts:
        h.update(str(x).encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def load_index(count, fp=""):
    """读磁盘索引。没有 / 过期 / 段数或内容对不上 → None。"""
    try:
        import numpy as np
        p = _index_file()
        if not p.is_file():
            return None
        z = np.load(p, allow_pickle=False)
        if int(_val(z, "count", -1)) != int(count):
            return None
        if fp and str(_val(z, "fp")) != str(fp):
            return None
        if str(_val(z, "stamp")) != _stamp():
            return None
        mat = z["mat"]
        if mat.shape[0] != int(count):
            return None
        return mat
    except Exception:
        return None


def save_index(mat, fp=""):
    """存磁盘。先写临时件再改名，不让半截文件被下次读到。

    2026-10-30 实测踩到的坑：`np.savez("x.tmp")` 会**自动补 .npz**，
    实际写成 `x.tmp.npz`；再去 rename `x.tmp` 就找不到文件，
    于是索引每次都白算、从不落盘。所以临时名必须自带 .npz。
    """
    try:
        import numpy as np
        p = _index_file()
        tmp = p.with_name(p.stem + ".tmp.npz")
        np.savez(tmp, mat=mat, count=int(mat.shape[0]), stamp=_stamp(), fp=str(fp or ""))
        if p.exists():
            p.unlink()
        tmp.rename(p)
        return True
    except Exception as e:
        _LAST_ERR = 'save_index: %s' % e
        return False


def build_index(texts, batch=16):
    """算全库向量并存盘。返回矩阵（或 None）。"""
    _PROGRESS["on"] = True
    _PROGRESS["total"] = len(texts)
    _PROGRESS["done"] = 0
    try:
        vec = embed(texts, batch=batch)
    finally:
        _PROGRESS["on"] = False
        _PROGRESS["done"] = _PROGRESS["total"]
    if vec is None:
        return None
    save_index(vec, fp=fingerprint(texts))
    return vec

# ===== 索引状态 + 懒预热（2026-10-30）=====

_BUILDING = {"on": False}
_BUILD_STATE = {"ok": None, "err": ""}


def index_status(count=0, fp=""):
    """索引现在什么状态。前端拿它决定按钮灰不灰。"""
    if not ready()["可语义检索"]:
        return {"就绪": False, "建中": False, "可用": False, "段": 0}
    if _BUILDING["on"]:
        return {"就绪": False, "建中": True, "可用": True,
                "段": int(count or 0), "进度": progress()}
    mat = load_index(int(count or 0), fp) if count else None
    return {"就绪": mat is not None, "建中": False, "可用": True, "段": int(count or 0)}


def _lower_self_priority():
    """把**当前线程**调到最低优先级（Windows）。

    2026-10-31：想把预热做轻，正确做法不是减线程（只拖慢），
    而是降优先级——没人用时全速跑，有人用时自动让路。
    **只调当前线程**，不动整个进程，否则界面请求也跟着变慢。
    """
    try:
        import ctypes
        THREAD_PRIORITY_LOWEST = -2
        _k = ctypes.windll.kernel32
        _k.SetThreadPriority(_k.GetCurrentThread(), THREAD_PRIORITY_LOWEST)
        return True
    except Exception:
        return False


def start_build(texts, count=0, fp=""):
    """开一个后台线程建索引。**已在建就不重复开**。"""
    if _BUILDING["on"]:
        return False
    _BUILDING["on"] = True
    _BUILD_STATE["ok"] = None
    _BUILD_STATE["err"] = ""

    def _run():
        import time as _t
        _lower_self_priority()

        t0 = _t.time()
        try:
            vec = build_index(texts)
            _BUILD_STATE["ok"] = vec is not None and save_index(vec, fp=fp)
            if not _BUILD_STATE["ok"]:
                _BUILD_STATE["err"] = "build/save 失败"
        except Exception as e:  # noqa: BLE001
            _BUILD_STATE["ok"] = False
            _BUILD_STATE["err"] = "%s: %s" % (type(e).__name__, e)
        finally:
            _BUILDING["on"] = False
            _BUILD_STATE["secs"] = round(_t.time() - t0, 1)

    th = threading.Thread(target=_run, name="loom-embed-build", daemon=True)
    th.start()
    return True
