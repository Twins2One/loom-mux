# vendor：这里为什么不放大文件

`loom/vendor/` 放**必须随程序走的小件**（`bili-vision/bv.py`、`embed/tokenizer.json`），
但**不放大文件**。下面三个加起来约 550 MB，放进仓库会让 clone 变成一次大下载，
也会超出常见代码托管平台的单文件限制。

| 文件 | 大小 | 是什么 |
|---|---|---|
| `embed/model.onnx` | 279,247,489 B | 语义检索用的向量模型（bge-large-zh-v1.5，**本地导出的 ONNX**） |
| `ffmpeg/ffmpeg.exe` | 105,423,872 B | 视频/音频解码：画面提取、转写前处理 |
| `ffmpeg/ffprobe.exe` | 105,221,120 B | 读媒体信息（时长/码率/流），配合 ffmpeg 用 |

**缺了会怎样**：程序不会崩，但相关能力降级——
- 缺 `model.onnx`：语义检索退回关键词/字面匹配，命中质量下降（不报错）；
- 缺 `ffmpeg`/`ffprobe`：视频只能走"纯文字"路线（字幕/转写），看不了画面。

**怎么拿**：ffmpeg/ffprobe 可由下载脚本获取并校验；`model.onnx` 没有官方下载地址，必须按下方方案 B 本地导出或由维护者另行提供经过核验的发布附件。

```bash
python -m loom.vendor.fetch_vendor --only ffmpeg
```

默认落到 `~/.loom/vendor/`（`config.vendor_dir()`，可用 `LOOM_VENDOR` 覆盖）。
程序查找顺序是**先包内 `loom/vendor/`、再 `~/.loom/vendor/`**，所以落到后者无需改代码。
想把它们塞进安装目录也可以：`--dest "<安装目录>/loom/vendor"`（目录可能只读）。

## 校验（两层，别只做一层）

脚本对 `ffmpeg.exe` / `ffprobe.exe` 做**两级**校验：

1. **来源可信**：下载上游 zip 后，用上游发布的 `.sha256` 校验这个 zip；
2. **产物是我们要的那份**：从 zip 里解出 exe，再用下面的 sha256 校验一次。

只做第 1 层是不够的——"来自官方"不等于"就是我们要分发的那个构建"。
钉了带版本号的包之后第 2 层本不该再失败；万一失败，脚本**不会静默接受**，而是打印两边的
sha256 并退出。确认接受新构建后再加 `--allow-upstream-drift`（会记录这一偏差）。

手工校验（PowerShell / shasum 均可）：

```
470B1A4F914B2F40C72B7A495240A75FC8858F52F7B6ED8FB13185E571F98F71  embed/model.onnx
3256173F3F8BFFD7DF12227C68ADF68025EDB1832273A9530688A7BB1ED8EDEC  ffmpeg/ffmpeg.exe
F0D36ECBBDD3BCFAC3EFA078C96C7271C2E68B3810595552AC3B7F17E9A65C52  ffmpeg/ffprobe.exe
```

### 为什么钉的是**带版本号**的包（2026-11-12 修正）

清单里原本钉的是 `ffmpeg-release-essentials.zip`——那是 gyan 的**滚动别名**，恒指最新构建。
上游一换版，钉住的 exe sha256 立刻过期：实测上游到 9.0.2 后，`ffmpeg.exe` 变成
105,423,872 B，而当时清单还写着上一版的 136,121,856 B ⇒ **默认路径当场 `FAIL:exe-sha256`**，
用户被卡死，只能靠 `--allow-upstream-drift` 才装得上。

改成 `packages/ffmpeg-9.0.2-essentials_build.zip`（URL 与内容一起固定，旁边
`.sha256` 同样存在且数值一致）之后，钉子不再随上游轮换而腐坏。

> 这类"滚动别名"是钉版本的反面教材：**能下到 ≠ 下到的是同一份**。
> 凡是给二进制钉摘要，就必须同时把**取得该二进制的地址**钉住。

## `model.onnx`：没有官方下载地址（重要）

它不是从上游下的，而是**用 torch 从 bge-large-zh-v1.5 导出的 ONNX**
（文件头里 `producer_name=pytorch`、`producer_version=2.0.1`）。
上游仓库（`BAAI/bge-large-zh-v1.5`）**只提供 `pytorch_model.bin`，没有 `onnx/` 目录**，
所以这里**没有可写的官方 URL**——留空，脚本遇到空 URL 会明确告诉你"需要发布或本地导出"。

> ⚠️ 不要拿别的镜像上同名 ONNX 凑数：例如某些转换版是 **1.3 GB**，与我们要的
> 279 MB 不是同一个产物——下载会"成功"，但 sha256 不符、向量维度也可能不同。
> **可达 ≠ 可用。**

### 方案 A：当前不提供自动下载

当前维护者不把这份本地导出模型作为正式 Release 附件提供，因此 `fetch_vendor --only model`
会明确返回非零并提示本地导出；在取得可核验的正式附件前，不填写伪造或不可追溯的 URL。


### 方案 B：本地自建（离线/自用）

需要 `torch`、`transformers`、`onnx`（仅导出环境使用，不是程序运行依赖）：

```bash
pip install torch transformers onnx
```

脚本采用 PyTorch legacy ONNX 导出器（`dynamo=False`），输出已做 CLS 池化的句向量，并在同目录保存配对的 `tokenizer.json`。运行时也兼容既有三维逐词输出模型，但保留其原有 mean pooling。

```bash
python -m loom.vendor.export_embed_onnx --out ./model.onnx
```

导出后把 `model.onnx` 和同目录的配对 `tokenizer.json` 一起放到 Loom 的用户 vendor/embed 目录（默认 `~/.loom/vendor/embed/`，受 `LOOM_HOME`、`LOOM_VENDOR` 覆盖），运行时会自动发现。也可放在包内 `loom/vendor/embed/`。
自定义位置须同时设置 `LOOM_EMBED_MODEL` 与 `LOOM_EMBED_TOKENIZER`，不要将不同模型的两件文件混配。
**注意**：自己导出的文件与上表 sha256 通常不同（取决于 torch 版本与是否量化），那属正常；以你自己的 sha256 为准记录一份，别去改这里的期望值。

相关环境变量（见 `loom/study/semantic.py`）：
- `LOOM_EMBED_MODEL`：模型文件路径（默认包内 `loom/vendor/embed/model.onnx`）
- `LOOM_EMBED_TOKENIZER`：tokenizer 路径（默认包内 `loom/vendor/embed/tokenizer.json`）

## 许可与分发边界

- `model.onnx` / `tokenizer.json`：源自 **BAAI/bge-large-zh-v1.5**，MIT。
- ffmpeg / ffprobe：上游 gyan.dev `ffmpeg-release-essentials.zip` 官方说明为 **GPL v3**，并列出 GPL 组件；本仓库不随源码树或发布副本分发这两个二进制，`fetch_vendor` 只在用户主动运行时从上游获取并校验。
- GPL v3 原文：https://www.gnu.org/licenses/gpl-3.0.txt 。上游构建页：https://www.gyan.dev/ffmpeg/builds/；上游 `.sha256` 文件作为下载时的来源校验依据。当前具体文件的 checksum 以 `fetch_vendor.py` 清单为准。
- 若今后把 FFmpeg 二进制放入安装包或 Release，必须重新制作对应的 GPL 分发材料（版权/许可文本、对应源码获取方式与构建配置），不能把当前“运行时用户主动获取”说明直接当作二进制再分发证据。

详见仓库根 `THIRD-PARTY-NOTICES.md`。

## 相关代码位置（排查用）

- `loom/study/semantic.py`：`vendor/embed/`（模型与 tokenizer），`ready()` 会返回缺项
- `loom/study/asr.py`：`vendor/ffmpeg/ffmpeg.exe`、`vendor/whisper/`（转写）
- `loom/study/video.py`：`vendor/ffmpeg/ffmpeg.exe` + `ffprobe.exe`（画面提取）
- `loom/config.py`：`vendor_dir()` → `~/.loom/vendor`（`LOOM_VENDOR` 可覆盖）
