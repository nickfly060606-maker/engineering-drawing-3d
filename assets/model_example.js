/* ============================================================
 * 起手模板：被斜面切过、顶面开一条通槽的方块
 * （结构同 2026-11-10 第 6 题，已通过反投影与体积校验）
 *
 * 用法：复制到 _ai_work/model.js，改尺寸参数即可。
 * 页面里 __KIT 由 build_viewer.py 自动注入，不用 import、不用复制任何样板。
 *
 * ---------- MODEL 契约 ----------
 *   center  [x,y,z]  相机注视点（finish 按包围盒自动给）
 *   radius  number   包围球半径（模板据此算视距 D = 4.2*radius）
 *   views   object   可选，覆盖默认机位。值是**相机位置数组** [x,y,z]，
 *                    注视点固定 center —— 写成 {pos,target} 会白屏
 *   meshes  []       { positions, indices, normals?, highlight? }
 *   edges   []       扁平数值数组 [x1,y1,z1,x2,y2,z2, ...]
 *   polylines[]      可选，[[x,y,z],...]
 *
 * ---------- 坐标系 ----------
 *   y 竖直向上，x 主视图水平向右，z 深度（0 = 前面）。
 *   尺寸直接用步骤 1 量到的像素数（或除以 10 的"单位"），量纲自定，只要一致。
 * ============================================================ */
const MODEL = (function () {
  const { hull, subtract, finish } = __KIT;

  /* ---------- 由三视图反解的几何参数 ---------- */
  const L = 165, H = 131, DP = 114;      // 长 高 深
  const A0 = 19.75, B0 = 0.30;           // 斜台阶面 y = A0 + B0·x
  const ZC0 = 99.0, MM = 0.50;           // 竖直斜切面 z = ZC0 − MM·x
  const XLT = 29.5, XRT = 134.5;         // 槽口两棱
  const XLF = 50.5, XRF = 114.5;         // 槽底两棱
  const HY = 85.5;                       // 槽底高度

  /* ---------- 半空间写法：[nx,ny,nz,d] 表示 n·p ≤ d ---------- */
  const nrm = (a, b) => { const t = Math.hypot(a, b); return [a / t, b / t]; };
  const neg = p => [-p[0], -p[1], -p[2], -p[3]];
  const wl = nrm(H - HY, XLF - XLT);                  // 槽左壁法线
  const wr = nrm(H - HY, XRF - XRT);                  // 槽右壁法线

  const BASE = [
    [-1, 0, 0, 0], [1, 0, 0, L],        // x ≥ 0, x ≤ L
    [0, -1, 0, 0], [0, 1, 0, H],        // y ≥ 0, y ≤ H
    [0, 0, -1, 0], [0, 0, 1, DP],       // z ≥ 0, z ≤ DP
  ];
  const HS = [-B0, 1, 0, A0];           // y ≤ 19.75+0.3x   （斜台阶面）
  const ZC = [-MM, 0, -1, -ZC0];        // z ≥ 99−0.5x      （竖直斜切面）
  const FL = [0, 1, 0, HY], UP = neg(FL);     // 槽底：材料在 y ≤ HY
  const WL = [wl[0], wl[1], 0, wl[0] * XLT + wl[1] * H];
  const WR = [-wr[0], -wr[1], 0, -(wr[0] * XRF + wr[1] * HY)];

  /* 形体 = 「台阶面以下」∪「台阶面以上且斜切面之后」，再从后半段挖掉梯形通槽。
   *
   * 用 `subtract(base, cuts)` 表达"挖掉"：cuts 里每个平面写**要削掉的那一侧**，
   * 它会自动按平面正反组合切成 2^k-1 块凸体，并天然满足「拆彻底」——
   * 每块都被所有与它相交的平面切过，所以每个面要么整块露在表面、要么整块埋进
   * 邻块，finish 剔内面时不会切出半露半埋的假线。手写这 7 块时漏切过一刀，
   * 体积偏了 14.5%（见 SKILL.md 硬规则 30）。 */
  const cells = [
    hull([...BASE, HS, neg(ZC)], { hl: [HS] }),                  // 台阶面以下
    hull([...BASE, HS, ZC], { hl: [HS] }),
    ...subtract([...BASE, neg(HS), ZC], [UP, neg(WL), neg(WR)], { hl: [ZC] }),
  ];

  /* 圆孔 / 凸台 —— 需要时按下面几行加：
   *
   *   const CY = 65, CZ = 57, R = 18;                  // 孔心、半径
   *   body.add(plate({ axis:'z', w:0, u0:0, u1:L, v0:0, v1:H,
   *                    hole:{u:CY, v:CZ, r:R}, seg:64 }));   // 孔完全在面内
   *   body.add(sector({ axis:'z', c1:CY, c2:CZ, rOut:R, w0:0, w1:DP,
   *                     inward:true, seg:64 }));             // 孔壁（法线朝里）
   *
   * 孔心正好落在面的边上（半圆孔口）plate 干不了，改用 fan：
   *   fan({ axis:'x', w:XB, c:[Z1,YC], r:R, a0:Math.PI/2, a1:Math.PI*1.5,
   *         seg:64, outer:[[Z1,YC-GA],[Z1,YC+GA],[ZF,YC+GB],[ZF,YC-GB]] })
   * 凸台（半圆柱 + 通孔）用 sector 的 rIn/rOut + a0/a1 取半圈、wall:true 只出壁。
   * 要换掉 hull 的某个端面时，给 hull 传 { skip:[那个平面] } 再 add 上面的面。
   * 完整例子见第 5 题：_ai_work 里 q5（半圆凸台 + 通孔 + 倒梯形槽）。
   */

  return finish(cells);
})();
