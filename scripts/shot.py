#!/usr/bin/env python3
"""无头浏览器给页面拍一张，同时用 DOM 探针程序化判定「渲染成功 / 白屏 / 布局错位」。

为什么要有这个：交付前总得看一眼页面，但人肉截图 → 看图 → 猜问题，
一次判断要花好几轮。本脚本把「渲染引擎有没有加载失败、canvas 有没有被挤下去、
页面有没有多出滚动条、模型区域是不是空的」全部变成一条结论。

用法：
  python shot.py --html 三视图还原-立体图.html --out _ai_work/shot.png
  python shot.py --html x.html --out x.png --size 1280x900 --wait 5000 --views "iso,left,right"

退出码：0 = 通过（可交付）；1 = 有问题（stderr 里写明是哪种）。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

PROBE = r"""
<script>
window.__p = {err: [], t0: performance.now()};
window.addEventListener('error', function(e){ window.__p.err.push(String(e.message || e.type)); }, true);
window.addEventListener('unhandledrejection', function(e){ window.__p.err.push('promise: ' + String(e.reason)); });
function __probe(){
  var c = document.querySelector('canvas');
  var r = c ? c.getBoundingClientRect() : null;
  var ld = document.getElementById('loading');
  var dk = document.getElementById('dock');
  var hd = document.getElementById('title');
  var out = {
    ms: Math.round(performance.now() - window.__p.t0),
    err: window.__p.err,
    canvas: r ? [Math.round(r.width), Math.round(r.height), Math.round(r.top), Math.round(r.left)] : null,
    win: [innerWidth, innerHeight],
    scroll: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    loading: ld ? {vis: getComputedStyle(ld).display !== 'none',
                   txt: (ld.textContent || '').replace(/\s+/g, ' ').slice(0, 90)} : null,
    dock: dk ? (function(){var q = dk.getBoundingClientRect(); return [Math.round(q.top), Math.round(q.height)];})() : null,
    head: hd ? Math.round(hd.getBoundingClientRect().height) : null,
    views: Array.prototype.map.call(document.querySelectorAll('#dock [data-view]'), function(b){ return b.dataset.view; }),
    px: (function(){ try { return null; } catch(e){ return 'x'; } })()
  };
  document.title = 'PROBE:' + JSON.stringify(out);
  var d = document.getElementById('__probe') || document.createElement('div');
  d.id = '__probe'; d.textContent = 'PROBE:' + JSON.stringify(out);
  document.body.appendChild(d);
}
/* 轮询而不是死等：canvas 一出现、遮罩一隐藏就立即取样（成功时更快），
 * 一直没起来才等到 __WAIT__ 满（CDN 慢的时候要给足时间，否则会误判成白屏）。 */
(function __poll(){
  var dl = performance.now() + __WAIT__;
  function tick(){
    var c = document.querySelector('canvas');
    var ld = document.getElementById('loading');
    var done = c && (!ld || getComputedStyle(ld).display === 'none');
    if (done || performance.now() >= dl) { __probe(); return; }
    setTimeout(tick, 150);
  }
  tick();
})();
</script>
"""


def find_browser(explicit=None):
    if explicit:
        return explicit
    for c in CANDIDATES:
        if Path(c).is_file():
            return c
    for name in ("chrome", "google-chrome", "msedge", "chromium"):
        w = shutil.which(name)
        if w:
            return w
    return None


def nonbg_ratio(png_path, box=None):
    """截图里「非背景色」像素占比：页面白/浅灰底之外的东西都算。"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    im = Image.open(png_path).convert("RGB")
    if box:
        im = im.crop(box)
    a = np.asarray(im).astype(int)
    # 页面底色是 --bg（近白）与白色面板；用「偏暗或明显有色」判定内容
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    content = ((mx < 235) | ((mx - mn) > 18))
    return float(content.mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--out", default="_ai_work/shot.png")
    ap.add_argument("--size", default="1280x880")
    ap.add_argument("--wait", type=int, default=9000,
                    help="最多等多久让 canvas 出现并隐藏遮罩（毫秒，就绪即提前返回；"
                         "与模板的 9 秒 CDN 超时提示对齐）")
    ap.add_argument("--budget", type=int, default=9000, help="虚拟时间预算（毫秒）")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--keep-html", action="store_true", help="保留注入后的临时 HTML")
    args = ap.parse_args()

    src = Path(args.html).resolve()
    if not src.is_file():
        sys.exit(f"[错误] 找不到 {src}")
    exe = find_browser(args.browser)
    if not exe:
        sys.exit("[错误] 找不到 Chrome/Edge/Chromium，用 --browser 指定可执行文件路径")

    html = src.read_text(encoding="utf-8")
    html = html.replace("</body>", PROBE.replace("__WAIT__", str(args.wait)) + "</body>")
    tmp = Path(tempfile.gettempdir()) / f"_shot_{os.getpid()}.html"
    tmp.write_text(html, encoding="utf-8")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_abs = out.resolve()

    w, h = (int(v) for v in args.size.lower().split("x"))
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           f"--window-size={w},{h}", f"--virtual-time-budget={args.budget}",
           f"--screenshot={out_abs}", "--dump-dom", tmp.as_uri()]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    dom = r.stdout or ""
    if not args.keep_html:
        tmp.unlink(missing_ok=True)

    # 用 raw_decode：探针的 JSON 里嵌着多个 {}, 正则难以切准
    cands = [mm.start() for mm in re.finditer(r"PROBE:\s*\{", dom)]
    info = None
    for pos in reversed(cands):
        brace = dom.index("{", pos)
        try:
            info, _end = json.JSONDecoder().raw_decode(dom[brace:])
            break
        except json.JSONDecodeError:
            continue
    if info is None:
        tail = ((r.stderr or "") + "\n" + dom[-400:])[-800:]
        sys.exit(f"[失败] 探针没回话：页面可能在模块加载阶段就挂了，或浏览器启动失败。\n{tail}")

    problems, notes = [], []
    if not out.exists():
        problems.append("截图文件没生成")
    if info["canvas"] is None:
        problems.append("页面里没有 canvas —— 渲染引擎没起来（多半是 three.js CDN 没加载到）")
    else:
        cw, chh, ctop, cleft = info["canvas"]
        if chh < h * 0.6:
            problems.append(f"canvas 高度只有 {chh}px（窗口 {h}px）—— 有元素把画布挤下去了")
        if ctop > 20:
            problems.append(f"canvas 顶部偏离 {ctop}px —— 上方有块级元素占了位置（检查 #hint 之类是否 position:fixed）")
        notes.append(f"canvas {cw}x{chh} @top={ctop}")
    if info.get("scroll") and info["scroll"][1] > h + 2:
        problems.append(f"页面出现竖直滚动条（内容高 {info['scroll'][1]} > 窗口 {h}）—— 有元素超界")
    if info.get("loading") and info["loading"]["vis"]:
        txt = info["loading"]["txt"]
        if "失败" in txt or "错误" in txt:
            problems.append(f"页面停在错误提示：「{txt}」")
        else:
            notes.append(f"loading 遮罩仍可见：「{txt}」（可能只是还没渲染完）")
    if info.get("err"):
        problems.append("运行时错误：" + " | ".join(str(e) for e in info["err"][:3]))

    ratio = nonbg_ratio(out_abs) if out.exists() else None
    if ratio is not None:
        notes.append(f"非背景像素占比 {ratio:.3%}")
        if ratio < 0.004:
            problems.append("截图几乎是空白 —— 模型没画出来")

    print(f"[信息] {len(info.get('views') or [])} 个视角按钮：{', '.join(info.get('views') or [])}")
    print(f"[信息] 探针耗时 {info['ms']} ms · " + " · ".join(notes))
    if problems:
        for p in problems:
            print(f"[问题] {p}", file=sys.stderr)
        sys.exit(1)
    print(f"[通过] 页面渲染正常 · 截图 {out_abs}")


if __name__ == "__main__":
    main()
