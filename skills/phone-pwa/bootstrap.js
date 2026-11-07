/* Same-origin PWA overlay. No service worker, credential storage or API cache. */
(() => {
 'use strict';
 const pairPrefix='#phone-pair=';
 if(location.hash.startsWith(pairPrefix)){
  const token=decodeURIComponent(location.hash.slice(pairPrefix.length));
  const manifest=document.querySelector('link[rel=manifest]');
  if(manifest)manifest.href='/_phone/manifest.webmanifest?pair='+encodeURIComponent(token);
 }
 const originalFetch=window.fetch.bind(window);
 window.fetch=async function(input,init){
  const req=new Request(input,init);const url=new URL(req.url);
  if(url.origin!==location.origin||url.pathname.startsWith('/_phone/'))return originalFetch(req);
  const headers=new Headers(req.headers);headers.set('X-Phone-Stream','websocket');
  const response=await originalFetch(new Request(req,{headers}));
  if(response.headers.get('X-Phone-Stream')!=='websocket')return response;
  const description=await response.json();let socket,ended=false;
  const body=new ReadableStream({
   start(controller){
    socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/_phone/stream/${encodeURIComponent(description.stream)}`);socket.binaryType='arraybuffer';
    socket.onmessage=e=>{if(ended)return;const chunk=new Uint8Array(e.data);if(chunk.byteLength>controller.desiredSize){ended=true;controller.error(new TypeError('Phone stream reader fell behind (4 MiB limit)'));socket.close(1009,'Reader fell behind');return;}controller.enqueue(chunk);};
    socket.onerror=()=>{ended=true;try{controller.error(new TypeError('Phone stream connection failed'));}catch(_){};};
    socket.onclose=e=>{ended=true;try{if(e.code===1000)controller.close();else controller.error(new TypeError('Phone stream ended or device was revoked'));}catch(_){};};
    const abort=()=>{ended=true;socket.close();try{controller.error(req.signal.reason||new DOMException('Aborted','AbortError'));}catch(_){};};
    if(req.signal.aborted)abort();else req.signal.addEventListener('abort',abort,{once:true});
    socket.addEventListener('close',()=>req.signal.removeEventListener('abort',abort),{once:true});
   },cancel(){ended=true;socket?.close();}
  },{highWaterMark:4*1024*1024,size:chunk=>chunk.byteLength});
  return new Response(body,{status:description.status,headers:description.headers});
 };
 class PhoneEventSource extends EventTarget {
  static CONNECTING=0;static OPEN=1;static CLOSED=2;
  constructor(url,options={}){super();this.url=new URL(url,location.href).href;this.withCredentials=!!options.withCredentials;this.readyState=0;this.lastEventId='';this.retry=3000;this.stopped=false;this.timer=null;this.controller=null;this.connect();}
  close(){this.stopped=true;this.readyState=2;clearTimeout(this.timer);this.controller?.abort();}
  emit(type,event){this.dispatchEvent(event);const callback=this[`on${type}`];if(typeof callback==='function')callback.call(this,event);}
  async connect(){
   const controller=new AbortController();this.controller=controller;this.readyState=0;let reader=null;
   try{
    const headers={Accept:'text/event-stream'};if(this.lastEventId)headers['Last-Event-ID']=this.lastEventId;
    const r=await window.fetch(this.url,{headers,signal:controller.signal,cache:'no-store'});
    if(r.status===204){this.close();return;}
    if(!r.ok||!(r.headers.get('Content-Type')||'').startsWith('text/event-stream'))throw new Error('Event stream refused');
    this.readyState=1;this.emit('open',new Event('open'));
    reader=r.body.getReader();const decoder=new TextDecoder();let buffer='',data=[],dataChars=0,type='message';
    const line=text=>{
     if(!text){if(data.length)this.emit(type,new MessageEvent(type,{data:data.join('\n'),lastEventId:this.lastEventId,origin:new URL(this.url).origin}));data=[];dataChars=0;type='message';return;}
     if(text[0]===':')return;const colon=text.indexOf(':');const field=colon<0?text:text.slice(0,colon);let value=colon<0?'':text.slice(colon+1);if(value[0]===' ')value=value.slice(1);
     if(field==='data'){dataChars+=value.length+1;if(dataChars>4*1024*1024)throw new Error('Event frame too large');data.push(value);}else if(field==='event')type=value||'message';else if(field==='id'&&!value.includes('\0'))this.lastEventId=value;else if(field==='retry'&&/^\d+$/.test(value))this.retry=Math.max(100,Math.min(60000,Number(value)));
    };
    while(!this.stopped){const chunk=await reader.read();if(chunk.done)break;buffer+=decoder.decode(chunk.value,{stream:true});if(buffer.length>4*1024*1024)throw new Error('Event frame too large');let match;while((match=/\r\n|\r|\n/.exec(buffer))){if(match[0]==='\r'&&match.index===buffer.length-1)break;line(buffer.slice(0,match.index));buffer=buffer.slice(match.index+match[0].length);}}
   }catch(error){if(this.stopped)return;}
   finally{if(reader){try{await reader.cancel();}catch(_){}reader.releaseLock();}controller.abort();if(this.controller===controller)this.controller=null;}
   if(!this.stopped){this.readyState=0;this.emit('error',new Event('error'));this.timer=setTimeout(()=>this.connect(),this.retry);}
  }
 }
 Object.assign(PhoneEventSource.prototype,{CONNECTING:0,OPEN:1,CLOSED:2});window.EventSource=PhoneEventSource;
 document.addEventListener('DOMContentLoaded',()=>{
  // Explicit installation help stays outside the composer and conversation feed.
  const link=document.createElement('a');link.href='/_phone/help';link.textContent='Phone installation & reconnect';link.className='phone-help';
  const style=document.createElement('link');style.rel='stylesheet';style.href='/_phone/assets/phone.css';
  // Do not load page CSS over the existing SPA. A small native link lives in About if present.
  const about=document.querySelector('#page-about');if(about)about.append(link);
 });
})();
