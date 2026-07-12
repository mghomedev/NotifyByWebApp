"""Web UI for NotifyByWebApp: landing/generator page, the installable app
page, service worker, SVG icon and robots.txt — all served inline from the
serverless function (no static-file bundling surprises).

Key idea (see CLAUDE.md): the app URL carries the channel codes in the URL
FRAGMENT (`/a#codes=...`) so they never reach the server or its logs. The
app page injects a data:-URI web app manifest whose start_url includes the
fragment, so "Add to Home Screen" keeps the codes. localStorage is a second
layer, and in-app "Add channel" is the final fallback (relevant on iOS,
where the installed app has its own storage).
"""
import re

CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "manifest-src 'self' data:; "
    "worker-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

ROBOTS_TXT = "User-agent: *\nDisallow: /a\nDisallow: /api/\n"

# Shown at the bottom of both the landing page and the app page. Injected via
# the __DISCLAIMER__ placeholder so the two pages never drift apart.
DISCLAIMER_HTML = """<div class="disclaimer">
<p><strong>Free &amp; open-source.</strong> This is free, open-source software
(MIT-licensed) &mdash; anyone can read, fork and <strong>host their own copy</strong>
from the public <a href="https://github.com/mghomedev/NotifyByWebApp" rel="noopener">GitHub
repository</a>. It is a non-commercial hobby project; there is no company, paid service
or support behind it.</p>
<p><strong>No warranty &mdash; use entirely at your own risk.</strong> Provided
<strong>&ldquo;AS IS&rdquo; and &ldquo;AS AVAILABLE&rdquo;</strong>, without warranty
or condition of any kind, whether express, implied or statutory, including (without
limitation) any implied warranties of merchantability, fitness for a particular
purpose, reliability, accuracy, security or availability. Message delivery is
<strong>not guaranteed</strong> and may be delayed, duplicated, lost or fail entirely,
and the service may change, break or shut down at any time without notice.</p>
<p><strong>Do not rely on this service for any urgent, critical, medical, financial,
safety-related or emergency notifications.</strong> To the maximum extent permitted
by applicable law, the author and operator shall not be liable for any direct,
indirect, incidental or consequential loss or damage whatsoever arising from the use
of, or inability to use, this service. By using it you accept these terms and take
full responsibility for keeping your channel codes secret.</p>
<p lang="de"><strong>Freie Open-Source-Software &ndash; Nutzung auf eigene Gefahr.</strong>
Quelloffenes, kostenloses, nicht-kommerzielles Hobby-Projekt, das jede Person einsehen,
kopieren und selbst betreiben kann. Bereitgestellt &bdquo;wie besehen&ldquo; ohne
jegliche Garantie f&uuml;r Verf&uuml;gbarkeit, Zuverl&auml;ssigkeit, Sicherheit oder
die Zustellung von Nachrichten. Nicht f&uuml;r dringende, kritische, medizinische,
finanzielle oder sicherheitsrelevante Benachrichtigungen verwenden. Eine Haftung
f&uuml;r Sch&auml;den ist &ndash; soweit gesetzlich zul&auml;ssig &ndash;
ausgeschlossen.</p>
</div>"""

# Compatibility list (shared on both pages via the __COMPAT__ placeholder).
# Minimum versions verified 2026 — see CLAUDE.md "Web Push facts".
COMPAT_HTML = """<details class="compat">
<summary>Supported devices &amp; minimum versions</summary>
<p class="muted">Notifications use the browser's built-in Web Push. Minimum versions:</p>
<div class="compat-scroll"><table class="compat-table">
<tr><th>iPhone</th><td>iOS <strong>16.4</strong>+ (2023) &mdash; must be added to the Home Screen</td></tr>
<tr><th>iPad</th><td>iPadOS <strong>16.4</strong>+ (2023) &mdash; must be added to the Home Screen</td></tr>
<tr><th>Mac</th><td>Safari <strong>16.1</strong>+ on macOS 13 Ventura or newer, or Chrome / Firefox / Edge</td></tr>
<tr><th>Android</th><td>Chrome, Firefox, Edge, Opera or Samsung Internet (Android <strong>10</strong>+ recommended)</td></tr>
<tr><th>Windows / Linux</th><td>Chrome <strong>52</strong>+, Firefox <strong>44</strong>+, Edge <strong>17</strong>+, Opera <strong>42</strong>+</td></tr>
</table></div>
<p class="muted">On iPhone and iPad you must open this app from its Home Screen icon &mdash;
web push does not work in a Safari browser tab.</p>
</details>"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#6366F1"/><stop offset="1" stop-color="#4338CA"/>
</linearGradient></defs>
<rect width="512" height="512" rx="115" fill="url(#g)"/>
<circle cx="256" cy="124" r="18" fill="#fff"/>
<path d="M256 140c-66 0-104 46-110 106l-9 84h238l-9-84c-6-60-44-106-110-106z" fill="#fff"/>
<rect x="122" y="330" width="268" height="28" rx="14" fill="#fff"/>
<circle cx="256" cy="390" r="24" fill="#fff"/>
</svg>
"""

_STYLE = """
:root{color-scheme:light dark;--bg:#f4f5fa;--card:#ffffff;--text:#171722;
--muted:#6b7280;--accent:#4f46e5;--accent-press:#4338ca;--border:#e3e4ee;
--danger:#dc2626;--ok:#059669}
@media(prefers-color-scheme:dark){:root{--bg:#0f1015;--card:#1a1b23;
--text:#eceef4;--muted:#9aa1ad;--border:#2b2d3a;--accent:#6366f1}}
*{box-sizing:border-box}
/* author rules like button{display:inline-block} otherwise beat the UA
   [hidden] rule, leaving elements toggled via .hidden still visible */
[hidden]{display:none!important}
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.5;-webkit-text-size-adjust:100%}
.wrap{max-width:640px;margin:0 auto;padding:12px 16px calc(48px + env(safe-area-inset-bottom))}
header{display:flex;align-items:center;gap:12px;padding:14px 0 4px}
header img{width:42px;height:42px}
h1{font-size:1.35rem;margin:0}
h2{font-size:1.05rem;margin:0 0 6px}
.muted{color:var(--muted);font-size:.9rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;
padding:16px;margin:14px 0;overflow-wrap:anywhere}
input,textarea{width:100%;padding:10px 12px;margin:6px 0;border:1px solid var(--border);
border-radius:10px;background:var(--bg);color:var(--text);font:inherit}
label{display:block;font-size:.8rem;color:var(--muted);margin:10px 0 2px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85em;
background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:1px 5px}
.checkline{display:flex;gap:8px;align-items:flex-start;font-size:.95rem;
color:var(--text);margin:4px 0}
.checkline input{width:auto;margin:2px 0 0}
.warn{border-left:3px solid #f59e0b;padding-left:10px}
.apilist{padding-left:18px;line-height:1.75}
button,.btn{display:inline-block;padding:10px 16px;margin:6px 6px 0 0;border:0;
border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:600;
cursor:pointer;text-decoration:none}
button:active,.btn:active{background:var(--accent-press)}
button[disabled]{opacity:.6}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--border)}
button.danger{background:transparent;color:var(--danger);border:1px solid var(--border)}
.code-pill{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9rem;
background:var(--bg);border:1px dashed var(--border);border-radius:10px;
padding:10px 12px;margin:8px 0;word-break:break-all;user-select:all}
.banner{background:var(--accent);color:#fff;border-radius:14px;padding:12px 16px;margin:14px 0}
#qr{margin:12px 0;background:#fff;border-radius:10px;padding:8px;display:inline-block}
#qr svg{display:block;width:200px;height:200px}
.share{margin:12px 0;padding:14px;border:1px solid var(--border);border-radius:12px;text-align:center}
.share-label{margin-bottom:8px;text-align:center}
.share-app{font-weight:700}
.share-channel{font-size:.88rem;color:var(--muted)}
.qrshare{background:#fff;border-radius:10px;padding:8px;display:inline-block}
.qrshare svg{display:block;width:168px;height:168px}
.share-url{font-size:.76rem;text-align:left;margin:10px 0 0}
.msgs{margin-top:8px}
.msg{border-top:1px solid var(--border);padding:10px 30px 10px 0;position:relative}
.msg:first-child{border-top:0}
.msgs-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}
.iconbtn{background:transparent;border:0;cursor:pointer;font-size:1rem;line-height:1;
padding:2px 6px;margin:0;color:var(--muted)}
.iconbtn:hover{color:var(--danger)}
.msg-del{position:absolute;top:8px;right:-4px}
.more-msgs{margin-top:4px}
.more-msgs>summary{margin:8px 0 2px}
.msg-title{font-weight:600}
.msg-body{white-space:pre-wrap}
.msg-time{color:var(--muted);font-size:.78rem;margin-bottom:3px}
.msg-rel{opacity:.85}
.msgs-hint{font-size:.72rem;color:var(--muted);text-align:right;margin-bottom:2px}
.msg-link{margin-top:3px;font-size:.85rem}
.channel-latest{font-size:.72rem;color:var(--muted);margin-top:2px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.codes-item{display:flex;align-items:center;gap:8px;margin:6px 0}
.codes-item span{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all;flex:1}
details{margin-top:8px}
summary{cursor:pointer;color:var(--accent);font-weight:600}
pre{background:var(--bg);border:1px solid var(--border);border-radius:10px;
padding:12px;overflow-x:auto;font-size:.8rem}
a{color:var(--accent)}
footer{margin-top:22px;text-align:center}
.status-ok{color:var(--ok);font-weight:600}
.err{color:var(--danger);font-size:.9rem;min-height:1.2em}
.disclaimer{margin-top:12px;padding-top:12px;border-top:1px solid var(--border);
font-size:.72rem;line-height:1.55;color:var(--muted)}
.disclaimer p{margin:0 0 8px}
.disclaimer p:last-child{margin-bottom:0}
.disclaimer strong{color:var(--text)}
.disclaimer a{color:var(--accent)}
.compat{margin:14px 0}
.compat>summary{cursor:pointer;color:var(--accent);font-weight:600}
.compat-scroll{overflow-x:auto}
.compat-table{width:100%;border-collapse:collapse;font-size:.85rem;margin:8px 0}
.compat-table th{text-align:left;white-space:nowrap;color:var(--text);vertical-align:top}
.compat-table td{color:var(--muted)}
.compat-table th,.compat-table td{padding:7px 12px 7px 0;border-top:1px solid var(--border)}
.compat-table tr:first-child th,.compat-table tr:first-child td{border-top:0}
.warn-banner{background:#b45309}
"""

_HEAD_COMMON = (
    """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#4f46e5">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Notify">
<style>"""
    + _STYLE
    + "</style>\n"
)

# --------------------------------------------------------------- landing

INDEX_HTML = (
    """<!doctype html>
<html lang="en">
<head>
"""
    + _HEAD_COMMON
    + """<title>Notify by Web App</title>
</head>
<body>
<div class="wrap">
<header><img src="/icon.svg" alt=""><h1>Notify <span class="muted">by Web App</span></h1></header>
<p>Push notifications on your phone for anything — no app store, no account.
A <strong>channel</strong> is identified by a secret code: anyone with the code can
send and receive its messages.</p>

<div class="card">
<h2>1. Create a channel</h2>
<p class="muted">You get a secret channel code. Save it — it cannot be recovered.</p>
<input id="channel-name" maxlength="80" placeholder="Channel name (optional)" autocomplete="off">
<input id="channel-password" maxlength="128" placeholder="Send password (optional; only holders can send)" autocomplete="off">
<button id="create-btn">Create channel</button>
<p class="err" id="create-error"></p>
<div id="create-result" hidden>
<p>Your new channel code:</p>
<div class="code-pill" id="new-code"></div>
<button class="ghost" data-copy="#new-code">Copy code</button>
<p class="muted">It was added to your app link below.</p>
<p class="muted" id="create-protected" hidden>&#128274; Sending to this channel requires the send password you set. Anyone with the code can still receive.</p>
</div>
</div>

<div class="card">
<h2>2. Build your app link</h2>
<p class="muted">Add one or more channel codes. You get a link that opens the
notification app already set up for those channels.
On your phone: open the link (or scan the QR code), then use
<strong>Add to Home Screen</strong> to install it.</p>
<div id="code-list"></div>
<input id="code-input" placeholder="Paste a channel code" autocomplete="off">
<button id="add-code">Add code</button>
<p class="err" id="add-error"></p>
<div id="link-result" hidden>
<div class="code-pill" id="app-url"></div>
<div class="row">
<button class="ghost" data-copy="#app-url">Copy link</button>
<a id="open-app" class="btn">Open app</a>
</div>
<div class="share-label"><div class="share-app">Join NotifyByWebApp</div></div>
<div id="qr"></div>
<p class="muted">Scan with your phone camera &rarr; opens the app &rarr; Add to Home Screen.</p>
</div>
</div>

<div class="card">
<h2>3. Send a message</h2>
<p class="muted">Send to a channel's subscribers right now &mdash; anyone who has the
channel code can send.</p>
<label for="send-code">Channel code</label>
<input id="send-code" list="send-code-list" placeholder="Paste or pick a channel code" autocomplete="off">
<datalist id="send-code-list"></datalist>
<input id="send-title" maxlength="120" placeholder="Title (optional)" autocomplete="off">
<textarea id="send-body" maxlength="2000" rows="3" placeholder="Message text (optional if a title is given)"></textarea>
<input id="send-url" maxlength="500" placeholder="Link https://… (optional)" autocomplete="off">
<input id="send-password" maxlength="128" placeholder="Send password (only if the channel requires one)" autocomplete="off">
<button id="send-btn">Send message</button>
<p class="err" id="send-error"></p>
<p class="status-ok" id="send-ok" hidden></p>
</div>

<div class="card">
<h2>4. Send from your own code</h2>
<p class="muted">Any script or service that can POST JSON can send to a channel &mdash;
the channel code is the only credential, no SDK or login needed.</p>
<pre>curl -X POST <span id="curl-host"></span>/api/message \\
  -H "Content-Type: application/json" \\
  -d '{"code":"<span id="curl-code">YOUR_CHANNEL_CODE</span>","title":"Hello","body":"World"}'</pre>
<details>
<summary>All endpoints</summary>
<p class="muted">All are <code>POST</code> with a JSON body; the channel code goes in
the body, never the URL.</p>
<ul class="muted apilist">
<li><code>/api/message</code> &mdash; send (title &le;120, body &le;2000, optional http(s) url &le;500)</li>
<li><code>/api/messages</code> &mdash; recent messages + subscriber count</li>
<li><code>/api/channel</code> &mdash; create a channel (optional name)</li>
<li><code>/api/subscribe</code> / <code>/api/unsubscribe</code> &mdash; register a device (used by the app)</li>
</ul>
</details>
</div>

<div class="card">
<h2>Remember my channels on this device</h2>
<label class="checkline"><input type="checkbox" id="save-consent">
Save my channel codes so they reappear when I come back</label>
<p class="muted warn">&#9888; If you tick this and press <strong>Save</strong>, a
<strong>cookie</strong> is stored in this browser holding your channel codes, and it
is sent to this site on future visits so your channels reappear. Channel codes are
secrets (anyone with a code can send and read) &mdash; only do this on a device you
trust.</p>
<div class="row">
<button id="save-btn">Save channels</button>
<button class="ghost" id="forget-btn">Forget saved channels</button>
</div>
<p class="muted" id="save-status"></p>
</div>

<div class="card">__COMPAT__</div>

<footer class="muted">Free &amp; open-source hobby project &middot;
<a href="https://github.com/mghomedev/NotifyByWebApp" rel="noopener">Source on GitHub</a></footer>
__DISCLAIMER__
</div>
<script src="/vendor/qrcode.js"></script>
<script>
(function(){
'use strict';
var CODE_RE=/^[A-Za-z0-9_-]{16,64}$/;
var codes=[];
function $(s){return document.querySelector(s)}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;
if(text!==undefined)e.textContent=text;return e}
function api(path,payload){
return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(payload)}).then(function(r){
return r.json().catch(function(){return{}}).then(function(j){
if(!r.ok){var e=new Error((j&&j.error)||('HTTP '+r.status));e.status=r.status;throw e}
return j})})}
function renderCodes(){
var list=$('#code-list');list.textContent='';
codes.forEach(function(c){
var item=el('div','codes-item');
item.appendChild(el('span','',c));
var rm=el('button','danger','Remove');
rm.addEventListener('click',function(){
codes=codes.filter(function(x){return x!==c});renderCodes();updateLink();persistIfConsented()});
item.appendChild(rm);list.appendChild(item)});
updateSendUI()}
function updateLink(){
var res=$('#link-result');
if(!codes.length){res.hidden=true;return}
res.hidden=false;
var url=location.origin+'/a#codes='+codes.map(encodeURIComponent).join(',');
$('#app-url').textContent=url;
$('#open-app').setAttribute('href',url);
var qr=qrcode(0,'M');qr.addData(url);qr.make();
$('#qr').innerHTML=qr.createSvgTag({cellSize:4,margin:2,scalable:true})}
function addCode(c){
c=(c||'').trim();
if(!CODE_RE.test(c)){
$('#add-error').textContent='That does not look like a channel code (16-64 letters, digits, - or _).';
return false}
$('#add-error').textContent='';
if(codes.indexOf(c)<0)codes.push(c);
renderCodes();updateLink();persistIfConsented();return true}
$('#add-code').addEventListener('click',function(){
if(addCode($('#code-input').value))$('#code-input').value=''});
$('#code-input').addEventListener('keydown',function(e){
if(e.key==='Enter'&&addCode($('#code-input').value))$('#code-input').value=''});
$('#create-btn').addEventListener('click',function(){
var btn=$('#create-btn');btn.disabled=true;
$('#create-error').textContent='';
api('/api/channel',{name:$('#channel-name').value,send_password:$('#channel-password').value}).then(function(j){
$('#new-code').textContent=j.code;
$('#create-result').hidden=false;
$('#create-protected').hidden=!j.send_protected;
addCode(j.code);$('#send-code').value=j.code;
$('#send-password').value=$('#channel-password').value;updateCurlCode()}).catch(function(e){
$('#create-error').textContent='Could not create channel: '+e.message}).then(function(){
btn.disabled=false})});
document.addEventListener('click',function(e){
var t=e.target&&e.target.closest?e.target.closest('button[data-copy]'):null;
if(!t)return;
var src=document.querySelector(t.getAttribute('data-copy'));
if(!src||!navigator.clipboard)return;
navigator.clipboard.writeText(src.textContent).then(function(){
var old=t.textContent;t.textContent='Copied!';
setTimeout(function(){t.textContent=old},1200)})});
// ---- send a message from the landing page
function updateCurlCode(){
var c=$('#send-code').value.trim();
$('#curl-code').textContent=CODE_RE.test(c)?c:'YOUR_CHANNEL_CODE'}
function updateSendUI(){
var dl=$('#send-code-list');dl.textContent='';
codes.forEach(function(c){var o=document.createElement('option');o.value=c;dl.appendChild(o)});
var sc=$('#send-code');
if(!sc.value&&codes.length)sc.value=codes[codes.length-1];
updateCurlCode()}
$('#send-code').addEventListener('input',updateCurlCode);
$('#send-btn').addEventListener('click',function(){
var code=$('#send-code').value.trim(),title=$('#send-title').value.trim();
var err=$('#send-error'),ok=$('#send-ok');err.textContent='';ok.hidden=true;
if(!CODE_RE.test(code)){err.textContent='Enter a valid channel code (create one above, or paste it).';return}
if(!title&&!$('#send-body').value.trim()){err.textContent='Enter a title or a message.';return}
var btn=this;btn.disabled=true;
api('/api/message',{code:code,title:title,body:$('#send-body').value,url:$('#send-url').value,send_password:$('#send-password').value})
.then(function(j){
$('#send-title').value='';$('#send-body').value='';$('#send-url').value='';
var m;
if(j.push_disabled)m='Stored. Push is not configured on this server.';
else if(j.sent>0)m='Sent to '+j.sent+' device(s).';
else m='Message stored, but no device is subscribed to this channel yet. Install the app on a phone and enable notifications to receive it.';
ok.textContent=m;ok.hidden=false})
.catch(function(e){
if(e&&e.status===403)err.textContent='This channel requires a valid send password.';
else err.textContent='Could not send: '+(e.message||'error')})
.then(function(){btn.disabled=false})});
// ---- optional: remember channels on this device (opt-in). Persisted to BOTH a
// cookie AND localStorage for durability (browsers cap JS cookies — Safari ~7 days),
// merged + self-healed on every load. Never cleared implicitly; only via Remove /
// Forget / the user clearing site data. See CLAUDE.md persistence requirement.
var LS_SAVED='nbw_saved_codes';
function setCookie(n,v,days){
var d=new Date();d.setTime(d.getTime()+days*864e5);
var sec=location.protocol==='https:'?';Secure':'';
document.cookie=n+'='+encodeURIComponent(v)+';expires='+d.toUTCString()+';path=/;SameSite=Lax'+sec}
function getCookie(n){
var m=document.cookie.match(new RegExp('(?:^|; )'+n+'=([^;]*)'));
return m?decodeURIComponent(m[1]):null}
function delCookie(n){
document.cookie=n+'=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/;SameSite=Lax'}
function lsSaveArr(k,a){try{localStorage.setItem(k,JSON.stringify(a))}catch(e){}}
function lsLoadArr(k){try{var v=JSON.parse(localStorage.getItem(k));
return Array.isArray(v)?v:[]}catch(e){return[]}}
function persistSaved(){setCookie('nbw_codes',codes.join(','),365);lsSaveArr(LS_SAVED,codes)}
function persistIfConsented(){var c=$('#save-consent');if(c&&c.checked)persistSaved()}
$('#save-btn').addEventListener('click',function(){
var st=$('#save-status');
if(!$('#save-consent').checked){
st.textContent='Tick the box first \\u2014 that is your consent to store on this device.';return}
if(!codes.length){st.textContent='Create or add at least one channel first.';return}
persistSaved();
st.textContent='Saved '+codes.length+' channel(s) on this device (cookie + local storage).'});
$('#forget-btn').addEventListener('click',function(){
delCookie('nbw_codes');try{localStorage.removeItem(LS_SAVED)}catch(e){}
$('#save-consent').checked=false;
$('#save-status').textContent='Saved channels cleared from this device.'});
(function loadSaved(){
var merged=[];
(getCookie('nbw_codes')||'').split(',').concat(lsLoadArr(LS_SAVED)).forEach(function(c){
c=(c||'').trim();if(CODE_RE.test(c)&&merged.indexOf(c)<0)merged.push(c)});
if(!merged.length)return;
var added=false;
merged.forEach(function(c){if(codes.indexOf(c)<0){codes.push(c);added=true}});
persistSaved();  // heal: rewrite BOTH stores (restore a dropped one, refresh cookie window)
if(added){renderCodes();updateLink()}
$('#save-consent').checked=true;
$('#save-status').textContent='Loaded '+codes.length+' saved channel(s) from this device.'})();
$('#curl-host').textContent=location.origin;
updateSendUI();
})();
</script>
</body>
</html>
"""
)

# -------------------------------------------------------------- app page

_APP_HTML_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
"""
    + _HEAD_COMMON
    + """<title>Notify</title>
</head>
<body>
<div class="wrap">
<header><img src="/icon.svg" alt=""><h1>Notify</h1></header>
<noscript><div class="banner">This app needs JavaScript.</div></noscript>
<div class="banner" id="ios-hint" hidden>
On iPhone/iPad, notifications only work for installed web apps:
open the <strong>Share</strong> menu, choose <strong>Add to Home Screen</strong>,
then open Notify from your Home Screen and enable notifications there.
</div>
<div class="banner warn-banner" id="too-old" hidden></div>
<div class="card" id="notif-card">
<h2>Notifications</h2>
<div id="notif-state" class="muted">Notifications are off.</div>
<button id="enable-btn">Enable notifications</button>
__COMPAT__
</div>
<div id="channels"></div>
<div class="card">
<h2>Add a channel</h2>
<input id="add-input" placeholder="Paste a channel code" autocomplete="off">
<button id="add-btn">Add channel</button>
<p class="err" id="add-error"></p>
</div>
<div class="card muted" id="empty-hint" hidden>
No channels yet — paste a channel code above, or create one on the
<a href="/">start page</a>.
</div>
<footer class="muted"><a href="/">Notify start page</a></footer>
__DISCLAIMER__
</div>
<script src="/vendor/qrcode.js"></script>
<script>
(function(){
'use strict';
var VAPID_PUBLIC_KEY='__VAPID_PUBLIC_KEY__';
var CODE_RE=/^[A-Za-z0-9_-]{16,64}$/;
var LS_CODES='nbw_codes',LS_REMOVED='nbw_removed',LS_SUB='nbw_subscribed',
LS_PENDING='nbw_pending_unsub',LS_MUTED='nbw_muted';
var codes=[];
var _lastSubBody=null;
function $(s){return document.querySelector(s)}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;
if(text!==undefined)e.textContent=text;return e}
function api(path,payload){
return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(payload)}).then(function(r){
return r.json().catch(function(){return{}}).then(function(j){
if(!r.ok){var e=new Error((j&&j.error)||('HTTP '+r.status));e.status=r.status;throw e}
return j})})}
function lsGet(k,d){try{var v=JSON.parse(localStorage.getItem(k));
return v==null?d:v}catch(e){return d}}
function lsSet(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}
// muted channels (per device): the endpoint is unsubscribed from the channel
// on the server, so silencing does not rely on dropping pushes in the SW
function isMuted(code){return lsGet(LS_MUTED,[]).indexOf(code)>=0}
function setMuted(code,on){var a=lsGet(LS_MUTED,[]);var i=a.indexOf(code);
if(on&&i<0)a.push(code);if(!on&&i>=0)a.splice(i,1);lsSet(LS_MUTED,a)}
function muteChannel(code){
setMuted(code,true);
if('serviceWorker' in navigator){
navigator.serviceWorker.ready.then(function(r){return r.pushManager.getSubscription()})
.then(function(s){if(s)return api('/api/unsubscribe',{code:code,endpoint:s.endpoint})})
.catch(function(){})}
mirrorStateForSW()}
function unmuteChannel(code){
setMuted(code,false);
if(lsGet(LS_SUB,false)&&'serviceWorker' in navigator){
navigator.serviceWorker.ready.then(function(r){return r.pushManager.getSubscription()})
.then(function(s){if(s)return api('/api/subscribe',{code:code,subscription:s.toJSON()})})
.catch(function(){})}
mirrorStateForSW()}

function parseFragmentCodes(){
var m=(location.hash||'').match(/codes=([^&]*)/);
if(!m)return[];
return m[1].split(',').map(function(x){
try{return decodeURIComponent(x)}catch(e){return''}
}).filter(function(c){return CODE_RE.test(c)})}

function loadCodes(){
var removedArr=lsGet(LS_REMOVED,[]);
var stored=lsGet(LS_CODES,[]).filter(function(c){return CODE_RE.test(c)});
parseFragmentCodes().forEach(function(c){
if(removedArr.indexOf(c)<0&&stored.indexOf(c)<0)stored.push(c)});
codes=stored;
lsSet(LS_CODES,codes)}

// ------- install identity: data:-URI manifest keeps codes in start_url
function hashStr(s){var h=5381,i;for(i=0;i<s.length;i++){h=((h<<5)+h+s.charCodeAt(i))>>>0}
return h.toString(36)}
function injectManifest(){
var codesStr=codes.map(encodeURIComponent).join(',');
var startUrl=location.origin+'/a'+(codes.length?'#codes='+codesStr:'');
var man={name:'Notify',short_name:'Notify',
description:'Push notifications for your channels',
id:location.origin+'/a?install='+hashStr(codesStr),
start_url:startUrl,scope:location.origin+'/',
display:'standalone',background_color:'#0f1015',theme_color:'#4f46e5',
icons:[
{src:location.origin+'/icon-192.png',sizes:'192x192',type:'image/png',purpose:'any maskable'},
{src:location.origin+'/icon-512.png',sizes:'512x512',type:'image/png',purpose:'any maskable'}]};
var link=document.getElementById('manifest-link');
if(!link){link=document.createElement('link');link.id='manifest-link';
link.rel='manifest';document.head.appendChild(link)}
link.href='data:application/manifest+json;charset=utf-8,'+
encodeURIComponent(JSON.stringify(man))}

// ------- notifications
function isIOS(){return /iP(hone|ad|od)/.test(navigator.userAgent)||
(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1)}
function isStandalone(){return navigator.standalone===true||
(window.matchMedia&&matchMedia('(display-mode: standalone)').matches)}
function pushSupported(){return 'serviceWorker' in navigator&&
'PushManager' in window&&'Notification' in window}
// --- device compatibility (feature detection is authoritative; UA parsing only
// picks the wording of a too-old warning). Verified minimums 2026.
function iosVer(){var m=navigator.userAgent.match(/(?:CPU iPhone OS|CPU OS) (\\d+)[_.](\\d+)/);
return m?[parseInt(m[1],10),parseInt(m[2],10)]:null}
function iosAtLeast(a,b){var v=iosVer();if(!v)return null;
return v[0]>a||(v[0]===a&&v[1]>=b)}
function isIPhoneUA(){return /iP(hone|od)/.test(navigator.userAgent)}
function isIPadDevice(){return /iPad/.test(navigator.userAgent)||
(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1)}
function pushStatus(){
if(pushSupported())return{supported:true};
if(isIPhoneUA()&&iosAtLeast(16,4)===false)return{supported:false,reason:'ios-too-old'};
if(isIOS()&&!isStandalone())return{supported:false,reason:'ios-needs-install'};
if(isIPadDevice())return{supported:false,reason:isStandalone()?'ipad-too-old':'ios-needs-install'};
if(/Android/.test(navigator.userAgent))return{supported:false,reason:'android-too-old'};
return{supported:false,reason:'browser-unsupported'}}
function applyCompat(){
var s=pushStatus(),b=$('#too-old');
if(s.supported||s.reason==='ios-needs-install'){b.hidden=true;return}
var m;
if(s.reason==='ios-too-old')m='\\u26A0 This iPhone is too old for notifications. Web Push needs iOS 16.4 or newer (2023). Please update iOS, or use a newer device.';
else if(s.reason==='ipad-too-old')m='\\u26A0 This iPad is too old for notifications. Web Push needs iPadOS 16.4 or newer (2023). Please update iPadOS, or use a newer device.';
else if(s.reason==='android-too-old')m='\\u26A0 Your Android browser cannot show notifications. Please update Chrome, Firefox, or Samsung Internet (Android 10 or newer recommended).';
else m='\\u26A0 This browser cannot show web push notifications. Use a recent Chrome, Firefox, Edge, or Safari 16.1+ (macOS Ventura or newer).';
b.textContent=m;b.hidden=false}
function urlB64ToU8(s){
var pad='='.repeat((4-s.length%4)%4);
var b=atob((s+pad).replace(/-/g,'+').replace(/_/g,'/'));
var a=new Uint8Array(b.length);
for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);
return a}
function updateNotifUI(state){
var t=$('#notif-state'),b=$('#enable-btn');
t.className='muted';b.hidden=true;
if(state==='on'){t.textContent='Notifications are ON for this device.';
t.className='status-ok'}
else if(state==='partial'){t.textContent='Notifications are ON (some channels could not be registered \\u2014 reopen to retry).';
t.className='status-ok'}
else if(state==='subfail'){t.textContent='Could not register with the server. Check your connection and tap again.';
t.className='err';b.hidden=false}
else if(state==='blocked'){t.textContent='Notifications are blocked for this site in your browser settings.'}
else if(state==='unsupported'){t.textContent='This browser does not support web push notifications.'}
else if(state==='ios-install'){t.textContent='Install this app to your Home Screen first (see banner above).'}
else if(state==='unconfigured'){t.textContent='Push is not configured on this server yet (missing VAPID keys).'}
else{t.textContent='Notifications are off.';b.hidden=false}}

// keep an offline mirror of {codes,key,subscription} in the Cache so the
// service worker can re-subscribe on 'pushsubscriptionchange' (it cannot
// read localStorage)
function mirrorStateForSW(subBody){
if(subBody)_lastSubBody=subBody;
try{
if(!('caches' in window))return Promise.resolve();
return caches.open('nbw-state').then(function(c){
return c.put('/__nbw_state',new Response(JSON.stringify(
{codes:codes,key:VAPID_PUBLIC_KEY,subscription:_lastSubBody,muted:lsGet(LS_MUTED,[])}),
{headers:{'Content-Type':'application/json'}}))}).catch(function(){})
}catch(e){return Promise.resolve()}}

function subKeyMatches(sub){
try{
var existing=sub.options&&sub.options.applicationServerKey;
if(!existing)return true;
var a=new Uint8Array(existing),b=urlB64ToU8(VAPID_PUBLIC_KEY);
if(a.length!==b.length)return false;
for(var i=0;i<a.length;i++)if(a[i]!==b[i])return false;
return true}catch(e){return true}}

function freshSubscription(reg){
function sub(){return reg.pushManager.subscribe({userVisibleOnly:true,
applicationServerKey:urlB64ToU8(VAPID_PUBLIC_KEY)})}
return reg.pushManager.getSubscription().then(function(s){
if(s&&!subKeyMatches(s)){
// operator rotated the VAPID key: drop the stale endpoint and re-subscribe
return s.unsubscribe().then(sub,sub)}
return s||sub()})}

function ensureSubscribed(interactive){
if(!pushSupported()){
updateNotifUI(isIOS()&&!isStandalone()?'ios-install':'unsupported');
return Promise.resolve(false)}
if(Notification.permission==='denied'){updateNotifUI('blocked');return Promise.resolve(false)}
if(!VAPID_PUBLIC_KEY){updateNotifUI('unconfigured');return Promise.resolve(false)}
if(!codes.length){updateNotifUI('off');return Promise.resolve(false)}
var permP=Notification.permission==='granted'?Promise.resolve('granted'):
(interactive?Notification.requestPermission():Promise.resolve('default'));
return permP.then(function(p){
if(p!=='granted'){updateNotifUI(p==='denied'?'blocked':'off');return false}
return navigator.serviceWorker.ready.then(freshSubscription).then(function(sub){
var body=sub.toJSON();
// browser-level opt-in succeeded; remember intent so a transient server
// failure self-heals on next open, but never claim ON unless the server
// actually accepted at least one channel
lsSet(LS_SUB,true);
var active=codes.filter(function(c){return !isMuted(c)});  // skip muted channels
return Promise.all(active.map(function(c){
return api('/api/subscribe',{code:c,subscription:body}).then(
function(){return true},function(){return false})
})).then(function(oks){
mirrorStateForSW(body);
if(!active.length){updateNotifUI('on');return true}
var ok=oks.filter(Boolean).length;
updateNotifUI(ok===0?'subfail':(ok<active.length?'partial':'on'));
return ok>0})})})}

// ------- messages
function fmtTime(ts){
var dt=new Date(ts*1000);
var abs=dt.toLocaleString([],{year:'numeric',month:'short',day:'numeric',
hour:'2-digit',minute:'2-digit'});
var diff=Math.floor((Date.now()-dt.getTime())/1000);var rel;
if(diff<60)rel='just now';
else if(diff<3600)rel=Math.floor(diff/60)+' min ago';
else if(diff<86400)rel=Math.floor(diff/3600)+' h ago';
else rel=Math.floor(diff/86400)+' d ago';
return{abs:abs,rel:rel}}

// ------- channels UI
function channelCard(code){
var card=el('div','card channel');card.setAttribute('data-code',code);
card.appendChild(el('h2','','\\u2026'));
card.appendChild(el('div','muted stats',''));
card.appendChild(el('div','channel-latest',''));
card.appendChild(el('div','msgs'));
// always-visible shareable QR for this channel
var share=el('div','share');
var shareUrl=location.origin+'/a#codes='+encodeURIComponent(code);
var slabel=el('div','share-label');
slabel.appendChild(el('div','share-app','Join NotifyByWebApp'));
slabel.appendChild(el('div','share-channel','for Channel: \\u2026'));
share.appendChild(slabel);
var qrbox=el('div','qrshare');
try{var qr=qrcode(0,'M');qr.addData(shareUrl);qr.make();
qrbox.innerHTML=qr.createSvgTag({cellSize:3,margin:2,scalable:true})}catch(e){}
share.appendChild(qrbox);
share.appendChild(el('div','code-pill share-url',shareUrl));
var scopy=el('button','ghost','Copy share link');
scopy.addEventListener('click',function(){
if(navigator.clipboard)navigator.clipboard.writeText(shareUrl).then(function(){
scopy.textContent='Copied!';setTimeout(function(){scopy.textContent='Copy share link'},1200)})});
share.appendChild(scopy);
card.appendChild(share);
var d=document.createElement('details');
d.appendChild(el('summary','','Send a message'));
var ti=el('input');ti.placeholder='Title (optional)';ti.maxLength=120;
var bo=document.createElement('textarea');bo.placeholder='Message (optional if a title is given)';
bo.maxLength=2000;bo.rows=3;
var ur=el('input');ur.placeholder='Link https://\\u2026 (optional)';ur.maxLength=500;
var pw=el('input');pw.placeholder='Send password (required for this channel)';
pw.maxLength=128;pw.className='send-pw';pw.hidden=true;
var se=el('button','','Send');
var serr=el('div','muted');
se.addEventListener('click',function(){
if(!ti.value.trim()&&!bo.value.trim()){serr.textContent='Enter a title or a message.';return}
se.disabled=true;serr.textContent='';
api('/api/message',{code:code,title:ti.value,body:bo.value,url:ur.value,send_password:pw.value})
.then(function(j){
ti.value='';bo.value='';ur.value='';
serr.textContent='Sent to '+j.sent+' device(s).';
refreshChannel(code)})
.catch(function(e){
serr.textContent=(e&&e.status===403)?'This channel requires a valid send password.':('Error: '+e.message)})
.then(function(){se.disabled=false})});
d.appendChild(ti);d.appendChild(bo);d.appendChild(ur);d.appendChild(pw);d.appendChild(se);d.appendChild(serr);
card.appendChild(d);
var row=el('div','row');
var mute=el('button','ghost mute-btn','');
function paintMute(){var m=isMuted(code);
mute.textContent=m?'\\uD83D\\uDD15 Unmute':'\\uD83D\\uDD14 Mute';
mute.title=m?'Muted on this device \\u2014 tap to receive notifications again'
:'Silence notifications for this channel on this device'}
paintMute();
mute.addEventListener('click',function(){
if(isMuted(code))unmuteChannel(code);else muteChannel(code);paintMute()});
var cp=el('button','ghost','Copy code');
cp.addEventListener('click',function(){
if(navigator.clipboard)navigator.clipboard.writeText(code).then(function(){
cp.textContent='Copied!';setTimeout(function(){cp.textContent='Copy code'},1200)})});
var rf=el('button','ghost','Refresh');
rf.addEventListener('click',function(){refreshChannel(code)});
var rm=el('button','danger','Remove');
rm.addEventListener('click',function(){
if(confirm('Remove this channel from this device?'))removeChannel(code)});
row.appendChild(mute);row.appendChild(cp);row.appendChild(rf);row.appendChild(rm);
card.appendChild(row);
return card}

function renderChannels(){
var wrap=$('#channels');wrap.textContent='';
$('#empty-hint').hidden=codes.length>0;
codes.forEach(function(c){wrap.appendChild(channelCard(c));refreshChannel(c)})}

// order channel cards by latest event (newest message, else creation), top first
function sortChannels(){
var wrap=$('#channels');
var cards=Array.prototype.slice.call(wrap.querySelectorAll('.channel'));
cards.sort(function(a,b){
return (parseInt(b.getAttribute('data-ts'),10)||0)-(parseInt(a.getAttribute('data-ts'),10)||0)});
cards.forEach(function(c){wrap.appendChild(c)})}

// password to authorize a delete: use the card's send-password field, and for
// a protected channel with an empty field, prompt for it
function deletePw(card){
var f=card.querySelector('.send-pw');var v=f?f.value:'';
if(!v&&card.getAttribute('data-protected')==='1'){
v=prompt('This channel needs its send password to delete messages:')||''}
return v}

function refreshChannel(code){
var card=document.querySelector('.channel[data-code="'+code+'"]');
if(!card)return;
api('/api/messages',{code:code,limit:20}).then(function(j){
var cname=j.channel.name||'Unnamed channel';
card.querySelector('h2').textContent=cname;
var _sc=card.querySelector('.share-channel');
if(_sc)_sc.textContent='for Channel: '+cname;
var prot=!!(j.channel&&j.channel.send_protected);
card.setAttribute('data-protected',prot?'1':'0');
var _pw=card.querySelector('.send-pw');if(_pw)_pw.hidden=!prot;
var _sum=card.querySelector('details summary');
if(_sum)_sum.textContent=prot?'Send a message (password required)':'Send a message';
card.querySelector('.stats').textContent=j.subscribers+' subscribed device(s)';
var msgs=card.querySelector('.msgs');msgs.textContent='';
if(!j.messages.length){msgs.appendChild(el('div','muted','No messages yet.'))}
else{
var hdr=el('div','msgs-hdr');
hdr.appendChild(el('span','msgs-hint',j.messages.length>1?'Newest first':''));
var clr=el('button','iconbtn','\\uD83D\\uDDD1');
clr.title='Delete all messages';clr.setAttribute('aria-label','Delete all messages');
clr.addEventListener('click',function(){
if(!confirm('Delete ALL messages in this channel? This cannot be undone.'))return;
api('/api/messages/clear',{code:code,send_password:deletePw(card)})
.then(function(){refreshChannel(code)})
.catch(function(e){alert((e&&e.status===403)?'Wrong or missing send password.':'Could not delete messages.')})});
hdr.appendChild(clr);msgs.appendChild(hdr)}
function mkMsg(m){
var d=el('div','msg');
var del=el('button','iconbtn msg-del','\\uD83D\\uDDD1');
del.title='Delete this message';del.setAttribute('aria-label','Delete this message');
del.addEventListener('click',function(){
if(!confirm('Delete this message?'))return;
api('/api/message/delete',{code:code,id:m.id,send_password:deletePw(card)})
.then(function(){refreshChannel(code)})
.catch(function(e){alert((e&&e.status===403)?'Wrong or missing send password.':'Could not delete message.')})});
d.appendChild(del);
var t=fmtTime(m.ts);
var time=el('div','msg-time',t.abs);
time.appendChild(el('span','msg-rel',' \\u00b7 '+t.rel));
d.appendChild(time);
if(m.title)d.appendChild(el('div','msg-title',m.title));
if(m.body)d.appendChild(el('div','msg-body',m.body));
if(m.url&&/^https?:\\/\\//.test(m.url)){
var lk=el('div','msg-link');
var a=el('a','','Open link');a.href=m.url;a.target='_blank';a.rel='noopener noreferrer';
lk.appendChild(a);d.appendChild(lk)}
return d}
var VIS=3;
j.messages.slice(0,VIS).forEach(function(m){msgs.appendChild(mkMsg(m))});
if(j.messages.length>VIS){
var more=document.createElement('details');more.className='more-msgs';
more.appendChild(el('summary','','More \\u2026 ('+(j.messages.length-VIS)+' older)'));
var oh=el('div','msgs-hdr');
oh.appendChild(el('span','msgs-hint','Older messages'));
var delOld=el('button','iconbtn','\\uD83D\\uDDD1');
delOld.title='Delete all older messages';delOld.setAttribute('aria-label','Delete all older messages');
delOld.addEventListener('click',function(){
if(!confirm('Delete all older messages? (keeps the newest '+VIS+')'))return;
api('/api/messages/clear',{code:code,keep:VIS,send_password:deletePw(card)})
.then(function(){refreshChannel(code)})
.catch(function(e){alert((e&&e.status===403)?'Wrong or missing send password.':'Could not delete messages.')})});
oh.appendChild(delOld);more.appendChild(oh);
j.messages.slice(VIS).forEach(function(m){more.appendChild(mkMsg(m))});
msgs.appendChild(more)}
var latest=(j.messages[0]&&j.messages[0].ts)||j.channel.created||0;
card.setAttribute('data-ts',String(latest));
if(latest){var lt=fmtTime(latest);
card.querySelector('.channel-latest').textContent='Latest: '+lt.abs+' \\u00b7 '+lt.rel}
sortChannels();
}).catch(function(e){
if(e&&e.status===404){
card.querySelector('h2').textContent='Unknown channel';
card.querySelector('.stats').textContent=
'This code was not recognized (wrong code, or the channel expired).'}
else{
// offline / 5xx / rate-limited: keep whatever name we have, do not claim
// the channel is gone
card.querySelector('.stats').textContent=
'Could not refresh \\u2014 offline or server unavailable.'}})}

function addChannel(code){
if(codes.indexOf(code)>=0)return;
codes.push(code);lsSet(LS_CODES,codes);
lsSet(LS_REMOVED,lsGet(LS_REMOVED,[]).filter(function(x){return x!==code}));
renderChannels();injectManifest();mirrorStateForSW();
if(lsGet(LS_SUB,false))ensureSubscribed(false)}

function queuePendingUnsub(code,endpoint){
var q=lsGet(LS_PENDING,[]);q.push({code:code,endpoint:endpoint});lsSet(LS_PENDING,q)}
function drainPendingUnsub(){
var q=lsGet(LS_PENDING,[]);if(!q.length)return;
lsSet(LS_PENDING,[]);
q.forEach(function(it){
api('/api/unsubscribe',{code:it.code,endpoint:it.endpoint}).catch(function(e){
if(!(e&&e.status===404))queuePendingUnsub(it.code,it.endpoint)})})}

function removeChannel(code){
codes=codes.filter(function(x){return x!==code});lsSet(LS_CODES,codes);
setMuted(code,false);
var removedArr=lsGet(LS_REMOVED,[]);
if(removedArr.indexOf(code)<0)removedArr.push(code);
lsSet(LS_REMOVED,removedArr);
if('serviceWorker' in navigator){
navigator.serviceWorker.ready.then(function(r){
return r.pushManager.getSubscription()}).then(function(s){
if(s)return api('/api/unsubscribe',{code:code,endpoint:s.endpoint}).catch(function(e){
// unsubscribe failed (offline/5xx) — remember it so we do not keep
// pushing to a channel the user removed; retried on next open/online
if(!(e&&e.status===404))queuePendingUnsub(code,s.endpoint)})
}).catch(function(){})}
renderChannels();injectManifest();mirrorStateForSW()}

$('#add-btn').addEventListener('click',function(){
var v=$('#add-input').value.trim();
if(!CODE_RE.test(v)){$('#add-error').textContent='Invalid code format.';return}
$('#add-error').textContent='';$('#add-input').value='';
addChannel(v)});
$('#enable-btn').addEventListener('click',function(){
ensureSubscribed(true).catch(function(e){
$('#notif-state').textContent='Error: '+e.message})});
window.addEventListener('hashchange',function(){
loadCodes();renderChannels();injectManifest();mirrorStateForSW();
if(lsGet(LS_SUB,false))ensureSubscribed(false)});
window.addEventListener('online',drainPendingUnsub);

// ------- init
loadCodes();
injectManifest();
renderChannels();
mirrorStateForSW();
drainPendingUnsub();
if('serviceWorker' in navigator){
navigator.serviceWorker.register('/sw.js').catch(function(){})}
if(isIOS()&&!isStandalone()){$('#ios-hint').hidden=false}
applyCompat();
if(pushSupported()&&Notification.permission==='granted'&&lsGet(LS_SUB,false)){
ensureSubscribed(false).catch(function(){updateNotifUI('off')})}
else if(pushSupported()&&Notification.permission==='denied'){updateNotifUI('blocked')}
else if(!pushSupported()){updateNotifUI(isIOS()&&!isStandalone()?'ios-install':'unsupported')}
else{updateNotifUI('off')}
})();
</script>
</body>
</html>
"""
)

# -------------------------------------------------------- service worker

SW_JS = """'use strict';
var CACHE='nbw-v1';
var SHELL=['/a','/icon-192.png','/icon.svg'];

function urlB64ToU8SW(s){
var pad='='.repeat((4-s.length%4)%4);
var b=atob((s+pad).replace(/-/g,'+').replace(/_/g,'/'));
var a=new Uint8Array(b.length);
for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);
return a}

self.addEventListener('install',function(e){
e.waitUntil(caches.open(CACHE).then(function(c){return c.addAll(SHELL)})
.then(function(){return self.skipWaiting()}))});

self.addEventListener('activate',function(e){
e.waitUntil(caches.keys().then(function(ks){
return Promise.all(ks.filter(function(k){return k!==CACHE})
.map(function(k){return caches.delete(k)}))})
.then(function(){return self.clients.claim()}))});

self.addEventListener('fetch',function(e){
var url=new URL(e.request.url);
if(url.origin!==self.location.origin)return;
if(e.request.mode==='navigate'&&url.pathname==='/a'){
e.respondWith(fetch(e.request).then(function(r){
if(r.ok){var copy=r.clone();
caches.open(CACHE).then(function(c){c.put('/a',copy)})}
return r}).catch(function(){return caches.match('/a')}));
return}
if(SHELL.indexOf(url.pathname)>=0){
e.respondWith(caches.match(e.request).then(function(r){
return r||fetch(e.request)}))}});

self.addEventListener('push',function(e){
var d={};
try{d=e.data?e.data.json():{}}catch(err){d={body:e.data?e.data.text():''}}
var title=d.title||'Notify';
if(d.channel)title=title+' \\u2014 '+d.channel;
var opts={
body:d.body||'',
icon:'/icon-192.png',
badge:'/icon-192.png',
data:{url:d.url||'/a'},
timestamp:d.ts?d.ts*1000:Date.now()};
if(d.tag)opts.tag=d.tag;
e.waitUntil(self.registration.showNotification(title,opts))});

self.addEventListener('notificationclick',function(e){
e.notification.close();
var target=(e.notification.data&&e.notification.data.url)||'/a';
var abs;try{abs=new URL(target,self.location.origin).href}catch(err){abs=self.location.origin+'/a'}
var sameOrigin=abs.indexOf(self.location.origin+'/')===0||abs===self.location.origin;
e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(function(ws){
// a message link to another site: open it without destroying the app tab
if(!sameOrigin)return self.clients.openWindow(abs);
// prefer an existing app-page (/a) client over any other same-origin tab
var app=null,i;
for(i=0;i<ws.length;i++){try{if(new URL(ws[i].url).pathname==='/a'){app=ws[i];break}}catch(err){}}
if(app){
var p=('focus' in app)?app.focus():Promise.resolve(app);
return Promise.resolve(p).then(function(c){c=c||app;
if(c&&c.navigate&&new URL(c.url).href!==abs)return c.navigate(abs).catch(function(){return c});
return c})}
return self.clients.openWindow(abs)}))});

self.addEventListener('pushsubscriptionchange',function(e){
// the push service rotated our subscription — re-subscribe and re-register
// with the server so delivery does not silently stop
e.waitUntil(caches.open('nbw-state').then(function(c){return c.match('/__nbw_state')})
.then(function(r){return r?r.json():null}).then(function(st){
if(!st||!st.key||!st.codes||!st.codes.length)return;
var muted=st.muted||[];
var active=st.codes.filter(function(code){return muted.indexOf(code)<0});
return self.registration.pushManager.subscribe({userVisibleOnly:true,
applicationServerKey:urlB64ToU8SW(st.key)}).then(function(sub){
var body=sub.toJSON();
return Promise.all(active.map(function(code){
return fetch('/api/subscribe',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:code,subscription:body})}).catch(function(){})}))})})
.catch(function(){}))});
"""


def index_html() -> str:
    # On the landing page the compatibility list is expanded by default so the
    # supported devices are visible without a click; the app page keeps it
    # collapsed to stay tidy above the channel list.
    compat_open = COMPAT_HTML.replace(
        '<details class="compat">', '<details class="compat" open>'
    )
    return INDEX_HTML.replace("__DISCLAIMER__", DISCLAIMER_HTML).replace(
        "__COMPAT__", compat_open
    )


def app_html(vapid_public_key: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]", "", vapid_public_key or "")
    return (
        _APP_HTML_TEMPLATE.replace("__VAPID_PUBLIC_KEY__", key)
        .replace("__DISCLAIMER__", DISCLAIMER_HTML)
        .replace("__COMPAT__", COMPAT_HTML)
    )
