import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const uuid='12345678-1234-4234-8234-123456789012';
function packet(host='example.com',payload=[22,3,1],command=1,port=443) {
  const identity=uuid.replaceAll('-','').match(/../g).map(x=>parseInt(x,16));
  const address=new TextEncoder().encode(host);
  return new Uint8Array([0,...identity,0,command,port>>8,port&255,2,address.length,...address,...payload]);
}
const servers=[],calls=[],sockets=[],dnsRequests=[];
class MockSocket {
  readyState=1; listeners={}; sent=[];
  accept() {}
  addEventListener(name,fn){this.listeners[name]=fn;}
  send(value){this.sent.push([...value]);}
  close(){this.readyState=3;}
  emit(bytes){this.listeners.message({data:bytes});}
}
function connect(address) {
  calls.push(address);
  const writes=[];
  let reader;
  const result={
    opened: address.hostname==='example.com' ? Promise.reject(new Error('direct unavailable')) : Promise.resolve(),
    closed:Promise.resolve(),
    writable:new WritableStream({write(chunk){writes.push([...chunk]);}}),
    readable:new ReadableStream({start(controller){reader=controller;}}),
    close(){try{reader.close();}catch{}},writes,reader:()=>reader
  };
  sockets.push(result);return result;
}
const context={connect,URL,TextEncoder,TextDecoder,Uint8Array,ArrayBuffer,ReadableStream,WritableStream,
  setTimeout,clearTimeout,atob,AbortSignal,
  fetch:async(url,options)=>{
    assert.equal(url,'https://cloudflare-dns.com/dns-query');
    dnsRequests.push([...options.body]);
    return {ok:true,arrayBuffer:async()=>new Uint8Array([18,52]).buffer};
  },
  Response:class {constructor(body,options){this.body=body;Object.assign(this,options);}static json(body){return new this(body,{status:200});}},
  WebSocketPair:class {constructor(){this[0]=new MockSocket();this[1]=new MockSocket();servers.push(this[1]);}}
};
const code=fs.readFileSync(new URL('./vless-relay.mjs',import.meta.url),'utf8')
  .replace("import { connect } from 'cloudflare:sockets';",'')
  .replace('export function parseHeader','function parseHeader')
  .replace('export default ','globalThis.worker = ');
vm.runInNewContext(code+'\nglobalThis.parseHeader=parseHeader;',context);
const frame=packet();
const parsed=context.parseHeader(frame,uuid);
assert.equal(parsed.host,'example.com');assert.equal(parsed.port,443);
for(let length=0;length<parsed.offset;length++) assert.equal(context.parseHeader(frame.slice(0,length),uuid),null);
assert.throws(()=>context.parseHeader(frame,'00000000-0000-4000-8000-000000000000'),/Authentication/);
assert.throws(()=>context.parseHeader(packet('example.com',[],2,443),uuid),/Unsupported/);
const jobs=[];
const ctx={waitUntil(job){jobs.push(job);}};
for(const ip of ['1.1.1.1','8.8.8.8']) {
  const response=await context.worker.fetch(new Request('https://worker.test/',{headers:{Upgrade:'websocket'}}),{UUID:uuid,PROXYIP:ip},ctx);
  assert.equal(response.status,101);
  const server=servers.at(-1);
  server.emit(frame.slice(0,10));server.emit(frame.slice(10));
}
await new Promise(resolve=>setTimeout(resolve,30));
assert.equal(calls.filter(x=>x.hostname==='1.1.1.1').length,1);
assert.equal(calls.filter(x=>x.hostname==='8.8.8.8').length,1);
const outgoing=sockets.filter(x=>x.writes.length);
assert.equal(outgoing.length,2);
for(const upstream of outgoing) assert.deepEqual(upstream.writes,[[22,3,1]]);
for(const upstream of outgoing) upstream.reader().enqueue(new Uint8Array([22,3,3]));
await new Promise(resolve=>setTimeout(resolve,10));
for(const server of servers) assert.deepEqual(server.sent,[[0,0,22,3,3]]);
const before=calls.length;
await context.worker.fetch(new Request('https://worker.test/',{headers:{Upgrade:'websocket'}}),{UUID:uuid,PROXYIP:'1.1.1.1'},ctx);
const bad=frame.slice();bad[1]^=255;servers.at(-1).emit(bad);
await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(calls.length,before);
// An opened connection that returned no bytes may fail after later client
// fragments were queued. Preserve the complete prefix on the one retry.
await context.worker.fetch(new Request('https://worker.test/',{headers:{Upgrade:'websocket'}}),{UUID:uuid,PROXYIP:'9.9.9.9'},ctx);
const retryServer=servers.at(-1);
retryServer.emit(packet('no-response.example',[22]));
await new Promise(resolve=>setTimeout(resolve,10));
const emptyUpstream=sockets.at(-1);
retryServer.emit(new Uint8Array([3,1]));
await new Promise(resolve=>setTimeout(resolve,10));
emptyUpstream.reader().close();
await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(calls.at(-1).hostname,'9.9.9.9');
assert.deepEqual(sockets.at(-1).writes,[[22,3,1]]);
sockets.at(-1).reader().enqueue(new Uint8Array([22]));
await new Promise(resolve=>setTimeout(resolve,10));
const countAfterResponse=calls.length;
sockets.at(-1).reader().close();
await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(calls.length,countAfterResponse); // Never replay after a response.
// DNS UDP is length framed across WebSocket messages. It uses DoH and must
// not create an unauthenticated or arbitrary-port TCP connection.
await context.worker.fetch(new Request('https://worker.test/',{headers:{Upgrade:'websocket'}}),{UUID:uuid,PROXYIP:'1.1.1.1'},ctx);
const dnsServer=servers.at(-1);
dnsServer.emit(packet('dns.example',[0,3,1],2,53));
dnsServer.emit(new Uint8Array([2,3,0,1,4]));
await new Promise(resolve=>setTimeout(resolve,10));
assert.deepEqual(dnsRequests,[[1,2,3],[4]]);
assert.deepEqual(dnsServer.sent,[[0,0,0,2,18,52],[0,2,18,52]]);
assert.equal(calls.length,countAfterResponse);
for(const server of servers) server.listeners.close();
await Promise.allSettled(jobs);
console.log('VLESS relay tests passed: fragmented authentication/DNS, isolated targets, buffered retry, response framing, no replay after response and no unauthenticated dial.');
