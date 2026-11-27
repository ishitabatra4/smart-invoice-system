const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function fixture() {
 const pair={sales:['1180.03'],purchases:['472.00']};
 return {labels:['Oct'],monthly_sales_purchases:pair,monthly_cash:{inflow:['100'],outflow:['0']},invoice_status:{labels:['Verified'],values:[1]},expenses:['0'],customers:[['Synthetic','1180.03']],suppliers:[],outstanding:['1080.03','0'],gst_trend:{output:['180'],input:['72']},gst_breakdown:{labels:['CGST'],values:['90']},taxable:pair};
}
function run({data=fixture(),raw=null,missing=false,library=true,throwChart=false,dates=false}={}) {
 const notes=[],charts=[],nodes={},listeners={};
 const canvas=()=>({hidden:false,parentElement:{appendChild:n=>notes.push(n.textContent)}});
 for(const id of ['salesPurchasesChart','cashFlowChart','invoiceStatusChart','expensesChart','outstandingChart','customersChart','suppliersChart','gstTrendChart','gstBreakdownChart','taxableTrendChart'])nodes[id]=canvas();
 nodes.period={value:'this_month',form:{submit(){nodes.submitted=true;}},addEventListener:(event,callback)=>listeners[event]=callback};
 if(dates){
  nodes['report-dates-edited']={value:'0'};
  for(const id of ['report-start-date','report-end-date'])nodes[id]={addEventListener:(event,callback)=>listeners[id+':'+event]=callback};
 }
 nodes['custom-range']={classList:{toggle:(name,value)=>nodes.toggle=value}};
 if(!missing)nodes['analytics-chart-data']={textContent:raw===null?JSON.stringify(data):raw};
 const document={getElementById:id=>nodes[id],querySelectorAll:()=>Object.values(nodes).filter(n=>n.parentElement),createElement:()=>({})};
 const context={document,Intl,console:{error(){}}};if(library)context.Chart=function(canvas,config){if(throwChart)throw Error('render');charts.push(config);};
 vm.runInNewContext(fs.readFileSync('static/js/analytics-dashboard.js','utf8'),context);
 return {notes,charts,nodes,listeners};
}
test('analytics renders saved values with responsive options',()=>{const r=run();assert.equal(r.charts[0].data.datasets[0].data[0],1180.03);assert.equal(r.charts[0].options.responsive,true);});
test('analytics empty series show explanatory state',()=>{const data=fixture();data.monthly_sales_purchases={sales:['0'],purchases:['0']};assert.ok(run({data}).notes.some(n=>/No records/.test(n)));});
test('analytics rejects malformed JSON and invalid shapes without rendering',()=>{for(const raw of ['<html>login</html>','null','{}','{"labels":[]}']){const r=run({raw});assert.equal(r.charts.length,0);assert.ok(r.notes.length);}});
test('analytics rejects nonfinite values',()=>{const data=fixture();data.monthly_sales_purchases.sales=['NaN'];assert.equal(run({data}).charts.length,0);});
test('analytics missing data and library fail safely',()=>{assert.equal(run({missing:true}).charts.length,0);assert.ok(run({library:false}).notes.length);});
test('analytics chart exceptions show safe fallback',()=>{assert.ok(run({throwChart:true}).notes.some(n=>/could not be rendered/.test(n)));});
test('analytics date range interaction preserves custom selection',()=>{const r=run();r.nodes.period.value='custom';r.listeners.change();assert.equal(r.nodes.toggle,false);assert.equal(r.nodes.submitted,undefined);r.nodes.period.value='last_month';r.listeners.change();assert.equal(r.nodes.submitted,true);});

test('editing either date selects Custom and marks edited range',()=>{for(const id of ['report-start-date','report-end-date']){const r=run({dates:true});r.listeners[id+':input']();assert.equal(r.nodes.period.value,'custom');assert.equal(r.nodes['report-dates-edited'].value,'1');assert.equal(r.nodes.toggle,false);assert.equal(r.nodes.submitted,undefined);}});
test('choosing a preset clears stale manual-date intent',()=>{const r=run({dates:true});r.listeners['report-start-date:change']();r.nodes.period.value='last_month';r.listeners.change();assert.equal(r.nodes['report-dates-edited'].value,'0');assert.equal(r.nodes.submitted,true);});
