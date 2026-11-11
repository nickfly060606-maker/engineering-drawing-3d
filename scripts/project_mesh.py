#!/usr/bin/env python
"""把 model.js 的三维网格反投影回**原题截图的像素坐标**，叠加成对照图。

与 verify_projection.py 的区别：
  verify_projection.py 先把「模型反投影」和「原题裁剪」各自拉伸到同一格子里再比，
  一旦两边都有同样的拉伸/偏移，就会给出自洽的假阳性（2026-11-09 真踩过：三视图
  被统一转了 90°，吻合度仍有 0.978）。本脚本用像素标定直接落位，偏移 1 px
  都能看出来，是交付前最后一道防线。

两种标定方式：
  --auto-calib   从网格包围盒 + 原图画线包围盒自动求 S 与原点，并自动试
                 第一角/第三角的 4 种翻转组合，取落位最好的那个。**默认推荐**。
  --calib        "S=..,OX=..,OZ=..,OY=..[,OLX=..]" 手工标定，覆盖自动结果。

输出三格对照图：底色压淡的原题上叠
  红色粗线 = 网格轮廓（silhouette）  橙色细线 = 模型特征棱线（edges/polylines）

用法：
  python project_mesh.py --model _ai_work/model.js --image "<截图>" --auto-calib \\
      --out _ai_work/compare.png
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # 复用「中线包围盒」逻辑，避免两处各写一套


KIT = Path(__file__).resolve().parents[1] / "assets" / "model_kit.js"


def with_kit(src):
    """模型用了几何工具包（`const {...} = __KIT`）时把工具包拼在前面。

    node 求值不经过 build_viewer，不补这一步会报 `__KIT is not defined`。
    """
    if re.search(r"\b(?:const|let|var)\s+__KIT\s*=", src):
        return src
    if "__KIT" in src and KIT.is_file():
        return KIT.read_text(encoding="utf-8") + "\n" + src
    return src


def load_mesh(model_path):
    """在 node 里求值 model.js，取回 positions / indices / 特征棱线（模型是代码生成的）。"""
    path = Path(model_path).resolve()
    src = with_kit(path.read_text(encoding="utf-8"))
    if "module.exports" not in src:
        src += "\nmodule.exports = MODEL;\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        tmp = fh.name
    # 模型片段自己会 console.log（体积自检等），所以走临时文件而不是 stdout
    out_json = tmp + ".json"
    script = (
        "const fs=require('fs'),Module=require('module');"
        f"const m=new Module({json.dumps(tmp)});"
        f"m._compile(fs.readFileSync({json.dumps(tmp)},'utf8'),'model.js');"
        "const M=m.exports,g=(M.meshes||[])[0];"
        "if(!g){console.error('NO_MESH');process.exit(2);}"
        "const all=(M.meshes||[]).map(x=>({p:x.positions,i:x.indices}));"
        f"fs.writeFileSync({json.dumps(out_json)},JSON.stringify({{p:g.positions,i:g.indices,"
        "all:all,e:M.edges||[],pl:M.polylines||[]}));"
    )
    try:
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        if out.returncode != 0:
            sys.exit(f"[错误] node 求值失败：{out.stderr.strip()}")
        d = json.loads(Path(out_json).read_text(encoding="utf-8"))
    finally:
        Path(tmp).unlink(missing_ok=True)
        Path(out_json).unlink(missing_ok=True)
    return (np.asarray(d["p"], float).reshape(-1, 3),
            np.asarray(d["i"], int).reshape(-1, 3),
            [float(v) for v in d.get("e") or []],
            [[[float(c) for c in pt] for pt in pl] for pl in (d.get("pl") or [])])


def raster_tris(px, py, tris, w, h, ss):
    """三角形栅格化（ss 倍超采样后合并），返回 (h, w) 布尔掩码。"""
    cw, ch = w * ss, h * ss
    U = px * ss
    V = py * ss
    m = np.zeros((ch, cw), bool)
    for a, b, c in tris:
        ua, ub, uc = U[a], U[b], U[c]
        va, vb, vc = V[a], V[b], V[c]
        x0 = max(0, int(np.floor(min(ua, ub, uc))))
        x1 = min(cw - 1, int(np.ceil(max(ua, ub, uc))))
        y0 = max(0, int(np.floor(min(va, vb, vc))))
        y1 = min(ch - 1, int(np.ceil(max(va, vb, vc))))
        if x1 < x0 or y1 < y0:
            continue
        den = (vb - vc) * (ua - uc) + (uc - ub) * (va - vc)
        if abs(den) < 1e-9:
            continue
        yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        l1 = ((vb - vc) * (xx - uc) + (uc - ub) * (yy - vc)) / den
        l2 = ((vc - va) * (xx - uc) + (ua - uc) * (yy - vc)) / den
        m[y0:y1 + 1, x0:x1 + 1] |= (l1 >= -1e-6) & (l2 >= -1e-6) & ((1 - l1 - l2) >= -1e-6)
    return m.reshape(h, ss, w, ss).any(axis=(1, 3))


def edge(mask):
    e = np.zeros_like(mask, bool)
    e[:-1, :] |= mask[1:, :] ^ mask[:-1, :]
    e[:, :-1] |= mask[:, 1:] ^ mask[:, :-1]
    return e


def parse_kv(s):
    out = {}
    for part in s.split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = float(v)
    return out


def parse_boxes(s, w, h):
    if not s:
        third = w // 3
        return {"front": (0, 0, third, h), "top": (third, 0, 2 * third, h),
                "left": (2 * third, 0, w, h)}
    out = {}
    for part in s.split(";"):
        name, _, nums = part.partition("=")
        out[name.strip()] = tuple(int(float(v)) for v in nums.split(","))
    return out


def auto_calib(P, boxes_c, draw_mask, force=None):
    """由「网格包围盒 ↔ 原图画线中线包围盒」求标定，并试 4 种翻转取最优。

    返回 (S, cal, score, best_flip)。cal 里 OX/OZ/OY/OLX 可直接喂给 --calib。
    """
    x0m, x1m = float(P[:, 0].min()), float(P[:, 0].max())
    y0m, y1m = float(P[:, 1].min()), float(P[:, 1].max())
    z0m, z1m = float(P[:, 2].min()), float(P[:, 2].max())
    ratios = []
    f, t, l = boxes_c.get("front"), boxes_c.get("top"), boxes_c.get("left")
    if f:
        ratios.append((f[2] - f[0]) / max(1e-9, x1m - x0m))
        ratios.append((f[3] - f[1]) / max(1e-9, y1m - y0m))
    if t:
        ratios.append((t[3] - t[1]) / max(1e-9, z1m - z0m))
    if l:
        ratios.append((l[3] - l[1]) / max(1e-9, y1m - y0m))
        ratios.append((l[2] - l[0]) / max(1e-9, z1m - z0m))
    if not ratios:
        sys.exit("[错误] 没有可用的视图包围盒，无法自动标定")
    S = float(np.median(ratios))
    spread = (max(ratios) - min(ratios)) / S if S else 0
    best = (None, -1.0, None)
    tried = []
    for ft in (False, True):
        for fl in (False, True):
            if not f or (force is not None and (ft, fl) != force):
                continue
            cal = {"S": S, "OX": f[0] - S * x0m, "OZ": f[3] + S * y0m}
            # 四种手性要各自把原点解到「模型 z 的最小值落在该视图的哪一侧」：
            #   top  正向 y=OY+S·z  → z0m 落在 t[1]（俯视图上边）
            #   top  翻转 y=OY−S·z  → z0m 落在 t[3]（下边）
            #   left 正向 x=OLX−S·z → z0m 落在 l[2]（左边视图右边）
            #   left 翻转 x=OLX+S·z → z0m 落在 l[0]（左边）
            # 2026-11-11 修：原来 ft=False 写成 t[1]+S·z1m、fl=False 写成 l[0]+S·z1m，
            # 会把投影推出画面，导致这两种手性永远评低分、auto_calib 只能选同一种。
            if t:
                cal["OY"] = t[3] + S * z0m if ft else t[1] - S * z0m
            if l:
                cal["OLX"] = l[0] - S * z0m if fl else l[2] + S * z0m
            score = _score(P, cal, draw_mask, ft, fl)
            tried.append(((ft, fl), score))
            if score > best[1]:
                best = (cal, score, (ft, fl))
    if len(tried) > 1:
        print("       四种手性得分：" + " · ".join(
            f"{'T' if a else 't'}{'L' if b else 'l'} {sc:.1%}" for (a, b), sc in tried))

    # 消歧：俯视图若是圆（上下对称），top 两种手性得分几乎相同，单看分数会随机选到翻转。
    # 此时按第一角画法取「正向」（z 越大越靠前＝俯视图越靠下）。差 ≥2% 才认为可分辨。
    # left 同理：左右对称的形体（少见）才需要兜底，正常都能分辨。
    if len(tried) == 4 and force is None:
        sc = {(a, b): v for (a, b), v in tried}
        TIE = 0.02
        # 先定 left（在两种 top 下取较大的那个比）
        lf_gain = max(sc[(False, True)], sc[(True, True)]) - max(sc[(False, False)], sc[(True, False)])
        lf = lf_gain > TIE
        # 再定 top（固定 left）
        tf_gain = abs(sc[(True, lf)] - sc[(False, lf)])
        tf = sc[(True, lf)] > sc[(False, lf)] if tf_gain > TIE else False
        if (tf, lf) != best[2]:
            cal, _sc, _f = best
            # 用裁决后的手性重算一次原点，别沿用被否决组合的 OY/OLX
            f, t, l = boxes_c.get("front"), boxes_c.get("top"), boxes_c.get("left")
            cal = {"S": S, "OX": f[0] - S * float(P[:, 0].min()), "OZ": f[3] + S * float(P[:, 1].min())}
            if t:
                cal["OY"] = t[3] + S * float(P[:, 2].min()) if tf else t[1] - S * float(P[:, 2].min())
            if l:
                cal["OLX"] = l[0] - S * float(P[:, 2].min()) if lf else l[2] + S * float(P[:, 2].min())
            new_score = _score(P, cal, draw_mask, tf, lf)
            print(f"       [消歧] top 两种手性只差 {tf_gain:.1%}（<{TIE:.0%}，俯视图近圆对称）"
                  f"→ 按第一角取正向")
            best = (cal, new_score, (tf, lf))
    return S, best[0], best[1], best[2], spread


def _score(P, cal, draw_mask, flip_top, flip_left):
    """落位评分：网格轮廓像素落在原图线条上（±1 px）的比例。"""
    S = cal["S"]
    h, w = draw_mask.shape
    near = measure.dilate3(draw_mask, 1)
    tot = hit = 0
    for name, (px, py) in _view_pts(P, cal, flip_top, flip_left).items():
        # 只取该视图范围内的样本，避免算到别的视图里去
        sel = (px >= 0) & (px < w) & (py >= 0) & (py < h)
        if not sel.any():
            continue
        ix = np.clip(px[sel].astype(int), 0, w - 1)
        iy = np.clip(py[sel].astype(int), 0, h - 1)
        tot += len(ix)
        hit += int(near[iy, ix].sum())
    return hit / max(1, tot)


def _view_pts(P, cal, flip_top=False, flip_left=False):
    S = cal["S"]
    x, y, z = P[:, 0], P[:, 1], P[:, 2]
    views = {"front": (cal["OX"] + S * x, cal["OZ"] - S * y)}
    if "OY" in cal:
        views["top"] = (cal["OX"] + S * x,
                        (cal["OY"] - S * z) if flip_top else (cal["OY"] + S * z))
    if "OLX" in cal:
        views["left"] = ((cal["OLX"] + S * z) if flip_left else (cal["OLX"] - S * z),
                         cal["OZ"] - S * y)
    return views


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="model.js（代码生成几何亦可）")
    ap.add_argument("--image", required=True, help="原三视图截图")
    ap.add_argument("--calib", default="", help="S=..,OX=..,OZ=..,OY=..[,OLX=..] 手工标定")
    ap.add_argument("--auto-calib", action="store_true",
                    help="按网格包围盒自动标定（推荐；会顺带判定第一角/第三角）")
    ap.add_argument("--boxes", default="", help="拼图裁切框 name=x0,y0,x1,y1;...")
    ap.add_argument("--out", default="_ai_work/compare_mesh.png")
    ap.add_argument("--ss", type=int, default=2, help="超采样倍数")
    ap.add_argument("--no-left", action="store_true", help="原题没有左视图")
    ap.add_argument("--no-features", action="store_true", help="只画轮廓，不画特征棱线")
    ap.add_argument("--flip", default="",
                    help="强制投影手性，如 top=1,left=1（1=翻转）；不填则四种组合自动择优")
    args = ap.parse_args()

    src = Image.open(args.image).convert("RGB")
    W, H = src.size
    arr = np.asarray(src).astype(int)
    draw_mask = measure.line_mask(arr)

    P, I, edges, polylines = load_mesh(args.model)

    flip_top = flip_left = False
    if args.calib:
        cal = parse_kv(args.calib)
        if not {"S", "OX", "OZ"} <= set(cal):
            sys.exit(f"[错误] --calib 缺参数：{sorted({'S','OX','OZ'} - set(cal))}")
    elif args.auto_calib:
        boxes_c = {}
        # --boxes 必须参与标定：选择题拼图（主视左上、俯视左下、左视是选项 A/B）
        # 这类非标准布局，按默认三等分切出来的框全是错的，
        # 会得到 S 偏大、离散度 70% 这种结果（2026-11-10 第 6 题真踩过）。
        src_boxes = (parse_boxes(args.boxes, W, H) if args.boxes
                     else measure.parse_regions(None, W, H))
        for name, box in src_boxes.items():
            res = measure.bbox_centerline(draw_mask, box)
            if res:
                boxes_c[name] = res
        force = None
        if args.flip:
            force = (("top=1" in args.flip.replace(" ", "")),
                     ("left=1" in args.flip.replace(" ", "")))
        S, cal, score, flip, spread = auto_calib(P, boxes_c, draw_mask, force)
        if cal is None:
            sys.exit("[错误] 自动标定失败：没有有效视图")
        flip_top, flip_left = flip
        print(f"[标定] S={cal['S']:.4f} px/单位  各视图比例离散度 {spread * 100:.2f}%"
              f"（>3% 说明模型尺寸与图不符）")
        print(f"       OX={cal['OX']:.1f} OZ={cal['OZ']:.1f} "
              f"OY={cal.get('OY', float('nan')):.1f} OLX={cal.get('OLX', float('nan')):.1f}")
        print(f"       投影手性：top{'翻转' if flip_top else '正向'} / "
              f"left{'翻转' if flip_left else '正向'} · 落位命中率 {score:.1%}")
    else:
        sys.exit("[错误] 需要 --auto-calib 或 --calib 之一")

    S = cal["S"]
    views = _view_pts(P, cal, flip_top, flip_left)
    tris = [(int(a), int(b), int(c)) for a, b, c in I]
    masks = {k: raster_tris(px, py, tris, W, H, args.ss) for k, (px, py) in views.items()}

    base = src.point(lambda v: int(120 + v * 0.52))
    arr = np.asarray(base).copy()
    for m in masks.values():
        arr[edge(m)] = (225, 25, 25)
    out_img = Image.fromarray(arr)
    dr = ImageDraw.Draw(out_img)

    # 特征棱线（edges / polylines）单独投影：虚线表示的内部结构（孔、槽）
    # 在轮廓里根本不存在，只有把模型自己声明的棱线投上去才能核对那些线
    if not args.no_features:
        E = np.asarray(edges, float).reshape(-1, 3) if len(edges) >= 6 else np.zeros((0, 3))
        lines = []
        if len(E):
            for i in range(0, len(E) - 1, 2):
                lines.append((E[i], E[i + 1]))
        for pl in polylines:
            for i in range(len(pl) - 1):
                lines.append((np.asarray(pl[i], float), np.asarray(pl[i + 1], float)))
        def proj(pt, name):
            x, y, z = pt
            if name == "front":
                return (cal["OX"] + S * x, cal["OZ"] - S * y)
            if name == "top":
                return (cal["OX"] + S * x,
                        (cal["OY"] - S * z) if flip_top else (cal["OY"] + S * z))
            return ((cal["OLX"] + S * z) if flip_left else (cal["OLX"] - S * z),
                    cal["OZ"] - S * y)
        for name in views:
            for a, b in lines:
                pa, pb = proj(a, name), proj(b, name)
                if max(pa[0], pb[0]) < -50 or min(pa[0], pb[0]) > W + 50:
                    continue
                dr.line([pa, pb], fill=(255, 150, 40), width=1)

    if args.boxes:
        boxes = {k: v for k, v in parse_boxes(args.boxes, W, H).items() if k in views}
    else:
        # 默认按三视图分区裁切（和 measure/inspect 用同一套划分），
        # 之前用「三等分」在非等宽布局下会切到隔壁视图
        boxes = {k: tuple(int(v) for v in box)
                 for k, box in measure.parse_regions(None, W, H).items() if k in views}
        boxes = {k: v for k, v in boxes.items() if k in views}

    pad, head = 10, 22
    tiles = [(k, out_img.crop(b)) for k, b in boxes.items()]
    tw = max(t.width for _, t in tiles)
    th = max(t.height for _, t in tiles)
    canvas = Image.new("RGB", (pad + (tw + pad) * len(tiles), head + th + pad * 2),
                       (250, 250, 252))
    dr2 = ImageDraw.Draw(canvas)
    for i, (k, im) in enumerate(tiles):
        cx = pad + i * (tw + pad)
        canvas.paste(im, (cx, head))
        dr2.text((cx, 6), k.upper(), fill=(40, 60, 80))
    canvas = canvas.resize((canvas.width * 2, canvas.height * 2), Image.LANCZOS)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.out)
    print(f"[完成] {args.out}")
    print(f"       视图 {list(masks)} · 三角形 {len(tris)} · "
          f"特征棱线 {len(edges) // 6 + sum(len(p) - 1 for p in polylines)} 段")
    print("       红=网格轮廓 橙=模型特征棱线；红线离开描边就是几何错了，不要调渲染去凑")


if __name__ == "__main__":
    main()
