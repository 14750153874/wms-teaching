/* =============================================================================
   电商域前端（订单驱动）—— 与 app.js 共用 api / table / openModal / go 等工具
   覆盖：商城前台、我的订单、订单池、波次拣货、售后工作台、上下架、补货建议、运维
   设计原则：用户层（商城）只能下单；运营层（后台）才动库存与作业状态。
   ============================================================================= */
'use strict';

/* ------------------------------ 通用小工具 ------------------------------ */
// 通用表单弹窗：fields = [{ id, label, type: text|number|select|textarea, value, options, placeholder }]
function dialogForm(opts) {
  const fields = opts.fields || [];
  const body = fields.map(f => {
    const input = f.type === 'select'
      ? `<select class="select" id="dlg-${f.id}">${(f.options || []).map(o => `<option value="${esc(o.value)}" ${String(o.value) === String(f.value) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>`
      : f.type === 'textarea'
        ? `<textarea class="input" id="dlg-${f.id}" rows="2">${esc(f.value || '')}</textarea>`
        : `<input class="input" id="dlg-${f.id}" type="${f.type || 'text'}" value="${esc(f.value === undefined ? '' : f.value)}" placeholder="${esc(f.placeholder || '')}"/>`;
    return `<div class="field"><label>${f.required ? '<i>*</i>' : ''}${esc(f.label)}</label>${input}${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ''}</div>`;
  }).join('');
  openModal({
    title: opts.title,
    body,
    footer: `<button class="btn" id="modal-close">取消</button><button class="btn primary" id="dlg-submit">${esc(opts.submitText || '确定')}</button>`,
    onMount(close) {
      $('#dlg-submit').onclick = async () => {
        const values = {};
        fields.forEach(f => { const el = $('#dlg-' + f.id); values[f.id] = el ? el.value.trim() : ''; });
        try { await opts.onSubmit(values, close); } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
}

/* 当前页就地刷新（保持左侧菜单高亮，不跳回别的页面）
   注意：go() 会重渲染当前页并保留 hash，这里复用它。 */
function renderCurrentPage() { go(state.page); }

/* ============================== 商城前台（用户层） ============================== */async function pageShop() {
  const shops = state.cache.shops.length ? state.cache.shops : [{ id: 1, shop_name: '官方商城' }];
  if (!state.shopId) state.shopId = shops[0].id;
  const kw = $('#shop-kw') ? $('#shop-kw').value.trim() : '';
  const d = await api('GET', `/api/v1/shop/goods?shopId=${state.shopId}&keyword=${encodeURIComponent(kw)}`);
  state.shopGoods = {};
  const groups = new Map();
  (d.list || []).forEach(g => {
    state.shopGoods[g.goods_id] = g;
    const key = 'k' + (g.spu_id || ('g' + g.goods_id));
    if (!groups.has(key)) groups.set(key, { key, name: g.spu_name || g.goods_name, cat: g.category_name, skus: [] });
    groups.get(key).skus.push(g);
  });
  const cards = Array.from(groups.values()).map(gp => {
    const onShelf = gp.skus.filter(s => s.sale_status === 1);
    const chosen = gp.skus.find(s => s.goods_id === state.shopSel[gp.key]) || onShelf[0] || gp.skus[0];
    const available = Number(chosen.saleable) > 0 && chosen.sale_status === 1;
    const attrRow = gp.skus.length > 1
      ? `<div class="attr-row">${gp.skus.map(s => {
          const label = attrsText(s.spec_attrs) || s.goods_code;
          return `<span class="attr-chip ${s.goods_id === chosen.goods_id ? 'on' : ''} ${s.sale_status ? '' : 'off'}"
            onclick="__shopPick('${gp.key}',${s.goods_id})">${esc(label)}</span>`;
        }).join('')}</div>` : '';
    return `<div class="product-card">
      <img class="thumb" src="${esc(chosen.image_url || '/img/default.svg')}" alt="${esc(gp.name)}"
        loading="lazy" onerror="this.onerror=null;this.src='/img/default.svg'"/>
      <h4>${esc(gp.name)}</h4>
      <div class="sku-line">${esc(chosen.goods_code)}${gp.cat ? ' · ' + esc(gp.cat) : ''}</div>
      ${attrRow}
      <div class="price">￥${yuan(chosen.sale_price)}<small>/ ${esc(chosen.unit)}</small></div>
      <div class="sku-line">可售 <b style="color:${available ? '#529b2e' : '#c45656'}">${qty(chosen.saleable)}</b>
        · 累计售出 ${qty(chosen.sold_count)}
        ${chosen.sale_status ? '' : ' · <span class="tag gray">已下架</span>'}</div>
      <div class="product-foot">
        ${can('shop:buy') ? `<button class="btn primary" ${available ? '' : 'disabled'}
          onclick="__shopBuy(${chosen.goods_id})">${chosen.sale_status ? (available ? '立即购买' : '已售罄') : '已下架'}</button>`
        : '<span class="hint">当前账号无下单权限</span>'}
      </div>
    </div>`;
  }).join('');
  if (state.page !== 'shop') return;   // 用户已切页，别覆盖当前页面
  $('#content').innerHTML = `
    <div class="shop-hero">
      <h2>${esc(shops.find(s => s.id === state.shopId) ? shops.find(s => s.id === state.shopId).shop_name : '官方商城')}</h2>
      <p>这是「用户层」：用户看到的是<b>可售量</b>（全仓可用量 − 该渠道保留量），下单并不会直接改仓库数量；
      支付成功才按 FIFO 锁定具体库位与批次，发货过账时才真正扣减实物。</p>
    </div>
    <div class="card">
      <div class="card-head shop-toolbar">
        <h3>在售商品</h3>
        <select class="select" id="shop-select" style="width:150px">
          ${shops.map(s => `<option value="${s.id}" ${s.id === state.shopId ? 'selected' : ''}>${esc(s.shop_name)}</option>`).join('')}
        </select>
        <input class="input" id="shop-kw" placeholder="搜商品名称 / 编码" value="${esc(kw)}" style="width:220px"/>
        <button class="btn" onclick="pageShop()">搜索</button>
        <div class="spacer"></div>
        <span class="hint">共 ${(d.list || []).length} 个 SKU 在展示</span>
      </div>
      <div class="shop-grid">${cards || '<p class="hint">该店铺还没有上架商品</p>'}</div>
    </div>`;
  const sel = $('#shop-select');
  if (sel) sel.onchange = () => { state.shopId = Number(sel.value); state.shopSel = {}; pageShop(); };
  if ($('#shop-kw')) $('#shop-kw').onkeydown = (e) => { if (e.key === 'Enter') pageShop(); };
}
window.__shopPick = (key, goodsId) => { state.shopSel[key] = goodsId; pageShop(); };

window.__shopBuy = (goodsId) => {
  const g = (state.shopGoods || {})[goodsId];
  if (!g) return;
  dialogForm({
    title: '确认下单 · ' + g.goods_name,
    fields: [
      { id: 'qty', label: '购买数量', type: 'number', value: 1, required: true, hint: `可售 ${qty(g.saleable)} ${g.unit}` },
      { id: 'name', label: '收货人', value: '演示买家', required: true },
      { id: 'phone', label: '手机号', value: '13800001234' },
      { id: 'addr', label: '收货地址', type: 'textarea', value: '广东省深圳市南山区科技园 1 号', required: true },
    ],
    submitText: '提交订单并支付',
    async onSubmit(v, close) {
      const n = Number(v.qty);
      if (!(n > 0)) throw new Error('购买数量必须大于 0');
      const order = await api('POST', '/api/v1/shop/orders', {
        shop_id: state.shopId, buyer_name: v.name, buyer_phone: v.phone,
        receiver_name: v.name, receiver_phone: v.phone, receiver_address: v.addr,
        items: [{ goods_id: goodsId, qty: n }],
      });
      close();
      try {
        await api('POST', `/api/v1/shop/orders/${order.id}/pay`, {}, { 'X-Idempotency-Key': uuid() });
        toast(`支付成功：${order.orderNo} 已进入「待发货」，库存已按库位预占`);
      } catch (e) {
        toast(`下单成功（${order.orderNo}），但支付失败：${e.message}`, 'error');
      }
      await loadBasics();
      pageShop();
    },
  });
};

/* ============================== 我的订单（用户层） ============================== */
async function pageMyOrders(p = 1) {
  const f = $('#mo-status') ? $('#mo-status').value : '';
  const d = await api('GET', `/api/v1/shop/orders?page=${p}&size=12&status=${f}`);
  const rows = (d.list || []).map(o => [
    `<span class="mono">${esc(o.order_no)}</span>`,
    esc(o.shop_name), `${o.item_count} 种 / ${qty(o.total_qty)} 件`,
    `<span class="num">￥${yuan(o.pay_amount)}</span>`,
    tagOf(EC_TEXT[o.status] || o.status, EC_TAG[o.status]),
    esc(o.create_time),
    `<button class="btn small" onclick="openEcOrder(${o.id}, false)">详情</button>
     ${can('shop:buy') && o.status === 0 ? `<button class="btn small primary" onclick="__payOrder(${o.id})">去支付</button>` : ''}
     ${can('shop:buy') && o.status === 3 ? `<button class="btn small success" onclick="__receiveOrder(${o.id})">确认收货</button>` : ''}
     ${can('shop:buy') && (o.status === 0 || o.status === 1) ? `<button class="btn small danger" onclick="__cancelOrder(${o.id})">取消</button>` : ''}`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>我的订单</h3>
        <select class="select" id="mo-status" style="width:130px">
          <option value="">全部</option>
          ${Object.entries(EC_TEXT).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageMyOrders(1)">筛选</button>
        <div class="spacer"></div>
        <button class="btn" onclick="go('shop')">继续逛逛</button>
      </div>
      ${table([{ title: '订单号' }, { title: '店铺' }, { title: '商品' }, { title: '金额', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '下单时间' }, { title: '操作' }], rows, '还没有订单')}
      ${pager(d.total, d.page, d.size, (np) => pageMyOrders(np))}
    </div>`;
}
window.__payOrder = async (id) => {
  try {
    await api('POST', `/api/v1/shop/orders/${id}/pay`, {}, { 'X-Idempotency-Key': uuid() });
    toast('支付成功，订单进入待发货'); pageMyOrders();
  } catch (e) { toast(e.message, 'error'); }
};
window.__receiveOrder = async (id) => {
  try { await api('POST', `/api/v1/shop/orders/${id}/receive`, {}); toast('已确认收货'); pageMyOrders(); }
  catch (e) { toast(e.message, 'error'); }
};
window.__cancelOrder = (id) => {
  dialogForm({
    title: '取消订单', fields: [{ id: 'reason', label: '取消原因', value: '不想要了' }],
    submitText: '确认取消',
    async onSubmit(v, close) {
      await api('POST', `/api/v1/shop/orders/${id}/cancel`, { reason: v.reason });
      close(); toast('订单已取消，预占库存已释放'); pageMyOrders();
    },
  });
};

/* ============================== 订单详情（运营/用户共用） ============================== */
async function openEcOrder(id, admin) {
  const o = await api('GET', '/api/v1/ec/orders/' + id);
  const items = o.items.map(i => [
    `<span class="mono">${esc(i.goods_code)}</span>`, esc(i.goods_name),
    attrsText(i.spec_attrs) || '—', `<span class="num">￥${yuan(i.price)}</span>`,
    `<span class="num">${qty(i.qty)}</span>`, `<span class="num">￥${yuan(i.amount)}</span>`,
  ]);
  const allocs = o.allocs.map(a => [
    `<span class="mono">${esc(a.loc_code)}</span>`, esc(a.goods_code), esc(attrsText(a.spec_attrs) || '—'),
    esc(a.batch_no || '无批次'), `<span class="num">${qty(a.alloc_qty)}</span>`,
    tagOf(ALLOC_TEXT[a.status] || a.status, a.status === 3 ? 'green' : a.status === 4 ? 'gray' : 'blue'),
  ]);
  const afterRows = (o.afterSales || []).map(a => [
    `<span class="mono">${esc(a.after_no)}</span>`, esc(a.typeText), esc(a.goods_name), qty(a.qty),
    tagOf(a.statusText, AS_TAG[a.status]), qty(a.good_qty), qty(a.bad_qty), esc(a.ref_order_no || '—'),
  ]);
  const kv = `
    <div class="kv">
      <b>订单号</b><span class="mono">${esc(o.order_no)}</span>
      <b>订单类型</b><span>${esc(o.kindText)}${o.parent_order_id ? `（原单 #${o.parent_order_id}）` : ''}</span>
      <b>状态</b><span>${tagOf(o.statusText, EC_TAG[o.status])}</span>
      <b>店铺 / 买家</b><span>${esc(o.shop_name)} / ${esc(o.buyer_name)}</span>
      <b>收货信息</b><span>${esc(o.receiver_name || '')} ${esc(o.receiver_phone || '')}<br/>${esc(o.receiver_address || '')}</span>
      <b>金额</b><span>商品 ￥${yuan(o.goods_amount)} + 运费 ￥${yuan(o.freight)} = <b>￥${yuan(o.pay_amount)}</b></span>
      <b>时间</b><span>下单 ${esc(o.create_time)}${o.pay_time ? ' · 支付 ' + esc(o.pay_time) : ''}${o.ship_time ? ' · 发货 ' + esc(o.ship_time) : ''}</span>
      <b>波次 / 包裹</b><span>${o.wave ? esc(o.wave.wave_no) + '（' + WAVE_TEXT[o.wave.status] + '）' : '未进波次'}
        ${o.package ? ' · 包裹 ' + esc(o.package.package_no) + ' / ' + o.package.weight_g + 'g' : ''}
        ${o.shipment ? ' · ' + esc(o.shipment.carrier) + ' ' + esc(o.shipment.tracking_no) : ''}</span>
    </div>`;
  const actions = [];
  if (admin) {
    if (o.status === 0 && can('shop:buy')) actions.push(`<button class="btn primary" onclick="__payOrder(${o.id})">代客支付</button>`);
    if (o.status === 1 && can('ec:wave:create')) actions.push(`<button class="btn" onclick="__waveFrom(${o.id})">加入波次</button>`);
    if (o.status === 7 && !o.package && can('ec:order:edit')) actions.push(`<button class="btn" onclick="__packOrder(${o.id})">打包称重</button>`);
    if ((o.status === 7 || (o.status === 1 || o.status === 2) && !o.wave) && o.status !== 3 && can('ec:order:ship')) {
      actions.push(`<button class="btn success" onclick="__shipOrder(${o.id})">发货</button>`);
    }
    if ((o.status === 0 || o.status === 1) && can('ec:order:edit')) actions.push(`<button class="btn danger" onclick="__cancelOrder(${o.id})">取消订单</button>`);
    if ((o.status === 3 || o.status === 4) && can('ec:aftersale:audit')) actions.push(`<button class="btn" onclick="__afterSale(${o.id})">代客申请售后</button>`);
  } else {
    if (o.status === 0 && can('shop:buy')) actions.push(`<button class="btn primary" onclick="__payOrder(${o.id})">去支付</button>`);
    if (o.status === 3 && can('shop:buy')) actions.push(`<button class="btn success" onclick="__receiveOrder(${o.id})">确认收货</button>`);
    if ((o.status === 3 || o.status === 4) && can('shop:buy')) actions.push(`<button class="btn" onclick="__afterSale(${o.id})">申请售后</button>`);
  }
  openModal({
    title: `订单 ${o.order_no}`, wide: true,
    body: `${kv}
      <div class="field"><label>商品明细</label>
        ${table([{ title: 'SKU' }, { title: '名称' }, { title: '规格' }, { title: '单价', cls: 'num' },
          { title: '数量', cls: 'num' }, { title: '金额', cls: 'num' }], items, '无明细')}</div>
      <div class="field"><label>库存分配（支付时按 FIFO 锁定的库位 × 批次）</label>
        ${table([{ title: '库位' }, { title: 'SKU' }, { title: '规格' }, { title: '批次' },
          { title: '分配数量', cls: 'num' }, { title: '状态', cls: 'center' }], allocs, '还没有分配记录（未支付）')}</div>
      ${afterRows.length ? `<div class="field"><label>售后记录</label>
        ${table([{ title: '售后单' }, { title: '类型' }, { title: '商品' }, { title: '数量', cls: 'num' },
          { title: '状态', cls: 'center' }, { title: '良品', cls: 'num' }, { title: '残次', cls: 'num' },
          { title: '关联单' }], afterRows)}</div>` : ''}`,
    footer: `${actions.join(' ')}<span class="spacer"></span><button class="btn" id="modal-close">关闭</button>`,
  });
}
window.openEcOrder = openEcOrder;
window.__waveFrom = (id) => {
  dialogForm({
    title: '加入波次', fields: [{ id: 'wave', label: '波次号（留空则新建一个波次）', placeholder: '如 BC202601010001' }],
    submitText: '生成波次',
    async onSubmit(v, close) {
      const list = await api('GET', '/api/v1/ec/waves?status=0');
      // 演示：一次一单也能建波次；批量在「订单池」页勾选
      await api('POST', '/api/v1/ec/waves', { order_ids: [id] });
      close(); toast('波次已生成，去「波次拣货」作业'); loadBasics(); go('waves');
    },
  });
};
window.__packOrder = (id) => {
  dialogForm({
    title: '打包称重',
    fields: [
      { id: 'weight', label: '包裹重量(g)', type: 'number', value: 800, required: true },
      { id: 'remark', label: '包材/备注', value: '标准 3 号箱 + 气泡膜' },
    ],
    submitText: '完成打包',
    async onSubmit(v, close) {
      const r = await api('POST', `/api/v1/ec/orders/${id}/pack`, { weight_g: Number(v.weight), remark: v.remark });
      close(); toast('打包完成：' + r.packageNo); renderCurrentPage();
    },
  });
};
window.__shipOrder = (id) => {
  dialogForm({
    title: '发货过账',
    fields: [
      { id: 'carrier', label: '承运商', type: 'select', value: '顺丰速运',
        options: ['顺丰速运', '中通快递', '圆通速递', '韵达快递', '京东物流', '自提'].map(x => ({ value: x, label: x })) },
      { id: 'tracking', label: '运单号', required: true, value: 'SF' + Date.now().toString().slice(-13) },
    ],
    hintText: '发货会一次性完成：扣减实物 + 释放预占 + 写库存流水 + 生成发货单',
    submitText: '确认发货',
    async onSubmit(v, close) {
      const r = await api('POST', `/api/v1/ec/orders/${id}/ship`, { carrier: v.carrier, tracking_no: v.tracking });
      close(); toast(`已发货：${r.shipmentNo}，扣减 ${qty(r.shippedQty)} 件`);
      loadBasics(); renderCurrentPage();
    },
  });
};
window.__afterSale = async (id) => {
  const o = await api('GET', '/api/v1/ec/orders/' + id);
  const options = o.items.map(i => ({ value: i.goods_id, label: `${i.goods_code} ${i.goods_name} × ${qty(i.qty)}` }));
  dialogForm({
    title: '申请售后 · ' + o.order_no,
    fields: [
      { id: 'goods', label: '售后商品', type: 'select', options, value: options[0] && options[0].value },
      { id: 'type', label: '售后类型', type: 'select', value: 1,
        options: Object.entries(AS_TYPE).map(([k, v]) => ({ value: k, label: v })) },
      { id: 'qty', label: '数量', type: 'number', value: 1, required: true },
      { id: 'reason', label: '原因', value: '商品有质量问题' },
    ],
    submitText: '提交售后',
    async onSubmit(v, close) {
      const r = await api('POST', '/api/v1/ec/after-sales', {
        order_id: id, goods_id: Number(v.goods), type: Number(v.type), qty: Number(v.qty), reason: v.reason,
      });
      close(); toast('售后单已创建：' + r.afterNo); go('after-sales');
    },
  });
};

/* ============================== 订单池（运营层） ============================== */
async function pageEcOrders(p = 1) {
  const f = $('#ec-status') ? $('#ec-status').value : '';
  const kw = $('#ec-kw') ? $('#ec-kw').value.trim() : '';
  const d = await api('GET', `/api/v1/ec/orders?page=${p}&size=15&status=${f}&keyword=${encodeURIComponent(kw)}`);
  const rows = (d.list || []).map(o => [
    `<input type="checkbox" class="row-check" data-id="${o.id}" ${o.status === 1 ? '' : 'disabled'}
       ${state.pickedOrders.has(o.id) ? 'checked' : ''} onclick="__togglePick(${o.id}, this.checked)"/>`,
    `<span class="mono">${esc(o.order_no)}</span>`,
    esc(o.buyer_name), `<span class="num">${qty(o.total_qty)}</span>`,
    `<span class="num">￥${yuan(o.pay_amount)}</span>`,
    tagOf(o.statusText + (o.kindText !== '销售单' ? '·' + o.kindText : ''), EC_TAG[o.status]),
    esc(o.create_time),
    `<button class="btn small" onclick="openEcOrder(${o.id}, true)">处理</button>`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="panel-toolbar">
        <div class="toolbar-title-block"><span class="title-kicker">运营层</span><h3>订单池</h3></div>
        <div class="toolbar-actions">
          <select class="select" id="ec-status" style="width:130px">
            <option value="">全部状态</option>
            ${Object.entries(EC_TEXT).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v}</option>`).join('')}
          </select>
          <input class="input" id="ec-kw" placeholder="订单号 / 买家" value="${esc(kw)}" style="width:180px"/>
          <button class="btn" onclick="pageEcOrders(1)">筛选</button>
          ${permBtn('ec:wave:create', '<button class="btn primary" onclick="__waveFromSelected()">勾选订单合成波次</button>')}
        </div>
      </div>
      <p class="hint" style="margin-bottom:10px">
        只有 <b>待发货</b> 的订单可以进波次（勾选左侧复选框）。合成波次后 → 波次拣货 → 复核 → 打包 → 发货。
        单张订单也可以不建波次，直接「处理 → 发货」（单件直发，系统自动补包裹记录）。
      </p>
      ${table([{ title: '', cls: 'center' }, { title: '订单号' }, { title: '买家' }, { title: '件数', cls: 'num' },
        { title: '金额', cls: 'num' }, { title: '状态', cls: 'center' }, { title: '下单时间' }, { title: '操作' }],
        rows, '没有订单')}
      ${pager(d.total, d.page, d.size, (np) => pageEcOrders(np))}
    </div>`;
}
window.__togglePick = (id, on) => { on ? state.pickedOrders.add(id) : state.pickedOrders.delete(id); };
window.__waveFromSelected = async () => {
  const ids = Array.from(state.pickedOrders);
  if (!ids.length) return toast('请先勾选待发货订单', 'error');
  try {
    const r = await api('POST', '/api/v1/ec/waves', { order_ids: ids });
    state.pickedOrders.clear();
    toast(`波次 ${r.waveNo} 已生成：${r.orderCount} 单 / ${r.itemCount} 条拣货任务`);
    go('waves');
  } catch (e) { toast(e.message, 'error'); }
};

/* ============================== 波次拣货（作业核心） ============================== */
async function pageWaves(p = 1) {
  const f = $('#wave-status') ? $('#wave-status').value : '';
  const d = await api('GET', `/api/v1/ec/waves?page=${p}&size=15&status=${f}`);
  const rows = (d.list || []).map(w => [
    `<span class="mono">${esc(w.wave_no)}</span>`,
    tagOf(w.statusText, WAVE_TAG[w.status]),
    esc(w.wh_name || '—'),
    `<span class="num">${w.order_count}</span>`,
    `<span class="num">${w.item_count}</span>`,
    `<span class="num">${qty(w.total_qty)}</span>`,
    w.status < 3 ? `<span class="num" style="color:${w.open_tasks ? '#c45656' : '#529b2e'}">${w.open_tasks}</span>` : '—',
    esc(w.create_time),
    `<button class="btn small primary" onclick="openWave(${w.id})">拣货作业</button>`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>波次拣货</h3>
        <select class="select" id="wave-status" style="width:130px">
          <option value="">全部状态</option>
          ${Object.entries(WAVE_TEXT).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageWaves(1)">筛选</button>
        <div class="spacer"></div>
        <span class="hint">一个波次 = 一批订单 + 一条按库位排好序的拣货路径；拣完复核通过才允许打包发货</span>
      </div>
      ${table([{ title: '波次号' }, { title: '状态', cls: 'center' }, { title: '仓库' }, { title: '订单数', cls: 'num' },
        { title: '任务数', cls: 'num' }, { title: '总数量', cls: 'num' }, { title: '待拣', cls: 'num' },
        { title: '创建时间' }, { title: '操作' }], rows, '还没有波次')}
      ${pager(d.total, d.page, d.size, (np) => pageWaves(np))}
    </div>`;
}

async function openWave(id) {
  const w = await api('GET', '/api/v1/ec/waves/' + id);
  const taskRows = w.tasks.map(t => [
    `<span class="task-loc">${esc(t.loc_code)}</span>`,
    `<span class="mono">${esc(t.goods_code)}</span>`,
    esc(t.goods_name), esc(attrsText(t.spec_attrs) || '—'), esc(t.batch_no || '—'),
    `<span class="num">${qty(t.qty)}</span>`,
    `<span class="num">${qty(t.picked_qty)}</span>`,
    esc(t.order_no),
    t.status === 1 ? tagOf('已拣货', 'green') : tagOf('待拣', 'orange'),
    t.status === 1 ? '' : (can('ec:wave:pick')
      ? `<input class="mini-input" id="pick-${t.id}" type="number" value="${t.qty}"/>
      <button class="btn small" onclick="__pickTask(${w.id},${t.id})">拣货</button>` : '<span class="hint">无拣货权限</span>'),
  ]);
  const orderRows = w.orders.map(o => [
    `<span class="mono">${esc(o.order_no)}</span>`, esc(o.buyer_name),
    `<span class="num">${o.item_count}</span>`, `<span class="num">${qty(o.total_qty)}</span>`,
    `<span class="num">￥${yuan(o.pay_amount)}</span>`,
    tagOf(o.status === 0 ? '待拣货' : o.status === 1 ? '已拣货' : o.status === 2 ? '待打包' : '已发货',
      o.status === 0 ? 'orange' : o.status === 1 ? 'blue' : o.status === 2 ? 'purple' : 'green'),
    `<span style="display:flex;gap:6px;flex-wrap:wrap">
      ${o.status < 2 && can('ec:wave:review') ? `<button class="btn small" onclick="__reviewWave(${w.id},${o.order_id})">复核</button>` : ''}
      ${o.status === 2 && can('ec:order:edit') ? `<button class="btn small" onclick="__packOrder(${o.order_id})">打包</button>` : ''}
      ${o.status === 2 && can('ec:order:ship') ? `<button class="btn small success" onclick="__shipOrder(${o.order_id})">发货</button>` : ''}
      <button class="btn small" onclick="openEcOrder(${o.order_id}, true)">订单</button>
    </span>`,
  ]);
  const canPick = w.status < 2;
  openModal({
    title: `波次 ${w.wave_no}（${w.statusText}）`, wide: true,
    body: `<div class="kv" style="margin-bottom:12px">
        <b>仓库</b><span>${esc(w.wh_name || '—')}</span>
        <b>订单 / 任务</b><span>${w.order_count} 单 / ${w.item_count} 条 / 共 ${qty(w.total_qty)} 件</span>
        <b>说明</b><span>拣货任务已按<b>库位编码排序</b>，这就是仓库里的行走路径；实拣数小于应拣数时复核会拦下。</span>
      </div>
      <div class="field"><label>拣货任务（按库位路径）</label>
        ${table([{ title: '库位' }, { title: 'SKU' }, { title: '名称' }, { title: '规格' }, { title: '批次' },
          { title: '应拣', cls: 'num' }, { title: '已拣', cls: 'num' }, { title: '所属订单' },
          { title: '状态', cls: 'center' }, { title: '操作' }], taskRows, '没有拣货任务')}</div>
      <div class="field"><label>波次内订单</label>
        ${table([{ title: '订单号' }, { title: '买家' }, { title: '种数', cls: 'num' }, { title: '件数', cls: 'num' },
          { title: '金额', cls: 'num' }, { title: '作业状态', cls: 'center' }, { title: '下一步' }], orderRows)}</div>`,
    footer: `${canPick && can('ec:wave:pick') ? `<button class="btn primary" onclick="__pickAll(${w.id})">整波一键拣完</button>` : ''}
      ${w.status < 3 && can('ec:wave:create') ? `<button class="btn danger" onclick="__cancelWave(${w.id})">取消波次</button>` : ''}
      <span class="spacer"></span><button class="btn" id="modal-close">关闭</button>`,
    onMount() {
      // 拣完一批后刷新弹窗，保证数量和状态同步
      window.__refreshWave = () => { openWave(id); pageWaves(); };
    },
  });
}
window.openWave = openWave;
window.__pickTask = async (waveId, taskId) => {
  const el = $('#pick-' + taskId);
  const n = el ? Number(el.value) : 0;
  try {
    const r = await api('POST', `/api/v1/ec/waves/${waveId}/pick`, { task_id: taskId, picked_qty: n });
    toast(`已拣 ${qty(n)} 件（波次共拣 ${qty(r.pickedQty)}）`);
    openWave(waveId); pageWaves();
  } catch (e) { toast(e.message, 'error'); }
};
window.__pickAll = async (waveId) => {
  try {
    const r = await api('POST', `/api/v1/ec/waves/${waveId}/pick`, { all: true });
    toast(`整波拣完：${r.pickedTasks} 条任务 / ${qty(r.pickedQty)} 件`);
    openWave(waveId); pageWaves();
  } catch (e) { toast(e.message, 'error'); }
};
window.__reviewWave = async (waveId, orderId) => {
  try {
    await api('POST', `/api/v1/ec/waves/${waveId}/review`, { order_id: orderId });
    toast('复核通过，订单进入「待打包」');
    openWave(waveId); pageWaves();
  } catch (e) { toast(e.message, 'error'); }
};
window.__cancelWave = async (waveId) => {
  if (!confirm('取消这个波次？订单会回到「待发货」，预占库存不变。')) return;
  try { await api('POST', `/api/v1/ec/waves/${waveId}/cancel`, {}); toast('波次已取消'); $('#modal-root').innerHTML = ''; pageWaves(); }
  catch (e) { toast(e.message, 'error'); }
};

/* ============================== 售后工作台 ============================== */
async function pageAfterSales(p = 1) {
  const f = $('#as-status') ? $('#as-status').value : '';
  const d = await api('GET', `/api/v1/ec/after-sales?page=${p}&size=15&status=${f}`);
  const rows = (d.list || []).map(a => [
    `<span class="mono">${esc(a.after_no)}</span>`,
    `<span class="mono">${esc(a.order_no)}</span>`,
    esc(a.buyer_name),
    `<span class="tag blue">${esc(a.typeText)}</span>`,
    esc(a.goods_name), qty(a.qty),
    tagOf(a.statusText, AS_TAG[a.status]),
    `${qty(a.good_qty)} / ${qty(a.bad_qty)}`,
    esc(a.ref_order_no || '—'),
    `<span style="display:flex;gap:6px;flex-wrap:wrap">
      ${a.status === 0 && can('ec:aftersale:audit') ? `<button class="btn small success" onclick="__asApprove(${a.id})">同意</button>
        <button class="btn small danger" onclick="__asReject(${a.id})">拒绝</button>` : ''}
      ${a.status === 1 && can('ec:aftersale:receive') ? `<button class="btn small primary" onclick="__asReceive(${a.id},${qty(a.qty)})">质检收货</button>` : ''}
      <button class="btn small" onclick="openEcOrder(${a.order_id}, true)">订单</button>
    </span>`,
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>售后工作台</h3>
        <select class="select" id="as-status" style="width:130px">
          <option value="">全部</option>
          ${Object.entries(AS_TEXT).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageAfterSales(1)">筛选</button>
        <div class="spacer"></div>
        <span class="hint">退货退款/换货要经过「质检收货」：良品回原库位可再售，残次进 <code>WH01-QC-01</code> 次品区</span>
      </div>
      ${table([{ title: '售后单' }, { title: '原订单' }, { title: '买家' }, { title: '类型', cls: 'center' },
        { title: '商品' }, { title: '数量', cls: 'num' }, { title: '状态', cls: 'center' },
        { title: '良品 / 残次', cls: 'num' }, { title: '关联单' }, { title: '操作' }], rows, '没有售后单')}
      ${pager(d.total, d.page, d.size, (np) => pageAfterSales(np))}
    </div>`;
}
window.__asApprove = async (id) => {
  try {
    const r = await api('POST', `/api/v1/ec/after-sales/${id}/approve`, {});
    toast(r.status === 2 ? '已同意并自动开替换单（补发/仅退款直接完成）' : '已同意，等待退货质检');
    pageAfterSales();
  } catch (e) { toast(e.message, 'error'); }
};
window.__asReject = async (id) => {
  try { await api('POST', `/api/v1/ec/after-sales/${id}/reject`, {}); toast('已拒绝'); pageAfterSales(); }
  catch (e) { toast(e.message, 'error'); }
};
window.__asReceive = (id, total) => {
  dialogForm({
    title: '退货质检收货',
    fields: [
      { id: 'good', label: '良品数量（回原库位，可再售）', type: 'number', value: total, required: true },
      { id: 'bad', label: '残次数量（进次品区，不可售）', type: 'number', value: 0, required: true },
    ],
    submitText: '确认收货并入库',
    async onSubmit(v, close) {
      const r = await api('POST', `/api/v1/ec/after-sales/${id}/receive`, {
        good_qty: Number(v.good), bad_qty: Number(v.bad),
      });
      close(); toast(`已入库：良品 ${qty(r.goodQty)}，残次 ${qty(r.badQty)}`); loadBasics(); pageAfterSales();
    },
  });
};

/* ============================== 商品上下架 ============================== */
async function pageListings(p = 1) {
  const d = await api('GET', '/api/v1/listings');
  const rows = (d.list || []).map(l => [
    esc(l.shop_name), `<span class="mono">${esc(l.goods_code)}</span>`, esc(l.goods_name),
    esc(attrsText(l.spec_attrs) || '—'),
    `<span class="num">￥${yuan(l.cost_price)}</span>`,
    `<span class="num">￥${yuan(l.sale_price)}</span>`,
    `<span class="num" style="color:${l.margin > 0 ? '#529b2e' : '#c45656'}">￥${yuan(l.margin)}</span>`,
    `<span class="num">${qty(l.saleable)}</span>`,
    l.sale_status ? tagOf('已上架', 'green') : tagOf('已下架', 'gray'),
    can('listing:edit') ? `<button class="btn small" onclick="__editListing(${l.id},${l.sale_price},${l.reserve_qty},${l.sale_status})">编辑</button>
     <button class="btn small ${l.sale_status ? 'danger' : 'success'}" onclick="__toggleListing(${l.id},${l.sale_status})">
       ${l.sale_status ? '下架' : '上架'}</button>` : '<span class="hint">只读</span>',
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>商品上下架（渠道 × SKU）</h3>
        <div class="spacer"></div>
        ${permBtn('listing:edit', '<button class="btn primary" onclick="__newListing()">+ 新增上架</button>')}
      </div>
      <p class="hint" style="margin-bottom:10px">
        下架立即停止接单（商城前台不可见、下单接口直接拒绝）；<b>保留量</b>是该渠道不参与售卖的数量，
        用于给线下或其他渠道留货，演示按「可售 = 全仓可用 − 保留量」计算。
      </p>
      ${table([{ title: '店铺' }, { title: 'SKU' }, { title: '名称' }, { title: '规格' }, { title: '成本', cls: 'num' },
        { title: '售价', cls: 'num' }, { title: '毛利', cls: 'num' }, { title: '可售', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '操作' }], rows, '还没有上架记录')}
    </div>`;
}
window.__toggleListing = async (id, status) => {
  try {
    await api('PUT', `/api/v1/listings/${id}`, { sale_status: status ? 0 : 1 });
    toast(status ? '已下架' : '已上架'); loadBasics(); pageListings();
  } catch (e) { toast(e.message, 'error'); }
};
window.__editListing = (id, price, reserve, status) => {
  dialogForm({
    title: '编辑上下架',
    fields: [
      { id: 'price', label: '售价', type: 'number', value: price, required: true },
      { id: 'reserve', label: '渠道保留量', type: 'number', value: reserve },
      { id: 'status', label: '状态', type: 'select', value: status,
        options: [{ value: 1, label: '上架' }, { value: 0, label: '下架' }] },
    ],
    submitText: '保存',
    async onSubmit(v, close) {
      await api('PUT', `/api/v1/listings/${id}`, {
        sale_price: Number(v.price), reserve_qty: Number(v.reserve), sale_status: Number(v.status),
      });
      close(); toast('已保存'); pageListings();
    },
  });
};
window.__newListing = () => {
  const goods = state.cache.goods.map(g => ({ value: g.id, label: `${g.goods_code} ${g.goods_name}（成本 ${yuan(g.cost_price)}）` }));
  const shops = state.cache.shops.map(s => ({ value: s.id, label: s.shop_name }));
  const cats = state.cache.categories.map(c => ({ value: c.id, label: c.category_name }));
  const locs = state.cache.leafLocations.map(l => ({ value: l.id, label: `${l.loc_code} ${l.loc_name}` }));
  openModal({
    title: '新增上架',
    wide: true,
    body: `
      <div class="field"><label>上架方式</label>
        <select class="select" id="nl-mode">
          <option value="existing">从已有 SKU 上架（只加渠道价）</option>
          <option value="new">新建商品并上架（款 + SKU + 图片 + 初始库存）</option>
        </select>
      </div>
      <div id="nl-existing">
        <div class="form-grid">
          <div class="field"><label>店铺</label><select class="select" id="nl-shop">${shops.map(s => `<option value="${s.value}">${esc(s.label)}</option>`).join('')}</select></div>
          <div class="field"><label>SKU</label><select class="select" id="nl-goods">${goods.map(g => `<option value="${g.value}">${esc(g.label)}</option>`).join('')}</select></div>
          <div class="field"><label>售价</label><input class="input" id="nl-price" type="number" step="0.01" value="10"/></div>
          <div class="field"><label>渠道保留量</label><input class="input" id="nl-reserve" type="number" value="0"/></div>
        </div>
      </div>
      <div id="nl-new" class="hidden">
        <div class="form-grid">
          <div class="field"><label><i>*</i>款名（SPU）</label><input class="input" id="nn-spu" placeholder="如：保温杯 500ml"/></div>
          <div class="field"><label>分类</label><select class="select" id="nn-cat">${cats.map(c => `<option value="${c.value}">${esc(c.label)}</option>`).join('')}</select></div>
          <div class="field"><label><i>*</i>SKU 编码</label><input class="input" id="nn-code" placeholder="如 SP100"/></div>
          <div class="field"><label><i>*</i>SKU 名称</label><input class="input" id="nn-name" placeholder="如 保温杯 500ml 白色"/></div>
          <div class="field"><label>规格属性（JSON）</label><input class="input" id="nn-attrs" placeholder='{"颜色":"白色"}'/></div>
          <div class="field"><label>单位</label><input class="input" id="nn-unit" value="个"/></div>
          <div class="field"><label>成本价</label><input class="input" id="nn-cost" type="number" step="0.01" value="10"/></div>
          <div class="field"><label><i>*</i>售价</label><input class="input" id="nn-price" type="number" step="0.01" value="29.9"/></div>
        </div>
        <div class="form-grid">
          <div class="field"><label>商品图片 URL（留空用分类占位图）</label>
            <input class="input" id="nn-image" placeholder="https://... 或 /img/cat-1.svg"/></div>
          <div class="field"><label>初始库存（可选，走正式入库过账）</label>
            <input class="input" id="nn-qty" type="number" value="100"/></div>
          <div class="field"><label>入库库位</label>
            <select class="select" id="nn-loc">${locs.map(l => `<option value="${l.value}">${esc(l.label)}</option>`).join('')}</select></div>
          <div class="field"><label>安全库存</label><input class="input" id="nn-safe" type="number" value="100"/></div>
        </div>
        <p class="hint">新建商品会一次性完成：建款 → 建 SKU（带均价）→ 上架定价 → 初始库存入库过账，全部在一个事务里，失败整体回滚。</p>
      </div>`,
    footer: '<button class="btn" id="modal-close">取消</button><button class="btn primary" id="nl-save">保存</button>',
    onMount(close) {
      const mode = $('#nl-mode');
      mode.onchange = () => {
        $('#nl-existing').classList.toggle('hidden', mode.value !== 'existing');
        $('#nl-new').classList.toggle('hidden', mode.value !== 'new');
      };
      $('#nl-save').onclick = async () => {
        try {
          if (mode.value === 'existing') {
            await api('POST', '/api/v1/listings', {
              shop_id: Number($('#nl-shop').value), goods_id: Number($('#nl-goods').value),
              sale_price: Number($('#nl-price').value), reserve_qty: Number($('#nl-reserve').value),
              sale_status: 1,
            });
            toast('已上架');
          } else {
            const body = {
              spu_name: $('#nn-spu').value.trim(), category_id: Number($('#nn-cat').value),
              goods_code: $('#nn-code').value.trim(), goods_name: $('#nn-name').value.trim(),
              spec_attrs: $('#nn-attrs').value.trim() || '{}', unit: $('#nn-unit').value.trim() || '个',
              cost_price: Number($('#nn-cost').value || 0), sale_price: Number($('#nn-price').value || 0),
              image_url: $('#nn-image').value.trim(), shop_id: Number($('#nl-shop').value),
              init_qty: Number($('#nn-qty').value || 0), init_loc_id: Number($('#nn-loc').value),
              safety_stock: Number($('#nn-safe').value || 0),
            };
            if (!body.spu_name || !body.goods_code) throw new Error('款名和 SKU 编码必填');
            const r = await api('POST', '/api/v1/catalog/onboard', body);
            toast(`已新建并上架：${r.goodsCode}${r.orderNo ? '，初始库存过账 ' + r.orderNo : ''}`);
          }
          close(); await loadBasics(); pageListings();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
};

/* ============================== 补货建议 ============================== */
async function pageReplenish() {
  const showAll = $('#rp-all') ? $('#rp-all').value : '0';
  const d = await api('GET', `/api/v1/replenish/suggestions?all=${showAll}`);
  const rows = (d.list || []).map(r => [
    `<span class="mono">${esc(r.goods_code)}</span>`, esc(r.goods_name), esc(r.unit),
    `<span class="num">${qty(r.total_qty)}</span>`,
    `<span class="num" style="color:${Number(r.avail_qty) <= 0 ? '#c45656' : '#303133'}">${qty(r.avail_qty)}</span>`,
    `<span class="num">${qty(r.locked_qty)}</span>`,
    `<span class="num">${qty(r.safety_stock)}</span>`,
    `<span class="num">${qty(r.gap_qty)}</span>`,
    `<span class="num"><b>${qty(r.suggest_qty)}</b></span>`,
    `${r.lead_days} 天`,
    r.status === '缺货' ? tagOf('缺货', 'red') : r.status === '低于安全库存' ? tagOf('低于安全库存', 'orange') : tagOf('正常', 'green'),
    permBtn('replenish:edit', `<button class="btn small" onclick="__policy(${r.goods_id},${r.safety_stock},${r.replenish_qty},${r.lead_days})">设政策</button>`),
  ]);
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>补货建议</h3>
        <select class="select" id="rp-all" style="width:150px">
          <option value="0" ${showAll === '0' ? 'selected' : ''}>只看需补货</option>
          <option value="1" ${showAll === '1' ? 'selected' : ''}>显示全部 SKU</option>
        </select>
        <button class="btn" onclick="pageReplenish()">刷新</button>
        <div class="spacer"></div>
        ${permBtn('replenish:edit', '<button class="btn primary" onclick="__genReplenish()">一键生成采购入库草稿</button>')}
      </div>
      <p class="hint" style="margin-bottom:10px">
        水位线口径：<b>可用量 &lt; 安全库存</b> 即建议补货；建议量优先取政策里设的补货批量。
        生成的是<b>采购入库草稿</b>，仍需收货过账才会增加库存——避免"点一下就多出库存"。
      </p>
      ${table([{ title: 'SKU' }, { title: '名称' }, { title: '单位' }, { title: '实物', cls: 'num' },
        { title: '可用', cls: 'num' }, { title: '预占', cls: 'num' }, { title: '安全库存', cls: 'num' },
        { title: '缺口', cls: 'num' }, { title: '建议补货', cls: 'num' }, { title: '交期' },
        { title: '状态', cls: 'center' }, { title: '操作' }], rows, '库存都健康，暂不需要补货')}
    </div>`;
}
window.__policy = (goodsId, safety, batch, lead) => {
  dialogForm({
    title: '设置补货政策',
    fields: [
      { id: 'safety', label: '安全库存', type: 'number', value: safety, required: true },
      { id: 'batch', label: '补货批量（0 = 按缺口补）', type: 'number', value: batch },
      { id: 'lead', label: '供应商交期（天）', type: 'number', value: lead },
    ],
    submitText: '保存',
    async onSubmit(v, close) {
      await api('PUT', '/api/v1/replenish/policy', {
        goods_id: goodsId, safety_stock: Number(v.safety),
        replenish_qty: Number(v.batch), lead_days: Number(v.lead),
      });
      close(); toast('政策已保存'); pageReplenish();
    },
  });
};
window.__genReplenish = async () => {
  try {
    const r = await api('POST', '/api/v1/replenish/generate', {});
    toast(`已生成采购入库草稿 ${r.orderNo}：${r.lines} 行 / ${qty(r.total)} 件，去「入库管理」收货过账`);
    go('in-orders');
  } catch (e) { toast(e.message, 'error'); }
};

/* ============================== 运维层 ============================== */
async function pageOps() {
  const [h, m, c] = await Promise.all([
    api('GET', '/api/v1/ops/health'), api('GET', '/api/v1/ops/metrics'), api('GET', '/api/v1/ops/checks'),
  ]);
  const baks = await api('GET', '/api/v1/ops/backups');
  const jobs = await api('GET', '/api/v1/ops/jobs');
  const cfgs = await api('GET', '/api/v1/ops/config');
  const gb = (n) => (n / 1024 / 1024).toFixed(2) + ' MB';
  const rows = Object.entries(h.tableRows || {}).map(([t, n]) => [`<span class="mono">${esc(t)}</span>`, `<span class="num">${n}</span>`]);
  const checkRows = (c.checks || []).map(x => [
    `<span class="name">${esc(x.name)}</span>`, tagOf(x.level, x.passed ? 'green' : 'red'),
    `<span class="num">${x.badRows}</span>`, `<span class="hint">${esc(x.hint)}</span>`,
  ]);
  $('#content').innerHTML = `
    <div class="stat-row">
      <div class="stat ${h.status === 'ok' ? 'green' : 'red'}"><div class="label">服务状态</div>
        <div class="value" style="font-size:20px">${esc(h.status)} <small>v${esc(h.version)}</small></div></div>
      <div class="stat"><div class="label">运行时长</div><div class="value">${Math.floor(h.uptimeSeconds / 60)}<small>分钟</small></div></div>
      <div class="stat orange"><div class="label">数据库</div><div class="value" style="font-size:18px">${gb(h.dbSizeBytes)}</div></div>
      <div class="stat green"><div class="label">磁盘剩余</div><div class="value" style="font-size:18px">${gb(h.diskFreeBytes)}</div></div>
    </div>

    <div class="card">
      <div class="card-head"><h3>一致性校验（6 项硬校验）</h3>
        <div class="spacer"></div>
        <button class="btn" onclick="pageOps()">重新校验</button>
        ${c.allPassed ? tagOf('全部通过', 'green') : tagOf('存在异常，禁止继续堆业务', 'red')}
      </div>
      ${table([{ title: '校验项' }, { title: '结果', cls: 'center' }, { title: '异常行数', cls: 'num' },
        { title: '含义 / 修复提示' }], checkRows)}
      <p class="hint" style="margin-top:10px">校验只读、不修数据。任一 FAIL 都说明账实或账账不一致，要先定位再动业务。</p>
    </div>

    <div class="overview-grid">
      <div class="overview-card accent">
        <div class="overview-head"><span class="overview-kicker">业务指标</span><h3>今日 / 本月</h3></div>
        <ul class="summary-list">
          <li><span>今日订单</span><b>${m.todayOrders}</b></li>
          <li><span>今日销售额</span><b>￥${yuan(m.todaySales)}</b></li>
          <li><span>本月销售额</span><b>￥${yuan(m.monthSales)}</b></li>
          <li><span>支付→发货均时</span><b>${m.avgShipHours} h</b></li>
        </ul>
      </div>
      <div class="overview-card">
        <div class="overview-head"><span class="overview-kicker">风险</span><h3>需要盯的数</h3></div>
        <ul class="summary-list">
          <li><span>待发货</span><b>${m.pendingShip}</b></li>
          <li><span>待处理售后</span><b>${m.pendingAfterSale}</b></li>
          <li><span>低库存 SKU</span><b>${m.lowStockSku}</b></li>
          <li><span>超 24h 未发货</span><b>${m.staleAllocOrders}</b></li>
        </ul>
      </div>
      <div class="overview-card">
        <div class="overview-head"><span class="overview-kicker">容量</span><h3>表行数</h3></div>
        ${table([{ title: '表' }, { title: '行数', cls: 'num' }], rows)}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>批处理任务</h3>
        <span class="hint" style="margin-left:8px">超时未支付自动关单 / 预占与临期扫描（启动时也会跑一次）</span>
        <div class="spacer"></div>
        ${permBtn('ops:manage', '<button class="btn primary" onclick="__runJobs()">立即执行</button>')}
      </div>
      ${table([{ title: '任务' }, { title: '影响行数', cls: 'num' }, { title: '时间' }, { title: '说明' }],
        (jobs.list || []).map(j => [`<span class="mono">${esc(j.job_code)}</span>`, `<span class="num">${j.affected}</span>`,
          esc(j.run_at), `<span class="hint">${esc(j.detail || '')}</span>`]), '还没有执行记录')}
    </div>

    <div class="card">
      <div class="card-head"><h3>备份</h3>
        <div class="spacer"></div>
        ${permBtn('ops:manage', '<button class="btn primary" onclick="__backup()">立即备份</button>')}</div>
      ${table([{ title: '备份文件' }, { title: '大小' }, { title: '时间' }],
        (baks.list || []).map(b => [`<span class="mono">${esc(b.name)}</span>`, gb(b.sizeBytes), esc(b.createdAt)]),
        '还没有备份')}
    </div>

    <div class="card">
      <div class="card-head"><h3>系统参数（白名单可改）</h3>
        <div class="spacer"></div><span class="hint">改完立即生效，业务代码读参数而不是硬编码</span></div>
      ${table([{ title: '参数键' }, { title: '值' }, { title: '说明' }],
        (cfgs.list || []).map(x => [`<span class="mono">${esc(x.config_key)}</span>`,
          `<b>${esc(x.config_value)}</b>`, `<span class="hint">${esc(x.remark || '')}</span>`]))}
      <div style="margin-top:12px">
        ${permBtn('ops:manage', '<button class="btn" onclick="__editConfig()">修改参数</button>')}
      </div>
    </div>`;
}
window.__runJobs = async () => {
  try {
    const r = await api('POST', '/api/v1/ops/jobs/run', {});
    toast('批处理完成：' + r.results.map(x => x.job + '=' + x.affected).join(', '));
    pageOps();
  } catch (e) { toast(e.message, 'error'); }
};

window.__backup = async () => {
  try { const r = await api('POST', '/api/v1/ops/backup', {}); toast('已备份：' + r.name); pageOps(); }
  catch (e) { toast(e.message, 'error'); }
};
window.__editConfig = async () => {
  const cfgs = (await api('GET', '/api/v1/ops/config')).list;
  dialogForm({
    title: '修改系统参数',
    fields: cfgs.map(c => ({ id: c.config_key.replace(/\./g, '_'), label: c.config_key, value: c.config_value, hint: c.remark || '' })),
    submitText: '保存',
    async onSubmit(v, close) {
      const payload = {};
      cfgs.forEach(c => { payload[c.config_key] = v[c.config_key.replace(/\./g, '_')]; });
      await api('PUT', '/api/v1/ops/config', payload);
      close(); toast('参数已保存'); pageOps();
    },
  });
};

/* ============================== 系统管理（RBAC） ============================== */
const DATA_SCOPE_TEXT = { 1: '全部数据', 2: '自定义仓库', 3: '本仓库', 4: '本仓库及下级' };

async function pageSys() {
  const [usersRes, rolesRes] = await Promise.all([
    api('GET', '/api/v1/sys/users'), api('GET', '/api/v1/sys/roles'),
  ]);
  const users = usersRes.list || [], roles = usersRes.roles || [], whs = usersRes.warehouses || [];
  const catalog = rolesRes.catalog || [];
  state.sysUsers = users; state.sysRoles = roles; state.sysWhs = whs; state.sysCatalog = catalog;
  const roleName = {}; roles.forEach(r => { roleName[r.role_code] = r.role_name; });
  const whName = {}; whs.forEach(w => { whName[w.id] = w.loc_name; });

  const userRows = users.map(u => [
    `<span class="mono">${esc(u.username)}</span>`, esc(u.real_name), esc(u.phone || '—'),
    (u.roleCodes || []).map(c => tagOf(roleName[c] || c, c === 'admin' ? 'red' : 'blue')).join(' ') || '<span class="hint">未分配角色</span>',
    (u.whIds || []).length ? (u.whIds || []).map(i => esc(whName[i] || i)).join('、') : '<span class="hint">不限 / 未绑</span>',
    u.status ? tagOf('启用', 'green') : tagOf('停用', 'gray'),
    can('sys:manage') ? `<button class="btn small" onclick="__editUser(${u.id})">编辑</button>` : '',
  ]);
  const roleRows = roles.map(r => [
    esc(r.role_name), `<span class="mono">${esc(r.role_code)}</span>`,
    tagOf(DATA_SCOPE_TEXT[r.data_scope] || r.data_scope, 'purple'),
    r.role_code === 'admin' ? '<b>全部（*）</b>' : (r.permCount || 0) + ' 项',
    r.userCount || 0,
    can('sys:manage') && r.role_code !== 'admin'
      ? `<button class="btn small" onclick="__editRolePerm(${r.id})">配权限</button>`
      : '<span class="hint">固定</span>',
  ]);

  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>用户与角色</h3>
        <div class="spacer"></div>
        <span class="hint">权限在接口层强制校验；这一页改完立即生效（重新登录或刷新即可看到菜单变化）</span>
        ${permBtn('sys:manage', '<button class="btn primary" onclick="__newUser()">+ 新建用户</button>')}
      </div>
      ${table([{ title: '登录名' }, { title: '姓名' }, { title: '手机' }, { title: '角色' }, { title: '可见仓库' },
        { title: '状态', cls: 'center' }, { title: '操作' }], userRows, '没有用户')}
      <p class="hint" style="margin-top:10px">
        演示账号（密码均 <code>123456</code>）：<b>admin</b>（全部）、<b>ops01</b>（电商运营，不能做仓储作业）、
        <b>keeper01</b>（仓管员，只能看 WH01）、<b>keeper02</b>（仓管员，只能看 WH02）、
        <b>picker01</b>（拣货员，不能建波次/发货）、<b>viewer01</b>（只读，任何写操作 403）。
      </p>
    </div>

    <div class="card">
      <div class="card-head"><h3>角色权限</h3>
        <span class="hint" style="margin-left:8px">角色 = 权限码集合 + 数据范围（仓库隔离）</span></div>
      ${table([{ title: '角色' }, { title: '编码' }, { title: '数据范围', cls: 'center' },
        { title: '权限数', cls: 'center' }, { title: '用户数', cls: 'center' }, { title: '操作' }], roleRows)}
    </div>`;
}

function _permCheckboxGroups(catalog, selected) {
  const groups = {};
  catalog.forEach(p => { (groups[p.module] = groups[p.module] || []).push(p); });
  return Object.entries(groups).map(([mod, ps]) => `
    <div class="field"><label>${esc(mod)}</label>
      <div>${ps.map(p => `<label class="chk"><input type="checkbox" class="r-perm" value="${esc(p.perm_code)}"
        ${selected.indexOf(p.perm_code) >= 0 ? 'checked' : ''}/> ${esc(p.perm_name)}</label>`).join('')}</div>
    </div>`).join('');
}

window.__newUser = () => window.__openUserDialog(null);
window.__editUser = (id) => window.__openUserDialog((state.sysUsers || []).find(u => u.id === id));

window.__openUserDialog = (u) => {
  const roles = state.sysRoles || [], whs = state.sysWhs || [];
  const selRoles = (u && u.roleCodes) || ['viewer'];
  const selWhs = (u && u.whIds) || [];
  const roleChecks = roles.map(r => `<label class="chk"><input type="checkbox" class="u-role" value="${esc(r.role_code)}"
    ${selRoles.indexOf(r.role_code) >= 0 ? 'checked' : ''}/> ${esc(r.role_name)}</label>`).join('');
  const whChecks = whs.map(w => `<label class="chk"><input type="checkbox" class="u-wh" value="${w.id}"
    ${selWhs.indexOf(w.id) >= 0 ? 'checked' : ''}/> ${esc(w.loc_code)} ${esc(w.loc_name)}</label>`).join('')
    || '<span class="hint">还没有仓库</span>';
  openModal({
    title: u ? ('编辑用户 · ' + u.username) : '新建用户',
    body: `
      <div class="form-grid">
        <div class="field"><label><i>*</i>登录名</label>
          <input class="input" id="u-name" value="${esc(u ? u.username : '')}" ${u ? 'disabled' : ''}/></div>
        <div class="field"><label><i>*</i>姓名</label>
          <input class="input" id="u-real" value="${esc(u ? u.real_name : '')}"/></div>
        <div class="field"><label>手机</label>
          <input class="input" id="u-phone" value="${esc(u ? (u.phone || '') : '')}"/></div>
        <div class="field"><label>${u ? '重置密码（留空不改）' : '<i>*</i>初始密码'}</label>
          <input class="input" id="u-pwd" type="password" placeholder="至少 6 位" value="${u ? '' : '123456'}"/></div>
        <div class="field"><label>状态</label>
          <select class="select" id="u-status">
            <option value="1" ${!u || u.status ? 'selected' : ''}>启用</option>
            <option value="0" ${u && !u.status ? 'selected' : ''}>停用</option>
          </select></div>
      </div>
      <div class="field"><label><i>*</i>角色（可多选，权限取并集）</label><div>${roleChecks}</div></div>
      <div class="field"><label>可见仓库（角色数据范围=本仓库时生效）</label><div>${whChecks}</div></div>
      <p class="hint">数据范围说明：<b>全部数据</b>忽略仓库绑定；<b>本仓库</b>按这里勾选的仓库过滤列表与写入校验。</p>`,
    footer: '<button class="btn" id="modal-close">取消</button><button class="btn primary" id="u-save">保存</button>',
    onMount(close) {
      $('#u-save').onclick = async () => {
        const payload = {
          real_name: $('#u-real').value.trim(),
          phone: $('#u-phone').value.trim(),
          status: Number($('#u-status').value),
          role_codes: $$('.u-role:checked').map(x => x.value),
          wh_ids: $$('.u-wh:checked').map(x => Number(x.value)),
        };
        if (!payload.role_codes.length) { toast('至少选择一个角色', 'error'); return; }
        const pwd = $('#u-pwd').value;
        if (!u) {
          payload.username = $('#u-name').value.trim();
          payload.password = pwd;
          if (!payload.username) { toast('登录名必填', 'error'); return; }
          if (pwd.length < 6) { toast('密码至少 6 位', 'error'); return; }
        } else if (pwd) {
          payload.password = pwd;
        }
        try {
          if (u) await api('PUT', '/api/v1/sys/users/' + u.id, payload);
          else await api('POST', '/api/v1/sys/users', payload);
          close(); toast(u ? '用户已更新' : '用户已创建'); pageSys();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
};

window.__editRolePerm = (id) => {
  const role = (state.sysRoles || []).find(r => r.id === id);
  if (!role) return;
  openModal({
    title: '配置权限 · ' + role.role_name,
    body: `<p class="hint" style="margin-bottom:10px">勾选后保存，该角色所有用户立即生效；接口层会按这些权限码拦截请求。</p>
      ${_permCheckboxGroups(state.sysCatalog || [], role.perms || [])}`,
    footer: `<button class="btn" id="modal-close">取消</button>
      <button class="btn" id="r-all">全选</button><button class="btn" id="r-none">清空</button>
      <button class="btn primary" id="r-save">保存</button>`,
    onMount(close) {
      $('#r-all').onclick = () => $$('.r-perm').forEach(x => { x.checked = true; });
      $('#r-none').onclick = () => $$('.r-perm').forEach(x => { x.checked = false; });
      $('#r-save').onclick = async () => {
        try {
          await api('PUT', '/api/v1/sys/roles/' + role.id, {
            perm_codes: $$('.r-perm:checked').map(x => x.value),
          });
          close(); toast('权限已保存'); pageSys();
        } catch (e) { toast(e.message, 'error'); }
      };
    },
  });
};

/* ------------------------------ 轻量图表（纯 SVG，无第三方库） ------------------------------ */
function svgLineChart(series, labels) {
  const w = 780, h = 230, padL = 56, padR = 18, padT = 16, padB = 30;
  const n = labels.length || 1;
  const max = Math.max(1, ...series.flatMap(s => s.values.map(v => Number(v) || 0)));
  const x = (i) => padL + (n <= 1 ? 0 : (i * (w - padL - padR)) / (n - 1));
  const y = (v) => padT + (h - padT - padB) * (1 - (Number(v) || 0) / max);
  let grid = '';
  for (let i = 0; i <= 4; i += 1) {
    const gy = padT + ((h - padT - padB) * i) / 4;
    const val = Math.round((max * (4 - i)) / 4);
    grid += `<line x1="${padL}" y1="${gy}" x2="${w - padR}" y2="${gy}" stroke="#ebeef5" stroke-width="1"/>
      <text x="${padL - 8}" y="${gy + 4}" text-anchor="end" font-size="11" fill="#909399">${val}</text>`;
  }
  const step = Math.max(1, Math.ceil(n / 8));
  let xLabels = '';
  labels.forEach((lb, i) => {
    if (i % step !== 0 && i !== n - 1) return;
    xLabels += `<text x="${x(i)}" y="${h - 10}" text-anchor="middle" font-size="11" fill="#909399">${esc(lb)}</text>`;
  });
  const lines = series.map((s) => {
    const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(' ');
    const dots = s.values.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="3" fill="${s.color}">
      <title>${esc(s.name)} ${esc(labels[i] || '')}：￥${yuan(v)}</title></circle>`).join('');
    return `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2.5"
      stroke-linejoin="round" stroke-linecap="round"/>${dots}
      <text x="${w - padR - 6}" y="${y(s.values[s.values.length - 1] || 0) - 8}" text-anchor="end"
        font-size="11" fill="${s.color}">${esc(s.name)} ￥${yuan(s.values[s.values.length - 1] || 0)}</text>`;
  }).join('');
  const legend = series.map((s, i) => `<g transform="translate(${padL + i * 110},12)">
      <rect width="10" height="10" rx="2" fill="${s.color}"/>
      <text x="16" y="9" font-size="11" fill="#606266">${esc(s.name)}</text></g>`).join('');
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img">
    <rect width="${w}" height="${h}" fill="#fff"/>${grid}${lines}${xLabels}${legend}</svg>`;
}

function svgHBar(items, opts = {}) {
  items = items || [];
  if (!items.length) return '';
  const w = 640, rowH = 36, padL = 96, padR = 156;
  const max = Math.max(1, ...items.map(i => Number(i.value) || 0));
  const h = items.length * rowH + 10;
  const rows = items.map((it, i) => {
    const yy = 6 + i * rowH;
    const bw = Math.max(3, Math.round(((Number(it.value) || 0) / max) * (w - padL - padR)));
    return `<g><text x="${padL - 8}" y="${yy + 17}" text-anchor="end" font-size="12"
        fill="#606266">${esc(it.label)}</text>
      <rect x="${padL}" y="${yy + 6}" width="${bw}" height="18" rx="4" fill="${it.color || '#409eff'}">
        <title>${esc(it.label)}：${esc(it.text)}</title></rect>
      <text x="${padL + bw + 8}" y="${yy + 19}" font-size="12" fill="#303133">${esc(it.text)}</text></g>`;
  }).join('');
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img">
    <rect width="${w}" height="${h}" fill="#fff"/>${rows}</svg>`;
}

/* ============================== 盘点（库存作业） ============================== */
async function pageStocktakes(p = 1) {
  const f = $('#st-status') ? $('#st-status').value : '';
  const d = await api('GET', `/api/v1/stocktakes?page=${p}&size=15&status=${f}`);
  const ST = { 0: ['草稿', 'gray'], 1: ['盘点中', 'orange'], 2: ['待审核', 'blue'], 3: ['已完成', 'green'], 4: ['已作废', 'red'] };
  const rows = (d.list || []).map(s => {
    const [label, color] = ST[s.status] || ['?', 'gray'];
    return [
      `<span class="mono">${esc(s.order_no)}</span>`, esc(s.typeText), esc(s.wh_name || '—'),
      `<span class="num">${s.plan_count}</span>`, `<span class="num">${s.actual_count}</span>`,
      `<span class="num" style="color:${s.diff_count > 0 ? '#c45656' : '#909399'}">${s.diff_count}</span>`,
      `<span class="num">${qty(s.diff_in_qty)} / ${qty(s.diff_out_qty)}</span>`,
      tagOf(label, color), esc(s.create_time),
      `<button class="btn small primary" onclick="openStocktake(${s.id})">盘点作业</button>`,
    ];
  });
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>库存盘点</h3>
        <select class="select" id="st-status" style="width:130px">
          <option value="">全部状态</option>
          ${Object.entries(ST).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v[0]}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageStocktakes(1)">筛选</button>
        <div class="spacer"></div>
        <span class="hint">生成账面快照 → 录实盘 → 提交 → 审核（盘盈入 8 / 盘亏入 9 流水）</span>
        ${permBtn('stocktake:edit', '<button class="btn primary" onclick="__newStocktake()">+ 新建盘点</button>')}
      </div>
      ${table([{ title: '盘点单' }, { title: '类型' }, { title: '仓库' }, { title: '应盘', cls: 'num' },
        { title: '已盘', cls: 'num' }, { title: '差异行', cls: 'num' }, { title: '盘盈 / 盘亏', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '创建时间' }, { title: '操作' }], rows, '还没有盘点单')}
      ${pager(d.total, d.page, d.size, (np) => pageStocktakes(np))}
    </div>`;
}

window.__newStocktake = () => {
  const whs = state.cache.locations.filter(l => l.loc_type === 1);
  const whOptions = whs.map(w => ({ value: w.id, label: w.loc_name }));
  dialogForm({
    title: '新建盘点单',
    fields: [
      { id: 'wh', label: '盘点仓库', type: 'select', options: whOptions, value: whOptions[0] && whOptions[0].value, required: true },
      { id: 'type', label: '盘点方式', type: 'select', value: 1,
        options: [{ value: 1, label: '全盘（仓内全部有库存的库位）' }, { value: 2, label: '抽盘（指定库位）' }] },
      { id: 'locs', label: '抽盘库位（全盘可忽略；多个用逗号分隔库位编码）', placeholder: '如 A01-01,A01-02' },
      { id: 'remark', label: '备注', value: '周期盘点' },
    ],
    submitText: '生成盘点单',
    async onSubmit(v, close) {
      const body = { wh_id: Number(v.wh), stocktake_type: Number(v.type), remark: v.remark };
      if (Number(v.type) === 2 && v.locs.trim()) {
        const codes = v.locs.split(',').map(x => x.trim()).filter(Boolean);
        body.loc_ids = state.cache.leafLocations.filter(l => codes.includes(l.loc_code)).map(l => l.id);
      }
      const r = await api('POST', '/api/v1/stocktakes', body);
      close(); toast(`盘点单 ${r.orderNo} 已生成：应盘 ${r.planCount} 行`); pageStocktakes();
    },
  });
};

async function openStocktake(id) {
  const s = await api('GET', '/api/v1/stocktakes/' + id);
  const editable = s.status === 0 || s.status === 1;
  const rows = s.lines.map(l => [
    `<span class="task-loc">${esc(l.loc_code)}</span>`, `<span class="mono">${esc(l.goods_code)}</span>`,
    esc(l.goods_name), esc(l.batch_no || '—'),
    `<span class="num">${qty(l.book_qty)}</span>`,
    editable
      ? `<input class="mini-input" id="st-${l.id}" type="number" step="0.001" value="${l.actual_qty === null ? '' : qty(l.actual_qty)}"/>`
      : `<span class="num">${l.actual_qty === null ? '—' : qty(l.actual_qty)}</span>`,
    `<span class="num" style="color:${Number(l.diff_qty) === 0 ? '#909399' : Number(l.diff_qty) > 0 ? '#529b2e' : '#c45656'}">${qty(l.diff_qty)}</span>`,
    esc(l.diff_reason || '—'),
  ]);
  const actions = [];
  if (editable && can('stocktake:edit')) actions.push(`<button class="btn primary" onclick="__saveStocktake(${s.id})">保存实盘数</button>`);
  if (editable && can('stocktake:edit')) actions.push(`<button class="btn" onclick="__fillBook(${s.id})">按账面数填满</button>`);
  if ((s.status === 0 || s.status === 1) && can('stocktake:edit')) actions.push(`<button class="btn" onclick="__submitStocktake(${s.id})">提交审核</button>`);
  if (s.status === 2 && can('stocktake:audit')) actions.push(`<button class="btn success" onclick="__auditStocktake(${s.id})">审核并调整库存</button>`);
  if (s.status !== 3 && s.status !== 4 && can('stocktake:edit')) actions.push(`<button class="btn danger" onclick="__cancelStocktake(${s.id})">作废</button>`);
  openModal({
    title: `盘点单 ${s.order_no}（${s.statusText}）`, wide: true,
    body: `<div class="kv" style="margin-bottom:12px">
        <b>仓库</b><span>${esc(s.wh_name || '—')}</span>
        <b>应盘 / 已盘 / 差异</b><span>${s.plan_count} / ${s.actual_count} / ${s.diff_count}</span>
        <b>盘盈 / 盘亏</b><span>${qty(s.diff_in_qty)} / ${qty(s.diff_out_qty)}</span>
        <b>说明</b><span>差异 = 实盘 − 账面；审核后盘盈/盘亏会自动生成库存流水（biz_type 8/9）。</span>
      </div>
      ${table([{ title: '库位' }, { title: 'SKU' }, { title: '名称' }, { title: '批次' }, { title: '账面', cls: 'num' },
        { title: '实盘', cls: 'num' }, { title: '差异', cls: 'num' }, { title: '差异原因' }], rows, '没有明细')}`,
    footer: `${actions.join(' ')}<span class="spacer"></span><button class="btn" id="modal-close">关闭</button>`,
  });
}
window.openStocktake = openStocktake;

window.__fillBook = async (id) => {
  const s = (await api('GET', '/api/v1/stocktakes/' + id)).lines;
  const lines = s.map(l => ({ detail_id: l.id, actual_qty: Number(l.book_qty) }));
  try { await api('POST', `/api/v1/stocktakes/${id}/save`, { lines }); toast('已按账面数填充'); openStocktake(id); }
  catch (e) { toast(e.message, 'error'); }
};

window.__saveStocktake = async (id) => {
  const s = (await api('GET', '/api/v1/stocktakes/' + id)).lines;
  const lines = [];
  s.forEach(l => {
    const el = $('#st-' + l.id);
    if (el && el.value !== '') lines.push({ detail_id: l.id, actual_qty: Number(el.value) });
  });
  if (!lines.length) return toast('没有填写实盘数', 'error');
  try {
    await api('POST', `/api/v1/stocktakes/${id}/save`, { lines });
    toast(`已保存 ${lines.length} 行实盘数`); openStocktake(id); pageStocktakes();
  } catch (e) { toast(e.message, 'error'); }
};

window.__submitStocktake = async (id) => {
  try { await api('POST', `/api/v1/stocktakes/${id}/submit`, {}); toast('已提交，等待审核'); openStocktake(id); pageStocktakes(); }
  catch (e) { toast(e.message, 'error'); }
};
window.__auditStocktake = async (id) => {
  if (!confirm('审核后差异会立即调整库存并生成流水，确认继续？')) return;
  try {
    const r = await api('POST', `/api/v1/stocktakes/${id}/audit`, {});
    toast(`审核完成：调整了 ${r.postedLines} 行库存`); loadBasics(); openStocktake(id); pageStocktakes();
  } catch (e) { toast(e.message, 'error'); }
};
window.__cancelStocktake = async (id) => {
  if (!confirm('作废这张盘点单？')) return;
  try { await api('POST', `/api/v1/stocktakes/${id}/cancel`, {}); toast('已作废'); pageStocktakes(); }
  catch (e) { toast(e.message, 'error'); }
};

/* ============================== 调拨（库存作业） ============================== */
async function pageTransfers(p = 1) {
  const f = $('#tf-status') ? $('#tf-status').value : '';
  const d = await api('GET', `/api/v1/transfers?page=${p}&size=15&status=${f}`);
  const TF = { 0: ['草稿', 'gray'], 1: ['已审核(待调拨)', 'blue'], 2: ['已调拨', 'green'], 3: ['已作废', 'red'] };
  const rows = (d.list || []).map(t => {
    const [label, color] = TF[t.status] || ['?', 'gray'];
    return [
      `<span class="mono">${esc(t.order_no)}</span>`,
      `${esc(t.from_wh || '—')} → ${esc(t.to_wh || '—')}`,
      `<span class="num">${qty(t.total_qty)}</span>`,
      tagOf(label, color), esc(t.create_time),
      `<button class="btn small primary" onclick="openTransfer(${t.id})">处理</button>`,
    ];
  });
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>库存调拨</h3>
        <select class="select" id="tf-status" style="width:150px">
          <option value="">全部状态</option>
          ${Object.entries(TF).map(([k, v]) => `<option value="${k}" ${f === k ? 'selected' : ''}>${v[0]}</option>`).join('')}
        </select>
        <button class="btn" onclick="pageTransfers(1)">筛选</button>
        <div class="spacer"></div>
        <span class="hint">审核即锁定调出量；确认时同事务「调出减、调入加」写 6/7 两条流水</span>
        ${permBtn('transfer:edit', '<button class="btn primary" onclick="__newTransfer()">+ 新建调拨</button>')}
      </div>
      ${table([{ title: '调拨单' }, { title: '调出 → 调入' }, { title: '数量', cls: 'num' },
        { title: '状态', cls: 'center' }, { title: '创建时间' }, { title: '操作' }], rows, '还没有调拨单')}
      ${pager(d.total, d.page, d.size, (np) => pageTransfers(np))}
    </div>`;
}

window.__newTransfer = () => {
  const locs = state.cache.leafLocations.map(l => ({ value: l.id, label: `${l.loc_code} ${l.loc_name}` }));
  const goods = state.cache.goods.map(g => ({ value: g.id, label: `${g.goods_code} ${g.goods_name}（库存 ${qty(g.stock_qty)}）` }));
  if (!locs.length || !goods.length) return toast('缺少库位或商品数据', 'error');
  dialogForm({
    title: '新建调拨单',
    fields: [
      { id: 'from', label: '调出库位', type: 'select', options: locs, value: locs[0].value, required: true },
      { id: 'to', label: '调入库位', type: 'select', options: locs, value: locs[Math.min(1, locs.length - 1)].value, required: true },
      { id: 'goods', label: '商品', type: 'select', options: goods, value: goods[0].value, required: true },
      { id: 'qty', label: '数量', type: 'number', value: 10, required: true },
      { id: 'batch', label: '批次（留空=无批次）', placeholder: '如 B20260901' },
    ],
    submitText: '创建',
    async onSubmit(v, close) {
      const r = await api('POST', '/api/v1/transfers', {
        goods_id: Number(v.goods), from_loc_id: Number(v.from), to_loc_id: Number(v.to),
        qty: Number(v.qty), batch_no: v.batch,
      });
      close(); toast('调拨单已创建：' + r.orderNo); pageTransfers();
    },
  });
};

async function openTransfer(id) {
  const t = await api('GET', '/api/v1/transfers/' + id);
  const rows = t.lines.map(l => [
    `<span class="task-loc">${esc(l.from_loc_code)}</span> → <span class="task-loc">${esc(l.to_loc_code)}</span>`,
    `<span class="mono">${esc(l.goods_code)}</span>`, esc(l.goods_name), esc(l.batch_no || '—'),
    `<span class="num">${qty(l.qty)}</span>`, `<span class="num">${qty(l.available_qty)}</span>`,
  ]);
  const actions = [];
  if (t.status === 0 && can('transfer:edit')) actions.push(`<button class="btn primary" onclick="__auditTransfer(${t.id})">审核（锁定调出）</button>`);
  if (t.status === 1 && can('transfer:confirm')) actions.push(`<button class="btn success" onclick="__confirmTransfer(${t.id})">确认调拨过账</button>`);
  if ((t.status === 0 || t.status === 1) && can('transfer:edit')) actions.push(`<button class="btn danger" onclick="__cancelTransfer(${t.id})">作废</button>`);
  openModal({
    title: `调拨单 ${t.order_no}（${t.statusText}）`, wide: true,
    body: `<div class="kv" style="margin-bottom:12px">
        <b>调出 → 调入</b><span>${esc(t.from_wh || '—')} → ${esc(t.to_wh || '—')}</span>
        <b>总数量</b><span>${qty(t.total_qty)}</span>
        <b>说明</b><span>调拨不改变公司总库存与成本，只是把货从 A 库位搬到 B 库位；批次保持不变。</span>
      </div>
      ${table([{ title: '库位' }, { title: 'SKU' }, { title: '名称' }, { title: '批次' },
        { title: '数量', cls: 'num' }, { title: '调出可用', cls: 'num' }], rows)}`,
    footer: `${actions.join(' ')}<span class="spacer"></span><button class="btn" id="modal-close">关闭</button>`,
  });
}
window.openTransfer = openTransfer;

window.__auditTransfer = async (id) => {
  try { await api('POST', `/api/v1/transfers/${id}/audit`, {}); toast('已审核，调出量已锁定'); openTransfer(id); pageTransfers(); }
  catch (e) { toast(e.message, 'error'); }
};
window.__confirmTransfer = async (id) => {
  if (!confirm('确认调拨？将同时扣减调出库位、增加调入库位并写两条流水。')) return;
  try {
    const r = await api('POST', `/api/v1/transfers/${id}/confirm`, {}, { 'X-Idempotency-Key': uuid() });
    toast(`调拨完成，共 ${qty(r.movedQty)} 件`); loadBasics(); openTransfer(id); pageTransfers();
  } catch (e) { toast(e.message, 'error'); }
};
window.__cancelTransfer = async (id) => {
  if (!confirm('作废调拨单？已锁定的调出量会释放。')) return;
  try { await api('POST', `/api/v1/transfers/${id}/cancel`, { reason: '手工作废' }); toast('已作废'); pageTransfers(); }
  catch (e) { toast(e.message, 'error'); }
};

/* ============================== 报表中心 ============================== */
async function pageReports() {
  const tab = state.reportTab || 'stock';
  const begin = $('#rp-begin') ? $('#rp-begin').value : new Date(new Date().getFullYear(), new Date().getMonth(), 1).toISOString().slice(0, 10);
  const end = $('#rp-end') ? $('#rp-end').value : new Date().toISOString().slice(0, 10);
  let body = '';
  if (tab === 'stock') {
    const d = await api('GET', `/api/v1/reports/stock-summary?begin=${begin}&end=${end}`);
    const rows = (d.list || []).map(r => [
      `<span class="mono">${esc(r.goods_code)}</span>`, esc(r.goods_name), esc(r.unit),
      `<span class="num">${qty(r.begin_qty)}</span>`, `<span class="num" style="color:#529b2e">+${qty(r.in_qty)}</span>`,
      `<span class="num" style="color:#c45656">-${qty(r.out_qty)}</span>`,
      `<span class="num"><b>${qty(r.end_qty)}</b></span>`,
      `<span class="num">${qty(r.current_qty)}</span>`,
      `<span class="num" style="color:${Math.abs(Number(r.diff_qty)) > 0.001 ? '#c45656' : '#909399'}">${qty(r.diff_qty)}</span>`,
      `<span class="num">￥${yuan(r.amount)}</span>`,
    ]);
    body = table([{ title: 'SKU' }, { title: '名称' }, { title: '单位' }, { title: '期初', cls: 'num' },
      { title: '入库', cls: 'num' }, { title: '出库', cls: 'num' }, { title: '期末', cls: 'num' },
      { title: '当前库存', cls: 'num' }, { title: '差异', cls: 'num' }, { title: '金额', cls: 'num' }], rows);
  } else if (tab === 'profit') {
    const d = await api('GET', `/api/v1/reports/profit?begin=${begin}&end=${end}`);
    const rows = (d.list || []).map(o => [
      `<span class="mono">${esc(o.order_no)}</span>`, esc(o.buyer_name),
      `<span class="num">${qty(o.shipped_qty)}</span>`,
      `<span class="num">￥${yuan(o.pay_amount)}</span>`, `<span class="num">￥${yuan(o.cost_amount)}</span>`,
      `<span class="num" style="color:${Number(o.profit) >= 0 ? '#529b2e' : '#c45656'}">￥${yuan(o.profit)}</span>`,
      `<span class="num">${o.margin}%</span>`, esc(o.ship_time || '—'),
    ]);
    body = `<div class="stat-row" style="margin-bottom:12px">
        <div class="stat"><div class="label">销售额</div><div class="value" style="font-size:20px">￥${yuan(d.totalSales)}</div></div>
        <div class="stat orange"><div class="label">出库成本</div><div class="value" style="font-size:20px">￥${yuan(d.totalCost)}</div></div>
        <div class="stat green"><div class="label">毛利</div><div class="value" style="font-size:20px">￥${yuan(d.totalProfit)}</div></div>
        <div class="stat"><div class="label">毛利率</div><div class="value" style="font-size:20px">${d.profitRate}%</div></div>
      </div>` + table([{ title: '订单' }, { title: '买家' }, { title: '发货件数', cls: 'num' },
      { title: '销售额', cls: 'num' }, { title: '出库成本', cls: 'num' }, { title: '毛利', cls: 'num' },
      { title: '毛利率', cls: 'num' }, { title: '发货时间' }], rows, '该区间没有已发货订单');
  } else if (tab === 'turnover') {
    const d = await api('GET', '/api/v1/reports/turnover?days=30');
    const rows = (d.list || []).map(r => [
      `<span class="mono">${esc(r.goods_code)}</span>`, esc(r.goods_name), esc(r.unit),
      `<span class="num">${qty(r.stock_qty)}</span>`, `<span class="num">${qty(r.out_qty)}</span>`,
      `<span class="num">${r.dailyOut}</span>`,
      `<span class="num">${r.turnoverDays === null ? '—' : r.turnoverDays}</span>`,
      r.slowMoving ? tagOf('呆滞', 'red') : tagOf('动销', 'green'),
      `<span class="num">￥${yuan(r.avg_cost)}</span>`,
    ]);
    body = `<div class="stat-row" style="margin-bottom:12px">
        <div class="stat"><div class="label">SKU 数</div><div class="value">${d.skuCount}</div></div>
        <div class="stat green"><div class="label">30 天动销 SKU</div><div class="value">${d.activeSku}</div></div>
        <div class="stat orange"><div class="label">动销率</div><div class="value">${d.activeRate}%</div></div>
        <div class="stat red"><div class="label">呆滞 SKU</div><div class="value">${(d.list || []).filter(x => x.slowMoving).length}</div></div>
      </div>` + table([{ title: 'SKU' }, { title: '名称' }, { title: '单位' }, { title: '库存', cls: 'num' },
      { title: '30 天出库', cls: 'num' }, { title: '日均', cls: 'num' }, { title: '可周转天数', cls: 'num' },
      { title: '状态', cls: 'center' }, { title: '均价', cls: 'num' }], rows);
  } else if (tab === 'insight') {
    const [trend, shops, whs, advice] = await Promise.all([
      api('GET', '/api/v1/reports/trend?days=14'),
      api('GET', '/api/v1/reports/shop-sales?days=30'),
      api('GET', '/api/v1/reports/warehouse-stock'),
      api('GET', '/api/v1/reports/advice'),
    ]);
    const labels = trend.list.map(x => x.date.slice(5));
    const line = svgLineChart([
      { name: '销售额', color: '#409eff', values: trend.list.map(x => x.sales) },
      { name: '毛利', color: '#67c23a', values: trend.list.map(x => x.profit) },
    ], labels);
    const shopBars = svgHBar((shops.list || []).map((s, i) => ({
      label: s.shop_name, value: s.sales,
      text: `￥${yuan(s.sales)} · ${s.orders} 单`,
      color: ['#409eff', '#67c23a', '#e6a23c', '#909399'][i % 4],
    })), { unit: '元' });
    const catBars = svgHBar((shops.byCategory || []).map((c, i) => ({
      label: c.category, value: c.sales,
      text: `￥${yuan(c.sales)} · 占比 ${c.share}%`,
      color: ['#7b5bb8', '#409eff', '#e6a23c', '#67c23a', '#f56c6c'][i % 5],
    })), { unit: '元' });
    const whBars = svgHBar((whs.list || []).map((w, i) => ({
      label: w.whName, value: w.amount,
      text: `￥${yuan(w.amount)} · ${qty(w.qty)} 件 · ${w.skus} SKU`,
      color: ['#409eff', '#67c23a', '#e6a23c'][i % 3],
    })), { unit: '元' });
    const adviceHtml = (advice.list || []).map(a => `
      <div class="advice ${a.level}">
        <div class="advice-head">${tagOf({ high: '优先处理', mid: '建议关注', low: '正常' }[a.level], a.level === 'high' ? 'red' : a.level === 'mid' ? 'orange' : 'green')}
          <b>${esc(a.title)}</b><span class="hint">${esc(a.topic)}</span></div>
        <div class="advice-detail">${esc(a.detail)}</div>
        <div class="advice-action">→ ${esc(a.action)}</div>
      </div>`).join('');
    body = `
      <div class="card" style="box-shadow:none;padding:0;margin:0 0 16px">
        <div class="card-head" style="margin-bottom:6px"><h3>近 14 天营收趋势</h3>
          <span class="hint">销售额（按支付）与毛利（销售额 − 出库成本）</span></div>
        ${line}
      </div>
      <div class="overview-grid">
        <div class="overview-card">
          <div class="overview-head"><span class="overview-kicker">店铺营收</span><h3>近 30 天</h3></div>
          ${shopBars || '<p class="hint">暂无数据</p>'}
        </div>
        <div class="overview-card">
          <div class="overview-head"><span class="overview-kicker">分类营收</span><h3>近 30 天</h3></div>
          ${catBars || '<p class="hint">暂无数据</p>'}
        </div>
        <div class="overview-card">
          <div class="overview-head"><span class="overview-kicker">仓库库存</span><h3>金额（移动加权）</h3></div>
          ${whBars || '<p class="hint">暂无数据</p>'}
        </div>
      </div>
      <h3 style="margin:6px 0 10px">经营建议 <span class="hint">（按库存/履约/毛利/售后/周转五类规则生成）</span></h3>
      <div class="advice-list">${adviceHtml}</div>`;
  } else {
    const d = await api('GET', '/api/v1/reports/expiring?days=30');
    const rows = (d.list || []).map(r => [
      `<span class="mono">${esc(r.goods_code)}</span>`, esc(r.goods_name), esc(r.loc_code),
      esc(r.batch_no || '—'), `<span class="num">${qty(r.quantity)}</span>`, esc(r.expire_date),
      r.expired ? tagOf('已过期', 'red') : tagOf(`${r.days_left} 天`, 'orange'),
    ]);
    body = `<p class="hint" style="margin-bottom:10px">
        临期口径：30 天内到期的在库批次（过期批次的货不允许再拣，需走报损/退供）。
        当前 <b>${d.list.length}</b> 个批次临期，其中已过期 <b>${d.expiredCount}</b> 个。</p>` +
      table([{ title: 'SKU' }, { title: '名称' }, { title: '库位' }, { title: '批次' },
        { title: '数量', cls: 'num' }, { title: '到期日' }, { title: '状态', cls: 'center' }], rows, '没有临期批次');
  }
  if (state.page !== 'reports') return;   // 用户已切页，别覆盖当前页面
  $('#content').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>报表中心</h3>
        <div class="btn-group">
          ${[['insight', '经营分析'], ['stock', '收发存汇总'], ['profit', '毛利分析'], ['turnover', '动销与周转'], ['expiring', '临期预警']]
            .map(([k, label]) => `<button class="btn small ${tab === k ? 'primary' : ''}" onclick="__reportTab('${k}')">${label}</button>`).join('')}
        </div>
        <div class="spacer"></div>
        ${tab === 'stock' || tab === 'profit' ? `
          <input class="input" type="date" id="rp-begin" value="${begin}" style="width:150px"/>
          <span>至</span>
          <input class="input" type="date" id="rp-end" value="${end}" style="width:150px"/>
          <button class="btn" onclick="pageReports()">查询</button>` : ''}
      </div>
      ${body}
    </div>`;
}
window.__reportTab = (tab) => { state.reportTab = tab; pageReports(); };

/* ------------------------------ 注册页面 ------------------------------ */
Object.assign(PAGES, {
  shop: pageShop,
  'my-orders': pageMyOrders,
  'ec-orders': pageEcOrders,
  waves: pageWaves,
  'after-sales': pageAfterSales,
  listings: pageListings,
  replenish: pageReplenish,
  ops: pageOps,
  sys: pageSys,
  stocktake: pageStocktakes,
  transfer: pageTransfers,
  reports: pageReports,
});

// 深链接兜底：若带 #shop 之类刷新，等本文件注册完再把页面补渲染一次
setTimeout(() => {
  const p = (location.hash || '').replace('#', '');
  if (p && PAGES[p] && state.token && state.page !== p) go(p);
}, 400);

/* ---------------- 生产化：按履约单（仓）发货，支持分仓/部分发货 ---------------- */
const SHIP_CARRIERS = ['顺丰速运', '中通快递', '圆通速递', '韵达快递', '京东物流', '自提'];

window.__shipOrder = async (id) => {
  let fs;
  try { fs = await api('GET', `/api/v1/ec/orders/${id}/fulfillments`); }
  catch (e) { return toast(e.message, 'error'); }
  const pending = (fs.list || []).filter(f => Number(f.status) === 1);
  if (!pending.length) return toast('该订单没有待发货的履约单（可能已全部发货）', 'error');

  const shipForm = (ff) => dialogForm({
    title: `发货过账 · ${fs.orderNo}${ff ? ' · ' + (ff.wh_name || '仓库' + ff.wh_id) : ''}`,
    fields: [
      { id: 'carrier', label: '承运商', type: 'select', value: SHIP_CARRIERS[0],
        options: SHIP_CARRIERS.map(c => ({ value: c, label: c })) },
      { id: 'tracking', label: '运单号（必填，物流追溯凭据）', required: true,
        placeholder: ff ? `本仓 ${qty(ff.total_qty)} 件` : '' },
    ],
    submitText: '确认发货',
    async onSubmit(v, close) {
      const body = { carrier: v.carrier, tracking_no: v.tracking };
      if (ff) body.fulfillment_id = ff.id;
      const r = await api('POST', `/api/v1/ec/orders/${id}/ship`, body);
      close();
      toast(r.status === 8
        ? `已发 1 个仓（${qty(r.shippedQty)} 件），还剩 ${r.remainingFulfillments} 个仓待发`
        : `已发货：${r.shipmentNo}，扣减 ${qty(r.shippedQty)} 件`);
      pageEcOrders();
    },
  });

  if (pending.length === 1) return shipForm(pending[0]);   // 单仓直发，少一次点击

  openModal({
    title: `分仓发货 · ${fs.orderNo}（共 ${fs.splitCount} 个仓）`,
    wide: true,
    body: `<p class="hint" style="margin-bottom:10px">
        这个订单的货分布在多个仓，必须<b>逐仓发货</b>：每个仓一张发货单、一个运单号。
        全部发完订单才会变成「已发货」，中途是「部分发货」。</p>
      ${table([{ title: '仓库' }, { title: '件数', cls: 'num' }, { title: '已发', cls: 'num' },
               { title: '状态', cls: 'center' }, { title: '操作' }],
        pending.map(f => [
          esc(f.wh_name || '仓库' + f.wh_id), `<span class="num">${qty(f.total_qty)}</span>`,
          `<span class="num">${qty(f.shipped_qty)}</span>`, tagOf('待发货', 'orange'),
          `<button class="btn small success" onclick="__shipOne(${id}, ${f.id})">发货这个仓</button>`,
        ]))}`,
    footer: '<button class="btn" id="modal-close">关闭</button>',
  });
  window.__shipOne = async (orderId, fid) => {
    const ff = pending.find(x => Number(x.id) === Number(fid));
    document.getElementById('modal-close') && document.getElementById('modal-close').click();
    shipForm(ff);
  };
};
