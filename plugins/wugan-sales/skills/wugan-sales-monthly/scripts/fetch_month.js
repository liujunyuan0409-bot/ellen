// 在已登录的商家后台页面里执行（Playwright browser_evaluate）。
// 用法：把 __MONTH__ 替换成 YYYY-MM，整段作为 function 传入，filename 指向 raw json 落盘路径。
// 口径与后台「运营数据 → 销售汇总」页面默认查询一致：按支付时间(time_type=2)、全时段、不拆套餐、不合并同名。
// token 只在浏览器内使用，返回结果里会剔除 downloadurl（URL 里带 token）。
async () => {
  const MONTH = '__MONTH__';
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
  const start = `${MONTH}-01 00:00:00`;
  const end = `${MONTH}-${String(lastDay).padStart(2, '0')} 23:59:59`;

  const p = new URLSearchParams();
  p.append('date[0]', start); p.append('date[1]', end);
  const fixed = { with_classify: 0, with_department: 0, with_option: 0, merge: 0, meal_style: 0 };
  for (const [k, v] of Object.entries(fixed)) p.append(k, v);
  p.append('time_slot[0]', '00:00'); p.append('time_slot[1]', '23:59');
  const rest = { search_type: 1, tc_type: 0, split_tc: 0, goods_title: '', time_type: 2,
                 token, order_type: 0, third_order_type: 0, storeid };
  for (const [k, v] of Object.entries(rest)) p.append(k, v);

  const r = await fetch('/service/index.php/data/classify_item_data', {
    method: 'POST', body: p, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
  const d = await r.json();
  if (String(d.status) !== '1') return { error: 'API_STATUS', status: d.status, msg: d.msg || d.message };
  if (Number(d.total_count) > Number(d.page_size)) return { error: 'PAGINATED', total_count: d.total_count, page_size: d.page_size };

  return {
    month: MONTH, start, end, time_type: 'pay_time',
    fetched_at: new Date().toISOString(),
    store: (d.store_list || []).map(s => s.name),
    total_count: d.total_count,
    header_totals: (d.total_list || []).map(t => ({ title: t.title, value: t.value })),
    list: (d.list || []).map(x => ({
      classify_title: x.classify_title, title: x.title, key_list: x.key_list || [],
      type: x.type, one_price: x.one_price,
      sum_num: x.sum_num, sum_total: x.sum_total,
      discount_amount: x.discount_amount, refunded_total: x.refunded_total, refunded_num: x.refunded_num,
      give_num: x.give_num, real_num: x.real_num, real_total: x.real_total,
    })),
  };
}
