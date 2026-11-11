#!/usr/bin/env python3
"""把 model.js 几何片段注入通用渲染模板，产出单文件可交互 HTML。

用法：
  python build_viewer.py --model _ai_work/model.js --out 三视图还原-立体图.html \
      --title "三视图还原 · 立体图" --subtitle "圆柱体 · 斜切 + 台阶" \
      --tags "直径:高≈1:2.09|切口朝下 · 点「仰视」" --views "iso,front,top,left,under"

模板已内置渲染框架；本脚本只做注入、转义、占位符校验和覆盖保护。
"""
import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "assets" / "viewer_template.html"
KIT = ROOT / "assets" / "model_kit.js"
VENDOR = ROOT / "assets" / "vendor"
VENDOR_FILES = (("vw-three", "three.module.min.js"),
                ("vw-orbit", "OrbitControls.js"),
                ("vw-bgu", "BufferGeometryUtils.js"))

LABELS = {
    "iso": "轴测", "front": "主视", "back": "后视", "top": "俯视",
    "left": "左视", "right": "右视", "under": "仰视",
}
DEFAULT_VIEWS = "iso,front,top,left,under"


def parse_views(spec):
    names = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [n for n in names if n not in LABELS]
    if unknown:
        sys.exit(f"[错误] 未知视角 {unknown}；可用：{', '.join(LABELS)}")
    if not names:
        sys.exit("[错误] --views 不能为空")
    return names


def build_tags(spec):
    if not spec.strip():
        return ""
    parts = [p.strip() for p in spec.split("|") if p.strip()]
    out = []
    for i, t in enumerate(parts):
        warm = ' style="background:#fdf0e6;color:#8a5a2b"' if i > 0 else ""
        out.append(f'  <span class="tag"{warm}>{html.escape(t)}</span>')
    return "\n".join(out)


def check_module(html_text, quiet=False):
    """把拼好的 <script type="module"> 抽出来交给 node 做一次语法检查。

    这一步专门拦「模型片段里 const X 与模板顶层重名」这类致命错：
    重名是 SyntaxError，整个 module 直接不执行，页面只剩「渲染引擎加载失败」，
    而文件本身完全合法 —— 只有真跑一遍解析器才看得出来（2026-11-10 真踩过）。
    """
    m = re.search(r'<script type="module">(.*?)</script>', html_text, re.S)
    if not m:
        return '模板里没找到 <script type="module">'
    if not shutil.which("node"):
        if not quiet:
            print("[警告] 没装 node，跳过模块语法检查（重名常量这类错误只能到浏览器里才发现）")
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as fh:
        fh.write(m.group(1))
        tmp = fh.name
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        if r.returncode == 0:
            return None
        err = (r.stderr or "").strip()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    dup = re.search(r"Identifier '([\w$]+)' has already been declared", err)
    if dup:
        name = dup.group(1)
        tpl_names = set(re.findall(r"^\s*(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
                                   TEMPLATE.read_text(encoding="utf-8"), re.M))
        owner = "模板顶层" if name in tpl_names else "片段内部"
        return (f"模块语法错误：标识符 {name!r} 重复声明（与{owner}重名）。\n"
                f"       修法：把整个模型包进 IIFE —— `const MODEL = (function(){{ ... return {{...}}; }})();`\n"
                f"       并给内部变量加前缀（DP/HT/K 之类），不要用 D/R/N 这种单字母名。\n\n{err}")
    return f"模块语法错误（浏览器里会直接白屏）：\n{err}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="model.js 路径（必须定义 const MODEL）")
    ap.add_argument("--out", required=True, help="输出 HTML 路径")
    ap.add_argument("--title", default="三视图还原 · 立体图")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--tags", default="", help="用 | 分隔；第一个为蓝标签，其余为暖色提示")
    ap.add_argument("--views", default=DEFAULT_VIEWS, help="逗号分隔的视角名")
    ap.add_argument("--no-spin", action="store_true", help="默认关闭自动旋转")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出文件")
    ap.add_argument("--no-check", action="store_true", help="跳过模块语法检查")
    ap.add_argument("--no-kit", action="store_true", help="不注入几何工具包 __KIT")
    args = ap.parse_args()

    model_path = Path(args.model)
    out_path = Path(args.out)
    if not model_path.is_file():
        sys.exit(f"[错误] 找不到模型片段：{model_path}")
    if not TEMPLATE.is_file():
        sys.exit(f"[错误] 找不到模板：{TEMPLATE}")
    if out_path.exists() and not args.force:
        sys.exit(f"[错误] 输出已存在：{out_path}\n"
                 f"       换一个文件名，或确认要覆盖时加 --force")

    snippet = model_path.read_text(encoding="utf-8")
    if not re.search(r"\b(?:const|var|let)\s+MODEL\s*=", snippet):
        sys.exit("[错误] model.js 必须定义 const MODEL = { ... }，见 assets/model_example.js")
    if "EdgesGeometry" in snippet:
        print("[警告] 片段里出现 EdgesGeometry：未合并顶点时会画出三角网格，请改用手绘棱线")

    names = parse_views(args.views)
    buttons = []
    for i, n in enumerate(names):
        on = ' class="on"' if i == 0 else ""
        buttons.append(f'  <button data-view="{n}"{on}>{LABELS[n]}</button>')

    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("__TITLE__", html.escape(args.title))
    text = text.replace("__SUBTITLE__", html.escape(args.subtitle))
    text = text.replace("__TAGS_HTML__", build_tags(args.tags))
    text = text.replace("__VIEW_BUTTONS__", "\n".join(buttons))
    text = text.replace("__SPIN_ON__", "" if args.no_spin else "on")

    # 在注入业务片段之前检查残留，避免把用户片段里的常量名误判成占位符
    left = [t for t in re.findall(r"__[A-Z][A-Z0-9_]*__", text)
            if t != "__MODEL_SNIPPET__"]
    if left:
        sys.exit(f"[错误] 模板占位符未全部替换：{sorted(set(left))}")

    # 几何工具包随页面一起注入，模型里 `const {hull, ...} = __KIT;` 就能用。
    # 放在模型片段之前、同一个 module 作用域里，省掉每次复制 300 行样板。
    inject = snippet
    already = re.search(r"\b(?:const|let|var)\s+__KIT\s*=", snippet)
    if not args.no_kit and KIT.is_file() and not already:
        kit_src = KIT.read_text(encoding="utf-8")
        inject = kit_src + "\n" + snippet
        ops = set()
        for grp in re.findall(r"\{([^{}]*)\}\s*=\s*__KIT\b", snippet):
            ops |= {t.strip() for t in grp.split(",") if t.strip()}
        ops |= set(re.findall(r"__KIT\s*\.\s*(\w+)", snippet))
        print(f"[工具包] 已注入 model_kit.js（{len(kit_src.splitlines())} 行）"
              + (f" · 用到 {', '.join(sorted(ops))}" if ops else " · 片段里没取用工具包"))

    # three.js 引擎内联：三个源写成 text/plain 脚本，页面运行时打成 Blob URL 动态 import。
    # 不走 CDN —— 2026-11-10 实测 fastly.jsdelivr 12 秒只下 9.6 KB，页面直接白屏；
    # 内联后交付文件离线可用、首屏零等待。
    sources, total = [], 0
    for fid, fname in VENDOR_FILES:
        f = VENDOR / fname
        if not f.is_file():
            sys.exit(f"[错误] 缺少内联引擎源：{f}\n"
                     f"       重新下载：curl -sL -o {f} "
                     f"https://unpkg.com/three@0.161.0/{fname}")
        code = f.read_text(encoding="utf-8").replace("</script", "<\\/script")
        total += len(code)
        sources.append(f'<script type="text/plain" id="{fid}">{code}</script>')
    text = text.replace("<!--THREE_SOURCES-->", "\n".join(sources))
    print(f"[引擎] 已内联 three.js {total / 1024:.0f} KB · 离线可用，不再请求 CDN")

    text = text.replace("/*__MODEL_SNIPPET__*/", inject)

    if not args.no_check:
        err = check_module(text)
        if err:
            sys.exit(f"[错误] {err}\n       未写出文件（避免交付一个白屏页面）")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"[完成] {out_path.resolve()}")
    print(f"       视角：{', '.join(LABELS[n] for n in names)} · "
          f"自动旋转：{'关' if args.no_spin else '开'}")
    print(f"       大小：{out_path.stat().st_size / 1024:.1f} KB")
    print("       模块语法检查通过 · 引擎已内联，离线/断网都能打开")


if __name__ == "__main__":
    main()
