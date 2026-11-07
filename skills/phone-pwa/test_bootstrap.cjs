const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
function boot(){
 let socket;
 class Socket extends EventTarget{constructor(){super();socket=this;this.closed=false;}close(code=1000){this.closed=true;this.onclose?.({code});this.dispatchEvent(new Event('close'));}}
 const window={fetch:async()=>new Response(JSON.stringify({stream:'ticket',status:200,headers:{'Content-Type':'text/event-stream'}}),{headers:{'X-Phone-Stream':'websocket'}})};
 vm.runInNewContext(fs.readFileSync('bootstrap.js','utf8'),{window,location:new URL('https://phone.example/'),document:{addEventListener(){}},WebSocket:Socket,Request,Response,Headers,ReadableStream,Uint8Array,EventTarget,Event,MessageEvent,TextDecoder,AbortController,DOMException,URL,setTimeout,clearTimeout});
 return {window,get socket(){return socket;}};
}
test('stalled reader has bounded bytes and closes its transport',async()=>{
 const b=boot();const r=await b.window.fetch('https://phone.example/sse');
 const bytes=new Uint8Array(1024*1024).buffer;
 for(let i=0;i<10;i++)b.socket.onmessage({data:bytes});
 assert.equal(b.socket.closed,true);
 await assert.rejects(r.body.getReader().read(),/fell behind/);
});
test('finite EOF succeeds; upstream failure rejects; cancellation closes',async()=>{
 let b=boot(),r=await b.window.fetch('https://phone.example/sse');b.socket.onmessage({data:new TextEncoder().encode('data: ok\n\n').buffer});b.socket.onclose({code:1000});assert.equal(await r.text(),'data: ok\n\n');
 b=boot();r=await b.window.fetch('https://phone.example/sse');b.socket.onclose({code:1011});await assert.rejects(r.text(),/ended/);
 b=boot();r=await b.window.fetch('https://phone.example/sse');await r.body.cancel();assert.equal(b.socket.closed,true);
});
test('EventSource bounds multi-line frames and closes abandoned reader before reconnect',async()=>{
 const b=boot();const event=new b.window.EventSource('https://phone.example/sse');
 await new Promise(resolve=>setImmediate(resolve));
 const old=b.socket;const bytes=new TextEncoder().encode('data: '+ 'x'.repeat(4096)+'\n').buffer;
 for(let i=0;i<1100&&!old.closed;i++){old.onmessage({data:bytes});await new Promise(resolve=>setImmediate(resolve));}
 assert.equal(old.closed,true);event.close();assert.equal(event.readyState,2);
});
test('connection page retains the admission link across independent handoffs',async()=>{
 const fields={'#pair-link':{value:'',readOnly:false},'#result':{classList:{remove(){},add(){}},textContent:''},'#label':{value:'test'},'#open':{focus(){},href:''},'link[rel=manifest]':{href:''},'#submit':{disabled:false}};
 let handler;fields['#connect']={addEventListener(_,fn){handler=fn;}};const calls=[];
 const location={hash:'#shared-window-token',pathname:'/_phone/connect',origin:'https://phone.example'};
 vm.runInNewContext(fs.readFileSync('connect.js','utf8'),{location,document:{querySelector:s=>fields[s]},window:{addEventListener(){}},navigator:{},matchMedia:()=>({matches:false}),history:{replaceState(){throw new Error('normal browser must preserve admission URL');}},fetch:async(url,init)=>{calls.push(JSON.parse(init.body).token);return {ok:true};},URL,encodeURIComponent});
 await handler({preventDefault(){}});await handler({preventDefault(){}});
 assert.deepEqual(calls,['shared-window-token','shared-window-token']);
 assert.match(fields['link[rel=manifest]'].href,/pair=shared-window-token/);
 assert.equal(fields['#open'].href,'/#phone-pair=shared-window-token');assert.equal(location.hash,'#shared-window-token');
});
