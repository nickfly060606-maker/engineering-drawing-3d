#!/usr/bin/env node
/* 网格自检：闭合性 / 体积 / 绕向 / 法线一致性
 *
 * 用法：
 *   node check_mesh.js --model _ai_work/model.js [--expect 4.8625] [--tol 0.01]
 *
 * 检查项：
 *   1. 边界边数 —— 未被两个三角形共用的边。分片建模的接缝（圆柱侧面与端盖
 *      采样角度不同）天然不焊接，所以默认只提示；加 --strict 才当失败
 *   2. 有符号体积 —— 散度定理 Σa·(b×c)/6；为负说明整体绕向朝内
 *   3. 绕向一致性 —— 逐三角形比较「几何法线」与「顶点法线」
 *   4. 退化面 —— 面积≈0 的三角形
 *
 * --expect 给解析体积时，还会做体积比对（相对误差 < --tol 才通过）。
 * 退出码 0 = 全部通过。
 */
const fs = require('fs');
const path = require('path');
const Module = require('module');

/* ---------- 参数 ---------- */
const argv = process.argv.slice(2);
const opt = { tol: 0.01 };
for (let i = 0; i < argv.length; i += 2) {
  const k = argv[i].replace(/^--/, '');
  opt[k] = argv[i + 1];
}
if (!opt.model) {
  console.error('用法：node check_mesh.js --model <model.js> [--expect 体积] [--tol 0.01]');
  process.exit(2);
}

/* ---------- 载入模型 ---------- */
const abs = path.resolve(opt.model);
let src = fs.readFileSync(abs, 'utf8');
/* 模型用了几何工具包（const {...} = __KIT）时，把工具包拼在前面一起求值。
 * 不这么做的话 node 直接报 "__KIT is not defined"，而页面里其实是好的。 */
if (!/\b(?:const|let|var)\s+__KIT\s*=/.test(src) && src.includes('__KIT')) {
  const kit = path.resolve(__dirname, '..', 'assets', 'model_kit.js');
  if (fs.existsSync(kit)) src = fs.readFileSync(kit, 'utf8') + '\n' + src;
}
src += '\nmodule.exports = { MODEL };';
const m = new Module(abs);
try {
  m._compile(src, abs);
} catch (e) {
  console.error(`[错误] 模型片段无法执行：${e.message}`);
  process.exit(2);
}
const MODEL = (m.exports && m.exports.MODEL) || m.exports;
if (!MODEL || !MODEL.meshes || !MODEL.meshes.length) {
  console.error('[错误] 模型里没有 meshes');
  process.exit(2);
}

/* ---------- 逐网格检查 ---------- */
const Q = 1e4;                          // 顶点焊接精度：0.0001
let vol = 0, tris = 0, degen = 0, badWinding = 0, worstDot = 1;
const edgeUse = new Map();
const vertPos = new Map();              // 顶点 key → [x,y,z]，T 形接点判定要用
const rawEdges = [];                    // 三角形边，循环结束后统一切段

for (const [mi, mesh] of MODEL.meshes.entries()) {
  const P = mesh.positions, N = mesh.normals, I = mesh.indices;
  if (!P || !I) continue;
  const welded = mesh.normals && mesh.normals.length === P.length;

  for (let t = 0; t < I.length; t += 3) {
    const ia = I[t], ib = I[t + 1], ic = I[t + 2];
    const a = [P[ia * 3], P[ia * 3 + 1], P[ia * 3 + 2]];
    const b = [P[ib * 3], P[ib * 3 + 1], P[ib * 3 + 2]];
    const c = [P[ic * 3], P[ic * 3 + 1], P[ic * 3 + 2]];
    tris++;
    vol += (a[0] * (b[1] * c[2] - b[2] * c[1])
          - a[1] * (b[0] * c[2] - b[2] * c[0])
          + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6;

    const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    const w = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
    const g = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]];
    const gl = Math.hypot(g[0], g[1], g[2]);
    if (gl < 1e-12) { degen++; continue; }

    if (welded) {
      let sx = 0, sy = 0, sz = 0;
      for (const i0 of [ia, ib, ic]) { sx += N[i0 * 3]; sy += N[i0 * 3 + 1]; sz += N[i0 * 3 + 2]; }
      const sl = Math.hypot(sx, sy, sz) || 1;
      const d = (g[0] * sx + g[1] * sy + g[2] * sz) / (gl * sl);
      if (d < worstDot) worstDot = d;
      if (d < 0) badWinding++;
    }

    // 边界边统计（用于判断封闭性）
    const key = p => `${Math.round(p[0] * Q)},${Math.round(p[1] * Q)},${Math.round(p[2] * Q)}`;
    const ka = key(a), kb = key(b), kc = key(c);
    vertPos.set(ka, a); vertPos.set(kb, b); vertPos.set(kc, c);
    for (const [p, q] of [[ka, kb], [kb, kc], [kc, ka]]) {
      if (p === q) continue;                         // 退化边（极点扇形）跳过
      rawEdges.push([p, q]);                         // 先存，循环结束后统一按 T 形接点切段
    }
  }
  console.log(`  mesh[${mi}] 顶点 ${P.length / 3}  三角形 ${I.length / 3}` +
              (mesh.highlight ? '  （切面·暖色）' : ''));
}

/* ---------- 边界边统计（含 T 形接点修补） ----------
 * 多块拼合时，大面的整条棱会被邻面多个小面的顶点切成几段：大面用「整边」、
 * 小面用「子段」，两者 key 不同，于是整边和每个子段都只被用到 1 次 —— 但几何
 * 上是严丝合缝的，不是裂缝。判据：把每条边先按落在其内部的顶点切成子段再配对，
 * 与 model_kit.js 里 buildMesh 的做法一致。 */
function subSegs(pk, qk) {
  const p = vertPos.get(pk), q = vertPos.get(qk);
  if (!p || !q) return [[pk, qk]];
  const d = [q[0] - p[0], q[1] - p[1], q[2] - p[2]];
  const L2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
  if (L2 < 1e-12) return [[pk, qk]];
  const L = Math.sqrt(L2), tol = 1e-3 * L;            // 点到直线距离容差 = 边长 0.1%
  const ts = [{ t: 0, k: pk }, { t: 1, k: qk }];
  for (const [k, v] of vertPos) {
    if (k === pk || k === qk) continue;
    const w = [v[0] - p[0], v[1] - p[1], v[2] - p[2]];
    const t = (w[0] * d[0] + w[1] * d[1] + w[2] * d[2]) / L2;
    if (t <= 1e-4 || t >= 1 - 1e-4) continue;         // 不在中间
    const cx = w[1] * d[2] - w[2] * d[1];
    const cy = w[2] * d[0] - w[0] * d[2];
    const cz = w[0] * d[1] - w[1] * d[0];
    if (Math.hypot(cx, cy, cz) / L < tol) ts.push({ t, k });
  }
  if (ts.length === 2) return [[pk, qk]];
  ts.sort((x, y) => x.t - y.t);
  const out = [];
  for (let i = 0; i + 1 < ts.length; i++) out.push([ts[i].k, ts[i + 1].k]);
  return out;
}

let patched = 0;
for (const [p, q] of rawEdges) {
  const segs = subSegs(p, q);
  if (segs.length > 1) patched += segs.length - 1;
  for (const [s, t] of segs) {
    const e = s < t ? `${s}|${t}` : `${t}|${s}`;
    const rec = edgeUse.get(e) || { n: 0, dir: 0 };
    rec.n++;
    edgeUse.set(e, rec);
  }
}

/* 边界边：只被 1 个三角形使用的边 */
let openEdges = 0;
for (const rec of edgeUse.values()) if (rec.n === 1) openEdges++;
if (patched) console.log(`[T 形接点] ${patched} 条边被邻面顶点切开，已按子段配对`);

console.log(`总计：三角形 ${tris}  有符号体积 ${vol.toFixed(4)}`);
console.log(`边界边 ${openEdges}（0 = 封闭） 退化面 ${degen}  绕向异常 ${badWinding}  最差点积 ${worstDot.toFixed(3)}`);

let ok = true;
const fail = msg => { console.log(`[失败] ${msg}`); ok = false; };

if (openEdges > 0) {
  const msg = `存在 ${openEdges} 条未共用的边：T 形接点已按子段配对，剩下的是分片接缝或真缺口`
            + `（成片集中在某个面 → 那个面缺失；零散 1–2 条 → 接缝，可忽略）`;
  if (opt.strict !== undefined) fail(msg); else console.log(`[提示] ${msg}`);
}
if (degen > Math.max(4, tris * 0.005)) fail(`退化三角形过多：${degen} / ${tris}`);
else if (degen > 0) console.log(`[提示] ${degen} 个退化三角形（极点扇形常见，不占面积，可忽略）`);
if (badWinding > 0) fail(`${badWinding} 个三角形绕向与顶点法线相反：双面材质下不显形，但明暗是反的`);
if (vol <= 0) fail('总体积为负：所有面片绕向朝内');

if (opt.expect !== undefined) {
  const want = parseFloat(opt.expect);
  const err = Math.abs(vol - want) / Math.abs(want);
  console.log(`体积比对：期望 ${want.toFixed(4)}  实测 ${vol.toFixed(4)}  相对误差 ${(err * 100).toFixed(3)}%`);
  if (err > parseFloat(opt.tol)) fail(`体积偏离期望超过 ${(parseFloat(opt.tol) * 100).toFixed(1)}%`);
}

if (MODEL.edges || MODEL.polylines) {
  console.log(`棱线：${(MODEL.edges || []).length / 6} 段直线 · ${(MODEL.polylines || []).length} 条折线`);
}
// 工具包 finish() 会把剔掉的内面数挂在 MODEL 上：>0 说明走了「多块拼合并集」那条路，
// 为 0 是面清单型（只画真表面），都正常；undefined 说明没用工具包。
if (typeof MODEL.culled === 'number') console.log(`剔内面 ${MODEL.culled} 个（工具包 finish）`);

console.log(ok ? 'MESH_OK' : 'MESH_FAIL');
process.exit(ok ? 0 : 1);
