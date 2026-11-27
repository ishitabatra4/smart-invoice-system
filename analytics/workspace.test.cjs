const test=require('node:test');
const assert=require('node:assert/strict');
const {runInNewContext}=require('node:vm');
const {readFileSync}=require('node:fs');
const source=readFileSync(require('node:path').join(__dirname,'../static/js/workspace.js'),'utf8');
function element(extra={}) { return {attrs:{},listeners:{},setAttribute(n,v){this.attrs[n]=v;},removeAttribute(n){delete this.attrs[n];},hasAttribute(n){return n in this.attrs;},addEventListener(n,fn){this.listeners[n]=fn;},...extra}; }
function setup({manual=false, narrow=false, overflow=true}={}) {
 const button=element({name:'action',value:'prepare',disabled:false});
 const form=element({id:manual?'manual-invoice-form':'standard',children:[],querySelectorAll(){return [button];},appendChild(n){this.children.push(n);}});
 const region=element({scrollWidth:overflow?800:200,clientWidth:400});
 const summary={focused:false,focus(){this.focused=true;}};
 const media=element({matches:narrow});
 const menu=element({open:true,querySelector(){return summary;}});
 const chart={count:0,resize(){this.count++;}};
 const detail=element({open:true,querySelectorAll(){return [{}];}});
 const events={};
 const doc={querySelector(){return menu;},querySelectorAll(selector){return selector.startsWith('form')?[form]:selector==='.table-responsive'?[region]:selector==='details.workspace-secondary'?[detail]:[];},createElement(){return element({removed:false,remove(){this.removed=true;}});}};
 const window={document:doc,matchMedia(){return media;},addEventListener(n,fn){events[n]=fn;},Chart:{getChart(){return chart;}}};
 runInNewContext(source,{window,Promise,Map});
 return {form,button,menu,media,summary,region,events,detail,chart};
}
function event(){return {defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};}
test('submit guard preserves submitter action name/value and enabled state',()=>{const s=setup();s.form.listeners.submit(event());assert.equal(s.button.name,'action');assert.equal(s.button.value,'prepare');assert.equal(s.button.disabled,false);assert.equal(s.form.attrs['aria-busy'],'true');assert.equal(s.button.attrs['aria-disabled'],'true');});
test('duplicate submission is prevented without sending another action',()=>{const s=setup();s.form.listeners.submit(event());const second=event();s.form.listeners.submit(second);assert.equal(second.defaultPrevented,true);assert.equal(s.form.children.length,1);});
test('cancelled confirmation resets pending state',async()=>{const s=setup();const ev=event();s.form.listeners.submit(ev);ev.preventDefault();await Promise.resolve();assert.equal(s.form.attrs['aria-busy'],undefined);assert.equal(s.form.children[0].removed,true);});
test('already cancelled submission does not show pending state',()=>{const s=setup();const ev=event();ev.preventDefault();s.form.listeners.submit(ev);assert.equal(s.form.children.length,0);});
test('back navigation restores pending controls',()=>{const s=setup();s.form.listeners.submit(event());s.events.pageshow();assert.equal(s.button.attrs['aria-disabled'],undefined);assert.equal(s.form.attrs['aria-busy'],undefined);});
test('existing manual invoice submit owner is not registered twice',()=>{const s=setup({manual:true});assert.equal(s.form.listeners.submit,undefined);});
test('mobile navigation collapses and desktop resize opens it',()=>{const s=setup({narrow:true});assert.equal(s.menu.open,false);s.media.matches=false;s.media.listeners.change();assert.equal(s.menu.open,true);});
test('escape closes mobile navigation and restores summary focus',()=>{const s=setup({narrow:true});s.menu.open=true;s.menu.listeners.keydown({key:'Escape'});assert.equal(s.menu.open,false);assert.equal(s.summary.focused,true);});
test('desktop menu does not close on escape',()=>{const s=setup();s.menu.listeners.keydown({key:'Escape'});assert.equal(s.menu.open,true);});
test('overflow tables receive a keyboard scroll region',()=>{const s=setup();assert.equal(s.region.attrs.tabindex,'0');assert.equal(s.region.attrs.role,'region');assert.match(s.region.attrs['aria-label'],/Scrollable/);});
test('nonoverflow tables do not add unnecessary tab stops',()=>{const s=setup({overflow:false});assert.equal(s.region.attrs.tabindex,undefined);});
test('expanding analytics resizes existing charts without recalculating values',()=>{const s=setup();s.detail.listeners.toggle();assert.equal(s.chart.count,1);s.detail.open=false;s.detail.listeners.toggle();assert.equal(s.chart.count,1);});

function accordionSetup({animated=false,reduced=false}={}) {
    const groups=Array.from({length:2},()=>{
        const summary=element({getBoundingClientRect(){return {height:40};}});
        const group=element({open:false,style:{},scrollHeight:120,getBoundingClientRect(){return {height:this.open?120:40};},querySelector(){return summary;},summary});
        if(animated) group.animate=()=>{group.animation={cancel(){if(this.oncancel)this.oncancel();}};return group.animation;};
        return group;
    });
    const menu=element({open:true,querySelector(){return {focus(){}};}});
    const doc={querySelector(){return menu;},querySelectorAll(selector){return selector==='[data-nav-accordion]'?groups:[];}};
    const window={document:doc,matchMedia(query){return {matches:query.includes('reduced-motion')?reduced:false,addEventListener(){}};},addEventListener(){}};
    runInNewContext(source,{window,Promise,Map});
    const click=g=>g.summary.listeners.click(event());
    return {groups,click,menu};
}
test('navigation accordion opens one menu and closes siblings',()=>{const {groups,click}=accordionSetup();click(groups[0]);assert.equal(groups[0].open,true);click(groups[1]);assert.equal(groups[0].open,false);assert.equal(groups[1].open,true);click(groups[1]);assert.equal(groups[1].open,false);});
test('reduced motion navigation completes synchronously',()=>{const {groups,click}=accordionSetup({animated:true,reduced:true});click(groups[0]);assert.equal(groups[0].open,true);click(groups[0]);assert.equal(groups[0].open,false);assert.equal(groups[0].style.overflow,'');});
test('mobile navigation closes when a destination is selected',()=>{const s=setup({narrow:true});s.menu.open=true;s.menu.listeners.click({target:{closest(){return {};}}});assert.equal(s.menu.open,false);});

test('animated accordion finishes closed and supports reversing a pending close',()=>{
    const {groups,click}=accordionSetup({animated:true});const g=groups[0];
    click(g);g.animation.onfinish();assert.equal(g.open,true);
    click(g);g.animation.onfinish();assert.equal(g.open,false);assert.equal(g.style.overflow,'');
    click(g);g.animation.onfinish();click(g);click(g);g.animation.onfinish();assert.equal(g.open,true);
});

test('cash flow fragment reveals its existing panel and selects its sidebar link',()=>{
    function classes(){const values=new Set();return {add(v){values.add(v);},remove(v){values.delete(v);},toggle(v,on){if(on)values.add(v);else values.delete(v);},has(v){return values.has(v);}};}
    const dashboard=element({hash:'',pathname:'/analytics/',search:'',classList:classes()});
    const cash=element({hash:'#cashFlowChart',pathname:'/analytics/',search:'',classList:classes()});
    dashboard.classList.add('is-active');dashboard.setAttribute('aria-current','page');
    const summary=element({classList:classes()});const panel={tagName:'DETAILS',open:false,parentElement:null};
    const group=element({open:false,querySelector(){return summary;},contains(link){return link===cash;}});
    const target={parentElement:panel,scrollIntoView(){this.scrolled=true;}};
    const doc={querySelector(){return null;},getElementById(id){return id==='cashFlowChart'?target:null;},querySelectorAll(selector){return selector==='[data-nav-accordion]'?[group]:selector==='.workspace-navigation a[href]'?[dashboard,cash]:[];}};
    const window={document:doc,location:{hash:'#cashFlowChart',pathname:'/analytics/',search:''},addEventListener(){}};
    runInNewContext(source,{window,Promise,Map});
    assert.equal(cash.attrs['aria-current'],'page');assert.equal(dashboard.attrs['aria-current'],undefined);
    assert.equal(panel.open,true);assert.equal(group.open,true);assert.equal(target.scrolled,true);
});
