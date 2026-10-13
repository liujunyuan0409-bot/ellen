// 在已登录的商家后台页面里执行（Playwright browser_evaluate）。
// 用法：把 __MONTH__ 替换成 YYYY-MM，整段作为 function 传入，filename 指向 raw json 落盘路径。
// 取四份数据，全部只保留统计用字段，不带用户昵称/手机号/用户 ID：
//   list          销售汇总（商品级）：支付时间、全时段、不拆套餐、不合并同名
//   report_days   营业报表（按营业日）：订单数、新老客单、储值、支付方式
//   ledger        余额流水：时间、金额、变化后余额、备注；ledger_summary 含期末余额
//   big_orders    当月已支付、金额 ≥ 阈值的订单（异常候选）
// token 只在浏览器内使用，不写进返回结果。
async () => {
  const MONTH = '__MONTH__';
  const BIG_ORDER_MIN = 10000;
  const cookie = (name) => {
    const hit = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith(name + '='));
    return hit ? decodeURIComponent(hit.slice(name.length + 1)) : '';
  };
  const token = cookie('Admin-Token');
  const storeid = cookie('StoreId');
  if (!token || !storeid) return { error: 'NOT_LOGGED_IN', hint: '请先在浏览器里登录商家后台' };
  if (!/^\d{4}-\d{2}$/.test(MONTH)) return { error: 'BAD_MONTH', month: MONTH };

  const [y, m] = MONTH.split('-').map(Number);
  const lastDay = new Date(y, m, 0).getDate();
  const day0 = `${MONTH}-01`, day1 = `${MONTH}-${String(lastDay).padStart(2, '0')}`;
  const start = `${day0} 00:00:00`, end = `${day1} 23:59:59`;
  const post = async (path, params) => {
    const r = await fetch(path, { method: 'POST', body: params, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
    return r.json();
  };

  // 1) 销售汇总
  const p = new URLSearchParams();
  p.append('date[0]', start); p.append('date[1]', end);
  for (const [k, v] of Object.entries({ with_classify: 0, with_department: 0, with_option: 0, merge: 0, meal_style: 0 })) p.append(k, v);
  p.append('time_slot[0]', '00:00'); p.append('time_slot[1]', '23:59');
  for (const [k, v] of Object.entries({ search_type: 1, tc_type: 0, split_tc: 0, goods_title: '', time_type: 2,
                                         token, order_type: 0, third_order_type: 0, storeid })) p.append(k, v);
  const d = await post('/service/index.php/data/classify_item_data', p);
  if (String(d.status) !== '1') return { error: 'API_STATUS', api: 'classify_item_data', status: d.status, msg: d.msg || d.message };
  if (Number(d.total_count) > Number(d.page_size)) return { error: 'PAGINATED', total_count: d.total_count, page_size: d.page_size };

  // 2) 营业报表（一天一行）
  const REPORT_FIELDS = ['date', 'order_count', 'order_total', 'order_refund_total', 'order_total_balance', 'order_refund_balance',
    'order_total_wechat', 'order_total_alipay', 'order_total_offline', 'new_user_order_count', 'patrons_user_order_count',
    'visit_total', 'visit_new', 'topup_count', 'topup_total', 'topup_gift', 'cashier_count', 'cashier_total', 'cashier_total_balance',
    'order_count_ts', 'order_count_zq', 'order_total_coupon'];
  const rp = await post('/service/index.php/report', new URLSearchParams({ 'date[0]': day0, 'date[1]': day1, store_id: storeid, date_type: 0, storeid, token }));
  if (String(rp.status) !== '1') return { error: 'API_STATUS', api: 'report', status: rp.status, msg: rp.msg };
  const report_days = (rp.list || []).map(x => Object.fromEntries(REPORT_FIELDS.map(k => [k, x[k]])));

  // 3) 余额流水
  const lg = await post('/service/index.php/recharge_statistics/index', new URLSearchParams({
    date: JSON.stringify([start, end]), type: -1, search: '', page: 1, page_size: 5000, user_id: '', index_mode: 0, token, storeid }));
  const ledgerRows = lg.list || [];
  if (Number(lg.total_count || ledgerRows.length) > ledgerRows.length) return { error: 'PAGINATED', api: 'ledger', total_count: lg.total_count };
  const ledger = ledgerRows.map(x => ({ time: x.add_time, amount: x.amount, after: x.amount_after, remark: x.remark }));

  // 4) 大额订单（接口限 3 个月、每页 ≤100）
  const big_orders = [];
  for (let page = 1; page <= 10; page++) {
    const o = await post('/service/index.php/po_v/get_order_list', new URLSearchParams({
      safe: 1, optionsSearchVal: 0, table_name: '', page, pagesize: 100, presale: 0, children: '', parent_id: '',
      starttime: start, endtime: end, status: '', refund_status: '', user_id: '', payType: '0,1,3,4,5,6,99,8,9,10',
      participate_activities: '', mealType: '0,1,2,3,4,5,6', date_type: 0, total_min: BIG_ORDER_MIN, total_max: '',
      coupon_ids: '', waiter_id: '', pay_status: '', showChild: 0, app_version: '7.9.0', type: 1, storeid }));
    if (String(o.status) === '-1') return { error: 'API_STATUS', api: 'get_order_list', msg: o.msg };
    const l = o.list || [];
    for (const x of l) {
      if (x.status_text !== '已支付' && !String(x.status_text || '').includes('退款')) continue;
      let goods = [];
      try { goods = (typeof x.goods_list === 'string' ? JSON.parse(x.goods_list) : x.goods_list || []).map(g => ({ title: g.title, num: g.num, total: g.total })); } catch (e) {}
      big_orders.push({ pay_time: x.payTime, total: x.total, pay_type: x.payType, status: x.status_text, admin_order: x.admin_order !== '0', goods });
    }
    if (l.length < 100) break;
  }

  return {
    month: MONTH, start, end, time_type: 'pay_time', fetched_at: new Date().toISOString(),
    store: (d.store_list || []).map(s => s.name), total_count: d.total_count,
    header_totals: (d.total_list || []).map(t => ({ title: t.title, value: t.value })),
    list: (d.list || []).map(x => ({
      classify_title: x.classify_title, title: x.title, key_list: x.key_list || [],
      type: x.type, one_price: x.one_price,
      sum_num: x.sum_num, sum_total: x.sum_total,
      discount_amount: x.discount_amount, refunded_total: x.refunded_total, refunded_num: x.refunded_num,
      give_num: x.give_num, real_num: x.real_num, real_total: x.real_total,
    })),
    report_days, ledger,
    ledger_summary: lg.statistics || null,
    big_orders,
  };
}
