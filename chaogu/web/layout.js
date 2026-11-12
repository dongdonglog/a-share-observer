/* 看板栅格布局求解：24 列、面板互斥、重力压实。
 *
 * 为什么单独一个文件：这是拖拽手感的全部逻辑，也是最容易出错的地方
 * （面板互相盖住、拖动后弹回原位）。抽成不碰 DOM 的纯函数，就能用
 * tools/test_layout.js 离线跑各种拖拽场景，而不是靠肉眼看。
 *
 * 坐标约定与后端 panels.py 一致：x 从 0 起（列，约束 x + w <= 24），y 从 0 起（行）。
 * 尺寸上下限也和后端同一套值——两边不一致时保存会被后端改回，表现为"拖了没生效"。
 *
 * 两种求解：
 *   displace() —— 拖动过程中的"排斥"：被拖动的面板优先占位，和它相撞的依次往下让。
 *                 只往下、不上移，这样指针来回移动时其它面板不会乱跳。
 *   resolve()  —— 松手后的最终布局：先 displace 再按"用户意图顺序"重力压实。
 */
(function (root, factory) {
  const api = factory();
  if (root) root.ChaoguLayout = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const COLS = 24, MIN_W = 4, MAX_W = 24, MIN_H = 3, MAX_H = 20;

  function clamp(panel) {
    panel.w = Math.max(MIN_W, Math.min(MAX_W, Math.round(panel.w) || MIN_W));
    panel.h = Math.max(MIN_H, Math.min(MAX_H, Math.round(panel.h) || MIN_H));
    panel.x = Math.max(0, Math.min(COLS - panel.w, Math.round(panel.x) || 0));
    panel.y = Math.max(0, Math.round(panel.y) || 0);
    return panel;
  }

  /* active 既可以是 uid 字符串，也可以直接是面板对象。
   * 用对象比较而不是只认 uid：uid 是 app.js 生成的，布局算法不该依赖它存在
   * （后端一致性测试里的夹具就只有 id）——只按 uid 认身份时，一堆没有 uid 的面板
   * 会被当成同一块，求解结果就乱了。 */
  function isActive(p, active) {
    if (active === null || active === undefined) return false;
    return typeof active === "object" ? p === active : p.uid === active;
  }

  // a 放在 ay 行时是否与 b 重叠（矩形相交，边界相接不算）。
  function overlaps(a, ay, b) {
    return ay < b.y + b.h && b.y < ay + a.h && a.x < b.x + b.w && b.x < a.x + a.w;
  }

  // 从 p 当前位置往下找第一个不与 placed 重叠的 y。只往下，用于拖动中的排斥。
  function relaxedY(p, placed) {
    let y = p.y;
    for (;;) {
      const hit = placed.filter((q) => overlaps(p, y, q));
      if (!hit.length) return y;
      y = Math.min.apply(null, hit.map((q) => q.y + q.h));
    }
  }

  // 从 0 往下找第一个空位。重力用（拉平空洞）。
  function topmostY(p, placed) {
    let y = 0;
    for (;;) {
      const hit = placed.filter((q) => overlaps(p, y, q));
      if (!hit.length) return y;
      y = Math.min.apply(null, hit.map((q) => q.y + q.h));
    }
  }

  /* 拖动/缩放过程中的排斥：active 保持调用方给它的坐标，其余面板按当前 (y,x)
   * 顺序依次让到不与已放置面板重叠的位置（只往下）。
   * 调用方应在每次指针移动前，先把所有面板恢复到拖动开始时的快照，
   * 这样指针移开时它们会回到原位，而不是被推下去出不来。 */
  function displace(panels, activeUid) {
    const list = panels || [];
    const order = list.slice().sort(function (a, b) {
      if (isActive(a, activeUid)) return -1;
      if (isActive(b, activeUid)) return 1;
      return (a.y - b.y) || (a.x - b.x);
    });
    const placed = [];
    order.forEach(function (p) {
      clamp(p);
      if (!isActive(p, activeUid)) p.y = relaxedY(p, placed);
      placed.push(p);                       // 面板对象本身唯一，不必再按 uid 去重
    });
    return list;
  }

  /* 最终布局：先排斥消除重叠，再按"用户意图顺序"重力压实。
   *
   * 意图顺序 = 拖动开始时各面板的 (y, x)，被拖动的面板用手上的新 y。
   * 重力必须按这个顺序，否则被让位的面板会排到拖动面板前面去，
   * 表现成"把它拖上去，一松手又弹回原位"。
   * 意图位置相同时（拖到别人头上那一格），被拖动的面板优先占位，
   * 对方顺延到它下面——这正是"把这块放到这里、你让一下"的直觉。 */
  function resolve(panels, activeUid, options) {
    const list = panels || [];
    if (!list.length) return list;
    const opts = options || {};
    // 意图位置以面板对象为键：uid 缺失时也不能互相覆盖。
    const intent = new Map();
    list.forEach(function (p) {
      clamp(p);
      intent.set(p, {y: p.y, x: p.x});
    });

    displace(list, activeUid);

    if (opts.settle === false) return list;

    const compacted = [];
    list.slice().sort(function (a, b) {
      const ia = intent.get(a), ib = intent.get(b);
      if (ia.y !== ib.y) return ia.y - ib.y;
      if (ia.x !== ib.x) return ia.x - ib.x;
      if (isActive(a, activeUid)) return -1;
      if (isActive(b, activeUid)) return 1;
      return 0;
    }).forEach(function (p) {
      p.y = topmostY(p, compacted);
      compacted.push(p);
    });
    return list;
  }

  return {COLS: COLS, MIN_W: MIN_W, MAX_W: MAX_W, MIN_H: MIN_H, MAX_H: MAX_H,
          clamp: clamp, overlaps: overlaps, relaxedY: relaxedY, topmostY: topmostY,
          displace: displace, resolve: resolve};
});
