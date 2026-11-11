/* ============================================================
 * model_kit.js —— 几何工具包（由 build_viewer.py 自动注入到页面里）
 *
 * 模型文件里一行取用：
 *   const { hull, box, extrude, sector, plate, fan, subtract, finish } = __KIT;
 *
 * 为什么要有它：三视图反解的形体，99% 是「长方体被若干平面切」+
 * 「沿某方向等截面的槽/凸台」+「圆孔」。手写这些面片每道题要 200~300 行
 * 顶点运算，且绕向/耳切/接缝全靠人盯。这里把操作做成声明式：
 *
 *   hull([...半空间])       凸体 = 一堆平面的交集（方块、楔形、被切过的块）
 *   subtract(凸体, [切割面]) 从凸体里削掉一组半空间的交集（槽、切口、台阶缺口）
 *   box(x0,x1,y0,y1,z0,z1)  轴对齐长方体
 *   extrude(截面, 轴, a, b)  等截面拉伸（梯形槽、L 形、任意多边形柱）
 *   sector({...})           圆柱/半圆柱/圆管/扇环；wall:true 只出壁，inward:true 只出孔壁
 *   plate({...})            带圆孔的矩形面（孔完全在面内时用）
 *   fan({...})              扇形面（孔心落在面的边上：半圆孔口、房子形端面）
 *   finish([...sols])       合并 + 逐块定绕向 + 剔内面 + 按二面角自动提棱线
 *
 * 全部返回 Solid：{ P:[x,y,z,...], I:[...], IH:[...] }
 *   I  = 普通面的三角形索引；IH = 高亮面（被切出来的面，暖色）
 *
 * 半空间写法：[nx, ny, nz, d]  表示 nx*x + ny*y + nz*z <= d
 *   例：x <= 27   → [1,0,0,27]
 *       z >= 0    → [0,0,-1,0]
 *       斜切面 z <= 99-0.5x → [0.4472,0,0.8944,88.54]   （法线要归一化，d 同除）
 *
 * 绕向约定：hull/box/extrude/sector(闭合) 自动朝外；plate/fan 默认法线朝 +axis
 * （flip:true 反过来）；sector 的 wall/inward 只出壁、不封端，也没有有符号体积，
 * finish 不会去翻它们。
 * ============================================================ */

const __KIT = (function () {
  const TOL = 1e-7;

  /* ---------------- 向量 ---------------- */
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const crs = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const len = a => Math.hypot(a[0], a[1], a[2]);
  const uni = a => { const L = len(a) || 1; return [a[0] / L, a[1] / L, a[2] / L]; };

  /* ---------------- 多边形三角化（耳切） ----------------
   * 三个必踩的坑已写进实现：
   *   1) 先投影到平面坐标系再判凸凹，直接拿 3D 坐标判会算错符号；
   *   2) 先剔除共线点，否则凹多边形会卡在「剩余 4 点未收敛」；
   *   3) 耳判定必须测「其余所有点是否落在三角形内」，只看本顶点是凸角不够。
   * 顶点顺序按调用方给的绕向保持，法线由绕向决定（右手法）。 */
  function newell(poly) {
    let nx = 0, ny = 0, nz = 0;
    for (let i = 0; i < poly.length; i++) {
      const a = poly[i], b = poly[(i + 1) % poly.length];
      nx += (a[1] - b[1]) * (a[2] + b[2]);
      ny += (a[2] - b[2]) * (a[0] + b[0]);
      nz += (a[0] - b[0]) * (a[1] + b[1]);
    }
    return [nx, ny, nz];
  }

  function basis(n) {
    const ax = Math.abs(n[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
    let u = crs(ax, n);
    u = uni(u);
    return [u, crs(n, u)];
  }

  function triangulate(poly) {
    const n = poly.length;
    if (n < 3) return [];
    const nr = newell(poly);
    if (len(nr) < 1e-12) return [];
    const [U, V] = basis(uni(nr));
    const P2 = poly.map(p => [dot(p, U), dot(p, V)]);
    const cr = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    let idx = [];
    for (let i = 0; i < n; i++) {
      const a = P2[(i - 1 + n) % n], b = P2[i], c = P2[(i + 1) % n];
      if (Math.abs(cr(a, b, c)) > 1e-9) idx.push(i);        // 去共线点
    }
    if (idx.length < 3) return [];
    let A = 0;
    for (let i = 0; i < idx.length; i++) {
      const a = P2[idx[i]], b = P2[idx[(i + 1) % idx.length]];
      A += a[0] * b[1] - b[0] * a[1];
    }
    if (A < 0) idx.reverse();                                // 统一成逆时针
    const inTri = (p, a, b, c) => cr(a, b, p) >= -1e-12 && cr(b, c, p) >= -1e-12 && cr(c, a, p) >= -1e-12;
    const out = [];
    let guard = 0;
    while (idx.length > 3 && guard++ < 20000) {
      let cut = false;
      for (let i = 0; i < idx.length; i++) {
        const ia = idx[(i - 1 + idx.length) % idx.length], ib = idx[i], ic = idx[(i + 1) % idx.length];
        const a = P2[ia], b = P2[ib], c = P2[ic];
        if (cr(a, b, c) <= 1e-12) continue;
        let ok = true;
        for (const j of idx) {
          if (j === ia || j === ib || j === ic) continue;
          if (inTri(P2[j], a, b, c)) { ok = false; break; }
        }
        if (!ok) continue;
        out.push([ia, ib, ic]);
        idx.splice(i, 1);
        cut = true;
        break;
      }
      if (!cut) break;
    }
    if (idx.length === 3) out.push([idx[0], idx[1], idx[2]]);
    return out;
  }

  /* ---------------- Solid ---------------- */
  function Solid() { this.P = []; this.I = []; this.IH = []; }

  Solid.prototype.face = function (poly, hl) {
    if (!poly || poly.length < 3) return this;
    const q = [];
    for (const p of poly) {
      const last = q[q.length - 1];
      if (last && Math.abs(p[0] - last[0]) < 1e-6 && Math.abs(p[1] - last[1]) < 1e-6
          && Math.abs(p[2] - last[2]) < 1e-6) continue;
      q.push([p[0], p[1], p[2]]);
    }
    while (q.length > 1 &&
           Math.abs(q[0][0] - q[q.length - 1][0]) < 1e-6 &&
           Math.abs(q[0][1] - q[q.length - 1][1]) < 1e-6 &&
           Math.abs(q[0][2] - q[q.length - 1][2]) < 1e-6) q.pop();
    if (q.length < 3) return this;
    const tri = triangulate(q);
    if (!tri.length) return this;
    const b = this.P.length / 3;
    for (const p of q) this.P.push(p[0], p[1], p[2]);
    const T = hl ? this.IH : this.I;
    for (const t of tri) T.push(b + t[0], b + t[1], b + t[2]);
    return this;
  };

  Solid.prototype.edge = function (a, b) {
    (this.extra = this.extra || []).push(a[0], a[1], a[2], b[0], b[1], b[2]);
    return this;
  };

  Solid.prototype.add = function (o) {
    const b = this.P.length / 3;
    for (const v of o.P) this.P.push(v);
    for (const v of o.I) this.I.push(b + v);
    for (const v of o.IH) this.IH.push(b + v);
    if (o.extra) this.extra = (this.extra || []).concat(o.extra);
    this.pure = false;      // 拼过东西的块不再是「纯半空间」，不能用来判内外
    return this;
  };

  Solid.prototype.poly = function (pts) {
    for (let i = 1; i < pts.length; i++) this.edge(pts[i - 1], pts[i]);
    return this;
  };

  Solid.prototype.vol = function () { return signedVolume(this.P, this.I.concat(this.IH)); };

  Solid.prototype.flip = function () {
    const sw = A => { for (let t = 0; t < A.length; t += 3) { const k = A[t + 1]; A[t + 1] = A[t + 2]; A[t + 2] = k; } };
    sw(this.I); sw(this.IH);
    return this;
  };

  function signedVolume(P, I) {
    let v = 0;
    for (let t = 0; t < I.length; t += 3) {
      const a = I[t] * 3, b = I[t + 1] * 3, c = I[t + 2] * 3;
      v += (P[a] * (P[b + 1] * P[c + 2] - P[b + 2] * P[c + 1])
          - P[a + 1] * (P[b] * P[c + 2] - P[b + 2] * P[c])
          + P[a + 2] * (P[b] * P[c + 1] - P[b + 1] * P[c])) / 6;
    }
    return v;
  }

  /* ---------------- 半空间裁剪 ---------------- */
  function clipHalf(poly, n, d, eps) {
    const out = [];
    const m = poly.length;
    let anyIn = false, anyOut = false;
    for (let i = 0; i < m; i++) {
      const da = dot(n, poly[i]) - d;
      if (da <= eps) anyIn = true; else anyOut = true;
    }
    if (!anyOut) return poly.slice();          // 全在内：原样返回
    if (!anyIn) return [];                     // 全在外
    for (let i = 0; i < m; i++) {
      const a = poly[i], b = poly[(i + 1) % m];
      const da = dot(n, a) - d, db = dot(n, b) - d;
      if (da <= eps) out.push(a);
      if ((da > eps && db < -eps) || (da < -eps && db > eps)) {
        const t = da / (da - db);
        out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]);
      }
    }
    return out;
  }

  /* ---------------- hull：凸体 = 半空间交集 ----------------
   * 对每个平面，取一个足够大的方片，被其余全部平面裁一遍，剩下的就是该平面上的面。
   * 方块、楔形、被一刀/两刀切过的块、任意凸多面体都是同一句 hull([...])。
   * opt.hl = [平面1, 平面2] 把某几个平面切出来的面标成暖色（被削掉的斜面）。
   *           按数组对象本身识别，不用记下标 —— hull([... , HS, ZC], {hl:[HS, ZC]})
   * opt.hlAll = true 整块暖色 */
  function hull(planes, opt) {
    opt = opt || {};
    const hlSet = new Set(opt.hl || []);
    const skipSet = new Set(opt.skip || []);
    const Pl = planes.map(p => {
      const n = [p[0], p[1], p[2]];
      const L = len(n) || 1;
      return { n: [n[0] / L, n[1] / L, n[2] / L], d: p[3] / L };
    });
    let rmax = 1;
    for (const p of Pl) rmax = Math.max(rmax, Math.abs(p.d));
    const R = rmax * 6 + 8;
    const eps = 1e-7 * R;
    const S = new Solid();
    for (let i = 0; i < Pl.length; i++) {
      const n = Pl[i].n, d = Pl[i].d;
      const [u, v] = basis(n);
      const c = [n[0] * d, n[1] * d, n[2] * d];
      const at = (a, b) => [c[0] + u[0] * a + v[0] * b,
                            c[1] + u[1] * a + v[1] * b,
                            c[2] + u[2] * a + v[2] * b];
      let poly = [at(-R, -R), at(R, -R), at(R, R), at(-R, R)];
      for (let j = 0; j < Pl.length && poly.length >= 3; j++) {
        if (j === i) continue;
        poly = clipHalf(poly, Pl[j].n, Pl[j].d, eps);
      }
      if (poly.length >= 3 && !skipSet.has(planes[i])) {
        S.face(poly, opt.hlAll || hlSet.has(planes[i]));
      }
    }
    S.planes = Pl;          // 点是否在块内 = 是否同时满足全部半空间
    S.pure = true;
    return S;
  }

  function box(x0, x1, y0, y1, z0, z1) {
    return hull([
      [1, 0, 0, x1], [-1, 0, 0, -x0],
      [0, 1, 0, y1], [0, -1, 0, -y0],
      [0, 0, 1, z1], [0, 0, -1, -z0]
    ]);
  }

  /* ---------------- subtract：用一组半空间把凸体削出缺口 ----------------
   *   subtract(BASE, [槽底, 槽壁1, 槽壁2], { hl:[被切出的面] })
   *
   * 手册里最容易错的一步是"拆彻底"：并集拼块时，每块都必须被所有与它相交的平面
   * 切过，否则某个面一半露在外表面、一半埋在邻块里，剔内面时整块删或整块留都不对
   * —— 接缝假线会爬上可见面，体积也一起错。这里直接按每个切割平面的正反组合
   * 切成 2^k 块，丢掉"全部落在被削侧"的那一块：剩下的并集 = base 减去交集，
   * 而且天生就是拆彻底的。k 个平面 → 2^k-1 个 hull，k≤4 时毫秒级。
   *
   * cuts 里每个平面写的是**要削掉的那一侧**（n·p ≤ d）。
   * 削不到东西的平面只会让块数翻倍、不产生几何（空 hull 不出面），无害。 */
  function subtract(base, cuts, opt) {
    const n = cuts.length, out = [];
    for (let m = 0; m < (1 << n) - 1; m++) {      // 最后一块（全被削）跳过
      const planes = base.slice();
      for (let i = 0; i < n; i++) {
        const p = cuts[i];
        planes.push(m & (1 << i) ? p : [-p[0], -p[1], -p[2], -p[3]]);
      }
      out.push(hull(planes, opt));
    }
    return out;
  }

  /* ---------------- extrude：等截面拉伸 ----------------
   * profile: [[u,v],...] 截面多边形（逆时针看 +轴）
   * axis: 'x' | 'y' | 'z'，截面平面依次是 (y,z) / (x,z) / (x,y)
   * a, b: 拉伸的两端坐标
   * opt.capA / opt.capB = false 时不封端（要把端面换成带孔的面时用） */
  function extrude(profile, axis, a, b, opt) {
    opt = opt || {};
    const S = new Solid();
    const put = (p, w) => axis === 'z' ? [p[0], p[1], w]
                      : axis === 'y' ? [p[0], w, p[1]]
                      : [w, p[0], p[1]];
    const n = profile.length;
    // 侧面
    for (let i = 0; i < n; i++) {
      const p = profile[i], q = profile[(i + 1) % n];
      S.face([put(p, a), put(q, a), put(q, b), put(p, b)], opt.hl);
    }
    if (opt.capA !== false) S.face(profile.slice().reverse().map(p => put(p, a)), opt.hl);
    if (opt.capB !== false) S.face(profile.map(p => put(p, b)), opt.hl);
    // 'y'/'x' 轴的截面坐标系与拉伸方向是左手系，截面按逆时针给会整体朝内。
    // 封端完整时直接按体积符号纠正，省得每个模型都踩一次。
    if (opt.capA !== false && opt.capB !== false && S.vol() < 0) S.flip();
    return S;
  }

  /* ---------------- sector：圆柱 / 圆管 / 扇环 ----------------
   * {axis:'y', c1:cx, c2:cz, rIn:0, rOut:5, w0, w1, a0:0, a1:Math.PI, seg:64,
   *  cap0:true, cap1:true, hl:false}
   * axis 决定 w 是哪个坐标，c1/c2 是截面上另两个坐标：
   *   'y' → 截面 (x,z)，c1=x, c2=z
   *   'z' → 截面 (x,y)，c1=x, c2=y
   *   'x' → 截面 (y,z)，c1=y, c2=z
   * rIn=0 就是实心圆柱；a1-a0=2π 就是整圆；留一半就是半圆柱。
   * 通孔不要单独"减料"，直接把孔壁当 rIn 的圆管画出来即可。 */
  function sector(o) {
    o = Object.assign({ rIn: 0, a0: 0, a1: Math.PI * 2, seg: 64, cap0: true, cap1: true }, o);
    const S = new Solid();
    const put = (r, t, w) => {
      const c = [o.c1 + r * Math.cos(t), o.c2 + r * Math.sin(t)];
      return o.axis === 'z' ? [c[0], c[1], w]
           : o.axis === 'y' ? [c[0], w, c[1]]
           : [w, c[0], c[1]];
    };
    const full = Math.abs((o.a1 - o.a0) - Math.PI * 2) < 1e-6;
    const seg = Math.max(6, o.seg);
    const t0 = o.a0;
    const dt = (o.a1 - o.a0) / seg;
    const hasHole = o.rIn > 1e-9;

    if (o.inward || o.wall) {
      /* 只画圆柱壁：不封端、不出径向端面（端面由 plate / fan 铺，或本来就是内部面）。
       *   inward:true → 法线朝轴线，通孔内壁用
       *   wall:true   → 外壁朝外；rIn>0 时再补内壁朝里（凸台那圈管壁用） */
      for (let i = 0; i < seg; i++) {
        const ta = t0 + dt * i, tb = t0 + dt * (i + 1);
        if (o.inward) {
          S.face([put(o.rOut, ta, o.w0), put(o.rOut, ta, o.w1),
                  put(o.rOut, tb, o.w1), put(o.rOut, tb, o.w0)], o.hl);
        } else {
          S.face([put(o.rOut, ta, o.w0), put(o.rOut, tb, o.w0),
                  put(o.rOut, tb, o.w1), put(o.rOut, ta, o.w1)], o.hl);
          if (hasHole) {
            S.face([put(o.rIn, ta, o.w0), put(o.rIn, ta, o.w1),
                    put(o.rIn, tb, o.w1), put(o.rIn, tb, o.w0)], o.hl);
          }
        }
      }
      S.open = true;         // 开放曲面，有符号体积无意义
      return S;
    }
    for (let i = 0; i < seg; i++) {
      const ta = t0 + dt * i, tb = t0 + dt * (i + 1);
      // 外壁（朝外）
      S.face([put(o.rOut, ta, o.w0), put(o.rOut, tb, o.w0),
              put(o.rOut, tb, o.w1), put(o.rOut, ta, o.w1)], o.hl);
      // 内壁（朝内 → 反过来绕）
      if (hasHole) {
        S.face([put(o.rIn, ta, o.w0), put(o.rIn, ta, o.w1),
                put(o.rIn, tb, o.w1), put(o.rIn, tb, o.w0)], o.hl);
      }
      // 两端环形面
      for (const [w, sign] of [[o.w0, -1], [o.w1, 1]]) {
        if (sign < 0 && o.cap0 === false) continue;
        if (sign > 0 && o.cap1 === false) continue;
        if (hasHole) {
          const q = sign > 0
            ? [put(o.rIn, ta, w), put(o.rOut, ta, w), put(o.rOut, tb, w), put(o.rIn, tb, w)]
            : [put(o.rIn, ta, w), put(o.rIn, tb, w), put(o.rOut, tb, w), put(o.rOut, ta, w)];
          S.face(q, o.hl);
        } else {
          const q = sign > 0
            ? [put(0, 0, w), put(o.rOut, ta, w), put(o.rOut, tb, w)]
            : [put(0, 0, w), put(o.rOut, tb, w), put(o.rOut, ta, w)];
          S.face(q, o.hl);
        }
      }
    }
    // 扇环的两个端面
    if (!full) {
      for (const [t, sign] of [[t0, -1], [o.a1, 1]]) {
        const q = hasHole
          ? [put(o.rIn, t, o.w0), put(o.rOut, t, o.w0), put(o.rOut, t, o.w1), put(o.rIn, t, o.w1)]
          : [put(0, 0, o.w0), put(o.rOut, t, o.w0), put(o.rOut, t, o.w1), put(0, 0, o.w1)];
        S.face(sign > 0 ? q : q.slice().reverse(), o.hl);
      }
    }
    if (o.cap0 !== false && o.cap1 !== false && S.vol() < 0) S.flip();
    return S;
  }

  /* ---------------- plate：带圆孔的矩形面 ----------------
   * 面在某个坐标平面内，要挖一个圆孔。做法：
   *   孔的外接正方形把矩形切成网格 → 方框外的格子全是矩形，直接铺；
   *   方框内的环形区域按角度切成扇形四边形。
   * 这样既精确又不会出现"环带三角形穿模"。
   * u0,u1,v0,v1 是矩形在截面上的范围；hole = {u, v, r} 圆心与半径。
   * axis/w/flip 同 sector；flip=true 让面法线朝 -axis。 */
  function plate(o) {
    const S = new Solid();
    const put = (u, v) => o.axis === 'z' ? [u, v, o.w]
                       : o.axis === 'y' ? [u, o.w, v]
                       : [o.w, u, v];
    const h = o.hole;
    const rev = o.flip ? (q) => q.slice().reverse() : (q) => q;
    const R = h.r;
    const su0 = h.u - R, su1 = h.u + R, sv0 = h.v - R, sv1 = h.v + R;
    const xs = [o.u0, o.u1, Math.max(o.u0, su0), Math.min(o.u1, su1)];
    const ys = [o.v0, o.v1, Math.max(o.v0, sv0), Math.min(o.v1, sv1)];
    const ux = [...new Set(xs.map(v => +v.toFixed(6)))].sort((a, b) => a - b);
    const vy = [...new Set(ys.map(v => +v.toFixed(6)))].sort((a, b) => a - b);
    const inSq = (u, v) => u > su0 + 1e-9 && u < su1 - 1e-9 && v > sv0 + 1e-9 && v < sv1 - 1e-9;
    for (let i = 0; i < ux.length - 1; i++) {
      for (let j = 0; j < vy.length - 1; j++) {
        const a0 = ux[i], a1 = ux[i + 1], b0 = vy[j], b1 = vy[j + 1];
        if (a1 - a0 < 1e-9 || b1 - b0 < 1e-9) continue;
        if (inSq((a0 + a1) / 2, (b0 + b1) / 2)) continue;      // 这块被孔占了
        S.face(rev([put(a0, b0), put(a1, b0), put(a1, b1), put(a0, b1)]), o.hl);
      }
    }
    const seg = Math.max(12, o.seg || 64);
    const sq = (t) => 1 / Math.max(Math.abs(Math.cos(t)), Math.abs(Math.sin(t)));
    for (let i = 0; i < seg; i++) {
      const ta = Math.PI * 2 * i / seg, tb = Math.PI * 2 * (i + 1) / seg;
      const cA = Math.cos(ta), sA = Math.sin(ta), cB = Math.cos(tb), sB = Math.sin(tb);
      const kA = sq(ta), kB = sq(tb);
      S.face(rev([put(h.u + R * cA, h.v + R * sA),
                  put(h.u + R * kA * cA, h.v + R * kA * sA),
                  put(h.u + R * kB * cB, h.v + R * kB * sB),
                  put(h.u + R * cB, h.v + R * sB)]), o.hl);
    }
    S.open = true;
    return S;
  }

  /* 射线求交：从 c 沿方向 (cos t, sin t) 打到多边形 poly（[[u,v],...]）的边界。
   * 返回最近命中点；没命中时退化成单位距离（极少发生）。 */
  function rayTo(poly, c) {
    return function (t) {
      const dx = Math.cos(t), dy = Math.sin(t);
      let best = Infinity, hit = null;
      for (let i = 0; i < poly.length; i++) {
        const p = poly[i], q = poly[(i + 1) % poly.length];
        const ex = q[0] - p[0], ey = q[1] - p[1];
        const den = dx * ey - dy * ex;
        if (Math.abs(den) < 1e-12) continue;
        const ax = p[0] - c[0], ay = p[1] - c[1];
        const s = (ax * ey - ay * ex) / den;      // 沿射线的距离
        const u = (ax * dy - ay * dx) / den;      // 沿边的参数
        if (s > 1e-9 && u >= -1e-9 && u <= 1 + 1e-9 && s < best) {
          best = s; hit = [c[0] + dx * s, c[1] + dy * s];
        }
      }
      return hit || [c[0] + dx, c[1] + dy];
    };
  }

  /* ---------------- fan：扇形面（孔心落在面的边上） ----------------
   * plate 只能处理"孔完全在矩形内部"的面。工程图里更常见的是孔心正好落在
   * 面的边上：矩形端面减半圆孔、梯形端壁减半圆孔、「房子」形端面减整圆。
   * 这时改成"从孔心放射"铺扇形四边形，内圈是孔口圆弧，外圈由边界给出。
   *   {axis, w, c:[u,v], r, a0, a1, seg, outer, flip, hl}
   *   outer = 函数 (θ)→[u,v]，或点列 [[u,v],...]（按射线求交）
   *   θ 从 c 量起：u 方向 θ=0，v 方向 θ=π/2。
   *   绕向：默认法线朝 +axis（同 plate），flip:true 朝 -axis。
   * 想让孔口的顶点和 sector 的孔壁对齐，就取 seg 使角度步长一致
   * （半圈 64 段 ↔ 整圈 128 段）。 */
  function fan(o) {
    const S = new Solid();
    const put = (u, v) => o.axis === 'z' ? [u, v, o.w]
                       : o.axis === 'y' ? [u, o.w, v]
                       : [o.w, u, v];
    const seg = Math.max(6, o.seg || 64);
    const step = (o.a1 - o.a0) / seg;
    const at = typeof o.outer === 'function' ? o.outer : rayTo(o.outer, o.c);
    const rev = o.flip ? (q) => q.slice().reverse() : (q) => q;
    for (let i = 0; i < seg; i++) {
      const ta = o.a0 + step * i, tb = ta + step;
      const ia = [o.c[0] + o.r * Math.cos(ta), o.c[1] + o.r * Math.sin(ta)];
      const ib = [o.c[0] + o.r * Math.cos(tb), o.c[1] + o.r * Math.sin(tb)];
      const oa = at(ta), ob = at(tb);
      // 顺序必须是 内a → 外a → 外b → 内b：沿外圈是倒着走的，写成
      // 内a → 内b → 外b → 外a 会让整块面朝 -axis（实际踩过）。
      S.face(rev([put(ia[0], ia[1]), put(oa[0], oa[1]),
                  put(ob[0], ob[1]), put(ib[0], ib[1])]), o.hl);
    }
    S.open = true;
    return S;
  }

  /* ---------------- finish：合并 + 定向 + 剔内面 + 自动提棱线 ----------------
   * 手写面片时最容易错的三件事一次做完：
   *   1) 每块按有符号体积定绕向（分片建模整体朝内时全部翻回来）；
   *   2) **剔掉埋在邻块内部的面**。多块凸体拼一个形体时，每块的面都是完整多边形，
   *      其中一部分本该埋在邻块里；留着它会在可见面上留下横穿平面的假线，
   *      还会和邻块的面打架（z-fighting）。判据：面重心沿外法线挪一点，
   *      若落进任何一块实体内部，就说明这个面朝里，删掉。删完之后剩下的面
   *      正好是并集的表面，体积仍等于并集体积。
   *   3) 按 25° 折角分组算法线 + 提棱线：方块的棱是硬的、圆柱面是光的，
   *      不用再手写 normals / edges。
   * opt.angle 默认 25°；opt.noEdges 关闭自动棱线；opt.openEdges 也画开放边。 */
  function finish(list, opt) {
    opt = opt || {};
    const angle = (opt.angle === undefined ? 25 : opt.angle) * Math.PI / 180;
    const sols = (Array.isArray(list) ? list : [list]).filter(s => s && s.P.length);

    const P = [], I = [], HL = [], TRI_PURE = [];
    const bb = [[1e30, 1e30, 1e30], [-1e30, -1e30, -1e30]];
    for (const s of sols) {
      // 开放曲面（plate / fan / 只剩壁的 sector）没有有符号体积，只能靠绕向自己保证
      if (!s.open && s.vol() < 0) s.flip();
      const base = P.length / 3;
      for (let i = 0; i < s.P.length; i++) P.push(s.P[i]);
      for (let i = 0; i < s.P.length; i += 3) {
        for (let k = 0; k < 3; k++) {
          bb[0][k] = Math.min(bb[0][k], s.P[i + k]);
          bb[1][k] = Math.max(bb[1][k], s.P[i + k]);
        }
      }
      for (const v of s.I) I.push(base + v);
      for (const v of s.IH) I.push(base + v);
      for (let t = 0; t < s.I.length / 3; t++) { HL.push(0); TRI_PURE.push(!!s.pure); }
      for (let t = 0; t < s.IH.length / 3; t++) { HL.push(1); TRI_PURE.push(!!s.pure); }
    }

    const diag = Math.hypot(bb[1][0] - bb[0][0], bb[1][1] - bb[0][1], bb[1][2] - bb[0][2]);
    const buried = makeBuried(sols, diag * 2e-3, diag * 1e-7);

    /* 剔内面：面重心沿外法线挪 delta，落进任一块实体内部就是内部面 */
    const K = [], KH = [];
    let culled = 0;
    for (let t = 0; t < I.length / 3; t++) {
      const a = I[3 * t] * 3, b = I[3 * t + 1] * 3, c = I[3 * t + 2] * 3;
      const n = uni(crs(sub([P[b], P[b + 1], P[b + 2]], [P[a], P[a + 1], P[a + 2]]),
                        sub([P[c], P[c + 1], P[c + 2]], [P[a], P[a + 1], P[a + 2]])));
      if (!(n[0] || n[1] || n[2])) continue;                       // 退化面
      const m = [(P[a] + P[b] + P[c]) / 3, (P[a + 1] + P[b + 1] + P[c + 1]) / 3,
                 (P[a + 2] + P[b + 2] + P[c + 2]) / 3];
      // 只有「纯半空间块」的面才剔。拼过 plate/sector 的块不再能由平面判定内外
      // （孔壁就是反例：法线朝里，点落进包围盒里但那不是实体）。
      if (TRI_PURE[t] && buried(m, n)) { culled++; continue; }
      K.push(I[3 * t], I[3 * t + 1], I[3 * t + 2]);
      KH.push(HL[t]);
    }

    const res = buildMesh(P, K, KH, angle, opt.noEdges, opt.openEdges);
    res.edges = res.edges.concat(opt.edges || []);
    res.culled = culled;
    res.center = [(bb[0][0] + bb[1][0]) / 2, (bb[0][1] + bb[1][1]) / 2, (bb[0][2] + bb[1][2]) / 2];
    res.radius = diag / 2 * 1.02;
    res.size = [bb[1][0] - bb[0][0], bb[1][1] - bb[0][1], bb[1][2] - bb[0][2]];
    // 按 highlight 拆成两个 mesh（共享同一份 positions/normals）
    const i0 = [], i1 = [];
    for (let t = 0; t < res.hl.length; t++) {
      (res.hl[t] ? i1 : i0).push(res.indices[3 * t], res.indices[3 * t + 1], res.indices[3 * t + 2]);
    }
    res.meshes = [{ positions: res.positions, indices: i0, normals: res.normals }];
    if (i1.length) {
      res.meshes.push({ positions: res.positions, indices: i1, normals: res.normals, highlight: true });
    }
    return res;
  }

  /* 「这个点（面重心沿法线挪 delta 之后）是否埋在某块实体内部」 */
  function makeBuried(sols, delta, eps) {
    const boxes = sols.filter(s => s.pure && s.planes && s.planes.length).map(s => {
      const lo = [1e30, 1e30, 1e30], hi = [-1e30, -1e30, -1e30];
      for (let i = 0; i < s.P.length; i += 3) {
        for (let k = 0; k < 3; k++) {
          lo[k] = Math.min(lo[k], s.P[i + k]);
          hi[k] = Math.max(hi[k], s.P[i + k]);
        }
      }
      return { s, lo, hi };
    });
    return function (p, n) {
      const q = [p[0] + n[0] * delta, p[1] + n[1] * delta, p[2] + n[2] * delta];
      for (const b of boxes) {
        if (q[0] < b.lo[0] || q[0] > b.hi[0] || q[1] < b.lo[1] || q[1] > b.hi[1]
            || q[2] < b.lo[2] || q[2] > b.hi[2]) continue;
        let inside = true;
        for (const pl of b.s.planes) {
          // 容差必须远小于挪动量 delta，否则面会「被自己判成埋在里面」——
          // 面重心沿法线挪 delta 后正好落在自己那张平面的外侧 delta 处。
          if (pl.n[0] * q[0] + pl.n[1] * q[1] + pl.n[2] * q[2] > pl.d + eps) { inside = false; break; }
        }
        if (inside) return true;
      }
      return false;
    };
  }

  /* 顶点按位置+法线分组：既能让棱线变硬，又能让圆柱面光滑 */
  function buildMesh(P, I, HL, angle, noEdges, openEdges) {
    const nv = P.length / 3;
    const k2v = new Map(), vid = new Int32Array(nv), vpos = [];
    for (let i = 0; i < nv; i++) {
      const k = Math.round(P[3 * i] * 1e5) + ',' + Math.round(P[3 * i + 1] * 1e5) + ',' + Math.round(P[3 * i + 2] * 1e5);
      let id = k2v.get(k);
      if (id === undefined) { id = vpos.length; k2v.set(k, id); vpos.push([P[3 * i], P[3 * i + 1], P[3 * i + 2]]); }
      vid[i] = id;
    }
    const nf = I.length / 3;
    const fn = [];
    const vf = Array.from({ length: vpos.length }, () => []);
    for (let f = 0; f < nf; f++) {
      const a = vid[I[3 * f]], b = vid[I[3 * f + 1]], c = vid[I[3 * f + 2]];
      let n = crs(sub(vpos[b], vpos[a]), sub(vpos[c], vpos[a]));
      const L = len(n);
      n = L > 1e-12 ? [n[0] / L, n[1] / L, n[2] / L] : [0, 0, 0];
      fn.push(n);
      vf[a].push(f); vf[b].push(f); vf[c].push(f);
    }
    const cosA = Math.cos(angle);
    const outP = [], outN = [], outI = [], outH = [];
    const vmap = new Map();
    for (let f = 0; f < nf; f++) {
      for (let k = 0; k < 3; k++) {
        const v = vid[I[3 * f + k]];
        let s = [0, 0, 0];
        for (const g of vf[v]) if (dot(fn[g], fn[f]) >= cosA - 1e-9) { s[0] += fn[g][0]; s[1] += fn[g][1]; s[2] += fn[g][2]; }
        if (len(s) < 1e-9) s = fn[f];
        s = uni(s);
        const key = v + '|' + Math.round(s[0] * 1e4) + ',' + Math.round(s[1] * 1e4) + ',' + Math.round(s[2] * 1e4);
        let o = vmap.get(key);
        if (o === undefined) {
          o = outP.length / 3;
          vmap.set(key, o);
          outP.push(vpos[v][0], vpos[v][1], vpos[v][2]);
          outN.push(s[0], s[1], s[2]);
        }
        outI.push(o);
      }
      outH.push(HL[f]);
    }
    /* 棱线：二面角超过 angle 的边。夹角正好 180°（dot≈-1）的是两块实体的内部
     * 接缝，不是棱，不能画。 */
    const edges = [];
    if (!noEdges) {
      /* T 形接点修补：一条大面的边常常被对面几个小面的顶点切成几段，
       * 只按顶点对建索引的话两边对不上，那条真实的棱就画不出来（第 6 题
       * 台阶面与斜切面的交线就这么丢过一次）。先把每条三角形边按落在其上的
       * 其它顶点切开，再建边表。 */
      const em = new Map();
      const add = (u, w, f) => {
        const k = u < w ? u + '_' + w : w + '_' + u;
        const e = em.get(k);
        if (e) e.push(f); else em.set(k, [f]);
      };
      const hits = [];
      for (let f = 0; f < nf; f++) {
        const tri = [vid[I[3 * f]], vid[I[3 * f + 1]], vid[I[3 * f + 2]]];
        for (let k = 0; k < 3; k++) {
          const a = tri[k], b = tri[(k + 1) % 3];
          const pa = vpos[a], pb = vpos[b];
          const ab = sub(pb, pa);
          const L2 = dot(ab, ab);
          if (L2 < 1e-18) { add(a, b, f); continue; }
          hits.length = 0;
          const bx0 = Math.min(pa[0], pb[0]) - 1e-6, bx1 = Math.max(pa[0], pb[0]) + 1e-6;
          const by0 = Math.min(pa[1], pb[1]) - 1e-6, by1 = Math.max(pa[1], pb[1]) + 1e-6;
          const bz0 = Math.min(pa[2], pb[2]) - 1e-6, bz1 = Math.max(pa[2], pb[2]) + 1e-6;
          for (let v = 0; v < vpos.length; v++) {
            if (v === a || v === b) continue;
            const pv = vpos[v];
            if (pv[0] < bx0 || pv[0] > bx1 || pv[1] < by0 || pv[1] > by1
                || pv[2] < bz0 || pv[2] > bz1) continue;
            const t = dot(sub(pv, pa), ab) / L2;
            if (t <= 1e-6 || t >= 1 - 1e-6) continue;
            const dx = pv[0] - pa[0] - ab[0] * t, dy = pv[1] - pa[1] - ab[1] * t,
                  dz = pv[2] - pa[2] - ab[2] * t;
            if (dx * dx + dy * dy + dz * dz > 1e-8 * L2 + 1e-12) continue;
            hits.push([t, v]);
          }
          if (!hits.length) { add(a, b, f); continue; }
          hits.sort((x, y) => x[0] - y[0]);
          let prev = a;
          for (const h of hits) { add(prev, h[1], f); prev = h[1]; }
          add(prev, b, f);
        }
      }
      for (const [k, fs] of em) {
        let show = false;
        if (fs.length === 2) {
          const d = dot(fn[fs[0]], fn[fs[1]]);
          if (d < cosA && d > -1 + 1e-6) show = true;
        } else if (fs.length === 1) {
          show = !!openEdges;
        }
        if (!show) continue;
        const ab = k.split('_').map(Number);
        edges.push(vpos[ab[0]][0], vpos[ab[0]][1], vpos[ab[0]][2],
                   vpos[ab[1]][0], vpos[ab[1]][1], vpos[ab[1]][2]);
      }
    }
    return { positions: outP, normals: outN, indices: outI, hl: outH, edges };
  }

  return { hull, box, extrude, sector, plate, fan, subtract, finish, Solid, triangulate };
})();
