// Synthetic API fixtures only. Does not connect to a browser or a network.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync(new URL('./fetch_month.js',import.meta.url),'utf8').replaceAll('__MONTH__','2026-10');
const report = Array.from({length:31},(_,i)=>({date:`2026-10-${String(i+1).padStart(2,'0')}`}));
const sales = [1,2,3].map(i=>({title:`product ${i}`,one_price:25,real_num:1,real_total:25}));
async function run(change=()=>{},cookie='Admin-Token=fake-secret; StoreId=fake-store') {
  const calls=[];
  const context={document:{cookie},URLSearchParams,AbortController,setTimeout,clearTimeout,Date,console,
    fetch:async(path,options)=>{
      const p=options.body, page=Number(p.get('page')||1); calls.push({path,page});
      let data;
      if(path.includes('classify_item_data')) data={status:1,total_count:3,list:sales.slice((page-1)*2,page*2)};
      else if(path.endsWith('/report')) data={status:1,list:report};
      else if(path.includes('recharge_statistics')) data={status:1,total_count:0,list:[],statistics:{}};
      else data={status:1,list:[]};
      change(path,data,page);
      return {ok:!data.httpFail,status:data.httpFail?503:200,json:async()=>data};
    }};
  const result=await vm.runInNewContext(`(${source})()`,context);
  assert.ok(!JSON.stringify(result).includes('fake-secret'));
  return {result,calls};
}
let count=0;
async function check(name,test){await test();console.log(`PASS ${name}`);count++;}
await check('sales pagination combines every row',async()=>{const {result}=await run();assert.equal(result.error,undefined,JSON.stringify(result));assert.equal(result.list.length,3);assert.equal(result.fetch_meta.sales.pages,2);});
for(const endpoint of ['classify_item_data','/report','recharge_statistics','get_order_list']) {
  await check(`reject failed status ${endpoint}`,async()=>{const {result}=await run((path,d)=>{if(path.includes(endpoint))d.status=0;});assert.equal(result.error,'FETCH_FAILED');assert.match(result.reason,/API_STATUS/);});
}
await check('missing ledger total fails closed',async()=>{const {result}=await run((p,d)=>{if(p.includes('recharge_statistics'))delete d.total_count;});assert.match(result.reason,/TOTAL_MISSING/);});
await check('duplicate page detected',async()=>{const {result}=await run((p,d)=>{if(p.includes('classify_item_data')){d.list=[sales[0]];d.total_count=3;}});assert.match(result.reason,/PAGINATION_IGNORED/);});
await check('changing total detected',async()=>{const {result}=await run((p,d,page)=>{if(p.includes('classify_item_data')&&page===2)d.total_count=4;});assert.match(result.reason,/TOTAL_CHANGED/);});
await check('missing report day detected',async()=>{const {result}=await run((p,d)=>{if(p.endsWith('/report'))d.list=report.slice(1);});assert.match(result.reason,/REPORT_DAYS/);});
await check('HTTP failure detected',async()=>{const {result}=await run((p,d)=>{d.httpFail=true;});assert.match(result.reason,/HTTP_STATUS/);});
await check('missing list detected',async()=>{const {result}=await run((p,d)=>{delete d.list;});assert.match(result.reason,/API_SCHEMA/);});
await check('orders beyond first full page are not truncated',async()=>{
  const {result}=await run((p,d,page)=>{if(p.includes('get_order_list'))d.list=page===1?Array.from({length:100},(_,i)=>({status_text:'已支付',payTime:`2026-10-01 00:00:${i}`,total:10000,goods_list:[],admin_order:'0'})):[];});
  assert.equal(result.big_orders.length,100);assert.equal(result.fetch_meta.orders.pages,2);
});
await check('login missing makes no request',async()=>{const {result,calls}=await run(()=>{},'');assert.equal(result.error,'NOT_LOGGED_IN');assert.equal(calls.length,0);});
await check('bad month makes no request',async()=>{
  const context={document:{cookie:'Admin-Token=x; StoreId=y'},URLSearchParams};
  const r=await vm.runInNewContext(`(${source.replaceAll('2026-10','2026-13')})()`,context);assert.equal(r.error,'BAD_MONTH');
});
console.log(`${count} fetch tests passed`);
