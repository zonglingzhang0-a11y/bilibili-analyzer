"""
大模型情感分析：把评论正文分批发给 DeepSeek（OpenAI 兼容接口），逐条标注情感和评论类型。

用法:
    python llm_sentiment.py --set-key                  # 在自己的终端里输入 API key（不回显），保存到 .deepseek_key
    python llm_sentiment.py --check                    # 检查 key 是否可用、账户余额
    python llm_sentiment.py bilibili_output/20260928_192613            # 每个视频随机抽 300 条
    python llm_sentiment.py bilibili_output/20260928_192613 --sample 1000
    python llm_sentiment.py bilibili_output/20260928_192613 --full --max-cost 50
    python llm_sentiment.py --all                      # 输出目录下所有期

也可以用环境变量 DEEPSEEK_API_KEY 提供 key（优先于 .deepseek_key）。
结果按评论 rpid 缓存在每个视频目录的 llm_sentiment.json，重复运行只补没标过的，中断后可接着跑；
抽样按 rpid 的哈希排序取前 N 条，加大 N 时原来的样本保留、只补新增的。
只发送评论正文，不发送用户名、UID 等信息。完成后重新生成周报即可看到对比。
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import httpx

API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deepseek_key")
LABEL_FILE = "llm_sentiment.json"
PROMPT_VERSION = 1            # 提示词或类别改动时加 1，旧的标注会重新做

DEFAULT_SAMPLE = 300          # 每个视频默认抽样条数
BATCH_SIZE = 40               # 每次请求的评论条数
MAX_CHARS = 300               # 单条评论最多发送的字数
WORKERS = 8                   # 并发请求数
MAX_RETRIES = 5
DEFAULT_MAX_COST = 20.0       # 单次运行的花费上限（元）

# DeepSeek 官网价格（元 / 百万 tokens），价格调整时改这里；只用于估算花费和 --max-cost 上限
PRICE_INPUT_HIT = 0.2
PRICE_INPUT_MISS = 2.0
PRICE_OUTPUT = 3.0

SENTIMENT_CODES = {"正": "positive", "中": "neutral", "负": "negative"}
CATEGORIES = {
    "1": "夸赞支持",
    "2": "批评不满",
    "3": "调侃玩梗",
    "4": "讨论观点",
    "5": "提问求助",
    "6": "抽奖打卡",
    "7": "其他",
}

SYSTEM_PROMPT = """你是 B站（哔哩哔哩）评论区的情感标注员。用户会给你一批视频评论（JSON，键是编号，值是评论正文），
请逐条判断评论者表达的情感和评论类型，只输出 JSON。

情感（一个字）：
- 正：喜爱、赞美、感动、支持、开心、被逗笑
- 负：不满、批评、失望、愤怒、厌恶、讽刺挖苦、阴阳怪气
- 中：客观陈述、提问、单纯玩梗或复读而没有明显态度、抽奖打卡

类型（一个数字）：1 夸赞支持，2 批评不满，3 调侃玩梗，4 讨论观点（聊内容、分享经历或信息），5 提问求助，6 抽奖打卡（参与抽奖、前排、打卡），7 其他

B站 用语提示（要结合语境判断）：
- 「绷不住了」「笑死」「xswl」「蚌埠住了」「草」通常是被逗笑，偏正面
- 「典」「急了」「孝」「赢麻了」「好好好」「你说得对，但是」多为讽刺，偏负面
- 「awsl」「泪目」「破防了（被感动）」偏正面；「破防了（被气到）」「下头」「难绷（尴尬）」偏负面
- 「下次一定」「来了」「前排」多为调侃或打卡，偏中性
- [doge] 等方括号里的是表情：[doge] 常表示调侃，[笑哭] 多为好笑，[星星眼] 喜爱，[辣眼睛] 嫌弃

输出格式示例：{"labels": {"0": "正1", "1": "中3", "2": "负2"}}
每条评论都要有结果，编号与输入一致，不要输出其他内容。"""


class LLMError(Exception):
    """接口返回无法重试的错误（key 无效、余额不足等）"""


# ── API key ─────────────────────────────────────────────

def load_key(path: str = DEFAULT_KEY_FILE) -> str | None:
    """环境变量优先，其次是 .deepseek_key 文件"""
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    # 记事本可能存成带 BOM 的 UTF-8 或 UTF-16
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return clean_key(raw.decode(encoding, errors="ignore")) or None


def clean_key(value: str) -> str:
    """去掉粘贴时终端可能夹带的控制字符（如括号粘贴模式的 ESC[200~ … ESC[201~）、BOM 和空白"""
    value = re.sub(r"\x1b\[20[01]~", "", value or "")
    return "".join(ch for ch in value if ch.isprintable() and not ch.isspace()).lstrip("﻿")


def read_clipboard() -> str:
    """读取剪贴板文本（Windows 用 PowerShell，其他系统用 tkinter）"""
    if sys.platform == "win32":
        result = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        return result.stdout
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def save_key(value: str, path: str = DEFAULT_KEY_FILE):
    """原子写入 key 文件（该文件已加入 .gitignore）"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value.strip())
    os.replace(tmp, path)


def check_key(key: str) -> dict:
    """查询账户余额，顺便验证 key；key 无效时抛出 LLMError"""
    resp = httpx.get(f"{API_BASE}/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if resp.status_code == 401:
        raise LLMError("API key 无效（401），请重新运行 python llm_sentiment.py --set-key")
    resp.raise_for_status()
    return resp.json()


# ── 请求与解析 ──────────────────────────────────────────

@dataclass
class Usage:
    hit: int = 0
    miss: int = 0
    output: int = 0
    requests: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: dict):
        with self.lock:
            hit = usage.get("prompt_cache_hit_tokens", 0)
            self.hit += hit
            self.miss += usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
            self.output += usage.get("completion_tokens", 0)
            self.requests += 1

    @property
    def cost(self) -> float:
        return (self.hit * PRICE_INPUT_HIT + self.miss * PRICE_INPUT_MISS + self.output * PRICE_OUTPUT) / 1e6


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:MAX_CHARS]


def _post_chat(client: httpx.Client, key: str, model: str, texts: list[str]) -> tuple[str, dict]:
    """发送一批评论，返回 (模型输出的文本, usage)。可重试的错误在这里重试"""
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({str(i): t for i, t in enumerate(texts)}, ensure_ascii=False)},
        ],
    }
    delay = 5
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.post(f"{API_BASE}/chat/completions", json=payload,
                               headers={"Authorization": f"Bearer {key}"}, timeout=120)
        except httpx.TransportError as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"    网络出错（{type(e).__name__}），{delay}s 后重试...")
        else:
            if resp.status_code == 401:
                raise LLMError("API key 无效（401），请重新运行 python llm_sentiment.py --set-key")
            if resp.status_code == 402:
                raise LLMError("账户余额不足（402），充值后重新运行即可接着标注")
            if resp.status_code == 400:
                raise LLMError(f"请求格式错误（400）：{resp.text[:200]}")
            if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES:
                print(f"    服务繁忙（{resp.status_code}），{delay}s 后重试...")
            else:
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"], data.get("usage", {})
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")


def parse_labels(content: str, count: int) -> dict[int, str]:
    """解析模型输出，返回 {编号: 「正1」这样的标签}；格式不对的条目丢弃"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}
    labels = data.get("labels", data) if isinstance(data, dict) else {}
    result = {}
    for key, value in labels.items():
        if not str(key).isdigit() or not isinstance(value, str):
            continue
        index, value = int(key), value.strip()
        if 0 <= index < count and len(value) >= 2 and value[0] in SENTIMENT_CODES and value[1] in CATEGORIES:
            result[index] = value[:2]
    return result


def label_texts(client: httpx.Client, key: str, model: str, texts: list[str], usage: Usage) -> dict[int, str]:
    """标注一批文本；漏掉的条目拆成更小的批次再试一次"""
    content, used = _post_chat(client, key, model, texts)
    usage.add(used)
    labels = parse_labels(content, len(texts))
    missing = [i for i in range(len(texts)) if i not in labels]
    if missing and len(texts) > 1:
        half = (len(missing) + 1) // 2
        for start in range(0, len(missing), half):
            chunk = missing[start:start + half]
            content, used = _post_chat(client, key, model, [texts[i] for i in chunk])
            usage.add(used)
            for j, value in parse_labels(content, len(chunk)).items():
                labels[chunk[j]] = value
    return labels


# ── 标注结果缓存 ────────────────────────────────────────

def load_labels(video_dir: str, model: str = DEFAULT_MODEL) -> dict[str, str]:
    """{rpid: 标签}；模型或提示词版本不同的旧结果不用"""
    try:
        with open(os.path.join(video_dir, LABEL_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("model") != model or data.get("prompt_version") != PROMPT_VERSION:
        return {}
    return data.get("labels", {})


def save_labels(video_dir: str, labels: dict[str, str], model: str):
    path = os.path.join(video_dir, LABEL_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"model": model, "prompt_version": PROMPT_VERSION, "labels": labels}, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_label_file(video_dir: str) -> dict | None:
    """周报用：读取标注文件（不校验模型），没有时返回 None"""
    try:
        with open(os.path.join(video_dir, LABEL_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("labels") else None


def decode(label: str) -> tuple[str, str]:
    """「正1」→ ("positive", "夸赞支持")"""
    return SENTIMENT_CODES.get(label[:1], "neutral"), CATEGORIES.get(label[1:2], "其他")


def _sample_order(rpid) -> str:
    return hashlib.md5(str(rpid).encode()).hexdigest()


def select_comments(comments: list[dict], sample: int | None) -> list[dict]:
    """要标注的评论：全量，或按 rpid 哈希排序后的前 sample 条（稳定的随机样本）"""
    usable = [c for c in comments if c.get("rpid") is not None and _clean(c.get("content", ""))]
    if sample is None or len(usable) <= sample:
        return usable
    return sorted(usable, key=lambda c: _sample_order(c["rpid"]))[:sample]


# ── 运行 ────────────────────────────────────────────────

@dataclass
class _VideoJob:
    video_dir: str
    title: str
    labels: dict[str, str]
    groups: dict[str, list[str]]      # 相同文本 → rpid 列表（相同文本只发一次）
    pending: int = 0


def _video_dirs(run_dir: str) -> list[tuple[str, str]]:
    from rebuild import _video_dirs as dirs
    return [(os.path.join(run_dir, entry), title) for _, entry, title in dirs(run_dir)]


def estimate_cost(chars: int, texts: int) -> float:
    """粗略估算：中文约 0.6 token/字，每条输出约 6 tokens，提示词大部分命中缓存"""
    batches = max(1, texts // BATCH_SIZE + 1)
    miss = chars * 0.6 + texts * 4
    hit = batches * 600
    output = texts * 6
    return (hit * PRICE_INPUT_HIT + miss * PRICE_INPUT_MISS + output * PRICE_OUTPUT) / 1e6


def annotate_run(run_dir: str, key: str, *, sample: int | None = DEFAULT_SAMPLE, model: str = DEFAULT_MODEL,
                 max_cost: float = DEFAULT_MAX_COST, workers: int = WORKERS) -> Usage:
    """标注一个运行目录下所有视频的评论，返回用量"""
    jobs, batches = [], []
    for video_dir, title in _video_dirs(run_dir):
        try:
            with open(os.path.join(video_dir, "comments.json"), encoding="utf-8") as f:
                comments = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        labels = load_labels(video_dir, model)
        groups: dict[str, list[str]] = {}
        for comment in select_comments(comments, sample):
            rpid = str(comment["rpid"])
            if rpid not in labels:
                groups.setdefault(_clean(comment.get("content", "")), []).append(rpid)
        # 同样的文字之前已经标过（比如刷屏模板），直接复用
        known = {}
        for comment in comments:
            label = labels.get(str(comment.get("rpid")))
            if label:
                known.setdefault(_clean(comment.get("content", "")), label)
        for text in [t for t in groups if t in known]:
            for rpid in groups.pop(text):
                labels[rpid] = known[text]
        job = _VideoJob(video_dir, title, labels, groups)
        texts = list(groups)
        for start in range(0, len(texts), BATCH_SIZE):
            batches.append((job, texts[start:start + BATCH_SIZE]))
            job.pending += 1
        jobs.append(job)

    texts_total = sum(len(b) for _, b in batches)
    chars_total = sum(len(t) for _, b in batches for t in b)
    name = os.path.basename(os.path.normpath(run_dir))
    if not batches:
        print(f"✅ {name}: 没有需要标注的评论（已全部标注过）")
        for job in jobs:
            if job.labels:
                save_labels(job.video_dir, job.labels, model)
        return Usage()
    print(f"🤖 {name}: {len(jobs)} 个视频，待标注 {texts_total:,} 段文字（{len(batches)} 次请求），"
          f"预计约 ¥{estimate_cost(chars_total, texts_total):.2f}，上限 ¥{max_cost:.2f}")

    usage = Usage()
    done_texts = done_batches = 0
    started = time.time()
    stop_reason = None
    with httpx.Client(limits=httpx.Limits(max_connections=workers)) as client, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        queue = list(batches)
        running = {}

        def submit():
            job, texts = queue.pop(0)
            running[pool.submit(label_texts, client, key, model, texts, usage)] = (job, texts)

        while queue and len(running) < workers:
            submit()
        try:
            while running:
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    job, texts = running.pop(future)
                    try:
                        result = future.result()
                    except LLMError as e:
                        stop_reason = str(e)
                        queue.clear()
                        continue
                    except Exception as e:
                        print(f"    ⚠️ 一批 {len(texts)} 条标注失败（{type(e).__name__}: {e}），下次运行会重试")
                        result = {}
                    for index, label in result.items():
                        for rpid in job.groups[texts[index]]:
                            job.labels[rpid] = label
                    done_texts += len(texts)
                    done_batches += 1
                    job.pending -= 1
                    if job.pending == 0:
                        save_labels(job.video_dir, job.labels, model)
                        print(f"  ✓ {job.title[:30]}  已标注 {len(job.labels):,} 条")
                    if done_batches % 50 == 0:
                        print(f"  … {done_texts:,}/{texts_total:,} 段，已花费约 ¥{usage.cost:.2f}，"
                              f"用时 {(time.time() - started) / 60:.1f} 分钟")
                if usage.cost >= max_cost and queue:
                    stop_reason = f"已达到花费上限 ¥{max_cost:.2f}"
                    queue.clear()
                while queue and len(running) < workers:
                    submit()
        except KeyboardInterrupt:
            stop_reason = "手动中断"
            queue.clear()
            for future in running:
                future.cancel()
            raise
        finally:
            # 无论正常结束还是中断，都把已完成的标注写盘
            for job in jobs:
                if job.labels:
                    save_labels(job.video_dir, job.labels, model)
            print(f"📊 {name}: 请求 {usage.requests} 次，输入 {usage.hit + usage.miss:,} tokens"
                  f"（缓存命中 {usage.hit:,}），输出 {usage.output:,} tokens，约 ¥{usage.cost:.2f}")
            if stop_reason:
                print(f"⏸ 已停止：{stop_reason}。重新运行同样的命令会接着标注。")
    return usage


def main():
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="用 DeepSeek 大模型标注评论情感")
    parser.add_argument("run_dir", nargs="?", help="运行目录，如 bilibili_output/20260928_192613")
    parser.add_argument("--all", nargs="?", const="bilibili_output", default=None, metavar="OUTPUT_ROOT",
                        help="标注输出目录（默认 bilibili_output）下所有期")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help=f"每个视频抽样条数（默认 {DEFAULT_SAMPLE}）")
    parser.add_argument("--full", action="store_true", help="标注全部一级评论（相同文字只发送一次）")
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                        help=f"本次运行的花费上限，单位元（默认 {DEFAULT_MAX_COST:g}）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--workers", type=int, default=WORKERS, help=f"并发请求数（默认 {WORKERS}）")
    parser.add_argument("--no-report", action="store_true", help="标注完不重新生成周报")
    parser.add_argument("--set-key", action="store_true", help="输入并保存 DeepSeek API key（输入时不显示）")
    parser.add_argument("--clipboard", action="store_true",
                        help="配合 --set-key：直接从剪贴板读取 key（终端里粘贴不了时用，先复制 key 再运行）")
    parser.add_argument("--check", action="store_true", help="检查 API key 和账户余额")
    args = parser.parse_args()

    if args.set_key:
        if args.clipboard:
            value = clean_key(read_clipboard())
        else:
            print("提示：粘贴不了时，先复制 key，再运行 python llm_sentiment.py --set-key --clipboard")
            value = clean_key(getpass.getpass("粘贴 DeepSeek API key 后回车（输入不会显示）: "))
        if not value:
            print("没有读到 key，已取消" + ("（剪贴板是空的？）" if args.clipboard else ""))
            return
        if not value.startswith("sk-"):
            # 不打印内容，只提示长度和开头，方便判断是不是复制错了
            print(f"⚠️ 读到的内容不像 DeepSeek key（{len(value)} 个字符，不是以 sk- 开头），未保存")
            sys.exit(1)
        save_key(value)
        print(f"✅ 已保存到 {DEFAULT_KEY_FILE}（{len(value)} 个字符）")
        args.check = True

    key = load_key()
    if not key:
        parser.error(f"没有找到 API key：请运行 python llm_sentiment.py --set-key，或设置环境变量 {KEY_ENV}")
    if args.check:
        try:
            info = check_key(key)
        except (LLMError, httpx.HTTPError) as e:
            print(f"❌ {e}")
            sys.exit(1)
        balances = "，".join(f"{b.get('currency')} {b.get('total_balance')}" for b in info.get("balance_infos", []))
        print(f"✅ key 可用，余额：{balances or '未知'}")
        if not (args.run_dir or args.all):
            return

    if args.all is not None:
        from weekly_report import _run_dirs
        runs = _run_dirs(args.all)
    elif args.run_dir:
        runs = [args.run_dir]
    else:
        parser.error("请指定运行目录，或使用 --all")

    budget = args.max_cost
    sample = None if args.full else args.sample
    for run in runs:
        try:
            usage = annotate_run(run, key, sample=sample, model=args.model, max_cost=budget, workers=args.workers)
        except LLMError as e:
            print(f"❌ {e}")
            sys.exit(1)
        budget -= usage.cost
        if budget <= 0:
            print("已用完本次花费上限，后面的期没有标注")
            break

    if not args.no_report:
        from weekly_report import find_previous_run, generate_weekly_report
        for run in runs:
            path = generate_weekly_report(run, find_previous_run(run))
            print(f"📄 周报已更新: {path}")


if __name__ == "__main__":
    main()
