import { connect } from 'cloudflare:sockets';

// Each WebSocket owns its authentication, queues, socket and fallback target.
const LIMIT = 16 * 1024 * 1024;
export function parseHeader(bytes, uuid) {
  if (bytes.length < 18) return null;
  const expected = uuid.replaceAll('-', '').toLowerCase();
  const actual = Array.from(bytes.subarray(1,17), v => v.toString(16).padStart(2,'0')).join('');
  if (actual !== expected || bytes[0] !== 0) throw new Error('Authentication rejected');
  let offset = 18 + bytes[17];
  if (bytes.length < offset + 4) return null;
  const command = bytes[offset++];
  const port = (bytes[offset++] << 8) | bytes[offset++];
  const type = bytes[offset++];
  let host;
  if (type === 1) {
    if (bytes.length < offset + 4) return null;
    host = Array.from(bytes.subarray(offset,offset+4)).join('.');
    offset += 4;
  } else if (type === 2) {
    if (bytes.length < offset + 1) return null;
    const length = bytes[offset++];
    if (!length) throw new Error('Invalid hostname');
    if (bytes.length < offset + length) return null;
    host = new TextDecoder('utf-8',{fatal:true}).decode(bytes.subarray(offset,offset+length));
    offset += length;
  } else if (type === 3) {
    if (bytes.length < offset + 16) return null;
    const parts = [];
    for (let i=0;i<8;i++) parts.push(((bytes[offset+2*i]<<8)|bytes[offset+2*i+1]).toString(16));
    host = '['+parts.join(':')+']';
    offset += 16;
  } else throw new Error('Invalid address type');
  if (!port || (command !== 1 && !(command === 2 && port === 53))) throw new Error('Unsupported command');
  return {host,port,command,offset};
}

function concat(a,b) {
  const result = new Uint8Array(a.length+b.length);
  result.set(a); result.set(b,a.length); return result;
}

export default {
  async fetch(request,env,ctx) {
    const uuid = String(env.UUID || '').toLowerCase();
    if (!/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(uuid)) return new Response('Unavailable',{status:503});
    const url = new URL(request.url);
    if(url.pathname === '/version' && url.searchParams.get('uuid') === uuid) return Response.json({Version:20260611041617});
    if(request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') return new Response('Not found',{status:404});
    const proxyIP = url.searchParams.get('proxyip') || env.PROXYIP;
    if(!proxyIP || !/^(\d{1,3}\.){3}\d{1,3}$/.test(proxyIP) || proxyIP.split('.').some(x=>Number(x)>255)) return new Response('Unavailable',{status:503});
    const [client,server] = Object.values(new WebSocketPair());
    server.accept();
    server.binaryType = 'arraybuffer';
    let socket, writer, header, responseStarted=false, stopped=false, replay=null;
    let pending = new Uint8Array(0), dnsPending = new Uint8Array(0), queued=0;
    let chain = Promise.resolve();
    const deadline = setTimeout(()=>close(1008),15000);
    function close(code=1000) {
      if(stopped) return;
      stopped=true; clearTimeout(deadline);
      try { writer?.releaseLock(); } catch {}
      try { socket?.close(); } catch {}
      try { server.close(code); } catch {}
    }
    function send(chunk) {
      if(stopped || server.readyState !== 1) return;
      if(!responseStarted) { chunk=concat(new Uint8Array([0,0]),chunk); responseStarted=true; }
      server.send(chunk);
    }
    async function dial(host,port,initial) {
      const next = connect({hostname:host,port});
      next.closed.catch(()=>{});
      let timer;
      try {
        await Promise.race([next.opened,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Connect timeout')),5000);})]);
        if(stopped) throw new Error('Closed');
        const output=next.writable.getWriter();
        try { if(initial.length) await output.write(initial); }
        catch(error) { output.releaseLock(); throw error; }
        socket=next; writer=output;
        return next;
      } catch(error) { try{next.close();}catch{} throw error; }
      finally { clearTimeout(timer); }
    }
    async function downstream(initialSocket,initial,canRetry) {
      let incoming=false;
      const reader=initialSocket.readable.getReader();
      try {
        while(!stopped) {
          const {done,value}=await reader.read();
          if(done) break;
          if(value.length) {incoming=true;replay=null;send(value);}
        }
      } catch {} finally {reader.releaseLock();}
      if(stopped) return;
      if(!incoming && canRetry && replay !== null) {
        // Only retry a connection that produced no response; never replay after data.
        // Serialize fallback with all incoming writes to preserve socket ownership.
        const retry = async()=>{
          try{writer?.releaseLock();}catch{}
          try{initialSocket.close();}catch{}
          const buffered=replay;
          replay=null;
          const next=await dial(proxyIP,443,buffered);
          ctx.waitUntil(downstream(next,new Uint8Array(0),false));
        };
        chain=chain.then(retry).catch(()=>close(1011));
      } else close();
    }
    async function writeDNS(bytes) {
      dnsPending=concat(dnsPending,bytes);
      while(dnsPending.length>=2) {
        const length=(dnsPending[0]<<8)|dnsPending[1];
        if(!length) throw new Error('Invalid DNS packet');
        if(dnsPending.length<length+2) return;
        const packet=dnsPending.slice(2,length+2);
        dnsPending=dnsPending.slice(length+2);
        const result=await fetch('https://cloudflare-dns.com/dns-query',{
          method:'POST',headers:{'Content-Type':'application/dns-message'},body:packet,
          signal:AbortSignal.timeout(10000)
        });
        if(!result.ok) throw new Error('DNS request failed');
        const answer=new Uint8Array(await result.arrayBuffer());
        if(answer.length>65535) throw new Error('DNS response too large');
        send(concat(new Uint8Array([answer.length>>8,answer.length&255]),answer));
      }
    }
    async function consume(bytes) {
      if(stopped) return;
      if(!header) {
        pending=concat(pending,bytes);
        if(pending.length>65536) throw new Error('Header too large');
        header=parseHeader(pending,uuid);
        if(!header) return;
        clearTimeout(deadline);
        const initial=pending.slice(header.offset);
        pending=new Uint8Array(0);
        if(header.command===2) return writeDNS(initial);
        let next, retry=true;
        try { next=await dial(header.host,header.port,initial); replay=initial; }
        catch(error) {
          if(header.port!==443) throw error;
          next=await dial(proxyIP,443,initial); retry=false;
        }
        ctx.waitUntil(downstream(next,initial,retry && header.port===443));
      } else if(header.command===2) await writeDNS(bytes);
      else {
        if(replay !== null) {
          replay=concat(replay,bytes);
          if(replay.length>1024*1024) replay=null;
        }
        await writer.write(bytes);
      }
    }
    function enqueue(data) {
      if(stopped) return;
      if(!(data instanceof ArrayBuffer) && !ArrayBuffer.isView(data)) return close(1003);
      const bytes=data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer,data.byteOffset,data.byteLength);
      queued+=bytes.length;
      if(queued>LIMIT) return close(1009);
      chain=chain.then(()=>consume(bytes)).catch(()=>close(1011)).finally(()=>{queued-=bytes.length;});
    }
    server.addEventListener('message',event=>enqueue(event.data));
    server.addEventListener('close',()=>close());
    server.addEventListener('error',()=>close(1011));
    const early=request.headers.get('sec-websocket-protocol');
    if(early) {
      try {
        if(early.length>11000) throw new Error('Early data too large');
        const decoded=atob(early.replaceAll('-','+').replaceAll('_','/'));
        enqueue(Uint8Array.from(decoded,x=>x.charCodeAt(0)));
      } catch {close(1008);}
    }
    return new Response(null,{status:101,webSocket:client});
  }
};
