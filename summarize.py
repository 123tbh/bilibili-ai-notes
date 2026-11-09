#!/usr/bin/env python3
"""B站视频 -> 本地 AI 学习笔记

用法:
    bnote <b站视频链接> [输出目录]            # 单个视频(合集默认第1P或 ?p=N 指定)
    bnote <合集链接> --all [输出目录]                  # 批量处理合集全部分P
    bnote <合集链接> --all --range 2-4 [输出目录]      # 只处理第2~4P

流程: yt-dlp 下载音轨 -> faster-whisper 语音转文字(结果缓存,重复秒回)
      -> Ollama 本地模型分块总结 -> 结构化 Markdown 笔记
全程本地/免费,带下载与推理失败重试。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# 让 faster-whisper 从国内镜像下载识别模型(更快更稳)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

LLM_MODEL = os.environ.get("BILI_LLM_MODEL", "qwen2.5:7b")
WHISPER_MODEL = os.environ.get("BILI_WHISPER_MODEL", "small")
CHUNK_CHARS = int(os.environ.get("BILI_CHUNK_CHARS", "2800"))
RETRIES = int(os.environ.get("BILI_RETRIES", "3"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("BILI_CACHE_DIR", os.path.join(APP_DIR, "cache"))

SYSTEM = (
    "你是一个前端/网页开发课程的学习助手。用户给你视频的语音逐字稿，"
    "你要提炼成一份结构清晰、可以直接复习的学习笔记。仅输出 Markdown 正文，不要额外寒暄。"
)

ONE_SHOT = """下面是视频的完整语音转写文字，请整理成学习笔记，严格包含以下小节：

## 一、视频主旨
（两三句话说明这个视频讲了什么、要解决什么问题）

## 二、核心知识点
（分点列出，每个点给出关键概念的一句话解释）

## 三、代码 / 命令 / 术语
（单独列出视频中出现的代码片段、命令、工具名、术语，并简要解释）

## 四、重点与易错点
（视频中强调的坑、注意事项、容易出错的地方）

## 五、一句话总结

视频文字如下：
{text}
"""

PART_PROMPT = """下面是视频语音转写文字的第 {i} 部分，请把这一部分的要点提炼成要点列表（用 Markdown 的 - 列表，保留出现的代码、命令、术语、易错点）。不要遗漏技术细节。

{chunk}
"""

FINAL_PROMPT = """下面是对一个视频各个部分的要点摘录，请把它们整合成一份完整的、结构清晰的学习笔记，严格包含以下小节：

## 一、视频主旨
## 二、核心知识点
## 三、代码 / 命令 / 术语
## 四、重点与易错点
## 五、一句话总结

各部分要点如下：
{parts}
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def with_retry(desc, fn, retries=RETRIES):
    """通用重试:网络/风控类瞬时失败按指数退避重试。"""
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 统一重试入口
            last = e
            wait = 2 ** attempt
            log(f"  [重试 {attempt}/{retries}] {desc} 失败: {e} -> {wait}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"{desc} 重试 {retries} 次仍失败") from last


def ytdlp_bin():
    return os.path.join(os.path.dirname(sys.executable), "yt-dlp")


def ytdlp(args, desc, timeout=600):
    def run():
        p = subprocess.run(
            [ytdlp_bin(), "--no-warnings"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "").strip().splitlines()[-1])
        return p.stdout
    return with_retry(desc, run)


def get_ffmpeg():
    import static_ffmpeg
    ffmpeg, _ = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
    return ffmpeg


def probe(url):
    """拿单个视频的 id 和标题(--no-playlist)。"""
    out = ytdlp(
        ["--skip-download", "--no-playlist",
         "--print", "%(id)s", "--print", "%(title)s", url],
        "获取视频信息",
    ).strip().splitlines()
    vid, title = out[0].strip(), out[1].strip() if len(out) > 1 else vid
    return vid, title


def playlist_entries(url):
    """合集模式: 返回 [(跳转url, id, title), ...]。"""
    out = ytdlp(
        ["--skip-download", "--flat-playlist",
         "--print", "%(url)s", "--print", "%(id)s", "--print", "%(title)s", url],
        "获取合集分P列表", timeout=300,
    ).strip().splitlines()
    entries = []
    for i in range(0, len(out) - 2, 3):
        e_url, e_id, e_title = out[i].strip(), out[i+1].strip(), out[i+2].strip()
        if e_url and e_url != "NA":
            entries.append((e_url, e_id, e_title))
    return entries


def cache_path(vid):
    return os.path.join(CACHE_DIR, f"{vid}.json")


def load_cache(vid):
    try:
        with open(cache_path(vid), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("whisper_model") == WHISPER_MODEL:
            return data
    except (OSError, ValueError, KeyError):
        pass
    return None


def save_cache(vid, title, text, timeline, language):
    os.makedirs(CACHE_DIR, exist_ok=True)
    data = {
        "id": vid, "title": title, "language": language,
        "text": text, "timeline": timeline,
        "whisper_model": WHISPER_MODEL,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = cache_path(vid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, cache_path(vid))


def download_audio(url, workdir):
    """下载音轨, 返回音频文件路径。"""
    ffmpeg = get_ffmpeg()
    audio = os.path.join(workdir, "audio.m4a")
    ytdlp(
        ["-f", "bestaudio/best", "-x", "--audio-format", "m4a",
         "--ffmpeg-location", ffmpeg, "--no-playlist", "-o", audio, url],
        "下载音轨", timeout=1800,
    )
    if not os.path.exists(audio):
        raise RuntimeError("音轨下载后未找到文件")
    return audio


def transcribe(audio):
    from faster_whisper import WhisperModel
    log("加载语音识别模型(首次会下载, 走镜像)...")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio, language="zh", vad_filter=True, beam_size=5)
    log(f"识别语言: {info.language} (置信度 {info.language_probability:.0%})")

    text_parts, timeline = [], []
    for seg in segments:
        mm, ss = divmod(int(seg.start), 60)
        timeline.append(f"[{mm:02d}:{ss:02d}] {seg.text.strip()}")
        text_parts.append(seg.text.strip())
    return "".join(text_parts), "\n".join(timeline), info.language


def call_llm(prompt):
    def once():
        payload = json.dumps({
            "model": LLM_MODEL, "prompt": prompt, "system": SYSTEM,
            "stream": False, "options": {"temperature": 0.2},
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:11434/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=900) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if not resp.get("response"):
            raise RuntimeError("ollama 返回空响应")
        return resp["response"]
    return with_retry("调用本地模型", once)


def summarize(text):
    if len(text) <= CHUNK_CHARS:
        log("文字较短, 一次性总结...")
        return call_llm(ONE_SHOT.format(text=text))

    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    log(f"文字较长, 分 {len(chunks)} 段分别总结...")
    parts = []
    for i, c in enumerate(chunks, 1):
        log(f"  - 总结第 {i}/{len(chunks)} 段")
        parts.append(call_llm(PART_PROMPT.format(i=i, chunk=c)))
    return call_llm(FINAL_PROMPT.format(parts="\n\n".join(parts)))


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name[:80] or "note"


def write_note(outdir, title, url, note, timeline):
    safe = sanitize(title)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"{safe}-笔记.md")
    header = f"# {title}\n\n> 来源: {url}\n\n---\n\n"
    content = header + note + "\n\n---\n\n## 附: 带时间戳的完整转写\n\n" + timeline + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def process_video(url, outdir, workdir):
    """处理单个视频: 探测 -> 缓存 -> (下载+转写) -> 总结 -> 笔记。"""
    vid, title = probe(url)
    log(f"视频 [{vid}] {title}")

    cached = load_cache(vid)
    if cached:
        log("  命中转写缓存, 跳过下载与识别")
        text, timeline = cached["text"], cached["timeline"]
    else:
        audio = download_audio(url, workdir)
        text, timeline, lang = transcribe(audio)
        save_cache(vid, title, text, timeline, lang)

    log(f"  转写 {len(text)} 字, 开始总结...")
    note = summarize(text)
    path = write_note(outdir, title, url, note, timeline)
    log(f"  ✅ {path}")
    return path


def parse_range(spec, total):
    """解析 '2-4' / '3' 这类分P选择, 返回下标集合(0-based)。"""
    picked = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            picked.update(range(int(a) - 1, int(b)))
        else:
            picked.add(int(part) - 1)
    invalid = [i + 1 for i in picked if i < 0 or i >= total]
    if invalid:
        raise ValueError(f"分P编号超出范围: {invalid}")
    return sorted(picked)


def main():
    ap = argparse.ArgumentParser(description="B站视频 -> 本地 AI 学习笔记")
    ap.add_argument("url", help="B站视频/合集链接")
    ap.add_argument("outdir", nargs="?", default=os.path.expanduser("~"),
                    help="笔记输出目录(默认 ~)")
    ap.add_argument("--all", action="store_true", help="处理合集全部分P")
    ap.add_argument("--range", dest="rng", metavar="如 2-4",
                    help="只处理指定分P(需配合 --all)")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="bnote_")
    try:
        if args.all or args.rng:
            entries = playlist_entries(args.url)
            if not entries:
                raise RuntimeError("未获取到任何分P")
            if args.rng:
                entries = [entries[i] for i in parse_range(args.rng, len(entries))]
            log(f"合集共待处理 {len(entries)} 个分P")
            done = []
            for n, (e_url, _eid, e_title) in enumerate(entries, 1):
                log(f"\n=== [{n}/{len(entries)}] {e_title} ===")
                try:
                    done.append(process_video(e_url, args.outdir, workdir))
                except Exception as e:  # noqa: BLE001 - 单P失败不中断整批
                    log(f"  ❌ 该分P失败, 跳过: {e}")
            log(f"\n全部完成: 成功 {len(done)}/{len(entries)}")
        else:
            process_video(args.url, args.outdir, workdir)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()