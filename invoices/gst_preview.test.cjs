const {test} = require('node:test');
const assert = require('node:assert/strict');
const api = require('../static/js/gst-preview.js');
const amounts = {taxable_amount:'1000.00',total_discount_amount:'0.00',cgst_amount:'90.00',sgst_amount:'90.00',igst_amount:'0.00',total_amount:'1180.00'};

test('preview renders exact server decimal strings with no monetary rounding',()=>{
    assert.match(api.summary(amounts),/CGST ₹90.00/);
    assert.match(api.summary({...amounts,total_amount:'999999999999.99'}),/999999999999.99/);
});

test('nested field and line validation messages are flattened',()=>{
    assert.deepEqual(api.errors({invoice:{place:['Enter place of supply']},lines:[{}, {rate:['Choose a configured rate']}]}),['Enter place of supply','Choose a configured rate']);
});

function form(){return {dataset:{invoiceId:'42'},listeners:{},addEventListener(name,handler){this.listeners[name]=handler;}};}
function target(){return {textContent:'',attributes:{},setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];}};}
async function exercise(response){
    const old={fetch:global.fetch,FormData:global.FormData,setTimeout:global.setTimeout,clearTimeout:global.clearTimeout};
    const tasks=[],requests=[];
    global.setTimeout=(callback)=>{tasks.push(callback);return tasks.length;};
    global.clearTimeout=()=>{};
    global.FormData=class {constructor(){this.values={attachment:'private-file'};}delete(key){delete this.values[key];}set(key,value){this.values[key]=value;}};
    global.fetch=async (url,options)=>{requests.push({url,options});return response;};
    try{
        const element=form(),display=target();
        api.attach(element,'/invoices/calculate/sales/',display);
        await tasks.shift()();
        return {requests,display};
    }finally{Object.assign(global,old);}
}

test('preview posts to Python with CSRF form data, omits attachments and displays saved-format totals',async()=>{
    const {requests,display}=await exercise({ok:true,json:async()=>amounts});
    assert.equal(requests.length,1);
    assert.equal(requests[0].options.method,'POST');
    assert.equal(requests[0].options.credentials,'same-origin');
    assert.equal(requests[0].options.body.values.invoice_id,'42');
    assert.equal(requests[0].options.body.values.attachment,undefined);
    assert.equal(display.textContent,api.summary(amounts));
    assert.equal(display.attributes['aria-busy'],undefined);
});

test('server validation failure replaces stale totals with readable error text',async()=>{
    const {display}=await exercise({ok:false,json:async()=>({errors:['Special supply requires review']})});
    assert.equal(display.textContent,'Special supply requires review');
});
