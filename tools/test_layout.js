/* 看板布局求解的离线测试（node tools/test_layout.js）。
 *
 * 这里的每一条都对应一个真实踩过的坑：
 *   - 拖动时面板互相盖住（没有排斥），松手后靠重力才勉强分开；
 *   - 把面板拖到别人头上，一松手又弹回原位（"移动不了"）；
 *   - 被推开的面板回不来（指针移开后面板留在下面）；
 *   - 前端算出的布局与后端保存时的重力结果不一致，存完又跳一次。
 */
const assert = require('node:assert/strict');
const path = require('node:path');
const layout = require(path.join(__dirname, '../chaogu/web/layout.js'));

const P = (uid, x, y, w, h) => ({uid, x, y, w, h});
const clone = (ps) => ps.map((p) => ({...p}));
const at = (ps, uid) => ps.find((p) => p.uid === uid);
const pos = (ps) => ps.map((p) => `${p.uid}:${p.x},${p.y},${p.w},${p.h}`).join(' | ');

function overlapsAny(ps) {
  for (let i = 0; i < ps.length; i++) {
    for (let j = i + 1; j < ps.length; j++) {
      const a = ps[i], b = ps[j];
      if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) {
        return `${a.uid} ∩ ${b.uid}`;
      }
    }
  }
  return null;
}

function checkBounds(ps) {
  for (const p of ps) {
    assert.ok(p.x >= 0, `${p.uid} x 为负`);
    assert.ok(p.x + p.w <= layout.COLS, `${p.uid} 超出右边界：x+w=${p.x + p.w}`);
    assert.ok(p.y >= 0, `${p.uid} y 为负`);
    assert.ok(p.w >= layout.MIN_W && p.w <= layout.MAX_W, `${p.uid} 宽度越界`);
    assert.ok(p.h >= layout.MIN_H && p.h <= layout.MAX_H, `${p.uid} 高度越界`);
  }
}

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ✅ ${name}`);
  } catch (err) {
    console.error(`  ❌ ${name}\n     ${err.message}`);
    process.exitCode = 1;
  }
}

console.log('拖动中的排斥（displace）');

test('被拖动的面板保持指针给的位置，相撞的邻居往下让开', () => {
  const ps = [P('A', 0, 0, 24, 10), P('B', 0, 10, 24, 10)];
  at(ps, 'A').y = 5;                       // 用户把 A 拖到 B 身上
  layout.displace(ps, 'A');
  assert.equal(at(ps, 'A').y, 5, '拖动的面板不该被挪走');
  assert.equal(at(ps, 'B').y, 15, '被压住的 B 应该让到 A 下面');
  assert.equal(overlapsAny(ps), null, pos(ps));
});

test('排斥只往下、不上移（指针移开后面板能回到原位）', () => {
  const ps = [P('A', 0, 0, 24, 10), P('B', 0, 10, 24, 10)];
  at(ps, 'A').y = 5;
  layout.displace(ps, 'A');
  assert.equal(at(ps, 'B').y, 15);
  // 指针拖回原处：调用方恢复快照后重新求解，B 必须回到 10 而不是留在 15
  at(ps, 'A').y = 0; at(ps, 'B').y = 10;
  layout.displace(ps, 'A');
  assert.equal(at(ps, 'B').y, 10, 'B 应该回到原位');
});

test('同一行的邻居不受影响，被压住的才让开', () => {
  const ps = [P('A', 0, 0, 12, 10), P('B', 12, 0, 12, 10), P('C', 0, 10, 12, 8)];
  at(ps, 'C').y = 2;                       // C 被拖到 A 头上（A 在左半行）
  layout.displace(ps, 'C');
  assert.equal(at(ps, 'C').y, 2, '拖动的 C 保持位置');
  assert.equal(at(ps, 'B').y, 0, 'B 与 C 横向不相交，不该被顶下去');
  assert.ok(at(ps, 'A').y >= 10, `A 与 C 相撞应该让到 C 下面，实际 ${at(ps, 'A').y}`);
  assert.equal(overlapsAny(ps), null, pos(ps));
});

test('窄面板被顶下去时不改变横向位置', () => {
  const ps = [P('A', 0, 0, 24, 10), P('B', 6, 10, 12, 10)];
  at(ps, 'A').y = 6;
  layout.displace(ps, 'A');
  assert.equal(at(ps, 'B').y, 16);
  assert.equal(at(ps, 'B').x, 6);
});

console.log('\n松手后的最终布局（resolve）');

test('拖到别人头上：拖动的这块占住位置，对方顺延（以前会被弹回原位）', () => {
  const ps = [P('A', 0, 0, 24, 10), P('B', 0, 10, 24, 10)];
  at(ps, 'B').y = 0;                       // 把 B 拖到最上面，与 A 的原来位置重合
  layout.resolve(ps, 'B');
  assert.equal(at(ps, 'B').y, 0, 'B 应该占住用户放的位置');
  assert.equal(at(ps, 'A').y, 10, 'A 应该让到 B 下面');
  assert.equal(overlapsAny(ps), null, pos(ps));
});

test('拖到下方实现互换（重力不吞掉顺序变化）', () => {
  const ps = [P('A', 0, 0, 24, 10), P('B', 0, 10, 24, 10)];
  at(ps, 'A').y = 20;                      // 把 A 拖到 B 下面
  layout.resolve(ps, 'A');
  assert.equal(at(ps, 'B').y, 0, 'B 应该顶上来');
  assert.equal(at(ps, 'A').y, 10, 'A 落到 B 下面并压到最紧');
});

test('空洞被重力压实', () => {
  const ps = [P('A', 0, 0, 24, 10), P('B', 0, 40, 24, 10), P('C', 0, 60, 12, 8)];
  layout.resolve(ps, 'C');
  assert.equal(at(ps, 'A').y, 0);
  assert.equal(at(ps, 'B').y, 10);
  assert.equal(at(ps, 'C').y, 20);
});

test('并排的面板不会被压成上下', () => {
  const ps = [P('A', 0, 0, 12, 10), P('B', 12, 0, 12, 10), P('C', 0, 10, 24, 6)];
  layout.resolve(ps, 'C');
  assert.equal(at(ps, 'A').y, 0);
  assert.equal(at(ps, 'B').y, 0);
  assert.equal(at(ps, 'C').y, 10);
});

test('撑大面板会把它下面的邻居顶下去，且不重叠', () => {
  const ps = [P('A', 0, 0, 12, 10), P('B', 0, 10, 24, 8)];
  at(ps, 'A').h = 14;                      // 用户把 A 拉高，越过了 B
  layout.resolve(ps, 'A');
  assert.equal(at(ps, 'A').h, 14);
  assert.ok(at(ps, 'B').y >= 14, `B 应该被顶到 14 行以下，实际 ${at(ps, 'B').y}`);
  assert.equal(overlapsAny(ps), null, pos(ps));
});

test('求解是幂等的（前端算完、后端保存时再算一次不会变）', () => {
  const ps = [P('A', 0, 0, 12, 10), P('B', 0, 12, 24, 8), P('C', 12, 0, 12, 6),
              P('D', 6, 20, 12, 12)];
  layout.resolve(ps, 'D');
  const once = pos(ps);
  layout.resolve(ps, null);
  assert.equal(pos(ps), once, '第二次求解改变了布局，保存后会跳一下');
});

test('尺寸与坐标被夹到合法范围', () => {
  const ps = [P('A', 20, -5, 99, 99), P('B', -3, 0, 1, 1)];
  layout.resolve(ps, 'A');
  checkBounds(ps);
  assert.equal(at(ps, 'A').w, layout.MAX_W);
  assert.equal(at(ps, 'A').h, layout.MAX_H);
  assert.equal(at(ps, 'A').x, 0, 'x+w 超界要往左夹');
  assert.equal(at(ps, 'B').w, layout.MIN_W, '宽度不该小于下限');
  assert.equal(at(ps, 'B').h, layout.MIN_H);
  // A 被夹成整行宽后占住顶部，B 只能落在它下面
  assert.ok(at(ps, 'B').y >= at(ps, 'A').y + at(ps, 'A').h, pos(ps));
});

console.log('\n随机拖拽压力测试（不重叠 / 不越界 / 幂等）');

test('200 组随机布局 + 随机拖动后仍然合法', () => {
  let seed = 20261016;
  const rnd = (n) => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed % n;
  };
  for (let round = 0; round < 200; round++) {
    const n = 2 + rnd(6);
    const ps = [];
    for (let i = 0; i < n; i++) {
      const w = [6, 8, 12, 24][rnd(4)], h = 3 + rnd(12);
      ps.push(P('p' + i, rnd(layout.COLS - w + 1), rnd(60), w, h));
    }
    const active = 'p' + rnd(n);
    at(ps, active).y = rnd(60);            // 模拟用户拖到任意行
    at(ps, active).x = Math.min(at(ps, active).x, layout.COLS - at(ps, active).w);
    const snapshot = clone(ps);            // 拖动中的排斥
    layout.displace(ps, active);
    assert.equal(overlapsAny(ps), null, `第 ${round} 轮 displace 后重叠：${pos(ps)}`);
    // 松手：恢复邻居 + 保留落点，再压实（与 app.js 的松手流程一致）
    const drop = {...at(ps, active)};
    snapshot.forEach((s) => Object.assign(at(ps, s.uid), s));
    Object.assign(at(ps, active), drop);
    layout.resolve(ps, active);
    assert.equal(overlapsAny(ps), null, `第 ${round} 轮 resolve 后重叠：${pos(ps)}`);
    checkBounds(ps);
    const once = pos(ps);
    layout.resolve(ps, null);
    assert.equal(pos(ps), once, `第 ${round} 轮不幂等：${once} → ${pos(ps)}`);
  }
});

console.log('\n身份与健壮性');

test('没有 uid 的面板也能正确求解（不能互相当成同一块）', () => {
  // 回归：算法原先按 p.uid 认身份，一堆只有 id 的面板会共用同一个"意图"槽位，
  // 求解结果与后端不一致（前端与后端一致性测试抓到的就是这个）。
  const ps = [{id: 'verdict', x: 30, y: 3, w: 8, h: 6},
              {id: 'ladder', x: -5, y: -2, w: 40, h: 30}];
  layout.resolve(ps, null);
  const got = ps.map((p) => `${p.id}:${p.x},${p.y},${p.w},${p.h}`);
  // 与后端 panels._solve_layout 的结果一致（见 tools/test_dashboards.py 的对答案用例）
  assert.deepEqual(got, ['verdict:16,20,8,6', 'ladder:0,0,24,20'], got.join(' | '));
  assert.equal(overlapsAny(ps), null, pos(ps));
});

test('active 也可以直接传面板对象', () => {
  const ps = [{id: 'A', x: 0, y: 0, w: 24, h: 10}, {id: 'B', x: 0, y: 10, w: 24, h: 10}];
  ps[1].y = 0;
  layout.resolve(ps, ps[1]);              // 直接传对象，而不是 uid
  assert.equal(ps[1].y, 0);
  assert.equal(ps[0].y, 10, pos(ps));
});

console.log('\n拖动过程中（不压实）不应把面板往上拉');

test('displace 永远不上移任何面板', () => {
  const ps = [P('A', 0, 20, 12, 10), P('B', 12, 4, 12, 6), P('C', 0, 40, 24, 8)];
  const before = clone(ps);
  at(ps, 'A').y = 0;
  layout.displace(ps, 'A');
  for (const p of ps) {
    if (p.uid === 'A') continue;           // A 是用户正在拖的那块，它当然会移动
    const old = before.find((q) => q.uid === p.uid);
    assert.ok(p.y >= old.y, `${p.uid} 被上移了（${old.y} → ${p.y}）`);
  }
});

console.log(`\n${passed} 项通过${process.exitCode ? '，有失败' : ''}`);
