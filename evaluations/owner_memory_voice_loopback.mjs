/* Loopback adapter for the evaluation browser. Only minted, one-use Realtime upgrades can pass. */
import http from "node:http";
import https from "node:https";
import {once} from "node:events";
import {createInterface} from "node:readline";

const lines = createInterface({input:process.stdin});
const [line] = await once(lines, "line");
const root = new URL(JSON.parse(line).origin);
const tickets = new Set();
const server = http.createServer(async (req,res) => {
  if (req.method !== "POST" || req.url !== "/v1/browser-tickets" || Number(req.headers["content-length"]) > 100) {
    res.writeHead(404).end(); return;
  }
  try {
    let body = "";
    for await (const chunk of req) { body += chunk; if (body.length > 100) throw Error(); }
    if (JSON.parse(body).path !== "/v1/realtime" || !req.headers.authorization?.startsWith("Bearer ")) throw Error();
    const reply = await fetch(new URL("/v1/browser-tickets", root), {method:"POST", redirect:"error",
      headers:{"Authorization":req.headers.authorization,"Content-Type":"application/json"},
      body:JSON.stringify({path:"/v1/realtime"}), signal:AbortSignal.timeout(8000)});
    if (!reply.ok) {res.writeHead(reply.status).end(); return;}
    const {ticket} = await reply.json();
    if (!/^[A-Za-z0-9_-]{20,128}$/.test(ticket)) throw Error();
    tickets.add(ticket);
    setTimeout(() => tickets.delete(ticket), 30000).unref();
    res.writeHead(200,{"Content-Type":"application/json"}).end(JSON.stringify({ticket}));
  } catch { res.writeHead(502).end(); }
});
server.on("upgrade",(req,down,head) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (req.method !== "GET" || url.pathname !== "/v1/realtime" || !tickets.delete(url.searchParams.get("ticket")) ||
      [...url.searchParams.keys()].some(k=>!["ticket","duplex","autostart"].includes(k))) { down.destroy(); return; }
  const remote = new URL("/v1/realtime", root);
  remote.search = url.search;
  const upstream = (root.protocol === "https:" ? https : http).request(remote, {headers:{
    Connection:"Upgrade", Upgrade:"websocket", "Sec-WebSocket-Key":req.headers["sec-websocket-key"],
    "Sec-WebSocket-Version":"13"}});
  upstream.on("upgrade",(reply,up,extra) => {
    down.write("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: " +
      reply.headers["sec-websocket-accept"] + "\r\n\r\n");
    if (extra.length) down.write(extra);
    if (head.length) up.write(head);
    up.on("error",()=>down.destroy()); down.on("error",()=>up.destroy());
    up.on("close",()=>down.destroy()); down.on("close",()=>up.destroy());
    up.pipe(down).pipe(up);
  });
  upstream.on("response",reply=>{reply.resume();down.destroy();});
  upstream.on("error",()=>down.destroy());
  down.on("error",()=>upstream.destroy());
  upstream.end();
});
server.listen(0,"127.0.0.1",()=>console.log(server.address().port));
