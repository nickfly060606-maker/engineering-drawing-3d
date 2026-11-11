#!/usr/bin/env python3
"""一条命令跑完模式 B 的「校验 → 出页面 → 自检」。

以前这一步是 5 个独立命令、5 次往返，每次都要我读完输出再决定下一步。
现在一次跑完，按顺序打印 PASS/FAIL，任意一步挂了就地中止并指出该改哪儿。

用法（最小）：
  python pipeline.py --model _ai_work/model.js --html 三视图还原-立体图.html \\
      --title "三视图还原 · 立体图" --subtitle "支架 · 斜面 + 凸台" --tags "长:深:高≈276:257:238"

用法（带体素交叉校验）：
  python pipeline.py --model _ai_work/model.js --part _ai_work/part.py \\
      --image "<截图>" --html out.html --expect 8185

步骤：
  1 build_viewer   注入模型 + 模块语法检查（重名常量当场拦下）
  2 check_mesh     体积 / 缺面 / 绕向 / 退化面
  3 project_mesh   自动标定 + 反投影叠回原图（需要 --image）
  4 raycast_preview 体素机位预览（需要 --part）
  5 shot           无头浏览器实拍 + 布局探针
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(title, cmd, critical=True, tail=12):
    print(f"\n=== {title} ===")
    print("  $ " + " ".join(str(c) for c in cmd[1:]))
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for line in out.splitlines()[-tail:]:
        print("  " + line)
    ok = r.returncode == 0
    print(f"  → {'PASS' if ok else 'FAIL'}")
    return ok if critical else True, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--html", required=True)
    ap.add_argument("--image", default="", help="原三视图截图；给了就自动跑反投影叠加")
    ap.add_argument("--part", default="", help="体素模型 part.py（含 GRID/CENTER/RADIUS/inside）")
    ap.add_argument("--expect", type=float, default=0.0, help="解析体积，供 check_mesh 比对")
    ap.add_argument("--title", default="三视图还原 · 立体图")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--views", default="iso,front,top,left,under")
    ap.add_argument("--outdir", default="_ai_work/verify")
    ap.add_argument("--no-spin", action="store_true")
    ap.add_argument("--no-shot", action="store_true", help="跳过浏览器实拍")
    ap.add_argument("--boxes", default="",
                    help="非标准布局的视图框 name=x0,y0,x1,y1;...，透传给 project_mesh")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    od = Path(args.outdir)
    od.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    results = []

    _, ok = run("1/5 生成页面（含模块语法检查）", [
        py, HERE / "build_viewer.py", "--model", args.model, "--out", args.html,
        "--title", args.title, "--subtitle", args.subtitle, "--tags", args.tags,
        "--views", args.views] + (["--no-spin"] if args.no_spin else [])
        + (["--force"] if args.force else []))
    results.append(("生成页面", ok))
    if not ok:
        print("\n[中止] 页面没生成，后面几步没意义。")
        return 1

    cmd = ["node", HERE / "check_mesh.js", "--model", args.model]
    if args.expect:
        cmd += ["--expect", str(args.expect)]
    _, ok = run("2/5 网格自检", cmd)
    results.append(("网格自检", ok))

    if args.image:
        _, ok = run("3/5 反投影叠回原图（自动标定）", [
            py, HERE / "project_mesh.py", "--model", args.model, "--image", args.image,
            "--auto-calib", "--out", od / "overlay.png"]
            + (["--boxes", args.boxes] if args.boxes else []), critical=False)
        results.append(("反投影", ok))
    if args.part:
        _, ok = run("4/5 体素机位预览", [
            py, HERE / "raycast_preview.py", "--model", args.part,
            "--out", od / "views.png", "--views", "iso,under,front,left"],
            critical=False)
        results.append(("机位预览", ok))
    if not args.no_shot:
        _, ok = run("5/5 浏览器实拍 + 布局探针", [
            py, HERE / "shot.py", "--html", args.html,
            "--out", od / "shot.png"], critical=False)
        results.append(("页面实拍", ok))

    print("\n" + "=" * 46)
    for name, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}")
    bad = [n for n, o in results if not o]
    print("=" * 46)
    if bad:
        print(f"[未通过] {', '.join(bad)} —— 看上面的输出定位；"
              f"几何不对就改模型参数，不要调渲染去凑")
        return 1
    print(f"[完成] {args.html}")
    print(f"       对照图 {od / 'overlay.png'} · 实拍 {od / 'shot.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
