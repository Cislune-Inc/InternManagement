"""Shared-screen UI: no credentials or personal data in browser storage."""

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Don Pollo · Shop clock</title><style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#102b41;background:#eef3f8}
body{margin:0;padding:clamp(16px,3vw,36px)}main{max-width:680px;margin:auto}
header{font-size:1rem;font-weight:750;color:#265a78}h1{font-size:2rem;margin:.5em 0}
section{background:white;padding:24px;border:1px solid #bacddc;border-radius:16px}
p,label{font-size:1rem;line-height:1.5}label{display:block;font-weight:650}
input,select,button{box-sizing:border-box;font:inherit;font-size:1.125rem;border-radius:8px;padding:14px}
input,select{width:100%;border:2px solid #527187;margin:8px 0 16px}input{letter-spacing:.4em}
button{border:1px solid #527187;background:#e8f0f7;color:#102b41;cursor:pointer;font-weight:650;min-height:48px}
#start{background:#075e90;color:white;width:100%;margin-top:16px}button:disabled{opacity:.6}
:focus-visible{outline:3px solid #bd6200;outline-offset:3px}
.secondary{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.secondary button{flex:1}
#result{white-space:pre-wrap;font-size:1.125rem;line-height:1.5}small{display:block;font-size:1rem;line-height:1.5;color:#3c5364}
[hidden]{display:none!important}
</style><main><header>CISLUNE / DON POLLO</header><h1>Shop time clock</h1>
<section><form id="clock" autocomplete="off"><label for="person">Your name</label>
<select id="person" required><option value="">Choose your name</option></select>
<label id="pinlabel" for="pin">Your PIN (2–6 digits)</label>
<input id="pin" type="password" inputmode="numeric" pattern="[0-9]{2,6}" minlength="2" maxlength="6" autocomplete="off" required>
<div id="repeat" hidden><label for="confirmPin">Enter your new PIN again</label>
<input id="confirmPin" type="password" inputmode="numeric" pattern="[0-9]{2,6}" minlength="2" maxlength="6" autocomplete="off"></div>
<button id="start" type="submit">Start / return to work</button>
<div class="secondary" id="actions"><button type="button" data-action="out">Clock out</button><button type="button" data-action="lunch">Lunch</button><button type="button" data-action="rest">Paid rest</button></div>
</form><div class="secondary"><button type="button" id="setup">Set / reset PIN</button><button type="button" id="cancel">Clear screen</button></div>
<p id="result" role="status" aria-live="polite"></p>
<small>Type your PIN with the keyboard; Enter starts or returns to work. Four or more digits recommended. No phone needed after setup. Use Slack for work updates, your hours and corrections. Do not share your PIN.</small></section>
<p>Offsite work needs Erik’s advance approval. If DP is unavailable, send Erik your actual hours.</p></main>
<script nonce="__NONCE__">
const people=__PEOPLE__,csrf=__CSRF__,person=document.querySelector('#person'),pin=document.querySelector('#pin'),confirmPin=document.querySelector('#confirmPin'),result=document.querySelector('#result'),form=document.querySelector('#clock'),start=document.querySelector('#start');
let setup=false,timer,busy=false,pending=null;
for(const p of people){const o=document.createElement('option');o.value=p.id;o.textContent=p.name;person.append(o);}
function mode(value){setup=value;document.querySelector('#repeat').hidden=!value;confirmPin.required=value;document.querySelector('#actions').hidden=value;start.textContent=value?'Save my PIN':'Start / return to work';document.querySelector('#pinlabel').textContent=value?'Choose a PIN (2–6 digits)':'Your PIN (2–6 digits)';pin.value='';confirmPin.value='';pending=null;}
function clear(){if(busy)return;mode(false);person.value='';result.textContent='';clearTimeout(timer);}
function touch(){clearTimeout(timer);timer=setTimeout(clear,30000);}
person.addEventListener('change',()=>{mode(false);result.textContent='';touch();pin.focus();});
for(const field of [pin,confirmPin]){field.addEventListener('focus',touch);field.addEventListener('input',touch);}
document.querySelector('#setup').onclick=()=>{mode(!setup);result.textContent=setup?'One-time setup must be opened by Erik or by sending “kiosk setup” to DP in your own Slack. Choose 2–6 digits; four or more recommended. Enter your PIN here, never in chat.':'';touch();pin.focus();};
document.querySelector('#cancel').onclick=clear;
async function submit(action){if(busy||!form.reportValidity())return;const actor=person.value;const id=pending&&pending.actor===actor&&pending.action===action?pending.id:crypto.randomUUID();pending={actor,action,id};
const payload={actor,pin:pin.value,confirmation:confirmPin.value,action,request_id:id};busy=true;clearTimeout(timer);document.querySelectorAll('button,input,select').forEach(el=>el.disabled=true);result.textContent='Checking…';
try{const response=await fetch(setup?'/pin/setup':'/confirm',{method:'POST',headers:{'Content-Type':'application/json','X-Kiosk-CSRF':csrf},body:JSON.stringify(payload)});const data=await response.json();result.textContent=data.message;
if(response.ok){pending=null;mode(false);person.value='';}else{pin.value='';confirmPin.value='';}}
catch{pin.value='';confirmPin.value='';result.textContent='Connection interrupted. Check My hours in Slack before retrying. Report actual times to Erik if needed.';}
finally{payload.pin='';payload.confirmation='';busy=false;document.querySelectorAll('button,input,select').forEach(el=>el.disabled=false);timer=setTimeout(clear,15000);}}
form.addEventListener('submit',e=>{e.preventDefault();submit('start');});document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>submit(b.dataset.action));
document.addEventListener('visibilitychange',()=>{if(document.hidden){pin.value='';confirmPin.value='';clear();}});
</script></html>"""
