# THIRD-PARTY NOTICES

本仓库（Loom-mux，MIT License；Copyright (c) 2026 Chambers；项目署名 Two2One）基于 [WadeZhao23/loom-novel](https://github.com/WadeZhao23/loom-novel)（MIT）重构；0.6.4 起按个人需求重构了 Loom-mux。各组件版权归其各自的权利人所有。

## 一、Python 依赖（经 pip / pyproject.toml 分发）

以下许可证信息按各包在 PyPI 上的公开声明整理；安装时请以 pip 元数据（`pip show <pkg>` 的 License 字段）为准。

### 直接依赖

| 包 | 用途 | 许可证 |
|---|---|---|
| typer | CLI 框架 | MIT |
| rich | 终端富文本 | MIT |
| openai | OpenAI 兼容 API 客户端 | Apache-2.0 |
| python-dotenv | .env 读取 | BSD-3-Clause |
| fastapi | 本地 API 服务 | MIT |
| uvicorn | ASGI 服务器 | BSD-3-Clause |
| pywebview | 桌面原生窗口 | BSD-3-Clause |

### 可选依赖组

| 包 | 组 | 许可证 |
|---|---|---|
| pyinstaller | dist（打包） | GPL-2.0-or-later（含 bootloader 例外：PyInstaller bootloader 允许以任意许可证分发其产出的可执行文件） |
| pytest | dev（测试） | MIT |
| pyyaml | dev | MIT |
| build | dev | MIT |

### 主要间接依赖（uv.lock）

以下为锁文件中的传递依赖，许可证按 PyPI 公开声明整理（版本以 uv.lock 为准）：

- **MIT**：click、colorama、markdown-it-py、mdurl、pygments、packaging、pluggy、iniconfig、pydantic、pydantic-core（部分组件为衍生条款，见各包声明）、shellingham、sniffio、starlette、tqdm、typing-extensions、typing-inspection、pywin32-ctypes、macholib、altgraph、pefile、jiter、annotated-types、annotated-doc、proxy-tools、qtpy、setuptools
- **BSD 系列**：anyio（MIT）、httpcore（BSD-3）、httpx（BSD-3）、certifi（MPL-2.0）、idna（BSD-3-like）、cffi（MIT）、pycparser（BSD-3）、h11（MIT）、clr-loader（MIT）、distro（Apache-2.0）、bottle（MIT）、pyobjc-core 及 pyobjc-framework-* 系列（MIT）
- 上述个别包的许可证归类如有出入，以包内 LICENSE 文件为准。

### 构建系统

- hatchling — MIT（构建后端）

## 二、随包分发的 vendored 组件

### 1. bv.py（`loom/vendor/bili-vision/bv.py`）

来源：bili-vision 项目（https://github.com/icyaaaww/bili-vision），MIT License。
本仓库收录其单文件副本用于 B 站视频取稿（字幕/下载/抽帧/转写调度）。该文件以 MIT 许可证再分发，版权归原项目作者所有。

> 注：收录时原文件头部未含许可证声明行，本注释即为再分发声明。如原项目许可条款有更新，以上游仓库为准。

### 2. bge-large-zh 嵌入模型（`loom/vendor/embed/` 下的 tokenizer.json 及按需下载的 model.onnx）

- 模型：BAAI/bge-large-zh-v1.5（https://huggingface.co/BAAI/bge-large-zh-v1.5），MIT License，版权归北京智源人工智能研究院（BAAI）。
- 本仓库收录 tokenizer 配置文件；模型权重（model.onnx，约 266MB）**不入库**，因当前产物没有官方下载 URL，按 `loom/vendor/README.md` 本地导出/手动放置；若未来由维护者提供发布附件，须同时提供固定 URL 与 sha256。
- 口径必须分两条通道：**源码树/发布副本不含**该权重（现读：`loom-release` 内 glob `**/embed/*` 无模型文件）；但**已构建的 PyInstaller 安装包内含** `loom/vendor/embed/model.onnx`（279,247,489 B，sha256 `470B1A4F914B2F40C72B7A495240A75FC8858F52F7B6ED8FB13185E571F98F71`）与配对 `tokenizer.json`。按"甲（自建/手动导出，不作为正式 Release 附件）"裁定，这条通道若对外发版，须把"来源 = BAAI/bge-large-zh-v1.5（MIT）+ 导出脚本 + 固定 sha256"一并附上，不能只写"不入库"。
- ONNX 导出版本基于官方权重转换，转换本身不改变许可。

### 3. faster-whisper 运行时（可选，取稿语音转写）

- faster-whisper — MIT License（Guillaume Klein / SYSTRAN）。
- 转写模型 faster-whisper-small（Systran，https://huggingface.co/Systran/faster-whisper-small）— MIT License。
- 模型权重同样不入库，按需下载。

## 三、FFmpeg / ffprobe（构建口径以本节为准）

### 实际分发的构建

- 安装包（Release 附件）内含 `ffmpeg.exe` / `ffprobe.exe`，为 **BtbN master LGPL 构建**，
  版本串 `N-128227-g4811625b5f-20261031`（2026-10-31 构建）。
  构建配置核验（`ffmpeg -buildconf`）：**无 `--enable-gpl`**，且含 `--disable-libx264`、`--disable-libx265`。
- 许可：**LGPL v3**（该构建 `--enable-version3`）。许可原文 https://www.gnu.org/licenses/lgpl-3.0.html ；
  构建来源 https://github.com/BtbN/FFmpeg-Builds ；对应源码获取 https://ffmpeg.org/download.html 。
- **仓库本身不含这两个二进制**（体积原因）；拿到仓库的人需自行获取，程序在缺文件时给出明确提示。

### 口径沿革（避免误读，必须保留）

- 2026-11-01 之前用的是 **GPL v3** 构建（gyan.dev 2018 版 `N-92722`，含 `--enable-gpl`、libx264、libx265）。
  该构建**已换成上面的 LGPL 构建**，仅在本机留作回退备份（旧文件改名，不在任何分发路径上）。
- 2026-11-12 另行**验证过** gyan.dev 的 GPL 构建可运行（`ffmpeg-9.0.2-essentials_build.zip`，
  含 `--enable-gpl`/libx264/libx265），但该方案**未采纳**，也未进入任何分发包。
  本节记录它，只为说明"为什么最终不是 GPL"，不构成分发。
- 若将来改用 GPL 构建对外分发，须同时提供对应源码获取方式、构建配置与完整许可材料；
  本文不替代该分发证据。

## 四、其他素材

- agent 角色头像：除市场分析师外，由 ModelScope（modelscope.cn）的 Qwen-Image 生成；市场分析师头像由本项目使用 Pillow 在本地程序化绘制。上述头像随本仓库以 MIT 分发（见 README「License & 素材」）。
- Logo 与界面图标：本项目自有。

---

## 五、随**安装包**分发的第三方二进制（PyInstaller 产物）

> 口径与第三节不同：第三节说的是**源码树/发布副本不含二进制**；本节说的是 PyInstaller 安装包，它**确实分发**下列第三方二进制。按 2026-11-13 用户裁定（补 GPL 分发材料）逐条列明许可原文、对应源码获取方式与构建配置口径。

### GPL-2.0-or-later（随 PyAV wheel 分发，必须按 GPL 处理）

- **libx264** — 2,267,648 B；包内 `av-19.0.2.dist-info/RECORD` sha256 `D3HzfPPz7g4uysKwaCnp_zzys-I38luxSu9rjeFM13c`。
  许可原文：https://www.gnu.org/licenses/gpl-2.0.txt ；对应源码获取方式：https://git.videolan.org/?p=x264.git
- **libx265** — 12,830,720 B；包内 RECORD sha256 `7-B18e4AjHIutxrf4A2SYHGDbXd8ye8jtZyey5231X8`。
  许可原文：https://www.gnu.org/licenses/gpl-2.0.txt （上游另有 linking clause，见 x265 仓库 COPYING）；对应源码获取方式：https://bitbucket.org/multicoreware/x265_git/
- 这两个 DLL **不是本项目构建的**：来自 PyAV 官方 wheel（av-19.0.2）。构建配置口径 = 该 wheel 的构建脚本 + 包内 RECORD（文件名与 sha256 逐条可核）。
- ⚠️ 别把两件事混起来：安装包里的 **`ffmpeg.exe`/`ffprobe.exe` 是 LGPL 构建**（x264/x265 已在构建时禁用，见第三节）；
  而这两个 **`libx264`/`libx265` DLL 来自 PyAV 的 wheel**，与 ffmpeg 可执行文件无关，属 GPL-2.0，必须按本节处理。

### LGPL-2.1（随 Shapely wheel 分发）

- **GEOS**：包内 `Shapely.libs/geos-…dll`、`Shapely.libs/geos_c-…dll`。许可原文：https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html ；对应源码获取方式：https://libgeos.org/ 。Shapely 本身为 BSD-3-Clause。

### 随安装包分发、此前未点名的组件（以包内 LICENSE 为准）

- **BSD-3-Clause**：numpy、PyAV(av)、psutil、pyclipper、zstandard
- **MIT / MIT-CMU**：onnxruntime、ctranslate2、Pillow（包内声明为 MIT-CMU）、charset_normalizer、setuptools
- **Apache-2.0**：huggingface_hub、tokenizers、rapidocr_onnxruntime、opentelemetry-api、openai
- 包内另带上游自带的许可材料：`onnxruntime/ThirdPartyNotices.txt`（覆盖 protobuf 等，此前未在本文件点名）；numpy 与 Pillow 的 LICENSE 文件内含 GPL 段落（属其引用的第三方组件，非整体许可）。

若今后把上述二进制列入 GitHub Release 或安装包对外分发，必须把"许可原文 + 对应源码获取方式 + 构建配置"三项一并附上；本节的口径不替代那份材料。

---

以上清单为人工整理，如有遗漏以各组件随附的 LICENSE 文件为准。
