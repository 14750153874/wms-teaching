/* =============================================================================
   WMS 本地演示 —— 前端逻辑（原生 JS，无框架依赖）
   主轴：订单驱动（商城下单 → 支付锁库存 → 波次拣货 → 复核 → 打包 → 发货 → 售后）
   页面：经营看板 / 商城前台 / 我的订单 / 订单池 / 波次拣货 / 售后工作台 /
        商品 SPU/SKU / 上下架 / 补货建议 / 入库 / 出库 / 库存 / 流水 / 台账 / 库位 / 运维 / 日志
   ============================================================================= */
'use strict';

/* ------------------------------ 全局状态 ------------------------------ */
const state = {
  token: localStorage.getItem('wms_token') || '',
  user: null,
  page: 'dashboard',
  cache: { categories: [], locations: [], leafLocations: [], goods: [], suppliers: [], customers: [], shops: [] },
  idem: new Map(),                       // 幂等键：同一单据同一动作复用，演示"重复提交不重复过账"
  shopId: 0,                             // 当前商城店铺
  shopSel: {},                           // 商城前台：每个 SPU 当前选中的 SKU
  perms: new Set(),                      // 当前登录人的权限码（'*' = 全部）
  roles: [],                             // 当前登录人的角色
  pickedOrders: new Set(),               // 订单池：勾选待生成波次的订单
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const yuan = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 3 });
const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const STATUS = {
  in: { 0: ['草稿', 'gray'], 1: ['已审核', 'blue'], 2: ['部分入库', 'orange'], 3: ['已入库', 'green'], 4: ['已作废', 'red'] },
  out: { 0: ['草稿', 'gray'], 1: ['已审待出', 'blue'], 2: ['拣货中', 'orange'], 3: ['已出库', 'green'], 4: ['已作废', 'red'] },
};
const BIZ_TYPE = { 1: '采购入库', 2: '退货入库', 3: '其他入库', 4: '销售出库', 5: '领料出库',
  10: '手工调整', 11: '期初', 21: '电商发货', 22: '退货入库' };

/* ------------------------------ 电商域字典 ------------------------------ */
const EC_TEXT = { 0: '待付款', 1: '待发货', 2: '拣货中', 7: '待打包', 3: '已发货', 4: '已完成', 5: '已取消', 6: '售后中' };
const EC_TAG = { 0: 'gray', 1: 'blue', 2: 'orange', 7: 'purple', 3: 'green', 4: 'green', 5: 'red', 6: 'orange' };
const AS_TEXT = { 0: '待审核', 1: '同意待收货', 2: '已完成', 3: '已拒绝' };
const AS_TAG = { 0: 'orange', 1: 'blue', 2: 'green', 3: 'gray' };
const AS_TYPE = { 1: '退货退款', 2: '仅退款', 3: '换货', 4: '补发' };
const WAVE_TEXT = { 0: '待拣货', 1: '拣货中', 2: '待打包', 3: '已完成', 4: '已取消' };
const WAVE_TAG = { 0: 'orange', 1: 'blue', 2: 'purple', 3: 'green', 4: 'gray' };
const ALLOC_TEXT = { 1: '已分配', 2: '已拣货', 3: '已发货', 4: '已释放' };
const tagOf = (text, color) => `<span class="tag ${color || 'gray'}">${esc(text)}</span>`;
const attrsText = (s) => {
  try { const o = JSON.parse(s || '{}'); const v = Object.values(o || {}); return v.length ? v.join(' / ') : ''; }
  catch (e) { return ''; }
};

/* ------------------------------ 基础工具 ------------------------------ */
function toast(message, type = 'success') {
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'error' ? 'error' : type === 'info' ? 'info' : '');
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function uuid() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

// 独立前后端部署时由 config.js 注入后端基址；单文件版留空即同源请求
const API_BASE = (window.WMS_API_BASE || '').replace(/\/$/, '');

async function api(method, path, body, extraHeaders) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, extraHeaders || {});
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  const url = /^https?:\/\//i.test(path) ? path : (API_BASE || '') + path;
  const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const json = await res.json().catch(() => ({ code: 50000, message: '响应不是合法 JSON' }));
  if (json.code !== 200) {
    if (json.code === 40100 || json.code === 40103) { logout(); }
    throw Object.assign(new Error(json.message || '请求失败'), { code: json.code });
  }
  return json.data;
}

function openModal({ title, body, footer, wide, onMount }) {
  $('#modal-root').innerHTML = `
    <div class="mask" id="mask">
      <div class="modal ${wide ? 'wide' : ''}">
        <div class="modal-head"><span>${esc(title)}</span><span class="spacer"></span>
          <span class="x" id="modal-x">✕</span></div>
        <div class="modal-body">${body}</div>
        <div class="modal-foot">${footer || '<button class="btn" id="modal-close">关闭</button>'}</div>
      </div>
    </div>`;
  const close = () => { $('#modal-root').innerHTML = ''; };
  $('#modal-x').onclick = close;
  const c = $('#modal-close'); if (c) c.onclick = close;
  const mask = $('#mask');
  mask.onclick = (e) => { if (e.target === mask) close(); };
  if (onMount) onMount(close);
  return close;
}

function table(headers, rows, emptyText) {
  if (!rows.length) return `<table class="grid"><thead><tr>${headers.map(h => `<th class="${h.cls || ''}">${h.title}</th>`).join('')}</tr></thead><tbody><tr><td class="empty" colspan="${headers.length}">${emptyText || '暂无数据'}</td></tr></tbody></table>`;
  return `<div class="table-wrap"><table class="grid">
    <thead><tr>${headers.map(h => `<th class="${h.cls || ''}">${h.title}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map((c, i) => `<td class="${headers[i].cls || ''}">${c}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

function pager(total, page, size, onGo) {
  const pages = Math.max(1, Math.ceil(total / size));
  window.__pagerGo = onGo;
  return `<div class="pager">
    <span>共 <b>${total}</b> 条</span><span class="spacer"></span>
    <button ${page <= 1 ? 'disabled' : ''} onclick="__pagerGo(${page - 1})">上一页</button>
    <span>第 ${page} / ${pages} 页</span>
    <button ${page >= pages ? 'disabled' : ''} onclick="__pagerGo(${page + 1})">下一页</button>
  </div>`;
}

/* ------------------------------ 认证 / 权限 ------------------------------ */
// 权限码在接口层已经强制拦了一遍；前端这层只做「不给不该看的入口」和提前提示。
function can(code) { return state.perms.has('*') || state.perms.has(code); }
function permBtn(code, html) { return can(code) ? html : ''; }

function applyAuth(user) {
  state.user = user || null;
  state.perms = new Set(user && user.perms ? user.perms : []);
  state.roles = (user && user.roles) || [];
  renderMenu();
  const el = $('#current-user');
  if (el && state.user) {
    const roleText = state.roles.map(r => r.name).join(' / ') || '无角色';
    el.textContent = `${state.user.username}（${state.user.realName || ''}）· ${roleText}`;
  }
}

function renderMenu() {
  // 没有权限的菜单直接隐藏；整组都没权限时连组标题一起隐藏
  $$('#menu a[data-page]').forEach(a => {
    const need = a.dataset.perm;
    a.classList.toggle('hidden', !!(need && !can(need)));
  });
  $$('#menu .group').forEach(g => {
    let next = g.nextElementSibling, any = false;
    while (next && !next.classList.contains('group')) {
      if (next.tagName === 'A' && !next.classList.contains('hidden')) any = true;
      next = next.nextElementSibling;
    }
    g.classList.toggle('hidden', !any);
  });
  const reset = $('#btn-reset');
  if (reset) reset.classList.toggle('hidden', !can('sys:manage'));
}

function firstAllowedPage() {
  const a = $$('#menu a[data-page]').find(x => !x.classList.contains('hidden'));
  return a ? a.dataset.page : null;
}

/* ------------------------------ 登录 / 退出 ------------------------------ */
async function login() {
  const username = $('#login-username').value.trim();
  const password = $('#login-password').value;
  try {
    const data = await api('POST', '/api/v1/auth/login', { username, password });
    state.token = data.accessToken;
    applyAuth(data.user);
    localStorage.setItem('wms_token', state.token);
    toast('登录成功，欢迎 ' + data.user.realName);
    await boot();
  } catch (e) { toast(e.message, 'error'); }
}

function logout() {
  state.token = ''; state.user = null;
  localStorage.removeItem('wms_token');
  $('#app-view').classList.add('hidden');
  $('#login-view').classList.remove('hidden');
}

/* ------------------------------ 初始化 ------------------------------ */
async function loadBasics() {
  // 不同角色的可见数据不同：拉不到的（403）就留空，不要因此让整个页面挂掉
  const safe = async (path, fallback) => {
    try { return await api('GET', path); } catch (e) { return fallback; }
  };
  const [cats, locs, goods, sup, cus, shops] = await Promise.all([
    safe('/api/v1/categories', { list: [] }), safe('/api/v1/locations', { list: [], leafList: [] }),
    safe('/api/v1/goods?size=200', { list: [] }), safe('/api/v1/partners?type=supplier', { list: [] }),
    safe('/api/v1/partners?type=customer', { list: [] }), safe('/api/v1/shops', { list: [] }),
  ]);
  state.cache.categories = cats.list;
  state.cache.locations = locs.list;
  state.cache.leafLocations = locs.leafList;
  state.cache.goods = goods.list;
  state.cache.suppliers = sup.list;
  state.cache.customers = cus.list;
  state.cache.shops = shops.list;
  if (!state.shopId && shops.list.length) state.shopId = shops.list[0].id;
}

async function boot() {
  if (!state.token) return;
  try {
    const me = await api('GET', '/api/v1/auth/me');
    applyAuth(me.user);
  } catch (e) { return; }
  $('#login-view').classList.add('hidden');
  $('#app-view').classList.remove('hidden');
  await loadBasics();
  const initial = (location.hash || '').replace('#', '');
  go(PAGES[initial] ? initial : 'dashboard');
}

function go(page) {
  // 页面级权限：没权限的页面进不去（接口层还会强制校验一次）
  const link = $(`#menu a[data-page="${page}"]`);
  if (link && link.dataset.perm && !can(link.dataset.perm)) {
    toast('没有访问「' + (link.textContent || page) + '」的权限', 'error');
    const fallback = firstAllowedPage();
    if (!fallback || fallback === page) {
      $('#content').innerHTML = '<div class="card"><p class="hint">当前账号没有任何可见页面，请联系管理员分配角色与权限。</p></div>';
      return;
    }
    page = fallback;
  }
  state.page = page;
  const modalRoot = document.getElementById('modal-root');
  if (modalRoot) modalRoot.innerHTML = '';   // 切页时关掉残留弹窗
  $$('#menu a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  $('#crumb').textContent = ($(`#menu a[data-page="${page}"]`) || {}).textContent || '';
  if (location.hash !== '#' + page) history.replaceState(null, '', '#' + page);
  PAGES[page]();
}

/* ============================== 经营看板 ============================== */
async function pageDashboard() {
  const s = await api('GET', '/api/v1/stats/summary');
  // 运维指标需要 ops:view；没有权限的角色看板只显示业务数字
  const raw = await api('GET', '/api/v1/ops/metrics').catch(() => ({}));
  if (state.page !== 'dashboard') return;
  const m = Object.assign({ avgShipHours: 0, canceledToday: '—', deadStockLines: '—',
    staleAllocOrders: '—', pendingShip: '—', lowStockSku: '—' }, raw);
  $('#content').innerHTML = `
    <div class="stat-row">
      <div class="stat"><div class="label">今日订单</div><div class="value">${s.todayOrders}<small>单</small></div></div>
      <div class="stat green"><div class="label">今日销售额</div><div class="value">￥${yuan(s.todaySales)}</div></div>
      <div class="stat orange"><div class="label">待发货 / 待打包</div><div class="value">${s.pendingShip}<small>单</small></div></div>
      <div class="stat red"><div class="label">待处理售后</div><div class="value">${s.afterSaleOpen}<small>单</small></div></div>
    </div>

    <div class="card">
      <div class="card-head"><h3>订单驱动的履约主线</h3>
        <span class="hint" style="margin-left:8px">库存只在「支付（预占）」和「发货（扣减）」两处变动，其余步骤都只是作业状态</span></div>
      <div class="flow">
        <span class="step on">商城下单</span>→<span class="step on">支付（FIFO 分配库位·预占）</span>→
        <span class="step on">合成波次</span>→<span class="step on">按库位路径拣货</span>→
        <span class="step on">复核（数量核对）</span>→<span class="step on">打包/称重</span>→
        <span class="step on">发货过账（扣实物+释放预占）</span>→<span class="step on">签收</span>
      </div>
      <div class="flow" style="margin-top:10px">
        <span class="step">逆向：申请售后</span>→<span class="step">审核</span>→
        <span class="step">退货质检（良品回架 / 残次进次品区）</span>→<span class="step">换货/补发自动开单</span>
      </div>
    </div>

    <div class="overview-grid">
      <div class="overview-card accent">
        <div class="overview-head"><span class="overview-kicker">待办</span><h3>今天要先做的事</h3></div>
        <ul class="summary-list">
          <li><span>待发货订单</span><b>${s.pendingShip}</b></li>
          <li><span>待审核售后</span><b>${s.afterSaleOpen}</b></li>
          <li><span>低库存 SKU</span><b>${s.lowStockSku}</b></li>
          <li><span>预占未发货数量</span><b>${qty(s.lockedQty)}</b></li>
        </ul>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn small primary" onclick="go('ec-orders')">去处理订单</button>
          <button class="btn small" onclick="go('waves')">去波次拣货</button>
          <button class="btn small" onclick="go('after-sales')">去售后</button>
        </div>
      </div>

      <div class="overview-card">
        <div class="overview-head"><span class="overview-kicker">履约效率</span><h3>仓库侧指标</h3></div>
        <ul class="summary-list">
          <li><span>支付 → 发货平均时长</span><b>${m.avgShipHours || 0} 小时</b></li>
          <li><span>今日取消单</span><b>${m.canceledToday}</b></li>
          <li><span>呆滞库存行（30 天无出库）</span><b>${m.deadStockLines}</b></li>
          <li><span>超 24h 未发货</span><b>${m.staleAllocOrders}</b></li>
        </ul>
      </div>

      <div class="overview-card">
        <div class="overview-head"><span class="overview-kicker">库存</span><h3>实物与可用</h3></div>
        <ul class="summary-list">
          <li><span>SKU 数 / 上架数</span><b>${s.goodsCount} / ${s.listedCount}</b></li>
          <li><span>库存实数量</span><b>${qty(s.stockQty)}</b></li>
          <li><span>库存流水行数</span><b>${s.stockLines}</b></li>
          <li><span>启用库位</span><b>${s.locCount}</b></li>
        </ul>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>这套库存为什么不会超卖</h3></div>
      <p class="hint" style="line-height:2">
        ① 支付时按 <code>FIFO</code> 把订单行拆到「库位 × 批次」并预占（<code>locked_qty</code>），各渠道共用一个可用量池；
        ② 每次扣减都是带版本号与数量条件的 <code>UPDATE</code>，并发下影响行数为 0 即判定冲突重试；
        ③ 发货与退货用独立 <code>biz_type</code>（21/22）+ 唯一索引做幂等，重复点击不重复记账；
        ④ 运维页有 6 项硬校验（可用量恒等式、非负、流水可累加、单据明细数、预占可解释、发货必有流水）。
      </p>
    </div>`;
}

/* ============================== 商品 SPU / SKU ============================== */
async function pageGoods(p = 1) {
  const kw = $('#goods-kw') ? $('#goods-kw').value.trim() : '';
  const showDeleted = $('#goods-all') ? $('#goods-all').checked : false;
  const d = await api('GET', `/api/v1/goods?page=${p}&size=15&keyword=${encodeURIComponent(kw)}&includeDeleted=${showDeleted ? 1 : 0}`);
  const rows = d.list.map((g) => {
    const deleted = Number(g.is_deleted) === 1;
    return [
      `<span class="mono">${esc(g.spu_code || '—')}</span>`,
      `<span class="mono">${esc(g.goods_code)}</span>`, esc(g.goods_name),
      attrsText(g.spec_attrs) ? `<span class="tag blue">${esc(attrsText(g.spec_attrs))}</span>` : '<span class="hint">—</span>',
      esc(g.category_name), esc(g.unit), `<span class="num">${yuan(g.cost_price)}</span>`,
      `<span class="num">${qty(g.stock_qty)}</span>`,
      deleted ? tagOf('已归档', 'gray') : (g.status ? tagOf('启用', 'green') : tagOf('停用', 'gray')),
      deleted
        ? permBtn('goods:edit', `<button class="btn small" onclick="__restoreGoods(${g.id})">恢复</button>`)
        : `${permBtn('goods:edit', `<button class="btn small" onclick="editGoods(${g.id})">编辑</button>`)}
           ${permBtn('goods:edit', `<button class="btn small danger" onclick="__archiveGoods(${g.id})">归档删除</button>`)}`,
    ];
  });
  $('#content').innerHTML = `
    <div class="card">
      <div class="panel-toolbar">
        <div class="toolbar-title-block"><span class="title-kicker">商品模型</span><h3>SPU / SKU</h3></div>
        <div class="toolbar-actions">
          <input class="input" id="goods-kw" placeholder="按编码 / 名称搜索" value="${esc(kw)}" style="width:200px"/>
          <label class="chk" style="margin:0"><input type="checkbox" id="goods-all" ${showDeleted ? 'checked' : ''}/> 显示已归档</label>
          <button class="btn" onclick="pageGoods(1)">查询</button>
          ${permBtn('goods:edit', '<button class="btn primary" onclick="newGoods()">+ 新增 SKU</button>')}
        </div>
      </div>
      <p class="hint" style="margin-bottom:10px">
        <b>SPU</b> 是「款」，<b>SKU</b> 是可卖单位。商品有库存或流水时<b>不能物理删除</b>（会毁掉台账），
        只能「下架 → 归档」：归档后商城不可见、不可再开新单，但历史流水/订单仍能查到它。
      </p>
      ${table([
        { title: '款号 SPU' }, { title: 'SKU 编码' }, { title: '名称' }, { title: '规格属性' }, { title: '分类' },
        { title: '单位' }, { title: '成本价', cls: 'num' }, { title: '当前库存', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '操作' },
      ], rows, '没有匹配的商品')}
      ${pager(d.total, d.page, d.size, (np) => pageGoods(np))}
    </div>`;
  if ($('#goods-kw')) $('#goods-kw').onkeydown = (e) => { if (e.key === 'Enter') pageGoods(1); };
  if ($('#goods-all')) $('#goods-all').onchange = () => pageGoods(1);
}

window.__archiveGoods = async (id) => {
  const chk = await api('GET', `/api/v1/goods/${id}/delete-check`);
  if (chk.alreadyDeleted) return toast('该商品已归档', 'info');
  if (!chk.deletable) {
    const items = chk.blockers.map(b => `<li>${esc(b.text)}</li>`).join('');
    openModal({
      title: '暂时不能删除 · ' + chk.goodsName,
      body: `<p class="hint" style="margin-bottom:10px">
          商品「${esc(chk.goodsName)}」还有未清的业务，直接删会让台账对不上。请先处理下面这些，再回来归档：</p>
        <ul style="line-height:2;padding-left:20px;font-size:13px">${items}</ul>
        <p class="hint" style="margin-top:10px">
          提示：库存不为 0 时，可以做一次<b>盘点</b>把实盘数改成 0（或走报损），库存清零后再归档。</p>`,
      footer: '<button class="btn primary" id="modal-close">知道了</button>',
    });
    return;
  }
  dialogForm({
    title: '归档删除 · ' + chk.goodsCode,
    fields: [{ id: 'reason', label: '删除原因（会写进操作日志）', value: '商品下架不再销售' }],
    submitText: '确认归档',
    async onSubmit(v, close) {
      const r = await api('POST', `/api/v1/goods/${id}/archive`, { reason: v.reason });
      close();
      toast(`已归档：${r.goodsCode}${r.spuArchived ? '（款下已无在售 SKU，款也一并归档）' : ''}`);
      loadBasics(); pageGoods();
    },
  });
};

window.__restoreGoods = async (id) => {
  try {
    await api('POST', `/api/v1/goods/${id}/restore`, {});
    toast('已恢复，请到「商品上下架」重新上架'); loadBasics(); pageGoods();
  } catch (e) { toast(e.message, 'error'); }
};

function goodsForm(g) {
  const cats = state.cache.categories.map(c => `<option value="${c.id}" ${g && g.category_id === c.id ? 'selected' : ''}>${esc(c.category_name)}</option>`).join('');
  return `<div class="form-grid">
    <div class="field"><label><i>*</i>SKU 编码</label><input class="input" id="f-code" value="${esc(g ? g.goods_code : '')}" ${g ? 'disabled' : ''} placeholder="如 SP015"/></div>
    <div class="field"><label><i>*</i>SKU 名称</label><input class="input" id="f-name" value="${esc(g ? g.goods_name : '')}"/></div>
    <div class="field"><label>分类</label><select class="select" id="f-cat">${cats}</select></div>
    <div class="field"><label>规格型号</label><input class="input" id="f-spec" value="${esc(g ? g.spec : '')}"/></div>
    <div class="field"><label><i>*</i>计量单位</label><input class="input" id="f-unit" value="${esc(g ? g.unit : '个')}"/></div>
    <div class="field"><label>条码</label><input class="input" id="f-barcode" value="${esc(g ? g.barcode : '')}"/></div>
    <div class="field"><label>商品图片 URL（留空则用分类占位图）</label>
      <input class="input" id="f-image" value="${esc(g ? (g.image_url || '') : '')}" placeholder="https://... 或 /img/cat-1.svg"/></div>
    <div class="field"><label>成本价</label><input class="input" id="f-cost" type="number" step="0.01" value="${g ? g.cost_price : 0}"/></div>
    <div class="field"><label>状态</label><select class="select" id="f-status">
      <option value="1" ${!g || g.status ? 'selected' : ''}>启用</option><option value="0" ${g && !g.status ? 'selected' : ''}>停用</option></select></div>
  </div>
  <div class="field"><label>规格属性 JSON（用于商城选规格，如 {"颜色":"红色"}）</label>
    <input class="input" id="f-attrs" value="${esc(g ? (g.spec_attrs || '') : '')}" placeholder='{"颜色":"红色","尺码":"M"}'/></div>
  <div class="field"><label>备注</label><textarea class="input" id="f-remark" rows="2">${esc(g ? g.remark : '')}</textarea></div>`;
}

function newGoods() {
  openModal({
    title: '新增商品', body: goodsForm(null),
    footer: '<button class="btn" id="modal-close">取消</button><button class="btn primary" id="save-goods">保存</button>',
    onMount(close) {
      $('#save-goods').onclick = async () => {
        try {
          await api('POST', '/api/v1/goods', {
            goods_code: $('#f-code').value.trim(), goods_name: $('#f-name').value.trim(),
            category_id: Number($('#f-cat').value), spec: $('#f-spec').value.trim(), unit: $('#f-unit').value.trim(),
            barcode: $('#f-barcode').value.trim(), cost_price: Number($('#f-cost').value || 0), remark: $('#f-remark').value.trim(),
            spec_attrs: $('#f-attrs').value.trim() || '{}',
            image_url: $('#f-image').value.trim(),
          });
          toast('SKU 已新增'); close(); await loadBasics(); pageGoods(1);
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

async function editGoods(id) {
  const g = state.cache.goods.find(x => x.id === id) || (await api('GET', `/api/v1/goods?size=200`)).list.find(x => x.id === id);
  openModal({
    title: '编辑商品 · ' + g.goods_code, body: goodsForm(g),
    footer: '<button class="btn" id="modal-close">取消</button><button class="btn primary" id="save-goods">保存</button>',
    onMount(close) {
      $('#save-goods').onclick = async () => {
        try {
          await api('PUT', '/api/v1/goods/' + id, {
            goods_code: g.goods_code, goods_name: $('#f-name').value.trim(), category_id: Number($('#f-cat').value),
            spec: $('#f-spec').value.trim(), unit: $('#f-unit').value.trim(), barcode: $('#f-barcode').value.trim(),
            cost_price: Number($('#f-cost').value || 0), status: Number($('#f-status').value), remark: $('#f-remark').value.trim(),
            spec_attrs: $('#f-attrs').value.trim() || '{}',
            image_url: $('#f-image').value.trim(),
          });
          toast('已保存'); close(); await loadBasics(); pageGoods();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

/* ============================== 仓库库位 ============================== */
async function pageLocations() {
  const locs = state.cache.locations;
  const byId = {}; locs.forEach(l => { byId[l.id] = l; });
  const typeName = { 1: ['仓库', 'purple'], 2: ['库区', 'blue'], 3: ['货架', 'orange'], 4: ['库位', 'green'] };
  const rows = locs.map(l => {
    const parent = l.parent_id ? (byId[l.parent_id] ? byId[l.parent_id].loc_name : '-') : '—';
    const wh = l.loc_type === 1 ? l.loc_name : (byId[l.wh_id] ? byId[l.wh_id].loc_name : '-');
    const depth = l.loc_type - 1;
    return [
      `<span style="padding-left:${depth * 16}px">${esc(l.loc_code)}</span>`,
      `<span style="padding-left:${depth * 16}px">${esc(l.loc_name)}</span>`,
      (typeName[l.loc_type] || ['?', 'gray'])[0], esc(parent), esc(wh),
      l.status ? '<span class="tag green">启用</span>' : '<span class="tag gray">停用</span>',
      l.loc_type === 4 ? `<button class="btn small" onclick="go('inventory')">查库存</button>` : '',
    ];
  });
  const tree = locs.filter(l => l.loc_type === 4).length;
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head"><h3>仓库库位树</h3>
        <span class="hint" style="margin-left:8px">共 ${locs.length} 个节点（其中末级库位 ${tree} 个）—— 库存挂载在末级库位上</span>
      </div>
      ${table([{ title: '编码' }, { title: '名称' }, { title: '类型', cls: 'center' }, { title: '上级' }, { title: '所属仓库' }, { title: '状态', cls: 'center' }, { title: '操作' }], rows)}
    </div>`;
}

/* ============================== 入库管理 ============================== */
async function pageInOrders(p = 1) {
  const f = $('#in-status') ? $('#in-status').value : '';
  const d = await api('GET', `/api/v1/in-orders?page=${p}&size=15&status=${f}`);
  const rows = d.list.map(o => {
    const [label, color] = STATUS.in[o.status] || ['?', 'gray'];
    return [
      `<span class="mono">${esc(o.order_no)}</span>`, esc(o.partner_name || '—'), qty(o.total_plan_num), qty(o.total_actual_num),
      `<span class="tag ${color}">${label}</span>`, esc(o.create_time),
      `<button class="btn small" onclick="openInOrder(${o.id})">查看 / 处理</button>`,
    ];
  });
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>入库管理</h3>
        <select class="select" id="in-status" style="width:130px">
          <option value="">全部状态</option>
          ${Object.entries(STATUS.in).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v[0]}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageInOrders(1)">筛选</button>
        <div class="spacer"></div>
        ${permBtn('in:edit', '<button class="btn primary" onclick="newInOrder()">+ 创建入库单</button>')}
      </div>
      ${table([
        { title: '入库单号' }, { title: '供应商' }, { title: '计划数量', cls: 'num' }, { title: '实收数量', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '创建时间' }, { title: '操作' },
      ], rows)}
      ${pager(d.total, d.page, d.size, (np) => pageInOrders(np))}
    </div>`;
}

function lineEditor(prefix, locations, goods, showBatch = true) {
  return `<table class="lines-table" id="${prefix}-lines">
    <thead><tr><th style="width:32%">商品</th><th style="width:26%">库位</th><th style="width:14%">数量</th>
      ${showBatch ? '<th style="width:18%">批次号</th>' : ''}<th style="width:10%"></th></tr></thead>
    <tbody></tbody></table>
    <div style="margin-top:8px"><button class="btn small" id="${prefix}-add">+ 添加一行</button></div>`;
}

function addLine(prefix, opts) {
  const { locations, goods, showBatch } = opts;
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><select class="select l-goods">${goods.map(g => `<option value="${g.id}">${esc(g.goods_code)} ${esc(g.goods_name)}（库存 ${qty(g.stock_qty)}）</option>`).join('')}</select></td>
    <td><select class="select l-loc">${locations.map(l => `<option value="${l.id}">${esc(l.loc_code)} ${esc(l.loc_name)}</option>`).join('')}</select></td>
    <td><input class="input l-num" type="number" min="0" step="1" value="10"/></td>
    ${showBatch ? '<td><input class="input l-batch" placeholder="可空"/></td>' : ''}
    <td class="center"><button class="btn small danger l-del">删除</button></td>`;
  tr.querySelector('.l-del').onclick = () => tr.remove();
  $(`#${prefix}-lines tbody`).appendChild(tr);
}

function warehouseIdForLocation(locationId) {
  const byId = Object.fromEntries(state.cache.locations.map(l => [l.id, l]));
  let current = byId[locationId];
  const visited = new Set();
  while (current && current.loc_type !== 1) {
    if (visited.has(current.id)) return null;
    visited.add(current.id);
    current = byId[current.parent_id];
  }
  return current && current.status ? current.id : null;
}

function refreshLineLocations(prefix, warehouseId) {
  const locations = state.cache.leafLocations.filter(l => warehouseIdForLocation(l.id) === Number(warehouseId));
  $$(`#${prefix}-lines .l-loc`).forEach(select => {
    const previous = Number(select.value);
    select.innerHTML = locations.map(l => `<option value="${l.id}">${esc(l.loc_code)} ${esc(l.loc_name)}</option>`).join('');
    if (locations.some(l => l.id === previous)) select.value = String(previous);
  });
}

function readLines(prefix, showBatch) {
  return $$(`#${prefix}-lines tbody tr`).map(tr => ({
    goods_id: Number(tr.querySelector('.l-goods').value),
    loc_id: Number(tr.querySelector('.l-loc').value),
    plan_num: Number(tr.querySelector('.l-num').value || 0),
    batch_no: showBatch ? tr.querySelector('.l-batch').value.trim() : '',
  })).filter(x => x.plan_num > 0);
}

function newInOrder() {
  const partners = state.cache.suppliers;
  const goods = state.cache.goods;
  const locs = state.cache.leafLocations;
  if (!goods.length || !locs.length) { toast('请先准备商品与库位数据', 'error'); return; }
  const opts = { locations: locs, goods, showBatch: true };
  openModal({
    title: '创建入库单', wide: true,
    body: `<div class="form-grid">
        <div class="field"><label>入库类型</label><select class="select" id="f-type">
          <option value="1">采购入库</option><option value="2">退货入库</option><option value="3">其他入库</option></select></div>
        <div class="field"><label>供应商</label><select class="select" id="f-partner">
          ${partners.map(p => `<option value="${p.id}">${esc(p.partner_name)}</option>`).join('')}</select></div>
        <div class="field"><label>入库仓库</label><select class="select" id="f-wh">
          ${state.cache.locations.filter(l => l.loc_type === 1).map(l => `<option value="${l.id}">${esc(l.loc_name)}</option>`).join('')}</select></div>
      </div>
      <div class="field"><label>备注</label><input class="input" id="f-remark" placeholder="选填"/></div>
      <div class="field"><label><i>*</i>入库明细（库位为所选仓库的末级库位，批次号选填）</label>${lineEditor('in', locs, goods, true)}</div>`,
    footer: '<button class="btn" id="modal-close">取消</button><button class="btn primary" id="save-in">保存草稿</button>',
    onMount(close) {
      $('#in-add').onclick = () => addLine('in', opts);
      addLine('in', opts);
      $('#f-wh').onchange = () => refreshLineLocations('in', $('#f-wh').value);
      refreshLineLocations('in', $('#f-wh').value);
      $('#save-in').onclick = async () => {
        const lines = readLines('in', true);
        if (!lines.length) { toast('请至少填写一行明细', 'error'); return; }
        try {
          const r = await api('POST', '/api/v1/in-orders', {
            order_type: Number($('#f-type').value), partner_id: Number($('#f-partner').value),
            wh_id: Number($('#f-wh').value), remark: $('#f-remark').value.trim(), lines,
          });
          toast('入库单已创建：' + r.orderNo);
          close(); pageInOrders(1);
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

function newOutOrder() {
  const partners = state.cache.customers;
  const goods = state.cache.goods;
  const locs = state.cache.leafLocations;
  if (!goods.length || !locs.length) { toast('请先准备商品与库位数据', 'error'); return; }
  const opts = { locations: locs, goods, showBatch: true };
  openModal({
    title: '创建出库单', wide: true,
    body: `<div class="form-grid">
        <div class="field"><label>出库类型</label><select class="select" id="f-type">
          <option value="1">销售出库</option><option value="2">领料出库</option></select></div>
        <div class="field"><label>客户</label><select class="select" id="f-partner">
          ${partners.map(p => `<option value="${p.id}">${esc(p.partner_name)}</option>`).join('')}</select></div>
        <div class="field"><label>出库仓库</label><select class="select" id="f-wh">
          ${state.cache.locations.filter(l => l.loc_type === 1).map(l => `<option value="${l.id}">${esc(l.loc_name)}</option>`).join('')}</select></div>
      </div>
      <div class="field"><label>备注</label><input class="input" id="f-remark" placeholder="选填"/></div>
      <div class="field"><label><i>*</i>出库明细（库位和批次需与库存记录一致，审核时校验可用量并预占）</label>${lineEditor('out', locs, goods, true)}</div>`,
    footer: '<button class="btn" id="modal-close">取消</button><button class="btn primary" id="save-out">保存草稿</button>',
    onMount(close) {
      $('#out-add').onclick = () => addLine('out', opts);
      addLine('out', opts);
      $('#f-wh').onchange = () => refreshLineLocations('out', $('#f-wh').value);
      refreshLineLocations('out', $('#f-wh').value);
      $('#save-out').onclick = async () => {
        const lines = readLines('out', true);
        if (!lines.length) { toast('请至少填写一行明细', 'error'); return; }
        try {
          const r = await api('POST', '/api/v1/out-orders', {
            order_type: Number($('#f-type').value), partner_id: Number($('#f-partner').value),
            wh_id: Number($('#f-wh').value), remark: $('#f-remark').value.trim(), lines,
          });
          toast('出库单已创建：' + r.orderNo);
          close(); pageOutOrders(1);
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

/* ------- 单据详情 / 处理（入库与出库共用） ------- */
async function openOrder(kind, id) {
  const url = kind === 'in' ? '/api/v1/in-orders/' : '/api/v1/out-orders/';
  const o = await api('GET', url + id);
  const isIn = kind === 'in';
  const [label, color] = (isIn ? STATUS.in : STATUS.out)[o.status] || ['?', 'gray'];
  const lines = o.lines.map(l => [
    `<span class="mono">${esc(l.goods_code)}</span>`, esc(l.goods_name), esc(l.loc_code),
    qty(l.plan_num), qty(l.actual_num), esc(l.batch_no || '—'),
    isIn ? `${yuan(l.plan_num * 0)}` && '' : (l.available_qty !== undefined ? qty(l.available_qty) : ''),
  ]);
  const headers = [{ title: '商品编码' }, { title: '名称' }, { title: '库位' }, { title: '计划', cls: 'num' },
    { title: '实际', cls: 'num' }, { title: '批次' }];
  if (!isIn) headers.push({ title: '当前可用', cls: 'num' });

  const editPerm = isIn ? 'in:edit' : 'out:edit';
  const confirmPerm = isIn ? 'in:confirm' : 'out:confirm';
  let actions = '';
  if (o.status === 0 && can(editPerm)) {
    actions += `<button class="btn primary" id="act-${isIn ? 'inaudit' : 'outaudit'}">${isIn ? '审核' : '审核并预占'}</button>`;
  }
  if (isIn && (o.status === 1 || o.status === 2) && can('in:confirm')) actions += `<button class="btn success" id="act-inconfirm">确认入库（过账）</button>`;
  if (!isIn && (o.status === 1 || o.status === 2) && can('out:confirm')) actions += `<button class="btn success" id="act-outconfirm">确认出库（过账）</button>`;
  if (o.status !== 3 && o.status !== 4 && can(editPerm)) actions += `<button class="btn danger" id="act-cancel">作废</button>`;

  openModal({
    title: `${isIn ? '入库单' : '出库单'} ${o.order_no}`, wide: true,
    body: `<div class="form-grid">
        <div class="field"><label>状态</label><div><span class="tag ${color}">${label}</span></div></div>
        <div class="field"><label>${isIn ? '供应商' : '客户'}</label><div>${esc(o.partner_name || '—')}</div></div>
        <div class="field"><label>计划 / 实际数量</label><div>${qty(o.total_plan_num)} / ${qty(o.total_actual_num)}</div></div>
        <div class="field"><label>创建时间</label><div>${esc(o.create_time)}</div></div>
      </div>
      <div class="field"><label>明细</label>${table(headers, lines, '无明细')}</div>
      <p class="hint">过账为幂等操作：同一个 <code>X-Idempotency-Key</code> 重复提交只会生效一次（可反复点"确认"验证）。</p>`,
    footer: `${actions}<span class="spacer"></span><button class="btn" id="modal-close">关闭</button>`,
    onMount(close) {
      const bind = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
      const refresh = () => { close(); isIn ? pageInOrders() : pageOutOrders(); };
      bind(`#act-${isIn ? 'inaudit' : 'outaudit'}`, async () => {
        try { await api('POST', url + id + '/audit', {}); toast('审核成功' + (isIn ? '' : '，已预占可用量')); refresh(); }
        catch (e) { toast(e.message, 'error'); }
      });
      bind('#act-inconfirm', async () => {
        const key = state.idem.get('in-' + id) || (state.idem.set('in-' + id, uuid()), state.idem.get('in-' + id));
        try {
          const r = await api('POST', url + id + '/confirm', {}, { 'X-Idempotency-Key': key });
          toast(r.duplicated ? '该单据已入库（重复提交被幂等拦下）' : '入库成功，库存已增加并生成流水');
          refresh();
        } catch (e) { toast(e.message, 'error'); }
      });
      bind('#act-outconfirm', async () => {
        const key = state.idem.get('out-' + id) || (state.idem.set('out-' + id, uuid()), state.idem.get('out-' + id));
        try {
          const r = await api('POST', url + id + '/confirm', {}, { 'X-Idempotency-Key': key });
          toast(r.duplicated ? '该单据已出库（重复提交被幂等拦下）' : '出库成功，库存已扣减并生成流水');
          refresh();
        } catch (e) { toast(e.message, 'error'); }
      });
      bind('#act-cancel', async () => {
        if (!confirm('确认作废该单据？出库单作废会释放已预占的可用量。')) return;
        try { await api('POST', url + id + '/cancel', {}); toast('已作废'); refresh(); }
        catch (e) { toast(e.message, 'error'); }
      });
    },
  });
}
const openInOrder = (id) => openOrder('in', id);
const openOutOrder = (id) => openOrder('out', id);

/* ============================== 出库管理 ============================== */
async function pageOutOrders(p = 1) {
  const f = $('#out-status') ? $('#out-status').value : '';
  const d = await api('GET', `/api/v1/out-orders?page=${p}&size=15&status=${f}`);
  const rows = d.list.map(o => {
    const [label, color] = STATUS.out[o.status] || ['?', 'gray'];
    return [
      `<span class="mono">${esc(o.order_no)}</span>`, esc(o.partner_name || '—'), qty(o.total_plan_num), qty(o.total_actual_num),
      `<span class="tag ${color}">${label}</span>`, esc(o.create_time),
      `<button class="btn small" onclick="openOutOrder(${o.id})">查看 / 处理</button>`,
    ];
  });
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>出库管理</h3>
        <select class="select" id="out-status" style="width:130px">
          <option value="">全部状态</option>
          ${Object.entries(STATUS.out).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v[0]}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageOutOrders(1)">筛选</button>
        <div class="spacer"></div>
        ${permBtn('out:edit', '<button class="btn primary" onclick="newOutOrder()">+ 创建出库单</button>')}
      </div>
      ${table([
        { title: '出库单号' }, { title: '客户' }, { title: '计划数量', cls: 'num' }, { title: '实发数量', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '创建时间' }, { title: '操作' },
      ], rows)}
      ${pager(d.total, d.page, d.size, (np) => pageOutOrders(np))}
    </div>`;
}

/* ============================== 库存查询 ============================== */
async function pageInventory(p = 1) {
  const kw = $('#inv-kw') ? $('#inv-kw').value.trim() : '';
  const only = $('#inv-only') ? $('#inv-only').value : '1';
  const d = await api('GET', `/api/v1/inventory?page=${p}&size=15&keyword=${encodeURIComponent(kw)}&onlyStock=${only}`);
  const rows = d.list.map(i => [
    `<span class="mono">${esc(i.goods_code)}</span>`, esc(i.goods_name), `<span class="mono">${esc(i.loc_code)}</span>`,
    esc(i.batch_no || '—'), esc(i.unit),
    `<span class="num"><b>${qty(i.quantity)}</b></span>`,
    `<span class="num">${qty(i.locked_qty)}</span>`,
    `<span class="num" style="color:${Number(i.available_qty) > 0 ? '#529b2e' : '#c45656'}">${qty(i.available_qty)}</span>`,
    `<span class="num">${yuan(i.amount)}</span>`,
    `<span class="mono" style="color:#909399">v${i.version}</span>`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>库存查询</h3>
        <input class="input" id="inv-kw" placeholder="商品编码 / 名称 / 库位" value="${esc(kw)}" style="width:240px"/>
        <select class="select" id="inv-only" style="width:150px">
          <option value="1" ${only === '1' ? 'selected' : ''}>只看有库存</option>
          <option value="0" ${only === '0' ? 'selected' : ''}>显示全部</option>
        </select>
        <button class="btn" onclick="pageInventory(1)">查询</button>
        <div class="spacer"></div>
        <span class="hint">可用量 = 实物 − 预占；本页库存金额合计 <b>￥${yuan(d.totalAmount)}</b></span>
      </div>
      ${table([
        { title: '商品编码' }, { title: '名称' }, { title: '库位' }, { title: '批次' }, { title: '单位' },
        { title: '实物', cls: 'num' }, { title: '预占', cls: 'num' }, { title: '可用', cls: 'num' },
        { title: '金额', cls: 'num' }, { title: '版本', cls: 'num' },
      ], rows, '没有库存记录')}
      ${pager(d.total, d.page, d.size, (np) => pageInventory(np))}
    </div>`;
  if ($('#inv-kw')) $('#inv-kw').onkeydown = (e) => { if (e.key === 'Enter') pageInventory(1); };
}

/* ============================== 库存流水 ============================== */
async function pageTxn(p = 1) {
  const d = await api('GET', `/api/v1/inventory/txn?page=${p}&size=15`);
  const rows = d.list.map(t => [
    `<span class="mono">${esc(t.txn_no)}</span>`,
    `<span class="tag ${t.change_qty > 0 ? 'green' : 'orange'}">${BIZ_TYPE[t.biz_type] || t.biz_type}</span>`,
    esc(t.biz_no || '—'), `<span class="mono">${esc(t.goods_code)}</span>`, esc(t.goods_name), `<span class="mono">${esc(t.loc_code)}</span>`,
    esc(t.batch_no || '—'),
    `<span class="num" style="color:${t.change_qty > 0 ? '#529b2e' : '#c45656'}">${t.change_qty > 0 ? '+' : ''}${qty(t.change_qty)}</span>`,
    `<span class="num">${qty(t.before_qty)}</span>`, `<span class="num">${qty(t.after_qty)}</span>`, esc(t.create_time),
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head"><h3>库存流水（台账明细）</h3>
        <span class="hint" style="margin-left:8px">库存的每一次变动都在这里留痕：变动前 → 变动后 可逐笔对账</span>
      </div>
      ${table([
        { title: '流水号' }, { title: '业务类型' }, { title: '来源单据' }, { title: '商品编码' }, { title: '名称' },
        { title: '库位' }, { title: '批次' }, { title: '变动', cls: 'num' }, { title: '变动前', cls: 'num' },
        { title: '变动后', cls: 'num' }, { title: '时间' },
      ], rows)}
      ${pager(d.total, d.page, d.size, (np) => pageTxn(np))}
    </div>`;
}

/* ============================== 库存台账 ============================== */
async function pageLedger() {
  const begin = $('#lg-begin') ? $('#lg-begin').value : new Date(new Date().getFullYear(), new Date().getMonth(), 1).toISOString().slice(0, 10);
  const end = $('#lg-end') ? $('#lg-end').value : new Date().toISOString().slice(0, 10);
  const d = await api('GET', `/api/v1/reports/ledger?begin=${begin}&end=${end}`);
  const rows = d.list.map(r => [
    `<span class="mono">${esc(r.goods_code)}</span>`, esc(r.goods_name), `<span class="mono">${esc(r.loc_code)}</span>`, esc(r.unit),
    `<span class="num">${qty(r.begin_qty)}</span>`, `<span class="num" style="color:#529b2e">+${qty(r.in_qty)}</span>`,
    `<span class="num" style="color:#c45656">-${qty(r.out_qty)}</span>`, `<span class="num"><b>${qty(r.end_qty)}</b></span>`,
    `<span class="num">${qty(r.current_available_qty)}</span>`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>库存台账</h3>
        <input class="input" type="date" id="lg-begin" value="${begin}" style="width:150px"/>
        <span>至</span>
        <input class="input" type="date" id="lg-end" value="${end}" style="width:150px"/>
        <button class="btn" onclick="pageLedger()">查询</button>
        <div class="spacer"></div>
        <span class="hint">口径：期初 + 期间入库 − 期间出库 = 查询日期末；可用量为当前库存状态</span>
      </div>
      ${table([
        { title: '商品编码' }, { title: '名称' }, { title: '库位' }, { title: '单位' },
        { title: '期初', cls: 'num' }, { title: '入库', cls: 'num' }, { title: '出库', cls: 'num' },
        { title: '查询日期期末', cls: 'num' }, { title: '当前可用', cls: 'num' },
      ], rows)}
    </div>`;
}

/* ============================== 操作日志 ============================== */
async function pageLogs(p = 1) {
  const d = await api('GET', `/api/v1/logs?page=${p}&size=15`);
  const opTag = { CREATE: 'blue', UPDATE: 'blue', AUDIT: 'purple', CONFIRM: 'green', CANCEL: 'red', LOGIN: 'gray', INIT: 'gray' };
  const rows = d.list.map(l => [
    esc(l.op_time), esc(l.user_name), esc(l.module),
    `<span class="tag ${opTag[l.op_type] || 'gray'}">${esc(l.op_type)}</span>`,
    esc(l.biz_no || '—'), `<span class="mono" style="font-size:12px">${esc((l.params || '').slice(0, 60))}</span>`,
    l.result === 'SUCCESS' ? '<span class="tag green">成功</span>' : `<span class="tag red">失败</span>`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head"><h3>操作日志</h3><span class="hint" style="margin-left:8px">增删改、审核、过账、登录都会留痕</span></div>
      ${table([
        { title: '时间' }, { title: '操作人' }, { title: '模块' }, { title: '类型', cls: 'center' },
        { title: '单号' }, { title: '参数摘要' }, { title: '结果', cls: 'center' },
      ], rows)}
      ${pager(d.total, d.page, d.size, (np) => pageLogs(np))}
    </div>`;
}

/* ============================== 路由表 & 启动 ============================== */
const PAGES = {
  dashboard: pageDashboard, goods: pageGoods, locations: pageLocations,
  'in-orders': pageInOrders, 'out-orders': pageOutOrders,
  inventory: pageInventory, txn: pageTxn, ledger: pageLedger, logs: pageLogs,
};

$('#login-btn').onclick = login;
$('#login-password').onkeydown = (e) => { if (e.key === 'Enter') login(); };
$('#btn-logout').onclick = logout;
$('#btn-reset').onclick = async () => {
  if (!confirm('重置演示数据？将删除当前所有单据与库存流水，恢复到初始演示数据。')) return;
  try { await api('POST', '/api/v1/admin/reset-demo', {}); await loadBasics(); toast('演示数据已重置'); go('dashboard'); }
  catch (e) { toast(e.message, 'error'); }
};
$$('#menu a').forEach(a => { a.onclick = () => go(a.dataset.page); });
window.addEventListener('hashchange', () => {
  const p = (location.hash || '').replace('#', '');
  if (PAGES[p] && p !== state.page) go(p);
});

// 启动：支持 ?autologin=1 免登录进入（演示/截图用）
const DEMO = new URLSearchParams(location.search);
if (!state.token && DEMO.get('autologin') === '1') {
  $('#login-username').value = 'admin';
  $('#login-password').value = '123456';
  login();
} else {
  boot();
}




