"""把 bge-large-zh-v1.5 导出成 Loom 用的 ONNX（方案 B：本地自建）。

为什么需要它：`loom/vendor/embed/model.onnx` 不是官方发布物，而是本机用 torch
导出的（ONNX 头 `producer_name=pytorch`）。上游仓库只有 `pytorch_model.bin`，
所以想自己造一份等价的，就得跑这个脚本。

用法：
    # 一次性：装依赖（几百 MB）
    pip install torch transformers onnx

    # 导出（输出 model.onnx 及配对 tokenizer.json；按下方提示设置运行路径）
    python -m loom.vendor.export_embed_onnx --out ./model.onnx
    python -m loom.vendor.export_embed_onnx --out ./model.onnx --model BAAI/bge-large-zh-v1.5

**导出后 sha256 通常与仓库里记录的那份不同**（取决于 torch 版本、是否量化）。
这属正常：此时请用 `LOOM_EMBED_MODEL` 指向你的文件，并以你自己的 sha256 为准；
不要为了"对上"去改 `loom/vendor/README.md` 里记录的分发版指纹。

注意 **pooling**：本脚本采用 bge 的 CLS + normalize，烘进图里（输出即二维句向量）；
运行时直接归一化使用，不再重复池化。配对 tokenizer 同目录保存。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MODEL = "BAAI/bge-large-zh-v1.5"
DEFAULT_MAX_LEN = 512


def export(out: Path, model_id: str, max_len: int, opset: int) -> int:
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        print(f"缺依赖：{exc}\n请先 pip install torch transformers onnx", file=sys.stderr)
        return 2

    print(f"加载 {model_id} …（首次会下载权重）")
    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id).eval()

    class _Wrapped(torch.nn.Module):
        """把「主干 + CLS 池化 + L2 归一化」合成一张图，输出直接是句向量。"""

        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask, token_type_ids=None):
            kwargs = {"input_ids": input_ids, "attention_mask": attention_mask}
            if token_type_ids is not None:
                kwargs["token_type_ids"] = token_type_ids
            hidden = self.inner(**kwargs).last_hidden_state
            vec = hidden[:, 0]                       # bge：取 [CLS]
            return torch.nn.functional.normalize(vec, p=2, dim=1)

    sample = tok(["示例文本"], padding=True, truncation=True,
                 max_length=max_len, return_tensors="pt")
    inputs = (sample["input_ids"], sample["attention_mask"])
    names = ["input_ids", "attention_mask"]
    if "token_type_ids" in sample:
        inputs = inputs + (sample["token_type_ids"],)
        names.append("token_type_ids")

    out.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(
            _Wrapped(model), inputs, str(out),
            input_names=names, output_names=["sentence_embedding"],
            dynamic_axes={n: {0: "batch", 1: "seq"} for n in names}
                         | {"sentence_embedding": {0: "batch"}},
            opset_version=opset, do_constant_folding=True, dynamo=False,
        )
    # Keep tokenizer paired with the chosen model, especially for --model overrides.
    tok.save_pretrained(str(out.parent))
    if not (out.parent / "tokenizer.json").is_file():
        raise RuntimeError("导出器没有生成 tokenizer.json，请使用支持 fast tokenizer 的模型。")
    print(f"已写出：{out}（{out.stat().st_size:,} B）及配对 tokenizer.json")
    from ..config import vendor_dir
    print(f"放到位：把 model.onnx 与 tokenizer.json 一起放到 {vendor_dir() / 'embed'}；"
          "也可放到包内 loom/vendor/embed/。")
    print("或用 LOOM_EMBED_MODEL / LOOM_EMBED_TOKENIZER 指过去。")
    print("校验：见 loom/vendor/README.md 的 sha256 说明（自建件以你自己的值为准）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="loom.vendor.export_embed_onnx",
                                description="把 bge-large-zh-v1.5 导出成带池化的 ONNX")
    ap.add_argument("--out", default="model.onnx", help="输出路径（默认 ./model.onnx）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"HF 模型 id（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--max-len", type=int, default=DEFAULT_MAX_LEN, help="最大序列长度")
    ap.add_argument("--opset", type=int, default=17, help="ONNX opset（默认 17）")
    args = ap.parse_args(argv)
    return export(Path(args.out).expanduser(), args.model, args.max_len, args.opset)


if __name__ == "__main__":
    sys.exit(main())
