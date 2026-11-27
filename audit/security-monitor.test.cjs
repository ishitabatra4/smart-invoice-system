const test = require('node:test');
const assert = require('node:assert/strict');
const {validRange, attach} = require('../static/js/security-monitor.js');
test('security date range includes same day and 366 inclusive days',()=>{ assert.equal(validRange('2026-01-01','2026-12-31'),true); assert.equal(validRange('2026-01-01','2026-01-01'),true); });
test('security date range rejects reversed, invalid and excessive dates',()=>{ for(const values of [['2026-02-01','2026-01-01'],['bad','2026-01-01'],['2026-01-01','2027-01-02']]) assert.equal(validRange(...values),false); });
test('optional server default dates remain usable',()=>assert.equal(validRange('',''),true));
test('enhancement works on pages without forms',()=>assert.doesNotThrow(()=>attach({querySelectorAll:()=>[]})));
function filterForm(startValue,endValue) {
  const listeners={};
  const start={value:startValue};
  const end={value:endValue,message:'',reported:false,setCustomValidity(value){this.message=value;},reportValidity(){this.reported=true;}};
  const form={addEventListener(name,fn){listeners[name]=fn;},querySelector(name){return name.includes('start_date')?start:end;}};
  attach({querySelectorAll(selector){return selector==='[data-security-filter]'?[form]:[];}});
  return {listeners,start,end};
}
test('invalid filter submit shows validation and blocks navigation',()=>{
  const {listeners,end}=filterForm('2026-02-01','2026-01-01');let blocked=false;
  listeners.submit({preventDefault(){blocked=true;}});assert.equal(blocked,true);assert.equal(end.reported,true);assert.match(end.message,/366/);
  listeners.input();assert.equal(end.message,'');
});
test('valid filter submit preserves ordinary server navigation',()=>{
  const {listeners}=filterForm('2026-01-01','2026-02-01');let blocked=false;
  listeners.submit({preventDefault(){blocked=true;}});assert.equal(blocked,false);
});
test('alert decision confirmation supports cancel and explicit confirmation',()=>{
  let submit;
  const form={addEventListener(name,fn){submit=fn;}};
  attach({querySelectorAll(selector){return selector==='[data-security-confirm]'?[form]:[];}});
  const original=globalThis.confirm;
  try {
    let blocked=false;globalThis.confirm=()=>false;submit({preventDefault(){blocked=true;}});assert.equal(blocked,true);
    blocked=false;globalThis.confirm=()=>true;submit({preventDefault(){blocked=true;}});assert.equal(blocked,false);
  } finally {globalThis.confirm=original;}
});
