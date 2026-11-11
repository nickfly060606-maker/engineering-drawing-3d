#!/usr/bin/env python3
"""从三视图截图一步产出读图素材：放大图、实心掩码、归一化轮廓数据。

用法：
  python inspect_views.py --image <截图> --outdir _ai_work/inspect
  # 非标准布局时手动指定像素框：
  python inspect_views.py --image <截图> --outdir _ai_work/inspect \
      --regions "front=30,30,250,275;top=30,285,255,505;left=275,40,490,275"

说明：只识别「粗实线」（深黑 + 饱和品红），浅灰点画中心线会被排除，
否则 flood fill 求实心区域时会把区域切成两半。这是上次踩过的坑。
"""
import argparse
import json
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

# 标准三视图布局：主视左上 / 俯视左下 / 左视右上（相对整图宽高）
DEFAULT_REGIONS = {
    "front": (0.02, 0.02, 0.49, 0.52),
    "top":   (0.02, 0.52, 0.49, 1.00),
    "left":  (0.49, 0.02, 0.99, 0.52),
}
COLORS = {"front": (255, 120, 120), "top": (120, 200, 255), "left": (200, 120, 255)}


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


def dilate(m, n=1):
    o = m.copy()
    for _ in range(n):
        p = o.copy()
        o[1:, :] |= p[:-1, :]
        o[:-1, :] |= p[1:, :]
        o[:, 1:] |= p[:, :-1]
        o[:, :-1] |= p[:, 1:]
    return o


def major_components(m, min_frac=0.005):
    """保留所有「主要」8 邻域连通块，只滤掉题号文字等零散噪点。

    不能只取最大块：视图内部的实线分界线会把实心区域切成多块
    （例如圆被水平中心线分成上下两半），只留最大块会丢掉半个视图。
    """
    h, w = m.shape
    seen = np.zeros_like(m, bool)
    comps = []
    ys, xs = np.where(m)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        comp = []
        dq = deque([(sy, sx)])
        seen[sy, sx] = True
        while dq:
            y, x = dq.popleft()
            comp.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    dq.append((ny, nx))
        comps.append(comp)

    threshold = max(80, int(m.sum()) * min_frac)
    keep = np.zeros_like(m, bool)
    kept, nblocks = 0, 0
    for comp in comps:
        if len(comp) >= threshold:
            for y, x in comp:
                keep[y, x] = True
            kept += len(comp)
            nblocks += 1
    return keep, kept, nblocks


def solid_mask(arr, dark_thr=105, use_magenta=True):
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    dark = (r < dark_thr) & (g < dark_thr) & (b < dark_thr)
    mag = ((r > 130) & (b > 130) & (g < 130) &
           ((r - g) > 60) & ((b - g) > 60))
    obstacle = dilate((dark | (mag if use_magenta else False)), 2)
    h, w = obstacle.shape
    outside = np.zeros((h, w), bool)
    dq = deque()

    def seed(y, x):
        if not obstacle[y, x] and not outside[y, x]:
            outside[y, x] = True
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
            if 0 <= ny < h and 0 <= nx < w and not obstacle[ny, nx] and not outside[ny, nx]:
                outside[ny, nx] = True
                dq.append((ny, nx))
    return (~obstacle) & (~outside)


def scanlines(sub, n=21):
    ys, xs = np.where(sub)
    if not len(xs):
        return []
    bx0, bx1, by0, by1 = xs.min(), xs.max(), ys.min(), ys.max()
    span = max(1, bx1 - bx0)
    out = []
    for k in range(n):
        yy = by0 + int(round(k * (by1 - by0) / (n - 1)))
        row = np.where(sub[yy])[0]
        if not len(row):
            out.append({"v": round(k / (n - 1), 3), "spans": []})
            continue
        d = np.diff(row)
        breaks = np.where(d > 4)[0]
        parts, st = [], row[0]
        for bi in breaks:
            parts.append([round((int(st) - bx0) / span, 4), round((int(row[bi]) - bx0) / span, 4)])
            st = row[bi + 1]
        parts.append([round((int(st) - bx0) / span, 4), round((int(row[-1]) - bx0) / span, 4)])
        out.append({"v": round(k / (n - 1), 3), "spans": parts})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--outdir", default="_ai_work/inspect")
    ap.add_argument("--regions", default="", help="像素框：front=x0,y0,x1,y1;top=...;left=...")
    ap.add_argument("--dark-threshold", type=int, default=105)
    ap.add_argument("--no-magenta", action="store_true", help="不把品红线条当粗实线")
    ap.add_argument("--zoom", type=int, default=3, help="放大倍数")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        sys.exit(f"[错误] 找不到图片：{src}")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGB")
    w, h = img.size
    arr = np.asarray(img).astype(int)
    print(f"[信息] 原图 {w} x {h}")
    solid = solid_mask(arr, args.dark_threshold, not args.no_magenta)

    img.resize((w * 2, h * 2), Image.LANCZOS).save(outdir / "overview.png")
    mask_rgb = np.full((h, w, 3), 255, np.uint8)
    regions = parse_regions(args.regions, w, h)
    meta = {"image": {"path": str(src.resolve()), "width": w, "height": h}, "regions": {}}
    views_bin = {}   # 裁剪到各自 bbox 的二值掩码，供 auto_reconstruct.py 直接消费

    for name, box in regions.items():
        x0, y0, x1, y1 = box
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        crop = img.crop((x0, y0, x1, y1))
        z = args.zoom
        crop.resize((crop.width * z, crop.height * z), Image.LANCZOS).save(outdir / f"{name}.png")

        raw = solid[y0:y1, x0:x1]
        sub, kept, nblocks = major_components(raw)   # 滤掉文字碎点，保留被内部线分隔的各块
        print(f"[信息] {name}: 保留 {nblocks} 个连通块 / {kept} px，"
              f"滤除 {int(raw.sum()) - kept} px 噪声")
        m = np.zeros_like(solid)
        m[y0:y1, x0:x1] = sub
        if name in COLORS:
            mask_rgb[m] = COLORS[name]

        ys, xs = np.where(sub)
        if not len(xs):
            print(f"[警告] {name} 区域没有实心像素：粗实线可能不闭合，"
                  f"请用 --regions 收紧裁剪框或检查阈值")
            bbox = None
            aspect = None
        else:
            bbox = [int(xs.min() + x0), int(ys.min() + y0),
                    int(xs.max() + x0), int(ys.max() + y0)]
            bw, bh = bbox[2] - bbox[0] + 1, bbox[3] - bbox[1] + 1
            aspect = round(bw / bh, 4)
            print(f"[信息] {name}: bbox={bbox} 宽={bw} 高={bh} 宽高比={aspect} 面积={int(sub.sum())}")
            touched = [side for side, gap in
                       (("左", bbox[0] - x0), ("上", bbox[1] - y0),
                        ("右", x1 - bbox[2]), ("下", y1 - bbox[3])) if gap <= 2]
            if touched:
                print(f"[警告] {name} 实心区域贴到裁剪框 {'/'.join(touched)} 边，"
                      f"可能被截断；用 --regions 放宽该框后重跑")
            views_bin[name] = sub[bbox[1] - y0:bbox[3] - y0 + 1,
                                  bbox[0] - x0:bbox[2] - x0 + 1]

        meta["regions"][name] = {
            "box_px": [x0, y0, x1, y1],
            "bbox_px": bbox,
            "aspect": aspect,
            "area": int(sub.sum()),
            "scanlines": scanlines(sub),
        }

    Image.fromarray(mask_rgb).save(outdir / "mask.png")
    np.savez(outdir / "views.npz", **views_bin)
    (outdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] 输出目录 {outdir.resolve()}")
    print("       front/top/left.png 放大图 · mask.png 三色掩码 · meta.json 归一化轮廓")
    print("       views.npz 三张二值视图（供 auto_reconstruct.py 一键重建）")


if __name__ == "__main__":
    main()
