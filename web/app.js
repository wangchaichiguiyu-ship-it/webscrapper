(()=>{
'use strict';
const THEME_KEY='bilisonic.theme';
const POLL_MS=3000;
const STATUS={
queued:'排队中',resolving:'解析中',downloading:'下载中',converting:'转码中',
completed:'已完成',failed:'失败',canceled:'已取消',
};
const RUN=new Set(['resolving','downloading','converting']);
const CONN={connecting:'连接中…',live:'实时同步',polling:'轮询更新',offline:'连接中断'};
const TOAST_ICON={error:'#i-alert',warn:'#i-alert',success:'#i-check-circle',info:'#i-info'};
const q=(s,r=document)=>r.querySelector(s);
const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
const pad=n=>String(n).padStart(2,'0');
const pref=k=>{try{return localStorage.getItem(k);}catch(e){return null;}};
const savePref=(k,v)=>{try{localStorage.setItem(k,v);}catch(e){}};
const EMAP={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>EMAP[c]);
const txt=(el,v)=>{if(el)el.textContent=v==null?'':String(v);};
const hideTxt=(el,t)=>{if(el){el.hidden=!t;el.textContent=t||'';}};
function bytes(n){
n=Number(n);
if(!isFinite(n)||n<0)return '—';
if(n<1024)return Math.round(n)+' B';
const units=['KB','MB','GB','TB'];
let v=n/1024,i=0;
while(v>=1024&&i<units.length-1){v/=1024;i+=1;}
return (v>=100?v.toFixed(0):v>=10?v.toFixed(1):v.toFixed(2))+' '+units[i];
}
function dur(seconds){
let s=Number(seconds);
if(!isFinite(s)||s<=0)return '';
s=Math.round(s);
const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
return h>0?h+':'+pad(m)+':'+pad(x):m+':'+pad(x);
}
function eta(seconds){
const s=Number(seconds);
if(!isFinite(s)||s<=0)return '';
if(s<60)return '剩余 '+Math.max(1,Math.round(s))+' 秒';
if(s<3600)return '剩余 '+Math.round(s/60)+' 分钟';
return '剩余 '+(s/3600).toFixed(1)+' 小时';
}
function speed(bps){
const n=Number(bps);
return isFinite(n)&&n>0?bytes(n)+'/s':'';
}
function shortPath(path){
const text=String(path||'');
if(!text)return '输出目录';
const parts=text.split('/').filter(Boolean);
return parts.length<=2?text:'…/'+parts.slice(-2).join('/');
}
const S={
metaLoaded:false,tasks:[],format:'flac',formatTouched:false,
conn:'connecting',sse:null,polling:false,pollTimer:0,failStreak:0,
refreshing:false,submitting:false,audioId:null,cards:new Map(),
};
const E={};
class ApiError extends Error{
constructor(message,status,detail){
super(message);
this.name='ApiError';
this.status=status||0;
this.detail=detail||'';
}
}
async function api(path,opt={}){
const ctl=new AbortController();
const timer=setTimeout(()=>ctl.abort(),opt.timeout||20000);
try{
const headers={Accept:'application/json'};
const init={method:opt.method||'GET',headers,credentials:'same-origin',signal:ctl.signal};
if(opt.body!==undefined){
headers['Content-Type']='application/json';
init.body=JSON.stringify(opt.body);
}
const res=await fetch(path,init);
const json=(res.headers.get('content-type')||'').includes('application/json');
const data=json?await res.json().catch(()=>null):null;
if(!res.ok){
const raw=data&&data.detail;
const detail=typeof raw==='string'?raw:Array.isArray(raw)
?raw.map(x=>(x&&(x.msg||x.message))||'').filter(Boolean).join('；')
:raw&&typeof raw==='object'?raw.message||JSON.stringify(raw):'';
throw new ApiError(detail||`请求失败（HTTP ${res.status}）`,res.status,detail);
}
return data;
}catch(err){
if(err instanceof ApiError)throw err;
throw new ApiError(err&&err.name==='AbortError'?'请求超时，请确认后端服务仍在运行':'无法连接到本地服务，请确认后端已启动',0);
}finally{
clearTimeout(timer);
}
}
function toast(message,type='info',opt={}){
if(!E.toasts)return ;
const el=q('#toast-tpl').content.firstElementChild.cloneNode(true);
el.classList.add('toast-'+type);
el.setAttribute('role',type==='error'?'alert':'status');
q('use',q('.toast-icon',el)).setAttribute('href',TOAST_ICON[type]||TOAST_ICON.info);
q('.toast-title',el).textContent=message;
if(opt.detail){
const detail=q('.toast-detail',el);
detail.hidden=false;
detail.textContent=opt.detail;
}
const close=()=>{
if(!el.isConnected||el.classList.contains('is-leaving'))return ;
el.classList.add('is-leaving');
setTimeout(()=>el.remove(),280);
};
q('.toast-close',el).addEventListener('click',close);
E.toasts.appendChild(el);
while(E.toasts.children.length>4)E.toasts.firstElementChild.remove();
setTimeout(close,opt.timeout||(type==='error'?7000:type==='warn'?5500:4200));
}
function applyTheme(mode,persist){
const dark=mode!=='light';
document.documentElement.dataset.theme=dark?'dark':'light';
E.themeToggle.innerHTML=`<svg aria-hidden="true"><use href="#${dark ? 'i-sun' : 'i-moon'}"></use></svg>`;
const label=dark?'切换到浅色模式':'切换到深色模式';
E.themeToggle.setAttribute('aria-label',label);
E.themeToggle.title=label;
const metaTheme=q('meta[name="theme-color"]');
if(metaTheme)metaTheme.content=dark?'#080a0e':'#f5f1e9';
if(persist)savePref(THEME_KEY,dark?'dark':'light');
}
const chip=(tone,icon,label,title)=>
`<span class="status-chip ${tone}" title="${esc(title || label)}"><svg aria-hidden="true"><use href="#${icon}"></use></svg>`+
`<span class="chip-label">${esc(label)}</span></span>`;
function setFormat(fmt){
S.format=fmt;
const radio=document.querySelector(`input[name="output-format"][value="${fmt}"]`);
if(radio)radio.checked=true;
}
function renderMeta(meta){
S.metaLoaded=true;
const m=meta||{};
if(!S.formatTouched&&['flac','wav','m4a'].includes(m.default_format))setFormat(m.default_format);
const chips=[
m.ffmpeg_ok?chip('ok','i-check-circle','ffmpeg 就绪',m.ffmpeg_version?'ffmpeg '+m.ffmpeg_version:'转码可用')
:chip('bad','i-alert','ffmpeg 未就绪','未检测到 ffmpeg，无法转码'),
m.cookie_configured?chip('ok','i-check-circle','Cookie 已配置','Cookie 可用，可访问更高音质')
:chip('warn','i-info','访客模式','未配置 Cookie，部分高音质可能受限'),
];
if(m.output_dir){
chips.push(`<button class="status-chip path" id="reveal-output" type="button" title="${esc(m.output_dir)} · 点击在访达中显示">`+
`<svg aria-hidden="true"><use href="#i-folder"></use></svg><span class="chip-label">${esc(shortPath(m.output_dir))}</span></button>`);
}
E.chips.innerHTML=chips.join('');
const revealBtn=q('#reveal-output',E.chips);
if(revealBtn)revealBtn.addEventListener('click',()=>revealFolder(null));
E.footer.textContent=[
m.version&&'v'+m.version,
m.max_concurrent&&'并发 '+m.max_concurrent,
m.max_tasks&&'上限 '+m.max_tasks,
m.output_dir&&'输出 '+shortPath(m.output_dir),
].filter(Boolean).join(' · ')||'BiliSonic · 本地音频下载器';
}
function renderMetaUnavailable(){
S.metaLoaded=false;
E.chips.innerHTML=chip('bad','i-alert','后端未连接','请确认 FastAPI 后端已启动并监听 127.0.0.1:8765');
E.footer.textContent='等待后端连接… 请确认 FastAPI 服务运行在 127.0.0.1:8765';
}
function maybeMeta(){
if(S.metaLoaded)return ;
api('/api/meta',{timeout:10000}).then(renderMeta).catch(()=>{});
}
const URL_RE=/(?:https?:\/\/)?(?:[\w-]+\.)*(?:bilibili\.com|b23\.tv|bili2233\.cn)\/[^\s<>"'`，。；、）】」』]+/gi;
const ID_RE=/\b(?:BV[0-9A-Za-z]{10}|av\d{1,12})\b/gi;
function addEntry(list,seen,raw,url){
let parsed;
try{parsed=new URL(url);}catch(e){return;}
const key=parsed.href.toLowerCase();
if(seen.has(key))return;
seen.add(key);
list.push({raw,url:parsed.href});
}
function parseInput(text){
const list=[],seen=new Set();
let work=String(text||'');
for(const match of work.matchAll(URL_RE)){
const raw=match[0];
let url=raw.replace(/[)\]}>.,!?;:'"`，。；、）】」』]+$/g,'');
if(!/^https?:\/\//i.test(url))url='https://'+url;
work=work.split(raw).join(' ');
addEntry(list,seen,raw,url);
}
for(const match of work.matchAll(ID_RE)){
const id=match[0];
const base=id.toLowerCase().startsWith('av')?'av'+id.slice(2):id;
addEntry(list,seen,id,'https://www.bilibili.com/video/'+base);
}
return list;
}
function autoSize(){
E.input.style.height='auto';
E.input.style.height=clamp(E.input.scrollHeight+2,132,320)+'px';
}
function setSubmitting(flag){
S.submitting=flag;
E.submit.disabled=flag;
E.submit.setAttribute('aria-busy',flag?'true':'false');
txt(q('.btn-label',E.submit),flag?'正在加入…':'加入下载队列');
}
function stripRaws(text,raws){
let next=String(text||'');
raws.forEach(raw=>{if(raw)next=next.split(raw).join(' ');});
return next.replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').replace(/^\s+|\s+$/g,'');
}
async function submit(){
if(S.submitting)return ;
const text=E.input.value;
const entries=parseInput(text);
if(!entries.length){
toast(text.trim()?'没有识别到有效的 Bilibili 链接':'URL 池还是空的','error',
{detail:'支持 bilibili.com 视频链接与 b23.tv 短链。'});
E.input.focus();
return ;
}
setSubmitting(true);
let ok=0,dup=0,failMsg='';
const handled=[];
try{
for(const entry of entries){
try{
const data=await api('/api/tasks',{method:'POST',body:{url:entry.url,format:S.format}});
const task=data&&data.task?data.task:data;
if(task&&task.id!=null)upsert(task);
ok+=1;
handled.push(entry.raw);
}catch(err){
if(err.status===409){dup+=1;handled.push(entry.raw);}
else if(!failMsg)failMsg=err.message;
}
}
}finally{
setSubmitting(false);
}
if(!failMsg){
E.input.value='';
autoSize();
}else if(handled.length){
E.input.value=stripRaws(text,handled);
autoSize();
}
if(!failMsg){
if(ok&&dup)toast(`已加入 ${ok} 个任务，${dup} 个链接已在队列中`,'success');
else if(ok)toast(ok===1?'已加入下载队列':`已加入 ${ok} 个任务`,'success');
else toast('这些链接已经在队列中','warn');
}else {
toast(entries.length===1?`链接未能入队：${failMsg}`:'部分链接未能入队','error',
{detail:failMsg+(ok||dup?`（已处理 ${ok + dup} 个）`:'')});
}
E.input.focus();
}
function insertComposer(text){
const input=E.input;
const start=input.selectionStart==null?input.value.length:input.selectionStart;
const end=input.selectionEnd==null?input.value.length:input.selectionEnd;
const before=input.value.slice(0,start),after=input.value.slice(end);
const insertion=(before&&!/[\r\n]$/.test(before)?'\n':'')+String(text).trim();
input.value=before+insertion+after;
const caret=(before+insertion).length;
try{input.setSelectionRange(caret,caret);}catch(e){}
autoSize();
}
function onGlobalPaste(ev){
const target=ev.target;
if(target&&(target.tagName==='INPUT'||target.tagName==='TEXTAREA'||target.isContentEditable))return ;
if(!ev.clipboardData)return ;
let text='';
try{text=ev.clipboardData.getData('text/plain')||ev.clipboardData.getData('text');}catch(e){return ;}
if(!text||!text.trim())return ;
ev.preventDefault();
E.input.focus();
insertComposer(text);
toast('已粘贴到 URL 池','info');
}
function calcStats(tasks){
const s={total:tasks.length,queued:0,active:0,completed:0,failed:0,canceled:0};
tasks.forEach(t=>{
if(t.status==='queued')s.queued+=1;
else if(RUN.has(t.status))s.active+=1;
else if(t.status==='completed')s.completed+=1;
else if(t.status==='failed')s.failed+=1;
else if(t.status==='canceled')s.canceled+=1;
});
return s;
}
function setTasks(tasks){
S.tasks=(Array.isArray(tasks)?tasks:[]).slice().reverse();
syncList();
renderStats();
}
function upsert(task){
if(!task||task.id==null)return ;
const index=S.tasks.findIndex(x=>String(x.id)===String(task.id));
if(index===-1)S.tasks.unshift(task);
else S.tasks[index]=Object.assign({},S.tasks[index],task);
syncList();
renderStats();
}
function removeTask(id){
S.tasks=S.tasks.filter(t=>String(t.id)!==String(id));
syncList();
renderStats();
}
function renderStats(){
const s=calcStats(S.tasks),bits=[];
if(s.active)bits.push(`<span class="stat active">进行中 ${s.active}</span>`);
if(s.queued)bits.push(`<span class="stat queued">排队 ${s.queued}</span>`);
if(s.completed)bits.push(`<span class="stat completed">完成 ${s.completed}</span>`);
if(s.failed)bits.push(`<span class="stat failed">失败 ${s.failed}</span>`);
if(s.canceled)bits.push(`<span class="stat canceled">已取消 ${s.canceled}</span>`);
E.stats.innerHTML=bits.length?bits.join('<span class="stat-sep">·</span>'):'<span class="stat">暂无任务</span>';
const done=s.completed+s.failed+s.canceled;
E.clear.disabled=!done;
txt(q('span',E.clear),done?`清理已结束 · ${done}`:'清理已结束');
}
function syncList(){
const live=new Set(S.tasks.map(t=>String(t.id)));
for(const[key,entry]of S.cards){
if(!live.has(String(key))){
if(entry.el.contains(document.activeElement))E.input.focus();
entry.el.remove();
S.cards.delete (key);
if(String(S.audioId)===String(key))closePlayer();
}
}
let prev=null;
S.tasks.forEach((task,index)=>{
let entry=S.cards.get(task.id);
if(!entry){entry=createCard(task,index);S.cards.set(task.id,entry);}
paint(entry,task);
if(prev){if(entry.el.previousElementSibling!==prev)prev.after(entry.el);}
else if(entry.el!==E.list.firstElementChild)E.list.prepend(entry.el);
prev=entry.el;
});
E.empty.hidden=S.tasks.length>0;
E.list.hidden=S.tasks.length===0;
}
const REFS={
st:'.status-label',pct:'.status-pct',q:'.badge-quality',f:'.badge-format',t:'.card-title',
m:'.card-meta',pr:'.card-progress',pb:'.progress',fill:'.progress-fill',by:'.p-bytes',
sp:'.p-speed',et:'.p-eta',out:'.card-output',on:'.out-name',os:'.out-size',
er:'.card-error',wn:'.card-warnings',ac:'.card-actions',
};
function createCard(task,index){
const el=q('#card-tpl').content.firstElementChild.cloneNode(true);
el.dataset.id=String(task.id);
el.style.animationDelay=Math.min(index||0,8)*35+'ms';
const refs={};
Object.keys(REFS).forEach(key=>{refs[key]=q(REFS[key],el);});
return {el,task,status:null,refs};
}
const ACTIONS={
queued:[['cancel','i-x','取消']],
resolving:[['cancel','i-x','取消']],
downloading:[['cancel','i-x','取消']],
converting:[['cancel','i-x','取消']],
failed:[['retry','i-retry','重试','soft'],['delete','i-trash','移除','ghost danger']],
canceled:[['retry','i-retry','重试','soft'],['delete','i-trash','移除','ghost danger']],
completed:[
['play','i-play','播放','soft'],['download','i-download','下载'],
['reveal','i-folder','在访达中显示'],['delete','i-trash','移除','ghost danger'],
],
};
function actionsHtml(task){
return (ACTIONS[task.status]||[]).map(([action,icon,label,tone='ghost'])=>
`<button type="button" class="btn mini ${tone}" data-action="${action}" title="${esc(label)}" `+
`aria-label="${esc(label)}"><svg aria-hidden="true"><use href="#${icon}"></use></svg><span>${esc(label)}</span></button>`).join('');
}
function renderActions(entry,task){
const hadFocus=entry.el.contains(document.activeElement);
entry.refs.ac.innerHTML=actionsHtml(task);
if(hadFocus&&!entry.el.contains(document.activeElement)){
entry.el.tabIndex=-1;
entry.el.focus({preventScroll:true});
}
}
function paint(entry,task){
const el=entry.el,r=entry.refs;
const status=String(task.status||'queued'),prev=entry.status;
entry.status=status;
el.dataset.status=status;
const label=task.stage_label||STATUS[status]||status;
const progress=clamp(Number(task.progress)||0,0,100);
const pct=Math.round(progress);
const running=RUN.has(status);
el.setAttribute('aria-label',`${task.title || '音频任务'} · ${label}`);
txt(r.st,label);
const pctText=status==='completed'?'100%':(status==='downloading'||status==='converting')?pct+'%':'';
r.pct.hidden=!pctText;
r.pct.textContent=pctText;
const quality=task.source,qLabel=quality&&(quality.label||quality.quality_id);
r.q.hidden=!qLabel;
if(qLabel){
r.q.textContent=qLabel;
r.q.classList.toggle('lossless',!!quality.lossless);
const bw=Number(quality.bandwidth);
r.q.title=[quality.codec,quality.container,isFinite(bw)&&bw>0?Math.round(bw/1000)+' kbps':'',quality.note]
.filter(Boolean).join(' · ');
}
r.f.hidden=!task.format;
if(task.format){
let fmt=String(task.format).toUpperCase();
if(task.format==='m4a'&&task.source&&task.source.lossless)fmt='FLAC';
if(task.output_filename){const match=/\.([A-Za-z0-9]+)$/.exec(task.output_filename);if(match)fmt=match[1].toUpperCase();}
r.f.textContent=fmt;
}
const title=task.title||task.canonical_url||task.url||'未命名任务';
txt(r.t,title);
r.t.title=task.title||task.url||'';
const parts=[];
if(task.uploader)parts.push(String(task.uploader));
const duration=dur(task.duration);
if(duration)parts.push(duration);
if(task.page)parts.push('P'+task.page);
r.m.textContent=parts.join(' · ');
r.m.hidden=!parts.length;
const showProgress=running||status==='completed';
r.pr.hidden=!showProgress;
if(showProgress){
r.pr.classList.toggle('is-indeterminate',running&&!(Number(task.total_bytes)>0)&&progress===0);
r.fill.style.width=clamp(status==='completed'?100:progress,0,100)+'%';
r.pb.setAttribute('aria-valuenow',String(status==='completed'?100:pct));
let bytesText='';
if(status==='completed')bytesText=bytes(task.output_size||task.total_bytes||task.downloaded_bytes);
else if(Number(task.total_bytes)>0)bytesText=bytes(task.downloaded_bytes)+' / '+bytes(task.total_bytes);
else if(Number(task.downloaded_bytes)>0)bytesText=bytes(task.downloaded_bytes);
hideTxt(r.by,bytesText);
let speedText='';
if(status==='downloading')speedText=speed(task.speed_bps);
else if(status==='converting'&&pct>0)speedText='转码中';
hideTxt(r.sp,speedText);
hideTxt(r.et,status==='downloading'||status==='converting'?eta(task.eta_seconds):'');
}else {
r.pr.classList.remove('is-indeterminate');
}
const showOutput=status==='completed'&&!!task.output_filename;
r.out.hidden=!showOutput;
if(showOutput){
r.on.textContent=task.output_filename;
hideTxt(r.os,Number(task.output_size)>0?bytes(task.output_size):'');
}
const errorText=status==='failed'&&task.error?String(task.error):'';
r.er.hidden=!errorText;
if(errorText){r.er.textContent=errorText;r.er.title=errorText;}
const warnings=Array.isArray(task.warnings)
?task.warnings.filter(Boolean).map(x=>(typeof x==='string'?x:(x&&(x.message||x.detail))||JSON.stringify(x)))
:[];
r.wn.hidden=!warnings.length;
if(warnings.length){r.wn.textContent=warnings[0];r.wn.title=warnings.join('\n');}
if(prev!==status)renderActions(entry,task);
}
async function act(action,task,button){
if(action==='play'){playTask(task);return ;}
if(action==='download'){downloadTask(task);return ;}
if(button){button.disabled=true;button.classList.add('is-busy');}
try{
if(action==='cancel'){
const data=await api('/api/tasks/'+encodeURIComponent(task.id)+'/cancel',{method:'POST'});
upsert(data&&data.task?data.task:data);
toast('已取消该任务','success');
}else if(action==='retry'){
const data=await api('/api/tasks/'+encodeURIComponent(task.id)+'/retry',{method:'POST'});
upsert(data&&data.task?data.task:data);
toast('已重新加入队列','success');
}else if(action==='delete'){
await api('/api/tasks/'+encodeURIComponent(task.id),{method:'DELETE'});
removeTask(task.id);
toast('任务已移除','success');
}else if(action==='reveal'){
await api('/api/reveal',{method:'POST',body:{task_id:task.id}});
toast('已在访达中显示','success');
}
}catch(err){
if(action==='delete'&&err.status===409)toast('任务正在进行中，暂时无法移除','warn',{detail:err.message});
else if(action==='cancel'&&err.status===409)toast('任务已经结束','warn');
else toast('操作失败','error',{detail:err.message});
}finally{
if(button&&button.isConnected){button.disabled=false;button.classList.remove('is-busy');}
}
}
async function revealFolder(id){
try{
await api('/api/reveal',{method:'POST',body:id?{task_id:id}:{}});
toast('已在访达中打开输出目录','success');
}catch(err){
toast('无法打开访达','error',{detail:err.message});
}
}
async function clearFinished(){
if(E.clear.disabled)return ;
E.clear.disabled=true;
try{
const data=await api('/api/tasks/clear',{method:'POST',body:{scope:'finished'}});
const removed=data&&isFinite(Number(data.removed))?Number(data.removed):0;
toast(removed>0?`已清理 ${removed} 个已结束任务`:'没有可清理的任务',removed>0?'success':'info');
await refresh();
}catch(err){
toast('清理失败','error',{detail:err.message});
}finally{
renderStats();
}
}
function audioUrl(task){
return '/api/tasks/'+encodeURIComponent(task.id)+'/audio';
}
function playTask(task){
if(!task)return ;
if(S.audioId!=null&&String(S.audioId)===String(task.id)){
E.player.hidden=false;
document.body.classList.add('has-player');
if(E.audio.paused)E.audio.play().catch(()=>toast('无法播放音频','error'));else E.audio.pause();
return ;
}
S.audioId=task.id;
E.audio.src=audioUrl(task);
E.player.hidden=false;
document.body.classList.add('has-player');
E.playerTitle.textContent=task.title||task.output_filename||'音频预览';
E.audio.play().catch(()=>toast('无法播放音频','error',{detail:'音频可能尚未生成，或后端未启用流式播放。'}));
}
function closePlayer(){
if(!E.audio)return ;
E.audio.pause();
E.audio.removeAttribute('src');
try{E.audio.load();}catch(e){}
S.audioId=null;
E.player.hidden=true;
document.body.classList.remove('has-player');
}
function downloadTask(task){
if(!task)return ;
const link=document.createElement('a');
link.href=audioUrl(task)+'?download=1';
if(task.output_filename)link.download=task.output_filename;
document.body.appendChild(link);
link.click();
link.remove();
toast('开始下载音频文件','success');
}
function setConn(mode){
const next=CONN[mode]?mode:'connecting';
S.conn=next;
E.conn.dataset.state=next;
E.connLabel.textContent=CONN[next];
E.conn.title=CONN[next];
}
function startPolling(){
if(S.polling)return ;
S.polling=true;
setConn('polling');
S.pollTimer=setInterval(refresh,POLL_MS);
}
function stopPolling(){
if(!S.polling)return ;
S.polling=false;
clearInterval(S.pollTimer);
S.pollTimer=0;
}
async function refresh(){
if(S.refreshing)return ;
S.refreshing=true;
try{
const data=await api('/api/tasks',{timeout:12000});
S.failStreak=0;
setTasks(data&&Array.isArray(data.tasks)?data.tasks:[]);
if(S.polling)setConn('polling');
else if(S.sse&&S.sse.readyState===1)setConn('live');
maybeMeta();
}catch(err){
S.failStreak+=1;
if(S.failStreak===1)toast('获取任务列表失败，正在自动重试','error',{detail:err.message});
if(S.failStreak>=3)setConn('offline');
if(!S.polling)startPolling();
}finally{
S.refreshing=false;
}
}
function onServer(message){
if(!message||typeof message!=='object')return ;
if(message.type==='task'&&message.task)upsert(message.task);
else if(message.type==='deleted'&&message.id!=null)removeTask(message.id);
else if(message.type==='snapshot')refresh();
}
function connectSSE(){
if(!('EventSource'in window)){
toast('浏览器不支持实时推送，已切换为轮询更新','warn');
startPolling();
return ;
}
setConn('connecting');
let source;
try{source=new EventSource('/api/events');}catch(e){startPolling();return ;}
S.sse=source;
source.addEventListener('open',()=>{
S.failStreak=0;
stopPolling();
setConn('live');
refresh();
maybeMeta();
});
source.addEventListener('message',ev=>{
if(!ev.data)return ;
let message;
try{message=JSON.parse(ev.data);}catch(e){return ;}
onServer(message);
});
source.addEventListener('error',()=>{setConn('polling');startPolling();});
}
function cache(){
E.input=q('#url-input');E.form=q('#composer');E.submit=q('#submit-btn');
E.list=q('#task-list');E.empty=q('#empty-state');E.stats=q('#stats');
E.clear=q('#clear-btn');E.conn=q('#conn');E.connLabel=q('#conn-label');
E.chips=q('#status-chips');E.themeToggle=q('#theme-toggle');E.footer=q('#footer-meta');
E.toasts=q('#toasts');E.player=q('#player');E.playerTitle=q('#player-title');
E.playerClose=q('#player-close');E.audio=q('#audio');
}
function bind(){
E.form.addEventListener('submit',ev=>{ev.preventDefault();submit();});
E.input.addEventListener('keydown',ev=>{
if(ev.key!=='Enter'||ev.isComposing||ev.keyCode===229||ev.shiftKey)return ;
ev.preventDefault();
submit();
});
E.input.addEventListener('input',autoSize);
E.input.addEventListener('paste',()=>setTimeout(autoSize,0));
document.addEventListener('paste',onGlobalPaste);
document.addEventListener('change',ev=>{
const input=ev.target.closest&&ev.target.closest('input[name="output-format"]');
if(input){S.format=input.value;S.formatTouched=true;}
});
E.themeToggle.addEventListener('click',()=>{
applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light',true);
});
E.clear.addEventListener('click',clearFinished);
E.list.addEventListener('click',ev=>{
const button=ev.target.closest('[data-action]');
if(!button)return ;
const card=button.closest('.task-card');
if(!card)return ;
const task=S.tasks.find(t=>String(t.id)===card.dataset.id);
if(task)act(button.dataset.action,task,button);
});
E.audio.addEventListener('error',()=>{
if(S.audioId!=null)toast('音频播放失败','error');
});
E.playerClose.addEventListener('click',closePlayer);
}
async function loadInitial(){
setConn('connecting');
const[metaRes,tasksRes]=await Promise.allSettled([
api('/api/meta',{timeout:12000}),
api('/api/tasks',{timeout:12000}),
]);
if(metaRes.status==='fulfilled')renderMeta(metaRes.value);
else renderMetaUnavailable();
if(tasksRes.status==='fulfilled'){
S.failStreak=0;
setTasks(tasksRes.value&&Array.isArray(tasksRes.value.tasks)?tasksRes.value.tasks:[]);
}else {
S.failStreak=1;
toast('无法载入任务列表，正在自动重试','error',{detail:tasksRes.reason&&tasksRes.reason.message});
startPolling();
}
if(metaRes.status==='rejected'&&tasksRes.status==='fulfilled'){
toast('运行配置读取失败，稍后自动重试','warn',{detail:metaRes.reason&&metaRes.reason.message});
}
connectSSE();
}
function init(){
cache();
applyTheme(pref(THEME_KEY)==='light'?'light':'dark',false);
bind();
autoSize();
renderStats();
setConn('connecting');
loadInitial();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);
else init();
})();
