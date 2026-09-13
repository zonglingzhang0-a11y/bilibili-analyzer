"""
采集进度看板：在浏览器里实时查看一次运行的进度（只读，不影响采集进程）

用法:
    python dashboard.py bilibili_output/20260911_180000
    python dashboard.py bilibili_output/20260911_180000 --port 8765 --log run.log

读取运行目录下的 resume_state.json、comments_recollect.json 和日志文件
（默认依次查找 run.log、recollect.log；日志由 main.py --log-file 写入）。
支持完整采集和 --comments-only 评论重采两种运行。首次拿到期号后请求一次榜单接口，
获取各视频的评论总数用于估算剩余时间。
"""
import argparse
import json
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from main import RECOLLECT_STATE_FILE, RUN_START_MARKER

LOG_CANDIDATES = ("run.log", "recollect.log")
# 全量采集时一级评论约占接口评论总数（含楼中楼）的比例，用于估算当前视频的完成度
MAIN_COMMENT_RATIO = 0.45

RE_VIDEO = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")
RE_TITLE = re.compile(r"^🎬 (.+)$")
RE_PAGE = re.compile(r"已翻 (\d+) 页，采到 (\d+) 条（接口总数含楼中楼 (\d+)），用时 ([\d.]+) 分钟")
RE_EVENT = re.compile(r"✗|限流|拦截|网络出错|⛔|冷却 5 分钟|Traceback|本次完成|评论重采完成|汇总报告|详细结果保存在")
RE_FINISHED = re.compile(r"本次完成|评论重采完成|⛔|详细结果保存在|所有视频已处理完成")
STAGES = [("采集硬参数", "视频参数"), ("下载封面", "封面"), ("采集评论", "评论"), ("采集弹幕", "弹幕"),
          ("截取", "高潮截图"), ("📄 报告", "生成报告"), ("⏱ 冷却", "视频间冷却")]


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _to_epoch(value: str) -> float | None:
    """ISO 时间转时间戳：带时区的（清单里的 UTC 时间）按时区换算，不带的按本地时间"""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def parse_log(lines: list[str]) -> dict:
    """解析日志中最近一次运行的进度"""
    start_index, started_at = 0, None
    for i, line in enumerate(lines):
        if line.startswith(RUN_START_MARKER):
            start_index = i
            started_at = _to_epoch(line[len(RUN_START_MARKER):].strip()[:19])
    lines = lines[start_index:]

    result = {
        "mode": "recollect" if any("评论重采模式" in line for line in lines) else "pipeline",
        "started_at": started_at, "current_title": None, "stage": None, "page": None,
        "failed_titles": set(), "events": [], "finished": False,
    }
    for raw in lines:
        line = raw.strip()
        video = RE_VIDEO.match(line)
        if video and result["mode"] == "recollect":
            if "跳过" not in line:
                result.update(current_title=video.group(3), stage="评论", page=None)
            continue
        title = RE_TITLE.match(line)
        if title:
            result.update(current_title=title.group(1), stage=None, page=None)
            continue
        page = RE_PAGE.search(line)
        if page:
            result["page"] = {"pages": int(page.group(1)), "collected": int(page.group(2)),
                              "total": int(page.group(3)), "minutes": float(page.group(4))}
            continue
        for marker, stage in STAGES:
            if line.startswith(marker):
                result["stage"] = stage
                if stage != "评论":
                    result["page"] = None
        if "✗" in line and result["current_title"]:
            result["failed_titles"].add(result["current_title"])
        if RE_EVENT.search(line):
            result["events"].append(line)
        if RE_FINISHED.search(line):
            result["finished"] = True
    result["events"] = result["events"][-8:]
    return result


def build_status(run_dir: str, log_path: str | None, video_list: list[dict] | None) -> dict:
    """汇总运行状态

    Args:
        video_list: 本期视频 [{"aid", "title", "total"}]（来自榜单接口）；为 None 时只用清单里已有的视频
    """
    manifest = _load_json(os.path.join(run_dir, "resume_state.json"), {})
    recollect = _load_json(os.path.join(run_dir, RECOLLECT_STATE_FILE), {})
    lines = []
    if log_path and os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    log = parse_log(lines)
    started_at = log["started_at"] or (os.path.getctime(log_path) if lines else time.time())

    entries = manifest.get("videos", {})
    if video_list is None:
        video_list = [{"aid": int(aid), "title": e.get("title", ""), "total": 0}
                      for aid, e in entries.items()]

    rows, now = [], time.time()
    done_totals = pending_totals = this_run_totals = done_comments = 0
    for index, video in enumerate(video_list, 1):
        entry = entries.get(str(video["aid"]), {})
        record = recollect.get(str(video["aid"]), {})
        summary = entry.get("stats_summary") or {}
        if log["mode"] == "recollect":
            finished_at = record.get("finished_at")
            done = bool(record)
            count = record.get("count")
        else:
            finished_at = entry.get("completed_at")
            done = entry.get("state") == "completed"
            count = summary.get("comments")
        title = video["title"] or entry.get("title", "")

        if done:
            status = "done"
            done_totals += video["total"]
            done_comments += count or 0
            if (_to_epoch(finished_at) or 0) >= started_at:
                this_run_totals += video["total"]
        elif title == log["current_title"] and not log["finished"]:
            status = "running"
            pending_totals += video["total"]
        else:
            failed = entry.get("state") == "failed" or title in log["failed_titles"]
            status = "failed" if failed else "pending"
            pending_totals += video["total"]
        finished_epoch = _to_epoch(finished_at)
        rows.append({
            "index": index, "aid": video["aid"], "title": title, "status": status,
            "comments": count, "danmaku": summary.get("danmaku") if log["mode"] == "pipeline" else None,
            "total": video["total"],
            "finished_at": datetime.fromtimestamp(finished_epoch).strftime("%m-%d %H:%M")
                           if done and finished_epoch else "",
        })

    running = next((r for r in rows if r["status"] == "running"), None)
    fraction = 0.0
    if running and log["page"] and log["page"]["total"]:
        fraction = min(0.9, log["page"]["collected"] / (MAIN_COMMENT_RATIO * log["page"]["total"]))
    elif running and log["stage"] in ("弹幕", "高潮截图", "生成报告", "视频间冷却"):
        fraction = 0.9
    remaining = pending_totals - (running["total"] * fraction if running else 0)
    progress_in_run = this_run_totals + (running["total"] * fraction if running else 0)
    elapsed = now - started_at
    eta = remaining / (progress_in_run / elapsed) if progress_in_run > 0 and elapsed > 60 else None

    grand_total = done_totals + pending_totals
    return {
        "series": manifest.get("series_number"),
        "mode": log["mode"],
        "run_dir": os.path.basename(os.path.normpath(run_dir)),
        "now": datetime.now().strftime("%H:%M:%S"),
        "video_count": len(rows) or manifest.get("total_videos", 0),
        "done": sum(r["status"] == "done" for r in rows),
        "failed": sum(r["status"] == "failed" for r in rows),
        "done_comments": done_comments,
        "volume_percent": round(done_totals / grand_total * 100, 1) if grand_total else None,
        "current": {**running, "stage": log["stage"], **(log["page"] or {})} if running else None,
        "eta_seconds": None if log["finished"] else eta,
        "log_age": now - os.path.getmtime(log_path) if log_path and os.path.exists(log_path) else None,
        "finished": log["finished"],
        "events": log["events"],
        "rows": rows,
    }


class _VideoListCache:
    """首次拿到期号时请求一次榜单，之后复用"""

    def __init__(self):
        self.series = None
        self.videos = None

    def get(self, series: int | None) -> list[dict] | None:
        if series is None:
            return None
        if series != self.series:
            self.series, self.videos = series, None
            try:
                from ranking import _fetch_series_one
                self.videos = [{"aid": x["aid"], "title": x["title"], "total": x["stat"]["reply"]}
                               for x in _fetch_series_one(series)["list"]]
            except Exception as e:
                print(f"获取第 {series} 期视频列表失败，只显示清单中的视频: {e}")
        return self.videos


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>采集进度</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d2127;--muted:#667080;--card:#fff;--line:#e2e2de;--accent:#0a8fc4;--ok:#1f9d55;--warn:#c47a0a;--bad:#d0342c;--track:#e9e9e5}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e7e7e7;--muted:#98a1ab;--card:#1e2125;--line:#30343a;--accent:#38b3e6;--ok:#3fbf7f;--warn:#e0a040;--bad:#f06a60;--track:#2b2f34}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 18px 48px}
h1{font-size:20px;margin:0}.sub{color:var(--muted);font-size:13px;margin:2px 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.label{color:var(--muted);font-size:12px}.big{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.bar{height:8px;background:var(--track);border-radius:4px;overflow:hidden;margin-top:8px}.bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .6s}
.now h2{font-size:15px;margin:0 0 6px}.now .stats{color:var(--muted);font-variant-numeric:tabular-nums}
.alert{border-color:var(--warn)}.alert .label{color:var(--warn)}
.events{font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;color:var(--muted);max-height:160px;overflow:auto}
table{width:100%;border-collapse:collapse}th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left}
th{font-size:12px;color:var(--muted);font-weight:500;white-space:nowrap}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}.tw{overflow-x:auto;padding:4px 6px}
.s{font-size:12px;white-space:nowrap}.s.done{color:var(--ok)}.s.running{color:var(--accent);font-weight:600}.s.failed{color:var(--bad)}.s.pending{color:var(--muted)}
tr.running td{background:color-mix(in srgb,var(--accent) 8%,transparent)}
.hide{display:none}
</style></head><body><main>
<h1 id="title">采集进度</h1><div class="sub" id="sub">加载中…</div>
<div class="grid">
 <div class="card"><div class="label">已完成视频</div><div class="big" id="done">–</div><div class="bar"><i id="barCount"></i></div></div>
 <div class="card"><div class="label">按评论量计进度</div><div class="big" id="vol">–</div><div class="bar"><i id="barVol"></i></div></div>
 <div class="card"><div class="label">已采一级评论</div><div class="big" id="comments">–</div></div>
 <div class="card"><div class="label">预计剩余</div><div class="big" id="eta">–</div></div>
</div>
<div class="card now" style="margin-bottom:14px"><div class="label">正在处理</div><h2 id="nowTitle">–</h2><div class="stats" id="nowStats"></div><div class="bar"><i id="barNow"></i></div></div>
<div class="card" id="evCard" style="margin-bottom:14px"><div class="label">网络重试 / 限流 / 失败 / 完成</div><div class="events" id="events">暂无</div></div>
<div class="card tw"><table><thead><tr><th>#</th><th>视频</th><th>状态</th><th style="text-align:right">一级评论</th><th style="text-align:right" class="dm">弹幕</th><th style="text-align:right">评论总数(含楼中楼)</th><th style="text-align:right">完成时间</th></tr></thead><tbody id="rows"></tbody></table></div>
</main><script>
const $=id=>document.getElementById(id);
const fmt=n=>n==null?'–':n.toLocaleString('zh-CN');
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const dur=s=>{if(s==null)return'计算中';const h=Math.floor(s/3600),m=Math.round((s%3600)/60);return h?`约 ${h} 小时 ${m} 分`:`约 ${m} 分钟`};
const label={done:'✓ 完成',running:'● 进行中',pending:'等待',failed:'✗ 失败'};
let scrolled=false;
async function tick(){
 try{
  const d=await (await fetch('/status',{cache:'no-store'})).json();
  const kind=d.mode==='recollect'?'评论重采':'采集';
  const name=d.series?`第 ${d.series} 期${kind}进度`:`${kind}进度`;
  $('title').textContent=name;document.title=name;
  const stale=d.log_age!=null&&d.log_age>300&&!d.finished;
  $('sub').textContent=`${d.run_dir} · 每 5 秒刷新 · ${d.now}`+(d.log_age!=null?` · 日志 ${Math.round(d.log_age)} 秒前更新`:' · 尚未找到日志')+(stale?' · ⚠️ 日志较久未更新（可能在冷却，或进程已停止）':'');
  $('done').textContent=`${d.done} / ${d.video_count}`+(d.failed?`（失败 ${d.failed}）`:'');
  $('barCount').style.width=(d.video_count?d.done/d.video_count*100:0)+'%';
  $('vol').textContent=d.volume_percent==null?'–':d.volume_percent+'%';
  $('barVol').style.width=(d.volume_percent||0)+'%';
  $('comments').textContent=fmt(d.done_comments)+' 条';
  $('eta').textContent=d.finished?'已结束':dur(d.eta_seconds);
  const c=d.current;
  $('nowTitle').textContent=d.finished?'运行已结束':(c?`#${c.index} ${c.title}`:'等待下一个视频');
  let stats='';
  if(c){stats=(c.stage?`阶段：${c.stage}`:'');if(c.pages)stats+=` · 已翻 ${c.pages} 页 · 采到 ${fmt(c.collected)} 条 · 评论总数 ${fmt(c.total)} · 用时 ${c.minutes} 分钟`;else if(c.stage==='评论')stats+=' · 翻满 50 页后显示进度'}
  $('nowStats').textContent=stats;
  $('barNow').style.width=c&&c.pages&&c.total?Math.min(100,c.collected/(0.45*c.total)*100)+'%':'0%';
  $('events').textContent=d.events.length?d.events.join('\n'):'暂无';
  $('evCard').classList.toggle('alert',d.events.some(e=>/✗|限流|拦截|网络出错|⛔|冷却 5/.test(e)));
  const showDm=d.mode!=='recollect';document.querySelectorAll('.dm').forEach(e=>e.classList.toggle('hide',!showDm));
  $('rows').innerHTML=d.rows.map(r=>`<tr class="${r.status}"><td class="n">${r.index}</td><td>${esc(r.title)}</td><td><span class="s ${r.status}">${label[r.status]}</span></td><td class="n">${fmt(r.comments)}</td><td class="n dm${showDm?'':' hide'}">${fmt(r.danmaku)}</td><td class="n">${fmt(r.total)}</td><td class="n">${r.finished_at}</td></tr>`).join('');
  const run=document.querySelector('tr.running');if(run&&!scrolled){run.scrollIntoView({block:'center'});scrolled=true}
 }catch(e){$('sub').textContent='看板服务连接失败：'+e}
}
tick();setInterval(tick,5000);
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description="采集进度看板")
    parser.add_argument("run_dir", help="运行目录，如 bilibili_output/20260911_180000（可以尚未创建）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", default=None, help="日志文件（默认在运行目录下查找 run.log、recollect.log）")
    args = parser.parse_args()

    cache = _VideoListCache()

    def resolve_log():
        if args.log:
            return args.log
        for name in LOG_CANDIDATES:
            path = os.path.join(args.run_dir, name)
            if os.path.exists(path):
                return path
        return None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/status"):
                series = _load_json(os.path.join(args.run_dir, "resume_state.json"), {}).get("series_number")
                status = build_status(args.run_dir, resolve_log(), cache.get(series))
                body, ctype = json.dumps(status, ensure_ascii=False).encode("utf-8"), "application/json"
            else:
                body, ctype = PAGE.encode("utf-8"), "text/html"
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"进度看板: http://127.0.0.1:{args.port}  （{args.run_dir}）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
