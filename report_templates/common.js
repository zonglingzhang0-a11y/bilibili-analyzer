/* 周报与总览页共用的格式化函数、DOM 构造和折线图（由 weekly_report.py 在生成时嵌入） */
const $ = id => document.getElementById(id);
const fmt = n => n == null ? "–" : Math.round(n).toLocaleString("zh-CN");
const pct = (x, d = 1) => x == null ? "–" : (x * 100).toFixed(d) + "%";
const wan = n => n >= 1e8 ? (n / 1e8).toFixed(2) + " 亿" : n >= 10000 ? (n / 10000).toFixed(n >= 1e6 ? 0 : 1) + " 万" : fmt(n);
const hours = h => h == null ? "–" : h < 10 ? h.toFixed(1) + " 小时" : Math.round(h) + " 小时";

// 创建元素：字符串一律作为文本插入（数据来自评论等不可信内容）
function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
const svg = (tag, attrs) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
};

function sentiBar(s, height) {
  const total = (s.positive + s.neutral + s.negative) || 1;
  const bar = h("div", { class: "senti", style: height ? `height:${height}px` : "",
    title: `正面 ${pct(s.positive / total)} · 中性 ${pct(s.neutral / total)} · 负面 ${pct(s.negative / total)}` });
  [["p", s.positive], ["n", s.neutral], ["x", s.negative]].forEach(([c, v]) => {
    if (v > 0) bar.append(h("span", { class: c, style: `flex:${v}` }));
  });
  return bar;
}
function quote(q) {
  if (!q || !q.text) return null;
  const meta = [q.like != null ? `👍 ${fmt(q.like)}` : null, q.replies ? `💬 ${fmt(q.replies)} 条回复` : null].filter(Boolean).join(" · ");
  return h("blockquote", {}, "「", q.text, "」", meta ? h("div", { class: "meta" }, meta) : null);
}

/* ── 通用折线图：十字准线 + 悬停提示，可多条线 ── */
function lineChart(host, opt) {
  const { series, xLabel, tooltipExtra, markers = [], yFormat = fmt, height = 200, area = true } = opt;
  const n = series[0].values.length;
  if (!n) { host.append(h("div", { class: "note" }, "没有数据")); return; }
  const tip = h("div", { class: "tip", style: "display:none" });
  host.append(tip);
  let svgEl;
  function draw() {
    if (svgEl) svgEl.remove();
    const W = Math.max(host.clientWidth, 280), H = height;
    const m = { l: 44, r: series.length > 1 ? 58 : 12, t: 12, b: 26 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const maxY = Math.max(1e-9, ...series.flatMap(s => s.values));
    const step = niceStep(maxY / 4);
    const top = Math.ceil(maxY / step) * step;
    const x = i => m.l + (n === 1 ? iw / 2 : i / (n - 1) * iw);
    const y = v => m.t + ih - v / top * ih;
    svgEl = svg("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img" });
    const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
    for (let v = 0; v <= top + 1e-9; v += step) {
      grid.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
      const t = svg("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = yFormat(v); axis.append(t);
    }
    const ticks = Math.min(7, n);
    for (let k = 0; k < ticks; k++) {
      const i = Math.round(k / Math.max(1, ticks - 1) * (n - 1));
      const t = svg("text", { x: x(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === ticks - 1 ? "end" : "middle" });
      t.textContent = xLabel(i); axis.append(t);
    }
    svgEl.append(grid, axis, svg("line", { class: "base", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }));
    series.forEach(s => {
      const pts = s.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
      if (area && series.length === 1)
        svgEl.append(svg("path", { d: `M${x(0)},${y(0)}L${pts.join("L")}L${x(n - 1)},${y(0)}Z`, fill: s.color, opacity: 0.12 }));
      svgEl.append(svg("path", { d: "M" + pts.join("L"), fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    });
    if (series.length > 1) {
      const ends = series.map(s => ({ s, y: y(s.values[n - 1]) })).sort((p, q) => p.y - q.y);
      for (let k = 1; k < ends.length; k++) ends[k].y = Math.max(ends[k].y, ends[k - 1].y + 13);
      ends.forEach(({ s, y: ly }) => {
        const t = svg("text", { x: W - m.r + 4, y: ly + 4, fill: "var(--ink-2)", "font-size": 11 });
        t.textContent = s.name; svgEl.append(t);
      });
    }
    markers.forEach(mk => {
      const v = series[0].values[mk.i] || 0;
      svgEl.append(svg("circle", { cx: x(mk.i), cy: y(v), r: 5, fill: series[0].color, stroke: "var(--surface)", "stroke-width": 2 }));
      if (mk.label) { const t = svg("text", { x: x(mk.i), y: y(v) - 10, "text-anchor": "middle", fill: "var(--ink-2)", "font-size": 11 }); t.textContent = mk.label; svgEl.append(t); }
    });
    const cross = svg("line", { y1: m.t, y2: m.t + ih, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
    const dots = series.map(s => svg("circle", { r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }));
    svgEl.append(cross, ...dots);
    const hit = svg("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" });
    svgEl.append(hit);
    const move = ev => {
      const r = svgEl.getBoundingClientRect();
      const px = (ev.clientX - r.left) * W / r.width;
      const i = Math.max(0, Math.min(n - 1, Math.round((px - m.l) / iw * (n - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dots.forEach((d, k) => { d.setAttribute("cx", x(i)); d.setAttribute("cy", y(series[k].values[i])); d.setAttribute("visibility", "visible"); });
      tip.replaceChildren(
        ...series.map(s => h("div", { class: "row" },
          series.length > 1 ? h("span", { class: "key", style: `background:${s.color}` }) : null,
          h("span", { class: "v" }, yFormat(s.values[i])), h("span", { class: "k" }, s.name))),
        h("div", { class: "k" }, xLabel(i)),
        tooltipExtra ? tooltipExtra(i) : null);
      tip.style.display = "block";
      const tx = x(i) / W * r.width, tw = tip.offsetWidth;
      tip.style.left = Math.min(Math.max(0, tx + 12), r.width - tw) + "px";
      tip.style.top = "0px";
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); });
    host.prepend(svgEl);
  }
  draw();
  new ResizeObserver(() => draw()).observe(host);
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
