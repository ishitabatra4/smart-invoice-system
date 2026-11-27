const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('static/js/expenses-dashboard.js', 'utf8');
function render(monthly, categories, available=true) {
 const charts=[],notes=[];
 const canvas=()=>({hidden:false,parentElement:{appendChild:n=>notes.push(n.textContent)}});
 const trend=canvas(),category=canvas();
 const nodes={'expense-monthly-data':{textContent:JSON.stringify(monthly)},'expense-category-data':{textContent:JSON.stringify(categories)},expenseTrendChart:trend,expenseCategoryChart:category};
 const document={getElementById:id=>nodes[id],querySelectorAll:()=>[trend,category],createElement:()=>({})};
 const context={document,Intl};
 if(available)context.Chart=function(node,config){charts.push(config);};
 vm.runInNewContext(script,context);
 return {charts,notes,trend};
}
test('expense chart reads saved amount/label objects from Django',()=>{
 const result=render([{label:'Nov 2026',amount:'1180.03'}],[['Rent','1180.03']]);
 assert.equal(result.charts[0].data.labels[0],'Nov 2026');
 assert.equal(result.charts[0].data.datasets[0].data[0],1180.03);
 assert.equal(result.charts[1].data.datasets[0].data[0],1180.03);
});
test('empty expense charts show accessible explanatory text',()=>{
 const result=render([{label:'Nov',amount:'0.00'}],[]);
 assert.equal(result.charts.length,0);assert.equal(result.notes.length,2);assert.equal(result.trend.hidden,true);
});
test('missing chart library produces a readable fallback',()=>{
 const result=render([],[],false);assert.equal(result.notes.length,2);assert.match(result.notes[0],/Charts could not load/);
});
