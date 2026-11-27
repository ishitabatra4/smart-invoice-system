const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function run({raw=null,empty=false,missing=false,library=true,throwChart=false}={}) {
 const charts=[],notes=[],nodes={};for(const id of ['auditDailyChart','auditModuleChart','auditActionChart','auditUserChart','auditSecurityChart'])nodes[id]={hidden:false,parentElement:{appendChild:n=>notes.push(n.textContent)}};
 const data={};for(const key of ['daily','modules','actions','users','security'])data[key]={labels:empty?[]:['Synthetic'],values:empty?[]:[1]};
 if(!missing)nodes['audit-chart-data']={textContent:raw===null?JSON.stringify(data):raw};
 const document={getElementById:id=>nodes[id],createElement:()=>({}),addEventListener:(name,callback)=>callback()};
 const context={document,window:{}};if(library)context.window.Chart=context.Chart=function(node,config){if(throwChart)throw Error('render');charts.push(config);};
 vm.runInNewContext(fs.readFileSync('static/js/audit-dashboard.js','utf8'),context);return{charts,notes};
}
test('audit chart data renders responsively',()=>{const r=run();assert.equal(r.charts.length,5);assert.equal(r.charts[0].options.responsive,true);});
test('audit empty state is readable',()=>{const r=run({empty:true});assert.equal(r.charts.length,0);assert.equal(r.notes.length,5);assert.match(r.notes[0],/No activity/);});
test('audit malformed data uses fallback',()=>{for(const raw of ['invalid','null','{}','{"daily":{"labels":[],"values":"wrong"}}']){const r=run({raw});assert.equal(r.charts.length,0);assert.equal(r.notes.length,5);}});
test('audit missing data renders no private chart',()=>{assert.equal(run({missing:true}).charts.length,0);});
test('audit unavailable library and rendering failures are handled',()=>{assert.match(run({library:false}).notes[0],/unavailable/);assert.match(run({throwChart:true}).notes[0],/could not be rendered/);});
