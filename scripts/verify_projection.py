#!/usr/bin/env python3
"""把体素模型反投影成三视图，与原题截图并排生成对照图。

用法：
  python verify_projection.py --model _ai_work/volume_model.py \
      --image <截图> --out _ai_work/compare.png

模型文件只需定义（坐标：y 竖直，x 主视水平，z 深度）：
  GRID = dict(x=(lo,hi,n), y=(lo,hi,n), z=(lo,hi,n))
  def inside(X, Y, Z):  -> 布尔 numpy 数组（与 X/Y/Z 同形状）

对照图上排为原题、下排为模型反投影。形状不吻合说明形体解错了，
应修改 inside，而不是去改三维渲染。
"""
import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

CROPS = {
    "front": (0.055, 0.055, 0.457, 0.502),
    "top":   (0.055, 0.520, 0.466, 0.922),
    "left":  (0.503, 0.073, 0.896, 0.502),
}
LBL = {"front": "FRONT", "top": "TOP", "left": "LEFT"}


def load_model(path):
    p = Path(path)
    if not p.is_file():
        sys.exit(f"[错误] 找不到模型文件：{p}")
    spec = importlib.util.spec_from_file_location("volume_model", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "GRID") or not hasattr(mod, "inside"):
        sys.exit("[错误] 模型文件必须定义 GRID 和 inside(X,Y,Z)")
    return mod


def volume(mod):
    g = mod.GRID
    xs = np.linspace(*g["x"])
    ys = np.linspace(*g["y"])
    zs = np.linspace(*g["z"])
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    V = np.asarray(mod.inside(X, Y, Z), dtype=bool)
    if V.shape != X.shape:
        sys.exit(f"[错误] inside 返回形状 {V.shape}，应为 {X.shape}")
    print(f"[信息] 体素 {V.shape}，实心 {int(V.sum())} ({V.mean() * 100:.1f}%)")
    return V


def projections(V):
    # V 形状 (nx, ny, nz)
    front = V.any(axis=2)   # (x, y)  → 图像 (y, x)
    top = V.any(axis=1)     # (x, z)  → 图像 (z, x)
    left = V.any(axis=0)    # (y, z)  → 图像 (z, y)
    return {
        "front": np.flipud(front.T),
        "top":   np.flipud(top.T),
        "left":  np.flipud(left.T),
    }


def ratio(m):
    ys, xs = np.where(m)
    if not len(xs):
        return None
    w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    return round(w / h, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default="_ai_work/compare.png")
    ap.add_argument("--crops", default="", help="覆盖默认裁剪：front=x0,y0,x1,y1;...（像素或 0-1 小数）")
    ap.add_argument("--cell", type=int, default=320, help="每格边长")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        sys.exit(f"[错误] 找不到原图：{src}")
    img = Image.open(src).convert("RGB")
    w, h = img.size
    proj = projections(volume(load_model(args.model)))

    crops = dict(CROPS)
    for part in filter(None, args.crops.split(";")):
        name, box = part.split("=", 1)
        crops[name.strip()] = tuple(float(v) for v in box.split(","))

    C, PAD, HEAD = args.cell, 12, 26
    canvas = Image.new("RGB", (3 * C + 4 * PAD, 2 * (C + HEAD) + 3 * PAD), (246, 249, 252))
    draw = ImageDraw.Draw(canvas)

    for col, name in enumerate(("front", "top", "left")):
        box = crops[name]
        if max(box) <= 1.0:
            box = (box[0] * w, box[1] * h, box[2] * w, box[3] * h)
        x0, y0 = PAD + col * (C + PAD), PAD + HEAD
        draw.text((x0, PAD + 4), f"{LBL[name]} - original", fill=(40, 60, 80))
        canvas.paste(img.crop(tuple(int(v) for v in box)).resize((C, C), Image.LANCZOS), (x0, y0))

        m = proj[name]
        print(f"[信息] {name} 反投影宽高比 = {ratio(m)}")
        gray = Image.fromarray(np.where(m, 40, 255).astype(np.uint8), "L")
        y1 = PAD + HEAD + C + HEAD + PAD
        draw.text((x0, PAD + HEAD + C + PAD + 4), f"{LBL[name]} - reconstructed", fill=(40, 60, 80))
        canvas.paste(gray.resize((C, C), Image.NEAREST).convert("RGB"), (x0, y1))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"[完成] 对照图 {out.resolve()}")
    print("       用图像查看工具逐列核对：轮廓、缺口、斜面、内部线段是否一致")


if __name__ == "__main__":
    main()
