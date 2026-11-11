#!/usr/bin/env python3
"""一条命令量完三视图：所有实线/虚线段的像素坐标、圆弧、斜线。

替代「为每道题临时写扫描脚本」的做法（2026-11-10 第 5 题为此写了 4 个脚本、多花 10 分钟）。
本脚本直接给出可直接抄进建模代码的数字，并把检出结果画回放大图供肉眼复核。

用法：
  python measure.py --image "<截图>" --outdir _ai_work/measure
  python measure.py --image "<截图>" --outdir _ai_work/measure \
      --regions "front=30,30,250,275;top=30,285,255,505;left=275,40,490,275"

产出：
  measure.json   每视图的 H/V 线段、弧、斜线（绝对像素坐标）
  measure.txt    同上，人类可读表格
  m_<view>.png   3x 放大 + 10px 网格 + 检出线着色（H红 V蓝 弧橙 斜绿）
stdout 同时打印紧凑表格，一般不用再读 json。

阈值：--min-len 12（短于此当噪点）、--gap 6（虚线断开容忍）、--dark 105。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

DEFAULT_REGIONS = {
    "front": (0.02, 0.02, 0.49, 0.52),
    "top":   (0.02, 0.52, 0.49, 1.00),
    "left":  (0.49, 0.02, 0.99, 0.52),
}
TINT = {"H": (225, 40, 40), "V": (30, 90, 220), "arc": (235, 140, 20), "slant": (20, 150, 80)}


# ---------------- 基础工具 ----------------

def parse_regions(spec, w, h):
    if not spec:
        return {k: tuple(int(round(v * (w if i % 2 == 0 else h)))
                         for i, v in enumerate(vals))
                for k, vals in DEFAULT_REGIONS.items()}
    out = {}
    for part in spec.split(";"):
        if not part.strip():
            continue
        name, box = part.split("=", 1)
        nums = tuple(int(float(v)) for v in box.split(","))
        if len(nums) != 4:
            sys.exit(f"[错误] 区域 {name} 需要 4 个数：x0,y0,x1,y1")
        out[name.strip()] = nums
    return out


def line_mask(arr, dark=105):
    """粗实线 + 品红标注线；浅灰点画中心线被暗阈值排除。"""
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    dark_m = (r < dark) & (g < dark) & (b < dark)
    mag = ((r > 130) & (b > 130) & (g < 130) & ((r - g) > 60) & ((b - g) > 60))
    return dark_m | mag


def split_runs(idx, gap):
    """把有序下标数组按「间隔 > gap」切成若干段，返回 [(start, end), ...]。"""
    if len(idx) == 0:
        return []
    d = np.diff(idx)
    brk = np.flatnonzero(d > gap)
    starts = np.r_[idx[0], idx[brk + 1]]
    ends = np.r_[idx[brk], idx[-1]]
    return list(zip(starts.tolist(), ends.tolist()))


def scan(m, axis, min_len, gap):
    """按行(axis=0)或列(axis=1)扫描，返回横/竖候选段 [(p, s, e), ...]。

    p 是行号（H 段）或列号（V 段），s/e 是沿另一轴的范围。
    """
    runs = []
    n = m.shape[0] if axis == 0 else m.shape[1]
    for p in range(n):
        idx = np.flatnonzero(m[p]) if axis == 0 else np.flatnonzero(m[:, p])
        for s, e in split_runs(idx, gap):
            if e - s + 1 >= min_len:
                runs.append((p, s, e))
    return runs


def merge_runs(runs, tol=2):
    """把相邻扫描线上的同一条线合并成一条粗线段。"""
    segs = []
    for p, s, e in runs:
        best = None
        for sg in segs:
            if p - sg["p1"] <= tol and p >= sg["p0"] and \
               min(e, sg["e"]) - max(s, sg["s"]) >= 0:
                best = sg
                break
        if best is None:
            segs.append({"p0": p, "p1": p, "s": s, "e": e, "n": 1})
        else:
            best["p1"] = p
            best["s"] = min(best["s"], s)
            best["e"] = max(best["e"], e)
            best["n"] += 1
    return segs


def join_collinear(segs, max_gap=10, tol_center=1.5):
    """把同一条线上被虚线间隙切开的小段接回一条线。

    关键：不能按 s 全局排序后只回看 out[-1] —— 图上同时有多条横线，
    排序后紧邻的元素未必是同一条线的邻居（2026-11-10 踩过）。
    必须按「中线位置」归链，再在链内按 s 贪心合并。

    虚线判定也不靠扫描时放宽 gap（放宽会把两条真实相邻的线误并），
    而靠在整条线上看空隙分布 —— 这才是「虚线」的定义。
    """
    chains = []
    for sg in sorted(segs, key=lambda d: (d["s"], d["p0"])):
        c = (sg["p0"] + sg["p1"]) / 2
        best, best_gap = None, None
        for ch in chains:
            if abs(ch["c"] - c) > tol_center:
                continue
            g = sg["s"] - ch["e"] - 1
            if g <= max_gap and g >= -8 and (best_gap is None or g < best_gap):
                best, best_gap = ch, g
        if best is None:
            chains.append({"c": c, "p0": sg["p0"], "p1": sg["p1"],
                           "s": sg["s"], "e": sg["e"], "runs": [[sg["s"], sg["e"]]]})
        else:
            best["runs"].append([sg["s"], sg["e"]])
            best["e"] = max(best["e"], sg["e"])
            best["p0"] = min(best["p0"], sg["p0"])
            best["p1"] = max(best["p1"], sg["p1"])
            best["c"] = (best["p0"] + best["p1"]) / 2
    return chains


def finish_seg(joined, horizontal, min_len, off=0):
    """把一条合并后的线整理成输出结构：跨度、断裂数、是否虚线。"""
    span = joined["e"] - joined["s"] + 1
    runs = [[a + off, b + off] for a, b in joined["runs"]]
    gaps = len(runs) - 1
    gap_len = span - sum(e - s + 1 for s, e in runs)
    max_gap = max((runs[i + 1][0] - runs[i][1] - 1 for i in range(len(runs) - 1)), default=0)
    thick = joined["p1"] - joined["p0"] + 1
    d = {"span": span, "runs": runs, "gaps": gaps,
         "gap_frac": round(gap_len / span, 3), "max_gap": max_gap, "thick": thick,
         "dashed": bool(gaps >= 2 and gap_len >= 0.10 * span),
         "thin": thick <= 2}
    if horizontal:
        d["y"] = int(round((joined["p0"] + joined["p1"]) / 2))
        d["x0"], d["x1"] = int(joined["s"]), int(joined["e"])
        d["len"] = span
    else:
        d["x"] = int(round((joined["p0"] + joined["p1"]) / 2))
        d["y0"], d["y1"] = int(joined["s"]), int(joined["e"])
        d["len"] = span
    return d


def fit_circle(x, y):
    A = np.c_[2 * x, 2 * y, np.ones(len(x))]
    bb = x * x + y * y
    sol, *_ = np.linalg.lstsq(A, bb, rcond=None)
    cx, cy = sol[0], sol[1]
    r = float(np.sqrt(max(1e-9, sol[2] + cx * cx + cy * cy)))
    res = float(np.mean(np.abs(np.hypot(x - cx, y - cy) - r)))
    return cx, cy, r, res


def fit_line(x, y):
    """PCA 主方向直线拟合，返回端点、斜率、残差。"""
    mx, my = x.mean(), y.mean()
    u = np.c_[x - mx, y - my]
    w, v = np.linalg.eigh(u.T @ u)
    d = v[:, -1]
    t = u @ d
    p0 = (mx + d[0] * t.min(), my + d[1] * t.min())
    p1 = (mx + d[0] * t.max(), my + d[1] * t.max())
    res = float(np.mean(np.abs(u @ np.array([-d[1], d[0]]))))
    slope = float((p1[1] - p0[1]) / (p1[0] - p0[0])) if abs(p1[0] - p0[0]) > 1e-6 else float("inf")
    return p0, p1, slope, res


def curve_like(sub, d, horizontal, ox=0, oy=0, rest0=None, reach=5):
    """判断这段「横线/竖线」其实是圆弧的极值行。

    不用「厚度在两端变大」那种几何猜测 —— 直线端点的转角同样会变厚，
    误判率很高（2026-11-10 试过，一半横线被误判成弧）。
    改用直接证据：把横竖线全部扣掉之后，**两个端点的延长方向上都还有
    曲线像素**，才说明这一小段是一条连续曲线被误当成了直线。
    """
    if rest0 is None:
        return False
    if d["len"] < 18:
        return False
    if d.get("gaps"):
        # 自身带空隙 = 虚线/点画线（如中心线），是货真价实的线，不是弧的极值行
        return False
    if horizontal:
        y = d["y"] - oy
        r0, r1 = max(0, y - 3), min(sub.shape[0], y + 4)
        spots = [(max(0, r0), r1, d["x0"] - ox - reach, d["x0"] - ox - 1),
                 (max(0, r0), r1, d["x1"] - ox + 2, d["x1"] - ox + reach + 1)]
    else:
        x = d["x"] - ox
        c0, c1 = max(0, x - 3), min(sub.shape[1], x + 4)
        spots = [(d["y0"] - oy - reach, d["y0"] - oy - 1, max(0, c0), c1),
                 (d["y1"] - oy + 2, d["y1"] - oy + reach + 1, max(0, c0), c1)]
    hits = 0
    for a, b, c, e in spots:
        if b <= a or e <= c:
            continue
        if rest0[max(0, a):b, max(0, c):e].any():
            hits += 1
    return hits == 2


def dilate3(mask, n=1):
    """方形结构元膨胀 n 次，用来把被扣掉的线条切断的曲线接回一段。"""
    o = mask.copy()
    for _ in range(n):
        p = o.copy()
        o[1:, :] |= p[:-1, :]
        o[:-1, :] |= p[1:, :]
        o[:, 1:] |= p[:, :-1]
        o[:, :-1] |= p[:, 1:]
        o[1:, 1:] |= p[:-1, :-1]
        o[1:, :-1] |= p[:-1, 1:]
        o[:-1, 1:] |= p[1:, :-1]
        o[:-1, :-1] |= p[1:, 1:]
    return o


def label_components(mask):
    h, w = mask.shape
    lab = np.zeros((h, w), np.int32)
    cur = 0
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if lab[y0, x0]:
            continue
        cur += 1
        lab[y0, x0] = cur
        stack = [(y0, x0)]
        while stack:
            y, x = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not lab[ny, nx]:
                        lab[ny, nx] = cur
                        stack.append((ny, nx))
    return lab, cur


def arc_span(x, y, cx, cy):
    """由像素角度求圆弧的起止角：找最大空隙，弧就是它的补集。"""
    ang = np.sort(np.degrees(np.arctan2(y - cy, x - cx)) % 360)
    if len(ang) < 2:
        return float(ang[0]), float(ang[0])
    d = np.diff(np.r_[ang, ang[0] + 360])
    k = int(np.argmax(d))
    return float(ang[(k + 1) % len(ang)]), float(ang[k])


def ransac_circles(X, Y, r_max=170, trials=3000, tol=1.6, min_in=70,
                   min_span=90.0, seed=0):
    """RANSAC 找圆：三定圆 + 投票 + 内点重拟合 + 非极大抑制。

    为什么不用「逐连通块最小二乘」：横竖中心线会把同一圈切成几段，
    只剩 60° 的片段拟合出的半径系统性偏小（实测 98 px 的弧会报 76 px），
    而且圆的最顶端那一行长 run 会被当成横线扣掉，等于把弧的顶点也削了。
    """
    n = len(X)
    if n < 30:
        return []
    rs = np.random.default_rng(seed)
    if n > 1500:                       # 采样上限，保证速度
        sel = rs.choice(n, 1500, replace=False)
        PX, PY = X[sel], Y[sel]
    else:
        PX, PY = X, Y
    m = len(PX)
    votes = {}
    for _ in range(trials):
        i, j, k = rs.integers(0, m, 3)
        ax, ay, bx, by, cx_, cy_ = PX[i], PY[i], PX[j], PY[j], PX[k], PY[k]
        d = 2 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
        if abs(d) < 1e-6:
            continue
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx_ * cx_ + cy_ * cy_
        ux = (a2 * (by - cy_) + b2 * (cy_ - ay) + c2 * (ay - by)) / d
        uy = (a2 * (cx_ - bx) + b2 * (ax - cx_) + c2 * (bx - ax)) / d
        r = float(np.hypot(ax - ux, ay - uy))
        if not (6 < r < r_max):
            continue
        key = (int(ux // 5), int(uy // 5), int(r // 5))
        votes[key] = votes.get(key, 0) + 1
    out = []
    top = sorted(votes.items(), key=lambda kv: -kv[1])[:40]
    for (kx, ky, kr), cnt in top:
        cx, cy, r = kx * 5 + 2.5, ky * 5 + 2.5, kr * 5 + 2.5
        for _ in range(4):             # 内点重拟合
            d = np.hypot(X - cx, Y - cy)
            keep = np.abs(d - r) < tol * 2
            if keep.sum() < min_in:
                break
            cx, cy, r, _res = fit_circle(X[keep], Y[keep])
        d = np.hypot(X - cx, Y - cy)
        keep = np.abs(d - r) < tol
        if keep.sum() < min_in:
            continue
        a0, a1 = arc_span(X[keep], Y[keep], cx, cy)
        span = (a1 - a0) % 360
        if span < min_span:            # 太浅的弧和直线/拐角无法区分，宁可不报
            continue
        # 内点数要与「弧长 × 线宽」量级相符：一堆散点凑出来的假圆过不了这一关
        expect = 2 * tol * np.radians(span) * r
        if not (0.42 * expect <= keep.sum() <= 1.9 * expect):
            continue
        # 角度上必须铺满，不能挤在一小段
        ang = np.degrees(np.arctan2(Y[keep] - cy, X[keep] - cx)) % 360
        rel = ((ang - a0) % 360) / span
        bins = np.histogram(np.clip(rel, 0, 0.999), bins=20, range=(0, 1))[0]
        if (bins > 0).sum() < 13:
            continue
        if any(abs(cx - o["cx"]) < 12 and abs(cy - o["cy"]) < 12 and abs(r - o["r"]) < 12
               for o in out):
            continue
        out.append({"cx": float(cx), "cy": float(cy), "r": float(r),
                    "res": round(float(np.mean(np.abs(d[keep] - r))), 2),
                    "n": int(keep.sum()), "ang0": round(a0, 1), "ang1": round(a1, 1),
                    "span": round(span, 1), "full": span > 340})

    # 二次贪心：按内点从多到少接受，并把已用掉的像素从池子里扣掉。
    # 同心圆之间会出现「半径夹在两条真圆中间」的假圆，它借用上下两条真圆的
    # 像素就能凑够内点（第 5 题左视 96 / 42 之间冒出一个 68）。扣池子即可灭掉。
    out.sort(key=lambda o: -o["n"])
    keep, pool = [], np.ones(len(X), bool)
    for o in out:
        d = np.hypot(X - o["cx"], Y - o["cy"])
        m = (np.abs(d - o["r"]) < tol) & pool
        if m.sum() < max(min_in, 0.7 * o["n"]):
            continue
        o["n"] = int(m.sum())
        keep.append(o)
        pool &= ~m
    return keep


def bbox_centerline(m, box, min_len=12, gap=3):
    """视图的「描边中线包围盒」，供 project_mesh --auto-calib 求尺度。

    直接拿线条像素的包围盒会偏大一个线宽（描边有厚度），
    拿实心掩码又会偏小（那里的边界是描边内沿），两者差 4 px 就能让
    276 px 的宽度差 1.4% —— 叠加图上肉眼可见。所以先剔除细中心线
    （它会伸出轮廓外），再按描边半宽各内缩一半。
    """
    x0, y0, x1, y1 = box
    ox, oy = max(0, x0), max(0, y0)
    sub = m[oy:y1, ox:x1]
    if not sub.any():
        return None
    Hh, Vv = [], []
    for sg in join_collinear(merge_runs(scan(sub, 0, min_len, gap))):
        if sg["e"] - sg["s"] + 1 >= min_len:
            Hh.append(finish_seg(sg, True, min_len))
    for sg in join_collinear(merge_runs(scan(sub, 1, min_len, gap))):
        if sg["e"] - sg["s"] + 1 >= min_len:
            Vv.append(finish_seg(sg, False, min_len))
    thin = np.zeros_like(sub)
    for d in Hh:
        if d["thin"]:
            a, b = d["y"] - d["thick"] // 2 - 1, d["y"] + d["thick"] // 2 + 2
            thin[max(0, a):b, max(0, d["x0"]):d["x1"] + 1] = True
    for d in Vv:
        if d["thin"]:
            a, b = d["x"] - d["thick"] // 2 - 1, d["x"] + d["thick"] // 2 + 2
            thin[max(0, d["y0"]):d["y1"] + 1, max(0, a):b] = True
    solid = sub & ~thin
    if not solid.any():
        return None
    xs = np.nonzero(solid.any(axis=0))[0]
    ys = np.nonzero(solid.any(axis=1))[0]
    bx = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    ths = [d["thick"] for d in Hh + Vv
           if d["len"] > 0.5 * max(1, bx[2] - bx[0])]
    th = int(round(np.median(ths))) if ths else 2
    pad = th / 2.0
    return [round(bx[0] + pad + ox, 1), round(bx[1] + pad + oy, 1),
            round(bx[2] - pad + ox, 1), round(bx[3] - pad + oy, 1)]


def merge_slants(slants, max_gap=8, dslope=0.16):
    """把被交叉线打断的同一条斜线接回去（断点处端点几乎重合、斜率接近）。"""
    out = []
    for s in sorted(slants, key=lambda d: (d["x0"] + d["x1"])):
        hit = None
        for o in out:
            for a, b in (((s["x0"], s["y0"]), (o["x1"], o["y1"])),
                         ((s["x1"], s["y1"]), (o["x0"], o["y0"])),
                         ((s["x0"], s["y0"]), (o["x0"], o["y0"])),
                         ((s["x1"], s["y1"]), (o["x1"], o["y1"]))):
                if np.hypot(a[0] - b[0], a[1] - b[1]) <= max_gap and \
                   abs(s["slope"] - o["slope"]) <= dslope:
                    hit = o
                    break
            if hit:
                break
        if not hit:
            out.append(dict(s))
            continue
        o = hit
        pts = [(o["x0"], o["y0"]), (o["x1"], o["y1"]), (s["x0"], s["y0"]), (s["x1"], s["y1"])]
        far = max(((p, q) for i, p in enumerate(pts) for q in pts[i + 1:]),
                  key=lambda pq: np.hypot(pq[0][0] - pq[1][0], pq[0][1] - pq[1][1]))
        (o["x0"], o["y0"]), (o["x1"], o["y1"]) = far
        o["len"] = round(float(np.hypot(o["x1"] - o["x0"], o["y1"] - o["y0"])), 1)
        o["slope"] = (round((o["y1"] - o["y0"]) / (o["x1"] - o["x0"]), 4)
                      if abs(o["x1"] - o["x0"]) > 1e-6 else float("inf"))
    return out


def analyse(m, box, args):
    """在 box 区域内分析线条，返回 dict。所有坐标转成绝对像素。"""
    x0, y0, x1, y1 = box
    sub = m[y0:y1, x0:x1]

    H, V = [], []
    for sg in join_collinear(merge_runs(scan(sub, 0, args.min_len, args.gap))):
        if sg["e"] - sg["s"] + 1 < args.min_len:
            continue
        d = finish_seg(sg, True, args.min_len, off=x0)
        d["y"] += y0
        d["x0"] += x0
        d["x1"] += x0
        H.append(d)
    for sg in join_collinear(merge_runs(scan(sub, 1, args.min_len, args.gap))):
        if sg["e"] - sg["s"] + 1 < args.min_len:
            continue
        d = finish_seg(sg, False, args.min_len, off=y0)
        d["x"] += x0
        d["y0"] += y0
        d["y1"] += y0
        V.append(d)

    # 扣掉已认定的横竖线段（按 (行, 列) 顺序切片，别搞反），剩下的是弧和斜线。
    # 但圆弧的极值行会被误判成横线（R=98 px 时顶点那段「横线」长达 76 px），
    # 扣掉它等于把弧从顶点削掉一大块，弧拟合的半径会系统性偏小。
    all_bands = np.zeros_like(sub)
    for d in H:
        a, b = d["y"] - y0 - d["thick"] // 2 - 1, d["y"] - y0 + d["thick"] // 2 + 2
        all_bands[max(0, a):b, d["x0"] - x0:d["x1"] - x0 + 1] = True
    for d in V:
        a, b = d["x"] - x0 - d["thick"] // 2 - 1, d["x"] - x0 + d["thick"] // 2 + 2
        all_bands[d["y0"] - y0:d["y1"] - y0 + 1, max(0, a):b] = True
    rest0 = sub & ~all_bands            # 扣掉全部横竖线后剩下的：弧、斜线、文字

    used = np.zeros_like(sub)
    for d in H:
        if curve_like(sub, d, True, x0, y0, rest0):
            d["curve_like"] = True
            continue
        a, b = d["y"] - y0 - d["thick"] // 2 - 1, d["y"] - y0 + d["thick"] // 2 + 2
        used[max(0, a):b, d["x0"] - x0:d["x1"] - x0 + 1] = True
    for d in V:
        if curve_like(sub, d, False, x0, y0, rest0):
            d["curve_like"] = True
            continue
        a, b = d["x"] - x0 - d["thick"] // 2 - 1, d["x"] - x0 + d["thick"] // 2 + 2
        used[d["y0"] - y0:d["y1"] - y0 + 1, max(0, a):b] = True
    # 扣线会把弧切成几段（中心线正穿过圆心），先膨胀 3 px 把断口接回来再标连通块
    rest = sub & ~used
    lab_g, n = label_components(dilate3(rest, 3))
    lab = np.where(rest, lab_g, 0)

    slants, noise = [], []
    for k in range(1, n + 1):
        yy, xx = np.nonzero(lab == k)
        if len(xx) < 18:
            noise.append({"n": int(len(xx))})
            continue
        X, Y = (xx + x0).astype(float), (yy + y0).astype(float)
        p0, p1, slope, lres = fit_line(X, Y)
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        if abs(dx) < 2.5 and abs(dy) >= args.min_len:
            # 竖向细碎段：本质是没被 V 扫描接住的竖线，转回 V
            V.append({"x": int(round(X.mean())), "y0": int(Y.min()), "y1": int(Y.max()),
                      "len": int(Y.max() - Y.min() + 1), "span": int(Y.max() - Y.min() + 1),
                      "thick": 1, "dashed": False, "gaps": 0, "gap_frac": 0.0,
                      "max_gap": 0, "thin": True, "runs": [[int(Y.min()), int(Y.max())]]})
            continue
        if lres < 1.4 and np.hypot(dx, dy) >= args.min_len:
            slants.append({"x0": round(p0[0], 1), "y0": round(p0[1], 1),
                           "x1": round(p1[0], 1), "y1": round(p1[1], 1),
                           "slope": round(slope, 4),
                           "len": round(float(np.hypot(dx, dy)), 1),
                           "res": round(lres, 2), "n": int(len(xx))})
        else:
            noise.append({"n": int(len(xx)),
                          "bbox": [int(X.min()), int(Y.min()), int(X.max()), int(Y.max())],
                          "lres": round(lres, 2)})

    # 斜率≈0 的残余段其实是被 curve_like 放过的横线，H 里已有，别重复报
    slants = [d for d in merge_slants(slants) if abs(d["y1"] - d["y0"]) > 2]

    # 圆/弧用 RANSAC 在「线条像素减去长直线段」的掩码上找，不依赖连通块完整性
    cy_mask = rest.copy()
    for d in H:
        if d.get("curve_like"):
            a, b = d["y"] - y0 - d["thick"] // 2 - 2, d["y"] - y0 + d["thick"] // 2 + 3
            cy_mask[max(0, a):b, d["x0"] - x0:d["x1"] - x0 + 1] |= sub[max(0, a):b, d["x0"] - x0:d["x1"] - x0 + 1]
    for d in V:
        if d.get("curve_like"):
            a, b = d["x"] - x0 - d["thick"] // 2 - 2, d["x"] - x0 + d["thick"] // 2 + 3
            cy_mask[d["y0"] - y0:d["y1"] - y0 + 1, max(0, a):b] |= sub[d["y0"] - y0:d["y1"] - y0 + 1, max(0, a):b]
    cy_y, cy_x = np.nonzero(cy_mask)
    arcs = ransac_circles((cy_x + x0).astype(float), (cy_y + y0).astype(float),
                          r_max=args.min_len * 14)

    H.sort(key=lambda d: (d["y"], d["x0"]))
    V.sort(key=lambda d: (d["x"], d["y0"]))

    # 中心线会伸出轮廓外，不能参与包围盒，否则长对正/高平齐会假报 FAIL
    thin_mask = np.zeros_like(sub)
    for d in H:
        if d["thin"]:
            a, b = d["y"] - y0 - d["thick"] // 2 - 1, d["y"] - y0 + d["thick"] // 2 + 2
            thin_mask[max(0, a):b, d["x0"] - x0:d["x1"] - x0 + 1] = True
    for d in V:
        if d["thin"]:
            a, b = d["x"] - x0 - d["thick"] // 2 - 1, d["x"] - x0 + d["thick"] // 2 + 2
            thin_mask[d["y0"] - y0:d["y1"] - y0 + 1, max(0, a):b] = True
    solid = sub & ~thin_mask
    xs = np.nonzero(solid.any(axis=0))[0]
    ys = np.nonzero(solid.any(axis=1))[0]
    bbox = [int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0), int(ys.max() + y0)]
    ths = [d["thick"] for d in H + V if d["len"] > 0.5 * max(1, bbox[2] - bbox[0])]
    th = int(round(np.median(ths))) if ths else 0
    pad = th / 2.0
    bbox_c = [bbox[0] + pad, bbox[1] + pad, bbox[2] - pad, bbox[3] - pad]
    return {"box": [x0, y0, x1, y1], "bbox": bbox,
            "bbox_c": [round(v, 1) for v in bbox_c],
            "line_width": th,
            "w": bbox[2] - bbox[0] + 1, "h": bbox[3] - bbox[1] + 1,
            "w_c": round(bbox_c[2] - bbox_c[0], 1), "h_c": round(bbox_c[3] - bbox_c[1], 1),
            "H": H, "V": V, "arcs": arcs, "slants": slants, "noise": noise}


def draw_overlay(img, res, out_path, zoom=3):
    x0, y0, x1, y1 = res["box"]
    crop = img.crop((x0, y0, x1, y1)).resize(((x1 - x0) * zoom, (y1 - y0) * zoom), Image.LANCZOS)
    crop = crop.point(lambda v: int(150 + v * 0.42))       # 压淡原图，突出着色
    dr = ImageDraw.Draw(crop)
    W, Hh = crop.size
    for gx in range(x0 - x0 % 10, x1 + 1, 10):
        px = (gx - x0) * zoom
        heavy = gx % 50 == 0
        dr.line([(px, 0), (px, Hh)], fill=(205, 218, 232) if not heavy else (168, 190, 214))
        if heavy:
            dr.text((px + 2, 2), str(gx), fill=(90, 110, 135))
    for gy in range(y0 - y0 % 10, y1 + 1, 10):
        py = (gy - y0) * zoom
        heavy = gy % 50 == 0
        dr.line([(0, py), (W, py)], fill=(205, 218, 232) if not heavy else (168, 190, 214))
        if heavy:
            dr.text((2, py + 1), str(gy), fill=(90, 110, 135))

    def T(v):                                   # 绝对坐标 → 画布坐标
        return ((v[0] - x0) * zoom, (v[1] - y0) * zoom)

    for d in res["H"]:
        col = TINT["H"]
        a, b = T((d["x0"], d["y"])), T((d["x1"], d["y"]))
        if d["dashed"]:
            n = max(2, int(d["len"] / 5))
            for i in range(0, n, 2):
                dr.line([(a[0] + (b[0] - a[0]) * i / n, a[1]),
                         (a[0] + (b[0] - a[0]) * (i + 1) / n, a[1])], fill=col, width=2)
        else:
            dr.line([a, b], fill=col, width=2)
    for d in res["V"]:
        col = TINT["V"]
        a, b = T((d["x"], d["y0"])), T((d["x"], d["y1"]))
        if d["dashed"]:
            n = max(2, int(d["len"] / 5))
            for i in range(0, n, 2):
                dr.line([(a[0], a[1] + (b[1] - a[1]) * i / n),
                         (a[0], a[1] + (b[1] - a[1]) * (i + 1) / n)], fill=col, width=2)
        else:
            dr.line([a, b], fill=col, width=2)
    for d in res["arcs"]:
        cx, cy, r = d["cx"], d["cy"], d["r"]
        a0, a1 = d["ang0"], d["ang1"]
        if d["full"]:
            dr.ellipse([T((cx - r, cy - r)), T((cx + r, cy + r))], outline=TINT["arc"], width=2)
        else:
            dr.arc([T((cx - r, cy - r)), T((cx + r, cy + r))], a0, a1, fill=TINT["arc"], width=2)
        c = T((cx, cy))
        dr.line([(c[0] - 4, c[1]), (c[0] + 4, c[1])], fill=TINT["arc"], width=2)
        dr.line([(c[0], c[1] - 4), (c[0], c[1] + 4)], fill=TINT["arc"], width=2)
    for d in res["slants"]:
        dr.line([T((d["x0"], d["y0"])), T((d["x1"], d["y1"]))], fill=TINT["slant"], width=2)
    crop.save(out_path)


def fmt(res):
    L = []
    L.append(f"  bbox={res['bbox']}  {res['w']}x{res['h']}  线宽≈{res['line_width']}")
    L.append(f"  中线框 bbox_c={res['bbox_c']}  {res['w_c']}x{res['h_c']}"
             f"（按描边中线算，标定 S 用它）")
    L.append(f"  H 横线 {len(res['H'])} 条")
    for d in res["H"]:
        tag = f"虚线×{d['gaps']}" if d["dashed"] else "实线"
        if d["thin"]:
            tag += "·细"
        L.append(f"    y={d['y']:4d}  x={d['x0']:4d}..{d['x1']:4d}  len={d['len']:4d}  {tag}")
        if d["gaps"]:
            L.append(f"        分段 {d['runs']}")
    L.append(f"  V 竖线 {len(res['V'])} 条")
    for d in res["V"]:
        tag = f"虚线×{d['gaps']}" if d["dashed"] else "实线"
        if d["thin"]:
            tag += "·细"
        L.append(f"    x={d['x']:4d}  y={d['y0']:4d}..{d['y1']:4d}  len={d['len']:4d}  {tag}")
        if d["gaps"]:
            L.append(f"        分段 {d['runs']}")
    if res["arcs"]:
        L.append(f"  弧/圆 {len(res['arcs'])} 个")
        for d in res["arcs"]:
            kind = "整圆" if d["full"] else f"弧 {d['ang0']:.0f}°→{d['ang1']:.0f}°({d['span']:.0f}°)"
            L.append(f"    c=({d['cx']:.1f}, {d['cy']:.1f})  R={d['r']:.2f}  {kind}  "
                     f"残差{d['res']}  px={d['n']}")
    if res["slants"]:
        L.append(f"  斜线 {len(res['slants'])} 条")
        for d in res["slants"]:
            L.append(f"    ({d['x0']:.0f},{d['y0']:.0f})→({d['x1']:.0f},{d['y1']:.0f})  "
                     f"斜率={d['slope']:+.4f}  长{d['len']}  残差{d['res']}")
    if res["noise"]:
        big = [d for d in res["noise"] if d["n"] >= 40]
        if big:
            L.append(f"  未归类（可能是文字或曲线拐角）{len(big)} 块，最大 {max(d['n'] for d in big)} px")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--outdir", default="_ai_work/measure")
    ap.add_argument("--regions", default="", help="像素框：front=x0,y0,x1,y1;top=...;left=...")
    ap.add_argument("--min-len", type=int, default=12, help="线段最短长度（短于此当噪点）")
    ap.add_argument("--gap", type=int, default=3, help="扫描时同一行内允许的空缺；虚线靠合并后判空隙")
    ap.add_argument("--dark", type=int, default=105, help="粗实线暗阈值")
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--no-image", action="store_true", help="不画着色复核图")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        sys.exit(f"[错误] 找不到图片：{src}")
    img = Image.open(src).convert("RGB")
    w, h = img.size
    arr = np.asarray(img).astype(int)
    m = line_mask(arr, args.dark)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[信息] {w}x{h} · 线条像素 {int(m.sum())}")
    regions = parse_regions(args.regions, w, h)
    result = {}
    lines = []
    for name, box in regions.items():
        res = analyse(m, box, args)
        result[name] = res
        lines.append(f"=== {name} ===")
        lines.append(fmt(res))
        if not args.no_image:
            draw_overlay(img, res, outdir / f"m_{name}.png", args.zoom)

    report = "\n".join(lines)
    print(report)

    # 长对正 / 宽相等 自检（第一角投影的两条硬约束）
    checks = []
    if "front" in result and "top" in result:
        a, b = result["front"]["w"], result["top"]["w"]
        checks.append(("长对正 主视宽 vs 俯视宽", a, b, abs(a - b) <= max(3, 0.02 * max(a, b))))
    if "front" in result and "left" in result:
        a, b = result["front"]["h"], result["left"]["h"]
        checks.append(("高平齐 主视高 vs 左视高", a, b, abs(a - b) <= max(3, 0.02 * max(a, b))))
    if "top" in result and "left" in result:
        a, b = result["top"]["h"], result["left"]["w"]
        checks.append(("宽相等 俯视深 vs 左视深", a, b, abs(a - b) <= max(3, 0.02 * max(a, b))))
    if checks:
        print("\n--- 投影关系自检 ---")
        for label, a, b, ok in checks:
            print(f"  {'OK ' if ok else 'FAIL'} {label}: {a} vs {b} (差 {abs(a - b)})")

    (outdir / "measure.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "measure.txt").write_text(report + "\n", encoding="utf-8")
    print(f"\n[完成] {outdir.resolve()}")
    print("       measure.txt/json 全部数字 · m_*.png 着色复核图（红横 蓝竖 橙弧 绿斜）")


if __name__ == "__main__":
    main()
