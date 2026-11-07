'use strict';
const key = location.hash.slice(1); history.replaceState(null, '', '/');
let currentPair = null;
const status = document.querySelector('#status');
async function call(path, data) {
  const r = await fetch(path, {method:data ? 'POST':'GET', headers:{Authorization:`Bearer ${key}`, ...(data ? {'Content-Type':'application/json'}:{})}, body:data ? JSON.stringify(data):undefined, cache:'no-store'});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
function run(fn) { return async () => {try {status.classList.remove('error'); await fn();} catch(e) {status.textContent=e.message; status.classList.add('error');}}; }
async function refresh() {
  const data = await call('/status');
  document.querySelector('#origin').value=data.origin;
  const target=document.querySelector('#devices'); target.replaceChildren();
  for (const d of data.devices) {
    const row=document.createElement('div'); row.className='device';
    const name=document.createElement('strong'); name.textContent=d.label;
    const note=document.createElement('small'); note.textContent=d.revoked ? 'Revoked' : `Expires ${new Date(d.expires*1000).toLocaleString()}`;
    row.append(name,note);
    if (!d.revoked) {const b=document.createElement('button'); b.textContent='Revoke device'; b.className='secondary'; b.onclick=run(async()=>{await call('/revoke',{id:d.id}); await refresh(); status.textContent='Device revoked. Its active connections are closed.';}); row.append(b);}
    target.append(row);
  }
  if (!data.devices.length) target.textContent='No devices connected yet.';
}
document.querySelector('#save').onclick=run(async()=>{await call('/origin',{origin:document.querySelector('#origin').value.trim()}); status.textContent='Address selected. Create a new QR below.';});
document.querySelector('#pair').onclick=run(async()=>{
  const d=await call('/pair',{}); currentPair=d.id;
  // SVG is produced locally by the fixed qrcode encoder, never user-supplied markup.
  const parsed=new DOMParser().parseFromString(d.qr,'image/svg+xml');
  document.querySelector('#qr').replaceChildren(document.importNode(parsed.documentElement,true));
  document.querySelector('#link').value=d.link;
  document.querySelector('#expiry').textContent=`New connection available until ${new Date(d.expires*1000).toLocaleTimeString()}`;
  document.querySelector('#cancel').hidden=false; status.textContent='Scan this QR with your own phone.';
});
document.querySelector('#cancel').onclick=run(async()=>{await call('/revoke-pair',{id:currentPair}); currentPair=null; document.querySelector('#qr').replaceChildren();document.querySelector('#link').value='';document.querySelector('#expiry').textContent='QR revoked.';document.querySelector('#cancel').hidden=true;});
document.querySelector('#refresh').onclick=run(refresh);
run(refresh)();
