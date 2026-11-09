# bilibili-ai-notes

**全本地、零 API 成本的 B站课程视频 → 结构化学习笔记工具。**

一条命令,把一个(或一整部合集的)B站课程视频变成一份 Markdown 学习笔记:视频主旨 / 核心知识点 / 代码·命令·术语 / 重点易错点 / 一句话总结,并附带**带时间戳的完整逐字稿**。

```bash
bnote "https://www.bilibili.com/video/BVxxxx"           # 单个视频
bnote "https://www.bilibili.com/video/BVxxxx" --all      # 合集全部分P
bnote "https://www.bilibili.com/video/BVxxxx" --all 2-4  # 指定分P
```

---

## 一、问题与动机

在 B站自学技术课程时的真实痛点:

- **多数课程视频没有 CC 字幕** → 内容无法搜索、无法快速回看定位;
- 倍速听容易漏,手动记笔记跟不上;
- 云端总结工具依赖"视频有字幕",且存在**免费额度限制、上传隐私顾虑、API 成本**。

目标:本地运行、完全免费、无限次、不依赖视频自带字幕 —— 丢链接出笔记。

## 二、架构

```text
B站链接
  │  yt-dlp + 静态 ffmpeg(bundled, 免安装)
  ▼
音轨 audio.m4a
  │  faster-whisper small / int8 量化(ctranslate2, CPU 推理, VAD 过滤)
  ▼
带时间戳逐字稿 ──► 转写缓存 cache/<bvid>.json(重复处理秒级命中)
  │
  │  长文本分块(2800 字/块)map: 逐块要点提炼
  ▼
reduce: 归纳为五段式结构化笔记
  │  Ollama · qwen2.5:7b(本地推理, temperature=0.2)
  ▼
Markdown 学习笔记(主旨 / 知识点 / 代码术语 / 易错点 / 一句话总结 + 时间戳逐字稿)
```

单文件实现([summarize.py](summarize.py)),shell wrapper 一条命令调用。

## 三、关键技术决策

| 决策点 | 选择 | 理由(实测依据) |
|---|---|---|
| ASR 引擎 | faster-whisper(ctranslate2)而非 openai-whisper | 同精度下推理更快、内存更低;`int8` 量化在 Apple M1 CPU 上 73 分钟中文课程音频约 12~15 分钟完成转写 |
| ASR 模型档位 | small(默认,可切 medium) | 中文识别置信度 100%,同音字错误在总结环节可被容错;medium 更准但耗时约翻倍,按需环境变量切换 |
| 总结模型 | qwen2.5:7b(4.7GB Q4 量化, 本地) | 中文技术内容总结质量与速度的平衡点;16GB 内存可同时跑 ASR + LLM |
| 长文本策略 | map-reduce 分块归纳(2800 字/块) | 单块 ≈ 2000 token,安全适配 8k 上下文;逐块保细节(代码/命令/术语),reduce 保结构 |
| ASR 解码依赖 | PyAV 锁定 `<15` | PyAV 15 移除了 `av.open(metadata_errors=...)` 参数,faster-whisper 1.2.1 未适配 → 直接降级到 14.2.0(见踩坑 ①) |
| 模型下载 | `HF_ENDPOINT=hf-mirror.com` | 国内网络 HuggingFace 直连不稳定,镜像下载提速 |
| ffmpeg | static-ffmpeg 静态二进制 | 目标机免 brew / 免系统依赖,`uv` 一键拉起全套环境 |

## 四、工程化设计

- **转写缓存**:ASR 是最贵的一步(73 分钟音频 ≈ 12 分钟推理)。转写结果按 `<bvid>` 原子落盘(tmp + rename),重复处理**秒级命中**;缓存与 whisper 模型档位绑定,切档自动失效。
- **批量与容错**:`--all` 处理合集全部分P、`--range 2-4` 选段;**单P 失败不中断整批**,最后汇总成功数。
- **失败重试**:下载、探测、LLM 调用统一走指数退避重试(B站风控 412、ollama 瞬时不可用实测有效)。
- **可配置**:模型档位 / 分块大小 / 重试次数全部环境变量化,不改代码可调。

## 五、踩坑记录(真实排查)

1. **PyAV 19 × faster-whisper 1.2.1 不兼容**:运行时 `TypeError: open() got an unexpected keyword argument 'metadata_errors'`。定位:PyAV 15.0 起移除该参数,而 faster-whisper 的依赖声明是宽泛的 `av>=11`,uv 默认装了最新 19。修复:`av<15` 并强制 `--no-build`(仅预编译 wheel,避免源码编译要求 pkg-config),锁定 `av==14.2.0`。
2. **B站搜索/页面风控 412 Precondition Failed**:短时间高频请求触发反爬。缓解:指数退避重试 + 控制请求频率;下载接口本身风控较松,搜索接口需节制。
3. **无 brew 环境**(`pkg-config is required for building PyAV`):传统"装 brew → 装 ffmpeg → 装 python"路线在该机器上不可用/过重,改用 `uv`(独立 Python 3.12 + 极速依赖安装)+ `static-ffmpeg`(免系统依赖)。

## 六、效果示例

**输入**:3 分钟《前端简介》课程视频(B站,无字幕)
**输出**(节选):

> **三、代码 / 命令 / 术语**
> - 开发者工具:右键页面或 F12 打开,用于查看和调试网页代码
> - HTML:`<html>` `<body>` `<div>` `<a>` 等标签;CSS:`color: #000000;`;JS:`console.log("Hello World!")`
>
> **四、重点与易错点**
> - 注意区分 HTML / CSS / JavaScript 的作用:结构、样式、交互各司其职

**输入**:73 分钟 FDE(前沿部署工程师)课程第一集(合集 6 集,无字幕)
**输出**:16370 字逐字稿 → 6 块 map + 1 次 reduce 归纳为结构化笔记,**端到端约 20 分钟**(M1 CPU),成本 0 元(云端 ASR 同等时长约 ¥3~6)。

## 七、已知限制与改进方向

- small 模型存在同音字转写错误("图片"→"胡片"类),总结环节大多可容错;**实测发现专有名词是重灾区**:英文品牌/术语(如 Palantir)易误听并被带入总结,对精度敏感的场景切 `BILI_WHISPER_MODEL=medium` 或人工校对逐字稿。
- 尚无自动化的输出质量评估(计划:转写抽样 CER + 总结要点覆盖率人工对照表)。
- 单机串行处理;合集批量可进一步加队列并发(推理为瓶颈,收益有限)。
- 仅支持 B站;抽出平台层后可扩展 YouTube / 本地视频文件。

## 八、环境要求

macOS(Apple Silicon 实测)/ Linux;Python 3.12;[Ollama](https://ollama.com) 运行并拉取 `qwen2.5:7b`;首次运行自动下载 whisper small 模型(约 460MB,走国内镜像)。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python yt-dlp "faster-whisper>=1.2" "av<15" static-ffmpeg
```
