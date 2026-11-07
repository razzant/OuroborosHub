'use strict';
let token='';
const standalone=matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const field=document.querySelector('#pair-link'),result=document.querySelector('#result');
function rememberLink(value){
 token=value;
 const manifest=document.querySelector('link[rel=manifest]');
 manifest.href='/_phone/manifest.webmanifest?pair='+encodeURIComponent(token);
 document.querySelector('#open').href='/#phone-pair='+encodeURIComponent(token);
 field.value='Connection link received';field.readOnly=true;
}
function captureLink(){const value=location.hash.slice(1);if(value)rememberLink(value);}
captureLink();window.addEventListener('hashchange',captureLink);
document.querySelector('#connect').addEventListener('submit',async event=>{
 event.preventDefault();const button=document.querySelector('#submit');button.disabled=true;
 try {
  if(!token){const u=new URL(field.value);if(u.origin!==location.origin||u.pathname!=='/_phone/connect')throw new Error('Use a connection link for this address. A different tunnel needs its own new icon.');rememberLink(u.hash.slice(1));history.replaceState(null,'','/_phone/connect#'+token);}
  if(!token)throw new Error('Scan or paste a QR connection link from your PC.');
  const r=await fetch('/_phone/redeem',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,label:document.querySelector('#label').value}),cache:'no-store'});
  if(!r.ok)throw new Error(await r.text());
  result.classList.remove('error');result.textContent='Connected. The same link can connect another browser or installed app until its ten-minute window ends.';
  if(standalone){history.replaceState(null,'','/_phone/connect');location.replace('/');return;}
  // Keep the temporary admission link for Safari copy / Home Screen installation.
  document.querySelector('#open').focus();
 }catch(e){field.readOnly=false;field.value='';token='';result.classList.add('error');result.textContent=e.message;}finally{button.disabled=false;}
});
if(standalone){
 fetch('/_phone/session',{cache:'no-store'}).then(r=>{
  if(r.ok)location.replace('/');else if(token)document.querySelector('#connect').requestSubmit();
 }).catch(()=>{result.textContent='PC or tunnel unavailable. Try again when it is online.';});
}
