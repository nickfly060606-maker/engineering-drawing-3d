#!/usr/bin/env python3
"""三视图 → 三维模型 → 可旋转 HTML，一键完成。

原理：视觉外壳（visual hull）。一个体素属于形体，当且仅当它在三个投影方向
都落在实心区域内：
    V(x,y,z) = F(x,y) AND T(x,z) AND L(z,y)
因此不必人工反推形体、不必手写几何，三张二值掩码就能唯一确定「最大一致形体」。
投影方向的手性（第一角/第三角）由 4 种翻转组合自动择优，取三视图吻合度最高者。

一键用法：
  python auto_reconstruct.py --image <截图> --out _ai_work/auto \
      --html "三视图还原-第3题.html" \
      --title "三视图还原 · 立体图" --subtitle "圆柱切割体" \
      --tags "自动重建 · 视觉外壳" --force

分步用法（已经跑过 inspect_views.py 时）：
  python auto_reconstruct.py --inspect _ai_work/inspect --out _ai_work/auto --html ...

产出：model.js（MODEL 契约）、volume.npz、compare.png（反投影对照）、
      report.json（每个视图的吻合度、体素数、面片数、耗时）、以及可选 HTML。

局限：视觉外壳是「满足三视图的最大形体」，虚线表示的内部孔/槽会被填满。
遇到内部空腔题，改用 SKILL.md 的精确模式（手写 volume_model.py + model.js）。
"""
import argparse
import json
import subprocess
import sys
import time
from collections import deque
from itertools import product
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
INSPECT = HERE / "inspect_views.py"
BUILD = HERE / "build_viewer.py"


def fill_holes(m):
    """填掉被实心包围的细缝：视图内部的实线/中心线会在掩码里留白缝，
    直接拿去求视觉外壳会在模型里切出薄层空洞，也会压低吻合度。
    与边界连通的白色凹口保持不填。
    """
    h, w = m.shape
    inv = ~m
    reach = np.zeros_like(m, bool)
    dq = deque()

    def seed(y, x):
        if inv[y, x] and not reach[y, x]:
            reach[y, x] = True
            dq.append((y, x))

    for x in range(w):
        seed(0, x)
        seed(h - 1, x)
    for y in range(h):
        seed(y, 0)
        seed(y, w - 1)
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and inv[ny, nx] and not reach[ny, nx]:
                reach[ny, nx] = True
                dq.append((ny, nx))
    return m | (inv & ~reach)


def close_gaps(m, n=2):
    """形态学闭运算：填掉贯通白缝（视图内部的实线棱，本不是空隙）。

    贯通缝两端连着图外白区，fill_holes 抓不到，必须靠膨胀+腐蚀。
    """
    d = m.copy()
    for _ in range(n):
        p = d.copy()
        d[1:, :] |= p[:-1, :]
        d[:-1, :] |= p[1:, :]
        d[:, 1:] |= p[:, :-1]
        d[:, :-1] |= p[:, 1:]
    e = d.copy()
    for _ in range(n):
        p = e.copy()
        e[1:, :] &= p[:-1, :]
        e[:-1, :] &= p[1:, :]
        e[:, 1:] &= p[:, :-1]
        e[:, :-1] &= p[:, 1:]
    return e | m


def as_xy(mask, nx, ny):
    """图像掩码 (行=y向下, 列=x) → 数组 [x, y向上]，重采样到 (nx, ny)。

    注意 PIL 把 shape (H,W) 当作 高×宽；这里 a 的行是 x、列是 y，
    因此 resize 的尺寸必须写 (ny, nx)，且返回后不能再转置。
    写反会让三张掩码同时旋转 90°——三者仍自洽、吻合度依旧很高，
    但重建出的形体是错的，属于最隐蔽的一类错误。
    """
    a = np.flipud(np.asarray(mask, bool)).T          # (w, h) = [x, y_up]
    im = Image.fromarray((a * 255).astype(np.uint8))
    im = im.resize((ny, nx), Image.NEAREST)          # (宽, 高) → shape (nx, ny)
    a = np.asarray(im).astype(bool)
    return fill_holes(close_gaps(a))                 # [x, y]，填掉内部棱线造成的白缝


def normalize_grad(V):
    """体素角点的平滑外法线：对 8 邻域实心体素求质心偏移的负值。

    比逐面片平坦法线更圆润，圆柱面不会出现明显方块感；无额外依赖。
    """
    nx, ny, nz = V.shape
    S = np.pad(V.astype(np.float32), 1)
    G = np.zeros((nx + 1, ny + 1, nz + 1, 3), np.float32)
    for di, dj, dk in product((0, 1), repeat=3):
        block = S[di:di + nx + 1, dj:dj + ny + 1, dk:dk + nz + 1]
        G[..., 0] -= block * (di - 0.5)
        G[..., 1] -= block * (dj - 0.5)
        G[..., 2] -= block * (dk - 0.5)
    nrm = np.linalg.norm(G, axis=3, keepdims=True)
    return np.divide(G, nrm, out=np.zeros_like(G), where=nrm > 1e-8)


def exposed(V, axis, sign):
    nb = np.zeros_like(V)
    cur, src = [slice(None)] * 3, [slice(None)] * 3
    if sign > 0:
        cur[axis] = slice(0, -1)
        src[axis] = slice(1, None)
    else:
        cur[axis] = slice(1, None)
        src[axis] = slice(0, -1)
    nb[tuple(cur)] = V[tuple(src)]
    return V & ~nb


def greedy_mesh(V, H):
    """贪心合并共面暴露面（行内 + 跨行），共享角点顶点，附平滑法线。

    两项优化让单文件 HTML 从数 MB 降到几百 KB：
      1. 相邻行上位置相同的连续段合并成一个大四边形；
      2. 角点 (i,j,k) 只存一次，所有共享该角点的面片复用同一顶点与法线。
    """
    nx, ny, nz = V.shape
    dx, dz = 2.0 / nx, 2.0 / nz
    dy = H / ny
    G = normalize_grad(V)

    def P(i, j, k):
        return (-1.0 + i * dx, j * dy, -1.0 + k * dz)

    def N(i, j, k):
        v = G[i, j, k]
        return (float(v[0]), float(v[1]), float(v[2]))

    pos, nor, idx, vmap = [], [], [], {}

    def vid(i, j, k):
        key = (i, j, k)
        if key not in vmap:
            vmap[key] = len(pos) // 3
            pos.extend(round(c, 5) for c in P(i, j, k))
            nor.extend(round(c, 4) for c in N(i, j, k))
        return vmap[key]

    def emit(i0, i1, j0, j1, plane, u, v, axis, flip):
        def corner(uu, vv):
            p = [0, 0, 0]
            p[u], p[v], p[axis] = uu, vv, plane
            return vid(*p)

        A, B = corner(i0, j0), corner(i1, j0)
        C, D = corner(i1, j1), corner(i0, j1)
        q = (A, D, C, B) if flip else (A, B, C, D)
        idx.extend([q[0], q[1], q[2], q[0], q[2], q[3]])

    for axis in range(3):
        u, v = [a for a in range(3) if a != axis]
        flip_base = (axis == 1)
        for sign in (1, -1):
            E = np.transpose(exposed(V, axis, sign), (u, v, axis))
            flip = flip_base ^ (sign < 0)
            nu, nv, na = E.shape
            for k in range(na):
                plane = k + 1 if sign > 0 else k
                active = {}                       # (i0,i1) -> 起始行
                for jv in range(nv + 1):          # 多跑一行，强制冲刷未闭合段
                    row = E[:, jv, k] if jv < nv else np.zeros(nu, bool)
                    segs, i = [], 0
                    while i < nu:
                        if not row[i]:
                            i += 1
                            continue
                        t = i
                        while t + 1 < nu and row[t + 1]:
                            t += 1
                        segs.append((i, t + 1))
                        i = t + 1
                    segset = set(segs)
                    for key, j0 in list(active.items()):
                        if key not in segset:
                            emit(key[0], key[1], j0, jv, plane, u, v, axis, flip)
                            del active[key]
                    for key in segs:
                        if key not in active:
                            active[key] = jv
    return pos, nor, idx


def view_image(m, cell=300):
    """[x, y] → 可显示的黑白图（恢复 y 向下）。"""
    a = np.flipud(m.T.astype(np.uint8) * 255)
    return Image.fromarray(255 - a).convert("RGB").resize((cell, cell), Image.NEAREST)


def main():
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="原始三视图截图；给了就自动先跑 inspect_views.py")
    ap.add_argument("--inspect", help="已有 inspect_views.py 输出目录（与 --image 二选一）")
    ap.add_argument("--out", default="_ai_work/auto")
    ap.add_argument("--res", type=int, default=88, help="体素分辨率，越大越精细越慢")
    ap.add_argument("--html", help="可选，直接生成可旋转 HTML")
    ap.add_argument("--title", default="三视图还原 · 立体图")
    ap.add_argument("--subtitle", default="由三视图自动重建")
    ap.add_argument("--tags", default="视觉外壳 · 自动重建")
    ap.add_argument("--views", default="iso,front,top,left,under")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 HTML")
    ap.add_argument("--min-score", type=float, default=0.97, help="三视图吻合度告警阈值")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.image:
        insp = out / "inspect"
        cmd = [sys.executable, str(INSPECT), "--image", args.image, "--outdir", str(insp)]
        print("[1/4] 提取三视图掩码 …")
        subprocess.run(cmd, check=True)
    elif args.inspect:
        insp = Path(args.inspect)
    else:
        sys.exit("[错误] 需要 --image 或 --inspect")

    npz = insp / "views.npz"
    if not npz.is_file():
        sys.exit(f"[错误] 缺少 {npz}；请用最新版 inspect_views.py 重新生成")
    d = np.load(npz)
    need = ("front", "top", "left")
    if any(k not in d for k in need):
        sys.exit(f"[错误] views.npz 需要 front/top/left，实际 {list(d.keys())}")

    print("[2/4] 求视觉外壳并自动判别投影手性 …")
    nx = nz = args.res
    # 高度按主视图原始宽高比，保持「长对正」
    fh, fw = d["front"].shape
    ny = max(8, int(round(args.res * fh / fw)))
    H = 2.0 * ny / nx

    F = as_xy(d["front"], nx, ny)      # [x, y]
    T0 = as_xy(d["top"], nx, nz)       # [x, z]
    L0 = as_xy(d["left"], nz, ny)      # [z, y]

    best = None
    for ft, fl in product((False, True), repeat=2):
        T = T0[:, ::-1] if ft else T0
        L = L0[::-1, :] if fl else L0
        Vv = F[:, :, None] & T[:, None, :] & L.T[None, :, :]
        qF, qT, qL = Vv.any(axis=2), Vv.any(axis=1), Vv.any(axis=0)
        s = (float(qF.sum()) / max(1, F.sum()),
             float(qT.sum()) / max(1, T.sum()),
             float(qL.sum()) / max(1, L.sum()))
        sc = sum(s) / 3
        if best is None or sc > best["score"]:
            best = dict(score=sc, ft=ft, fl=fl, V=Vv, s=s, T=T, L=L,
                        pF=qF, pT=qT, pL=qL)
    score, ft, fl = best["score"], best["ft"], best["fl"]
    V, T, L = best["V"], best["T"], best["L"]
    s, pF, pT, pL = best["s"], best["pF"], best["pT"], best["pL"]
    print(f"       翻转组合 top_z_flip={ft} left_z_flip={fl}  吻合度 "
          f"主视={s[0]:.4f} 俯视={s[1]:.4f} 左视={s[2]:.4f} 平均={score:.4f}")
    if score < args.min_score:
        print(f"[警告] 平均吻合度 {score:.4f} < {args.min_score}：三视图可能不自洽、"
              f"裁剪框错位或存在内部空腔；请用 --regions 校正或改走精确模式")

    # 反投影对照图
    cell, pad, head = 300, 10, 22
    canvas = Image.new("RGB", (3 * cell + 4 * pad, 2 * (cell + head) + 3 * pad), (246, 249, 252))
    draw = ImageDraw.Draw(canvas)
    # 上排刻意用原始裁剪图而非变换后的掩码：一旦方向/缩放搞反，肉眼立刻能发现
    pairs = [("FRONT", "front", F, pF, s[0]),
             ("TOP", "top", T, pT, s[1]),
             ("LEFT", "left", L, pL.T, s[2])]
    for c, (name, key, src, rec, sc) in enumerate(pairs):
        x = pad + c * (cell + pad)
        y1 = pad + head
        y2 = pad + head + cell + head + pad
        draw.text((x, pad + 3), f"{name} - original", fill=(40, 60, 80))
        draw.text((x, y1 + cell + pad + 3), f"{name} - hull ({sc:.3f})", fill=(40, 60, 80))
        orig = insp / f"{key}.png"
        if orig.is_file():
            top_img = Image.open(orig).convert("RGB").resize((cell, cell), Image.LANCZOS)
        else:
            top_img = view_image(src, cell)
        canvas.paste(top_img, (x, y1))
        canvas.paste(view_image(rec, cell), (x, y2))
    canvas.save(out / "compare.png")
    np.savez_compressed(out / "volume.npz", V=V, H=H, nx=nx, ny=ny, nz=nz)

    print("[3/4] 提取表面网格 …")
    pos, nor, idx = greedy_mesh(V, H)
    radius = float(np.sqrt(1 + (H / 2) ** 2) * 1.02)
    js = lambda a: json.dumps(a, separators=(",", ":"))
    model_js = (
        "/* 由 auto_reconstruct.py 从三视图视觉外壳自动生成；勿手工改数值 */\n"
        "const MODEL = {\n"
        f"  center: [0, {H / 2:.6f}, 0],\n"
        f"  radius: {radius:.6f},\n"
        "  meshes: [{ positions: " + js(pos) + ", indices: " + js(idx) +
        ", normals: " + js(nor) + " }],\n"
        "  edges: []\n};\n"
    )
    (out / "model.js").write_text(model_js, encoding="utf-8")

    report = {
        "score": round(score, 5), "per_view": dict(zip(("front", "top", "left"),
                                                      [round(v, 5) for v in s])),
        "flip": {"top_z": ft, "left_z": fl},
        "grid": {"nx": nx, "ny": ny, "nz": nz, "H": round(H, 4)},
        "voxels": int(V.sum()), "quads": len(idx) // 6,
        "vertices": len(pos) // 3, "elapsed_sec": round(time.time() - t0, 2),
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"       体素 {report['voxels']} · 四边形 {report['quads']} · "
          f"耗时 {report['elapsed_sec']}s")

    if args.html:
        print("[4/4] 生成可旋转页面 …")
        cmd = [sys.executable, str(BUILD), "--model", str(out / "model.js"),
               "--out", args.html, "--title", args.title, "--subtitle", args.subtitle,
               "--tags", args.tags, "--views", args.views]
        if args.force:
            cmd.append("--force")
        subprocess.run(cmd, check=True)
    else:
        print("[4/4] 未指定 --html，只输出 model.js（可用 build_viewer.py 生成页面）")

    print(f"[完成] 报告 {out / 'report.json'}")
    print(f"       对照 {out / 'compare.png'} · 模型 {out / 'model.js'}")
    if score < args.min_score:
        print("[提示] 吻合度偏低，务必查看 compare.png 后再交付")


if __name__ == "__main__":
    main()
