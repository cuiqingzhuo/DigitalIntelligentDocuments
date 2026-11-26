import {$, api, asBase64, esc, findings, mapSvg, message, reasons, stamp, status, uid} from './common.js';

let token = sessionStorage.getItem('diw:token') || '';
let actor, order, floor, draft, documentMap, revision, mapDirty, publicRevision, importFiles, adminSequence=0, trialItems=[];
const roleTabs = {librarian:['orders','imports','maps'],admin:['orders','imports','maps','models'],verifier:['trials']};
const values = form => Object.fromEntries(new FormData(form));
const req = (path,options={}) => api(path,{...options,token});
const feedback = (text,error=false) => message($('admin-message'),text,error);

function configureForm(id, callback) {
  $(id).addEventListener('submit', event => {event.preventDefault();run(() => callback(event.currentTarget));});
}
function click(id, callback) {$(id).onclick = () => run(callback);}
async function run(callback) {
  feedback('');
  try {await callback();}
  catch (error) {feedback(error.message || '请求失败',true);}
}

function download(name, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;link.download = name;link.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}
const downloadCsv = (name,text) => download(name,new Blob(['\ufeff',text],{type:'text/csv'}));
const loginButtons = document.querySelectorAll('[data-token]');

async function login(value) {
  const request = ++adminSequence;
  const user = await api('/admin/me',{token:value});
  if (request !== adminSequence) return;
  actor = user;
  token = value;
  sessionStorage.setItem('diw:token',token);
  $('login-card').hidden = true;
  $('logout').hidden = false;
  $('workspace').hidden = false;
  $('token').value = '';
  $('publish-map').hidden = actor.role !== 'admin';
  for (const button of $('tabs').children) button.hidden = !roleTabs[actor.role].includes(button.dataset.tab);
  await showTab(roleTabs[actor.role][0]);
}
function logout() {
  adminSequence++;
  token = ''; actor = null; order = null;
  sessionStorage.removeItem('diw:token');
  $('workspace').hidden = true;
  $('login-card').hidden = false;
  $('logout').hidden = true;
}

async function showTab(name) {
  for (const button of $('tabs').children) {
    const open = name === button.dataset.tab;
    button.classList.toggle('active',open);
    button.setAttribute('aria-pressed',String(open));
    $(button.dataset.tab).hidden = !open;
  }
  if (name === 'orders') await listOrders();
  if (name === 'maps' && !documentMap) await openMap();
  if (name === 'models') await modelLabel();
  if (name === 'trials') await listTrials();
}

async function listOrders() {
  const query=new URLSearchParams({face_id:$('filter-face').value.trim().toUpperCase(),status:$('filter-status').value});
  const result = await req('/admin/workorders?'+query);
  $('order-editor').hidden = true;
  $('order-list').innerHTML = result.items.length ? '<table><thead><tr><th>架面</th><th>读者反馈</th><th>状态</th><th>经办人</th><th>更新时间</th><th>操作</th></tr></thead><tbody>' +
    result.items.map(item => `<tr><td>${esc(item.face_id)}</td><td>${esc(reasons[item.reason])}<br><small>${esc(item.note)}</small></td><td>${esc(status[item.status])}<br><small>${esc(findings[item.finding])}</small></td><td>${esc(item.assignee || '—')}</td><td>${esc(stamp(item.updated_at))}</td><td><button data-order="${esc(item.workorder_id)}">${item.status === 'closed' ? '查看' : '打开'}</button></td></tr>`).join('') +
    '</tbody></table>' : '<p class="empty">当前筛选条件下没有工单。</p>';
  $('order-list').querySelectorAll('[data-order]').forEach(button => button.onclick = () => {
    order = result.items.find(item => item.workorder_id === button.dataset.order);
    const options = {open:['assigned'],assigned:['investigating','closed'],investigating:['investigating','closed'],closed:[]}[order.status];
    $('order-status').innerHTML = options.map(value=>`<option value="${value}">${status[value]}</option>`).join('');
    $('order-finding').value = order.finding;
    $('order-receipt').value = order.receipt;
    $('order-heading').textContent = `${order.face_id} · ${reasons[order.reason]} · ${status[order.status]}`;
    $('order-editor').querySelector('button[type=submit],button').disabled = !options.length;
    $('order-editor').hidden = false;
  });
}

async function catalogImport(preview) {
  const files = preview ? await Promise.all(['edition-csv','holding-csv'].map(id => $(id).files[0]?.text() ?? '')) : importFiles;
  if (!files?.some(Boolean)) throw new Error('请至少选择一个 CSV 文件。');
  const result = await req('/admin/imports',{method:'POST',body:{editions_csv:files[0],holdings_csv:files[1],preview}});
  importFiles = preview ? files : null;
  $('commit-import').disabled = !importFiles;
  $('import-result').innerHTML = `<p class="notice">${preview ? '预览' : '已处理'} · 新增 ${result.inserted} · 更新 ${result.updated} · 重复 ${result.unchanged} · 较旧状态 ${result.older} · 错误 ${result.errors.length}</p>` +
    (result.errors.length ? '<div class="table-wrap"><table><thead><tr><th>文件</th><th>行号</th><th>错误</th></tr></thead><tbody>' + result.errors.map(item=>`<tr><td>${esc(item.file)}</td><td>${item.line}</td><td>${esc(item.error)}</td></tr>`).join('') + '</tbody></table></div>' : '');
}

async function openMap() {
  const id = $('map-floor').value.trim();
  const result = await req('/admin/maps/'+encodeURIComponent(id)+'/draft');
  floor = id;
  draft = result.draft;
  publicRevision = result.published.revision;
  revision = draft?.revision ?? publicRevision;
  documentMap = structuredClone((draft || result.published).document);
  mapDirty = false;
  $('map-name').value = documentMap.name;
  $('map-service').value = documentMap.service_label;
  $('map-editor').hidden = $('map-actions').hidden = $('qr-form').hidden = false;
  $('qr-output').replaceChildren();
  drawMap();
}

function drawMap() {
  $('map-preview').innerHTML = mapSvg(documentMap,{showNodes:true});
  const mapFile = {...documentMap};
  delete mapFile.image;
  $('map-json').value = JSON.stringify(mapFile,null,2);
  for (const id of ['edge-a','edge-b','face-node']) {
    const active = $(id).value;
    const nodes = documentMap.nodes.filter(node => id !== 'face-node' || node.kind === 'shelf');
    $(id).innerHTML = nodes.map(node=>`<option value="${esc(node.id)}">${esc(node.id+' · '+node.label)}</option>`).join('');
    if (nodes.some(node=>node.id===active)) $(id).value = active;
  }
  $('map-version').textContent = `已发布 v${publicRevision} · ${draft ? '草稿 v'+draft.revision+' · '+stamp(draft.updated_at) : '暂无保存的草稿'}${mapDirty ? ' · 有未保存编辑' : ''}`;
  $('publish-map').disabled = !draft || mapDirty;
}

function dirtyMap() {mapDirty = true;drawMap();}
function replace(items, value, match) {
  const index = items.findIndex(match);
  if (index === -1) items.push(value);
  else items[index] = value;
  dirtyMap();
}

async function listTrials() {
  const data = await req('/trial/tasks');
  trialItems = data.items;
  const selected = $('trial-select').value;
  $('trial-select').innerHTML = data.items.map(item=>`<option value="${esc(item.task_id)}">${esc(item.task_id+' · '+item.participant_code)}</option>`).join('');
  if (data.items.some(item=>item.task_id===selected)) $('trial-select').value = selected;
  const records = new Map(data.records.map(item => [item.task_id,item]));
  $('trial-list').innerHTML = data.items.length ? '<table><thead><tr><th>任务</th><th>目标</th><th>首拿</th><th>最终</th><th>结果</th><th>用时</th></tr></thead><tbody>' +
    data.items.map(item => {
      const event = records.get(item.task_id);
      const evidence = entry => entry ? `${{correct:'正确',wrong:'错误',pending:'待核验'}[entry.outcome] || '待核验'} ${entry.holding_id || entry.manual_edition_id || ''}` : '—';
      const outcomes = {in_progress:'进行中',success:'成功',timeout:'超时',abandoned:'放弃',negative:'负例',unverified:'待核验'};
      return `<tr><td>${esc(item.task_id)}</td><td>${esc(item.target_edition_id)}</td><td>${esc(evidence(event?.first))}</td><td>${esc(evidence(event?.final))}</td><td>${esc(outcomes[event?.outcome] || '待记录')}</td><td>${event?.elapsed_s ?? '—'}</td></tr>`;
    }).join('') + '</tbody></table>' : '<p class="empty">暂无锁定的任务。</p>';
  $('trial-event').querySelector('button[type=submit],button').disabled = !data.items.length;
}

async function modelLabel() {
  const model = await req('/admin/models/current');
  $('model-current').textContent = `读者端当前使用 ${model.version} · 唯一阈值 ${model.unique_threshold} · 最小分差 ${model.margin}`;
}

configureForm('login-form',() => login($('token').value.trim()));
loginButtons.forEach(button => button.onclick = () => run(() => login(button.dataset.token)));
click('logout',() => {logout();documentMap=null;});
for (const button of $('tabs').children) button.onclick=()=>run(()=>showTab(button.dataset.tab));
configureForm('order-filter',listOrders);
configureForm('order-editor',async () => {
  const body = {revision:order.revision,status:$('order-status').value,finding:$('order-finding').value,receipt:$('order-receipt').value.trim()};
  await req('/admin/workorders/'+order.workorder_id,{method:'PATCH',body});
  await listOrders();
  feedback('工单已更新。');
});
for (const id of ['edition-csv','holding-csv']) $(id).onchange=()=>{importFiles=null;$('commit-import').disabled=true;};
click('preview-import',()=>catalogImport(true));
click('commit-import',()=>catalogImport(false));
click('download-editions',()=>downloadCsv('editions-template.csv','edition_id,title,author,publisher,year,volume,isbn\nLOCAL-001,示例书目,示例作者,示例出版社,2025,第1版,\n'));
click('download-holdings',()=>downloadCsv('holdings-template.csv','holding_id,edition_id,barcode,call_number,status,synced_at\nLOCAL-H01,LOCAL-001,LOCAL0001,TP311/01,available,2026-11-26T10:00:00+08:00\n'));
configureForm('map-open',openMap);
$('map-preview').addEventListener('click', event => {
  const svg = $('map-preview').querySelector('svg');
  if (!svg) return;
  const bounds = svg.getBoundingClientRect();
  $('node-x').value = Math.max(0,Math.min(100,(event.clientX-bounds.left)/bounds.width*100)).toFixed(1);
  $('node-y').value = Math.max(0,Math.min(100,(event.clientY-bounds.top)/bounds.height*100)).toFixed(1);
});
$('map-image').onchange = () => run(async () => {
  const file=$('map-image').files[0];if (!file) return;
  if (file.size>3_000_000) throw new Error('底图不能超过 3 MB。');
  documentMap.image=await asBase64(file);dirtyMap();
});
click('remove-background',()=>{documentMap.image=null;$('map-image').value='';dirtyMap();});
click('apply-json',()=>{
  const value=JSON.parse($('map-json').value);
  if (!['nodes','edges','faces'].every(key=>Array.isArray(value[key])) || (value.blocks && !Array.isArray(value.blocks))) throw new Error('JSON 必须包含 nodes、edges、faces 数组。');
  documentMap={...value,image:documentMap.image};
  $('map-name').value=value.name ?? '';
  $('map-service').value=value.service_label ?? '';
  dirtyMap();
});
for (const [id,key] of [['map-name','name'],['map-service','service_label']]) $(id).onchange = () => {
  if (documentMap[key] !== $(id).value.trim()) {documentMap[key]=$(id).value.trim();dirtyMap();}
};
configureForm('add-node',()=>{
  const data = {id:$('node-id').value.trim(),label:$('node-label').value.trim(),kind:$('node-kind').value,x:Number($('node-x').value),y:Number($('node-y').value)};
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(data.id)) throw new Error('请输入有效节点编号。');
  replace(documentMap.nodes,data,item=>item.id===data.id);
});
configureForm('add-edge',()=>{
  const data={a:$('edge-a').value,b:$('edge-b').value,accessible:$('edge-accessible').checked};
  if (!data.a || !data.b || data.a===data.b) throw new Error('请选择两个不同的节点。');
  replace(documentMap.edges,data,item=>(item.a===data.a && item.b===data.b)||(item.a===data.b && item.b===data.a));
});
configureForm('add-face',()=>{
  const id=$('face-id').value.trim().toUpperCase(), data={id,label:id,shelf:$('shelf-id').value.trim(),side:$('face-side').value,
    direction:$('face-direction').value,node_id:$('face-node').value,marker_status:$('marker-status').value};
  if (!data.node_id || !/^[A-Z0-9][A-Z0-9_.-]{0,79}$/.test(id)) throw new Error('请输入有效架面编号并选择架面节点。');
  replace(documentMap.faces,data,item=>item.id===id);
});
click('save-map',async()=>{
  documentMap.name=$('map-name').value.trim();
  documentMap.service_label=$('map-service').value.trim();
  draft=await req('/admin/maps/draft',{method:'PUT',body:{floor,revision,document:documentMap}});
  revision=draft.revision;
  documentMap=draft.document;
  mapDirty=false;drawMap();
  feedback('已保存草稿，预览区显示待发布地图。');
});
click('publish-map',async()=>{
  await req('/admin/maps/publish',{method:'POST',body:{floor,revision}});
  await openMap();feedback('地图已发布。');
});
configureForm('qr-form',async()=>{
  const face=$('qr-face').value.trim().toUpperCase();
  const result=await req('/admin/shelf-faces/'+encodeURIComponent(floor)+'/'+encodeURIComponent(face)+'/qr',{raw:true});
  const blob=await result.blob(), url=URL.createObjectURL(blob);
  const previous=$('qr-output').querySelector('img');
  if (previous) URL.revokeObjectURL(previous.src);
  const img=new Image();img.src=url;img.alt=face+' 架标二维码';img.className='qr';
  const button=document.createElement('button');button.textContent='下载 '+face+' 架标';button.onclick=()=>download(face+'.png',blob);
  $('qr-output').replaceChildren(img,button);
});
configureForm('model-form',async form=>{
  const body=values(form);
  for (const key of ['unique_threshold','margin','candidate_threshold']) body[key]=Number(body[key]);
  body.detector=body.detector.trim() || null;
  const result=await req('/admin/models/publish',{method:'POST',body});
  await modelLabel();
  feedback(`已发布模型 ${result.version}。`);
});
configureForm('trial-create',async form=>{
  const body=values(form);
  body.target_holding_id=body.target_holding_id.trim() || null;
  body.allowed_holding_ids=body.allowed_holding_ids.split(/[,，]/).map(value=>value.trim()).filter(Boolean);
  body.target_quads=body.target_quads.trim() ? JSON.parse(body.target_quads) : [];
  await req('/trial/tasks',{method:'POST',body});
  form.reset();await listTrials();feedback('答案已冻结。');
});
configureForm('trial-event',async form=>{
  const body=values(form),id=body.task_id;
  delete body.task_id;
  for (const key of ['session_id','barcode','manual_edition_id']) body[key]=body[key].trim() || null;
  const item=trialItems.find(value => value.task_id === id);
  if (item?.method === 'manual') body.session_id=null;
  body.witnesses=body.witnesses.split(/[,，]/).map(value=>value.trim()).filter(Boolean);
  body.milestone=body.action==='milestone' ? body.milestone || null : null;
  if (body.action !== 'take') {
    body.barcode=null;body.manual_edition_id=null;body.witnesses=[];
  }
  body.elapsed_s=Number(body.elapsed_s);
  const binding=JSON.stringify([id,body]);
  const button=form.querySelector('button[type=submit],button');
  if (button.dataset.binding!==binding) {button.dataset.binding=binding;button.dataset.key=uid();}
  await req('/trial/tasks/'+encodeURIComponent(id)+'/events',{method:'POST',body,key:button.dataset.key});
  delete button.dataset.binding;
  await listTrials();feedback('核验事件已保存。');
});
for (const format of ['csv','json']) click('trial-'+format, async()=>{
  const result=await req('/trial/export?format='+format,{raw:true});
  download('trial-results.'+format,await result.blob());
});

run(async () => {
  try {const config=await api('/config');$('demo-logins').hidden=!config.demo;$('map-floor').value=config.floor;}
  catch { $('demo-logins').hidden=true; }
  if (token) {
    try {await login(token);}
    catch (error) {logout();throw error;}
  }
});
