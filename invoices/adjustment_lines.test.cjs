const {test} = require('node:test');
const assert = require('node:assert/strict');
const {addLine} = require('../static/invoices/adjustment_lines.js');
function fixture(value) {
  const inserted=[];
  const nodes={'id_items-TOTAL_FORMS':{value},'empty-adjustment-line':{innerHTML:'<input name="items-__prefix__-amount" id="id_items-__prefix__-amount">'},'adjustment-lines':{insertAdjacentHTML:(position,html)=>inserted.push(html)}};
  return {doc:{getElementById:id=>nodes[id]},nodes,inserted};
}
test('adding a row updates management count and unique names and IDs',()=>{
  const f=fixture('2');assert.equal(addLine(f.doc),true);assert.equal(f.nodes['id_items-TOTAL_FORMS'].value,'3');assert.match(f.inserted[0],/items-2-amount/);assert.doesNotMatch(f.inserted[0],/__prefix__/);
});
test('client row cap and invalid counts never insert rows',()=>{
  for (const value of ['100','-1','1.5','bad']) {const f=fixture(value);assert.equal(addLine(f.doc),false);assert.equal(f.inserted.length,0);assert.equal(f.nodes['id_items-TOTAL_FORMS'].value,value);}
});
