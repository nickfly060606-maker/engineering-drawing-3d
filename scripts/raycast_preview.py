#!/usr/bin/env python3
"""离线体素光线投射，预览候选相机机位，避免在浏览器里反复试视角。

用法：
  python raycast_preview.py --model _ai_work/volume_model.py \
      --out _ai_work/render_check.png --views "iso,under,front,left"

模型文件同 verify_projection.py；可选额外定义 CENTER=[x,y,z]、RADIUS=数字。
未给 CENTER/RADIUS 时从 GRID 范围自动推算。视角公式与 HTML 模板一致
（D = 4.2 × 包围球半径，FOV 38），因此预览结果就是页面里的实际效果。
"""
import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

VIEW_DIR = {
    "iso":   (-0.70,  0.35,  0.63),
    "front": ( 0.00,  0.00,  1.00),
    "back":  ( 0.00,  0.00, -1.00),
    "top":   ( 0.00,  1.00,  0.001),
    "left":  (-1.00,  0.00,  0.00),
    "right": ( 1.00,  0.00,  0.00),
    "under": (-0.62, -0.38,  0.68),
}


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


def voxelize(mod, res):
    g = mod.GRID
    xs = np.linspace(g["x"][0], g["x"][1], res)
    ys = np.linspace(g["y"][0], g["y"][1], res)
    zs = np.linspace(g["z"][0], g["z"][1], res)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    return xs, ys, zs, np.asarray(mod.inside(X, Y, Z), dtype=bool)


def render(eye, tgt, xs, ys, zs, V, size, steps):
    eye = np.asarray(eye, float)
    tgt = np.asarray(tgt, float)
    fwd = tgt - eye
    fwd /= np.linalg.norm(fwd)
    up = np.array([0., 1., 0.])
    if abs(float(np.dot(fwd, up))) > 0.99:      # 顶视/仰视退化保护
        up = np.array([0., 0., -1.])
    right = np.cross(fwd, up)
    right /= np.linalg.norm(right)
    up2 = np.cross(right, fwd)

    f = 1.0 / np.tan(np.radians(38) / 2)
    p = np.linspace(-1, 1, size)
    PX, PY = np.meshgrid(p, p)
    asp = 1.0
    dirs = (fwd[None, None, :]
            + (PX / asp / f)[:, :, None] * right[None, None, :]
            + (PY / f)[:, :, None] * up2[None, None, :])
    dirs /= np.linalg.norm(dirs, axis=2, keepdims=True)

    hit = np.zeros((size, size), bool)
    depth = np.full((size, size), np.nan)
    nx, ny, nz = len(xs), len(ys), len(zs)
    # 只在形体所在的深度区间步进，避免在空处浪费采样
    dist = float(np.linalg.norm(tgt - eye))
    span = np.array([xs[-1] - xs[0], ys[-1] - ys[0], zs[-1] - zs[0]])
    half = float(np.linalg.norm(span)) / 2
    for t in np.linspace(max(0.05, dist - half * 1.3), dist + half * 1.3, steps):
        q = eye[None, None, :] + dirs * t
        ix = ((q[:, :, 0] - xs[0]) / (xs[-1] - xs[0]) * (nx - 1)).astype(int)
        iy = ((q[:, :, 1] - ys[0]) / (ys[-1] - ys[0]) * (ny - 1)).astype(int)
        iz = ((q[:, :, 2] - zs[0]) / (zs[-1] - zs[0]) * (nz - 1)).astype(int)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny) & (iz >= 0) & (iz < nz)
        val = np.zeros((size, size), bool)
        val[ok] = V[ix[ok], iy[ok], iz[ok]]
        new = val & ~hit
        depth[new] = t
        hit |= val

    cov = float(hit.mean())
    if not hit.any():
        return np.full((size, size), 255, np.uint8), 0.0
    d = np.where(hit, depth, np.nanmax(depth))
    gy, gx = np.gradient(d)
    nrm = np.stack([-gx, -gy, np.ones_like(d)], axis=2)
    nrm /= (np.linalg.norm(nrm, axis=2, keepdims=True) + 1e-9)
    light = np.array([-0.45, 0.72, 0.53])
    light /= np.linalg.norm(light)
    shade = np.clip((nrm * light).sum(axis=2), 0, 1)
    img = 0.32 + 0.62 * shade
    img[~hit] = 1.0
    return (img * 255).astype(np.uint8), cov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="volume_model.py（与 --volume 二选一）")
    ap.add_argument("--volume", help="auto_reconstruct.py 产出的 volume.npz")
    ap.add_argument("--out", default="_ai_work/render_check.png")
    ap.add_argument("--views", default="iso,under,front,left")
    ap.add_argument("--res", type=int, default=80, help="体素分辨率，越大越慢")
    ap.add_argument("--size", type=int, default=360, help="每块图像边长")
    ap.add_argument("--steps", type=int, default=240, help="光线步进数")
    args = ap.parse_args()

    names = [s.strip() for s in args.views.split(",") if s.strip()]
    bad = [n for n in names if n not in VIEW_DIR]
    if bad:
        sys.exit(f"[错误] 未知视角 {bad}；可用：{', '.join(VIEW_DIR)}")
    if not args.model and not args.volume:
        sys.exit("[错误] 需要 --model 或 --volume")

    if args.volume:
        d = np.load(args.volume)
        V = np.asarray(d["V"], bool)
        Hv = float(d["H"])
        nx, ny, nz = V.shape
        xs = np.linspace(-1, 1, nx)
        ys = np.linspace(0, Hv, ny)
        zs = np.linspace(-1, 1, nz)
        center = np.array([0.0, Hv / 2, 0.0])
        radius = float(np.sqrt(1 + (Hv / 2) ** 2) * 1.02)
    else:
        mod = load_model(args.model)
        xs, ys, zs, V = voxelize(mod, args.res)
        g = mod.GRID
        center = np.asarray(getattr(mod, "CENTER", [
            (g["x"][0] + g["x"][1]) / 2,
            (g["y"][0] + g["y"][1]) / 2,
            (g["z"][0] + g["z"][1]) / 2]), float)
        radius = float(getattr(mod, "RADIUS", np.sqrt(sum(
            ((g[k][1] - g[k][0]) / 2) ** 2 for k in "xyz"))))
    print(f"[信息] 体素 {V.shape}，实心 {int(V.sum())}")
    D = radius * 4.2
    print(f"[信息] center={center.round(3).tolist()} radius={radius:.3f} D={D:.3f}")

    S, HEAD, GAP = args.size, 26, 8
    canvas = Image.new("RGB", (len(names) * S + (len(names) + 1) * GAP,
                               S + HEAD + 2 * GAP), (246, 249, 252))
    draw = ImageDraw.Draw(canvas)
    for i, name in enumerate(names):
        c = VIEW_DIR[name]
        eye = center + np.array(c) * D
        if name == "top":
            eye[2] += 0.001
        im, cov = render(eye, center, xs, ys, zs, V, S, args.steps)
        x = GAP + i * (S + GAP)
        draw.text((x + 4, GAP + 4), f"{name}  coverage={cov:.3f}", fill=(40, 60, 80))
        canvas.paste(Image.fromarray(im).convert("RGB"), (x, HEAD + GAP))
        flag = ""
        if cov < 0.02:
            flag = "  ← 几乎看不到形体，检查体素或相机"
        elif cov > 0.80:
            flag = "  ← 太近，可能出画面"
        print(f"[信息] {name:6s} coverage={cov:.3f}{flag}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"[完成] 视角预览 {out.resolve()}")
    print("       切口朝下时务必确认 under 能看见切面；看不到就调 MODEL.views 覆盖机位")


if __name__ == "__main__":
    main()
