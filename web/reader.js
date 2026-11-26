import {$, api, asBase64, bookMeta, esc, findings, mapSvg, message, read, readBarcode, route, save, stamp, status, uid} from './common.js';

let settings = read('config', {library:'demo', floor:'F1', demo:false, cloud_review:true});
const markerPath = location.pathname.startsWith('/shelf/') ? location.pathname.split('/').slice(2).map(decodeURIComponent) : [];
const prefilled = new URLSearchParams(location.search).get('face') || '';
let map, start = '', face = markerPath[2] || (/^[A-Z0-9][A-Z0-9_.-]{0,79}$/.test(prefilled) ? prefilled : ''), actual = markerPath[2] || '', scanned = markerPath[2] || '';
let pendingBook, edition, copy = null, task = null, requestedSearch = 0, catalogPage = 1;
let photo = null, sample = '', position = 0, flushing = false, lastOrder = null;
const cached = read('catalog', {books:[], copies:{}, at:null});
const queue = read('queue', []);
let sessionKey = uid();
const sticky = () => $('global-message');

function saveTask() {
  save('active-task',{library:settings.library,edition,copy,face,actual,scanned,task,sessionKey,lastOrder});
}

function progress(step, focus = '') {
  position = step;
  [...$('journey').children].forEach((item,index) => {
    item.classList.toggle('done', index < step);
    if (index === step) item.setAttribute('aria-current','step');
    else item.removeAttribute('aria-current');
  });
  document.querySelector('.reader-grid').classList.toggle('routing', step === 1);
  for (const [id, threshold] of [['arrival',1],['capture',2],['verify',3],['feedback',2]]) $(id).hidden = step < threshold;
  if (focus) $(focus).scrollIntoView({behavior:'smooth', block:'start'});
}

function refreshNetwork() {
  const button = $('network');
  button.classList.toggle('offline', !navigator.onLine || !settings.apiOnline);
  const online = navigator.onLine && settings.apiOnline;
  button.textContent = online ? (settings.demo ? '演示馆 · 在线' : '馆内服务在线')
    : '离线 · ' + (cached.at ? '已缓存 ' + stamp(cached.at) : '暂无本地书目');
  if (queue.length) button.textContent += ' · ' + queue.length + ' 项待同步';
}

function configUi() {
  $('examples').hidden = !settings.demo;
  $('samples').hidden = !settings.demo;
  $('demo-barcodes').hidden = !settings.demo;
  $('consent-label').hidden = !settings.cloud_review;
  $('consent-text').textContent = settings.photo_retention_days
    ? `同意上传本次照片用于云端复核，最长保存 ${settings.photo_retention_days} 天`
    : '同意上传本次照片用于云端复核，识别后删除';
  $('library-name').textContent = settings.demo ? '演示馆 · 开架借阅区' : '图书馆 · 开架借阅区';
}

function queryValue() {return $('query').value.trim();}
function norm(text) {return String(text || '').normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu,'');}

async function cacheCatalog() {
  if (!settings.apiOnline) return;
  const pilot = await api('/cache/pilot');
  cached.books = pilot.books;
  cached.copies = pilot.copies;
  cached.at = pilot.updated_at;
  save('catalog', cached);
}

async function search(page = 1) {
  const seq = ++requestedSearch;
  const q = queryValue(), selected = $('search-field').value, limit = 5;
  const path = '/editions?' + new URLSearchParams({q, field:selected, limit, page});
  let data;
  try {
    data = await api(path);
  } catch (error) {
    if (error.status) throw error;
    settings.apiOnline = false;
    refreshNetwork();
    const needle = norm(q);
    const found = cached.books.filter(book => {
      const values = {title:[book.title],author:[book.author],isbn:[book.isbn],call_number:(cached.copies[book.edition_id] || []).map(item => item.call_number)};
      return !needle || Object.entries(values).some(([field,terms]) => (selected === 'all' || selected === field) && terms.some(term => norm(term).includes(needle)));
    });
    data = {items:found.slice((page-1)*limit,page*limit),total:found.length};
  }
  if (seq !== requestedSearch) return;
  catalogPage = page;
  $('results-heading').textContent = q ? `匹配版本 · ${data.total} 条` : '馆藏书目';
  $('catalog-time').textContent = cached.at ? '缓存于 ' + stamp(cached.at) : '';
  $('results').innerHTML = data.items.length ? data.items.map(book => {
    const overdue = book.stale || (book.synced_at && Date.now() - Date.parse(book.synced_at) > (settings.stale_hours || 72)*3600000);
    const label = book.available ? `${book.available} 册可借` : (book.status === 'loaned' ? '全部借出' : status[book.status]);
    return `<article class="book"><span class="book-icon" aria-hidden="true">${esc([...book.title][0])}</span><div><strong>${esc(book.title)}</strong><p>${esc(bookMeta(book))}</p><small><span class="status ${book.available ? '' : 'neutral'}">${esc(label)}</span>${overdue ? '<span class="status caution">状态可能陈旧</span>' : ''}</small></div><button data-book="${esc(book.edition_id)}" aria-label="选择 ${esc(book.title+' '+(book.volume || book.year || ''))}">选择版本</button></article>`;
  }).join('') : `<p class="empty">${settings.apiOnline ? '没有匹配的版本，可调整关键词或检索字段。' : '缓存中没有匹配的版本，联网后可检索完整书目。'}</p>`;
  $('pagination').hidden = data.total <= limit;
  $('previous-page').disabled = page === 1;
  $('next-page').disabled = page * limit >= data.total;
  $('page-label').textContent = `第 ${page} 页 / 共 ${Math.ceil(data.total/limit)} 页`;
  $('results').querySelectorAll('[data-book]').forEach(button => button.onclick = () =>
    run(() => openBook(data.items.find(book => book.edition_id === button.dataset.book))));
}

async function openBook(book) {
  const bookID = book.edition_id;
  let copies;
  try { copies = (await api('/editions/' + encodeURIComponent(bookID) + '/holdings')).items; }
  catch (error) {if (error.status) throw error; copies = cached.copies[bookID] || [];}
  pendingBook = book;
  const available = copies.filter(item => item.status === 'available');
  $('copies').innerHTML = '<option value="">同版任一可用复本</option>' +
    available.map(item => `<option value="${esc(item.holding_id)}">${esc(item.call_number || '无索书号')} · ${esc(item.holding_id)}</option>`).join('');
  $('choose-book').disabled = !book.available;
  $('choose-book').textContent = book.available ? '确认版本' : '无可用复本';
  $('copy-picker').hidden = false;
  $('copies').focus();
}

async function loadMap() {
  const floor = markerPath[1] || settings.floor;
  try {
    map = await api('/maps/' + encodeURIComponent(floor));
    save('map:' + floor, map);
  } catch (error) {
    if (error.status) throw error;
    map = read('map:' + floor, null);
  }
  if (map) {
    start = map.start;
    const choices = map.document.nodes.filter(node => node.kind === 'entrance').map(node => [node.id,node.label]);
    if (actual && map.document.faces.some(item => item.id === actual)) choices.push([actual,'上个扫码点 · ' + actual]);
    $('route-start').innerHTML = choices.map(([id,label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('');
    $('route-face').innerHTML = '<option value="">选择架面</option>' + map.document.faces.filter(item => item.marker_status !== 'retired').map(item =>
      `<option value="${esc(item.id)}">${esc(item.label)} · ${esc(item.direction)}向</option>`).join('');
    $('floor-label').textContent = map.document.name;
    $('floor-tag').textContent = map.floor;
    $('help-text').textContent = '可前往' + map.document.service_label + '。';
    $('footer-context').textContent = map.revision ? `地图 v${map.revision} · ${stamp(map.updated_at)}` : '暂无已发布地图';
  }
  showMap();
}

function showMap() {
  const path = map ? route(map.document,start,face) : [];
  $('manual-face-form').hidden = Boolean(map?.document.faces.length);
  if (map?.revision) {
    $('map-view').innerHTML = mapSvg(map.document,{start,face,path,interactive:true});
    $('map-view').querySelectorAll('[data-face]').forEach(item => {
      const select = () => selectFace(item.dataset.face);
      item.onclick = select;
      item.onkeydown = event => {if (['Enter',' '].includes(event.key)) {event.preventDefault(); select();}};
    });
  } else {
    $('map-view').innerHTML = '<p class="empty">暂无可用的楼层地图</p>';
  }
  $('route-status').textContent = face ? path.length
    ? `${path[0].label} → ${face} · ${path.length === 1 ? '已到达' : '按绿色路线前往'}`
    : `${face} 暂无可通行路线，请前往${map?.document.service_label || '楼层入口服务台'}。`
    : `请选择要前往的架面。${!map?.revision ? '可手动输入架面编号。' : ''}`;
  $('route-face').value = face;
}

function selectFace(id) {
  if (id !== face && position >= 2) {
    task = null;
    sessionKey = uid();
    lastOrder = null;
    $('check-receipt').hidden = true;
    message($('feedback-result'),'');
    resetPhoto();
    progress(1);
  }
  face = id;
  showMap();
  showTarget();
  saveTask();
}

function showTarget() {
  $('target-card').hidden = !edition;
  $('target-info').innerHTML = edition ? `<strong>${esc(edition.title)}</strong><p class="small">${esc(bookMeta(edition))} · ${esc(copy || '同版任一可用复本')}</p>` : '';
  $('session-info').textContent = task ? '会话号 ' + task.session_id : '';
}

function parseMarker(value) {
  if (/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(value)) return value.toUpperCase();
  let uri;
  try {uri = new URL(value);} catch {throw new Error('请扫描正确的架标或输入架面编号。');}
  const parts = uri.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  if (uri.protocol === 'diw:' && uri.hostname === 'shelf' && parts.length === 3) parts.unshift('shelf');
  if (parts.length !== 4 || parts[0] !== 'shelf') throw new Error('请扫描正确的架标。');
  if (parts[1] !== settings.library || parts[2] !== (map?.floor || settings.floor)) throw new Error('此架标属于其他馆或楼层。');
  return parts[3].toUpperCase();
}

async function arrive(raw, fromMarker = false) {
  const id = parseMarker(raw);
  if (!face && fromMarker) selectFace(id);
  if (!face) throw new Error('请先在导览中选择目标架面，或直接扫描架标。');
  const floor = map?.floor || settings.floor;
  let location;
  try {
    location = await api('/shelf-faces/' + encodeURIComponent(id) + '?' + new URLSearchParams({floor, expected:face}));
  } catch (error) {
    if (error.status) throw error;
    location = map?.document.faces.find(item => item.id === id && item.marker_status !== 'retired');
    if (!location && map?.revision === 0) location = {id, direction:null};
    if (!location) throw new Error('离线地图中找不到此架面，请前往服务台。');
  }
  scanned = raw;
  actual = location.id;
  saveTask();
  if (![...$('route-start').options].some(item => item.value === actual) && map?.document.faces.some(item => item.id === actual)) {
    $('route-start').add(new Option('上个扫码点 · ' + actual, actual));
  }
  if (actual !== face) {
    message($('arrival-message'), `实际扫码位置为 ${actual}，所选架面为 ${face}。可改选，或将实际位置设为路线起点。`, true);
    $('adopt-face').hidden = false;
    return;
  }
  if (!edition) {
    message(sticky(), '架面已确认，请先选择图书版本。');
    return;
  }
  $('adopt-face').hidden = true;
  const body = {edition_id:edition.edition_id, holding_id:copy, floor, face_id:face, scanned_face_id:raw};
  const localId = 'local-' + sessionKey;
  try {
    task = await api('/sessions',{method:'POST',body,key:sessionKey});
  } catch (error) {
    if (error.status) throw error;
    if (!queue.some(item => item.localId === localId)) {
      queue.push({path:'/sessions',body,key:sessionKey,localId});
      save('queue',queue);
    }
    task = {session_id:localId, face:location};
  }
  message($('arrival-message'), `已确认 ${settings.library} 馆 · ${floor} · ${actual}`);
  $('face-direction').textContent = location.direction ? `架面朝${location.direction}，位置按读者面对书架的视角编号。` : '位置按读者面对书架的视角编号。';
  showTarget();
  saveTask();
  progress(2,'capture');
}

function resetPhoto() {
  if (photo?.url) URL.revokeObjectURL(photo.url);
  photo = null;
  sample = '';
  $('photo-file').value = '';
  $('photo').hidden = true;
  $('overlays').innerHTML = '';
  $('recognition-result').innerHTML = '';
  $('verification-result').innerHTML = '';
  $('clear-photo').hidden = true;
  $('recognize').disabled = true;
}

function displayPhoto(url) {
  $('shelf-photo').src = url;
  $('photo').hidden = false;
  $('clear-photo').hidden = false;
  $('recognize').disabled = false;
}

function drawResult(data) {
  const description = {unique:'定位到一本高可信候选，请取书核验', confirm:'有待确认的候选，请查看可见区别并扫码', retake:'建议重新拍摄', unrecognized:'本次未识别到目标'};
  $('overlays').innerHTML = data.candidates.map((item,i) => {
    const x = Math.min(...item.quad.map(p => p[0]))*1000+9, y = Math.min(...item.quad.map(p => p[1]))*1000+43;
    return `<polygon class="${data.state === 'unique' ? '' : 'alternate'}" points="${item.quad.map(([a,b]) => a*1000+','+b*1000).join(' ')}"/><text x="${x}" y="${y}">${i+1}</text>`;
  }).join('');
  $('recognition-result').innerHTML = `<p class="notice ${data.state === 'unique' ? '' : 'error'}">${esc(description[data.state])}${data.quality.length ? ' · '+esc(data.quality.join('；')) : ''}</p>` +
    data.candidates.map((item,i) => `<article class="candidate"><strong>${i+1} 号框 · 画面第 ${item.row} 排，${item.column_scope === 'shelf_row' ? '本层' : '画面'}左起第 ${item.column} 本</strong><p>读到 ${esc(item.text.replace(/\n/g,' · '))}</p><p>${esc(item.evidence.join('、'))} · ${Math.round(item.score*100)} 分${item.alternatives.length ? ' · 可比对 '+esc(item.alternatives.map(value => [value.title,value.volume,value.publisher,value.year].filter(Boolean).join(' ')).join('；')) : ''}</p></article>`).join('') +
    `<p class="small">${esc(data.model_version)} · ${esc(data.source)} · ${data.duration_ms} 毫秒</p>`;
  $('reason').value = data.state === 'retake' ? 'retake' : 'unrecognized';
  if (data.candidates.length) progress(3);
}

async function recognize() {
  if (!task || task.session_id.startsWith('local-')) throw new Error('网页版需要联网识别，安卓端支持本机识别。');
  if (photo && (!settings.cloud_review || !$('upload-consent').checked)) throw new Error('请单独同意上传本次照片，或使用安卓端本机识别。');
  const btn = $('recognize');
  btn.disabled = true;
  btn.textContent = '正在检测书脊…';
  try {
    const body = {capture_scope:$('capture-scope').value, upload_consent:!!photo && $('upload-consent').checked};
    if (photo) body.image_base64 = await asBase64(photo.file);
    else body.demo_sample = sample;
    const binding = JSON.stringify(body);
    if (recognize.binding !== binding) {recognize.binding = binding; recognize.key = uid();}
    const result = await api(`/sessions/${task.session_id}/recognitions`,{method:'POST',body,key:recognize.key});
    recognize.binding = null;
    drawResult(result);
    if (result.state !== 'retake' && result.state !== 'unrecognized') $('verify').hidden = false;
  } finally {btn.disabled = false; btn.textContent = '识别书脊';}
}

async function verify(barcode) {
  if (!task) return;
  const body = {barcode};
  const binding = JSON.stringify([task.session_id,body]);
  if (verify.binding !== binding) {verify.binding=binding;verify.key=uid();}
  const result = await api(`/sessions/${task.session_id}/verifications`,{method:'POST',body,key:verify.key});
  verify.binding = null;
  const entry = result.current;
  const title = {correct:'条码核对正确',wrong:'拿到的版本或复本与目标不符',pending:'缺少可用证据，待核验'};
  const actualBook = entry.actual;
  $('verification-result').innerHTML = `<p class="notice ${entry.outcome==='correct' ? '' : 'error'}">${title[entry.outcome]}</p>` +
    (entry.expected ? `<p class="small">目标 ${esc([entry.expected.title,bookMeta(entry.expected),copy].filter(Boolean).join(' · '))}</p>` : '') +
    (actualBook ? `<p class="small">实物 ${esc([actualBook.title,bookMeta(actualBook),entry.holding_id].filter(Boolean).join(' · '))}</p>` : '') +
    `<p class="small">首拿 ${title[result.first.outcome]} · 最终 ${result.final ? '已核对正确' : '待核验'}</p>`;
  if (entry.outcome === 'wrong') $('reason').value = 'wrong_edition';
}

async function sendFeedback() {
  if (!task) return;
  const body = {session_id:task.session_id, reason:$('reason').value, note:$('feedback-note').value.trim()};
  const binding = JSON.stringify(body);
  if (sendFeedback.binding !== binding) {sendFeedback.binding=binding;sendFeedback.key=uid();}
  const key = sendFeedback.key;
  try {
    if (body.session_id.startsWith('local-')) throw new TypeError('offline');
    const data = await api('/feedback',{method:'POST',body,key});
    showFeedback(data);
  } catch (error) {
    if (error.status) throw error;
    if (!queue.some(item => item.key === key)) {
      queue.push({path:'/feedback',body,key});
      save('queue',queue);
    }
    message($('feedback-result'),'反馈已离线排队，联网后自动重传。');
    refreshNetwork();
  }
}

function showFeedback(result) {
  if (result.workorder_id) lastOrder = result.workorder_id;
  saveTask();
  $('check-receipt').hidden = !lastOrder;
  message($('feedback-result'), result.workorder_id ? `工单 ${lastOrder} · ${status[result.status]}` : '已保存自报反馈。');
}

async function flush() {
  if (flushing || !navigator.onLine || !queue.length) return;
  flushing = true;
  try {
    while (queue.length) {
      const item = queue[0];
      const result = await api(item.path,{method:'POST',body:item.body,key:item.key});
      if (item.localId) {
        for (const pending of queue) if (pending.body.session_id === item.localId) pending.body.session_id = result.session_id;
        if (task?.session_id === item.localId) {task = result;showTarget();saveTask();}
      } else if (task?.session_id === item.body.session_id) showFeedback(result);
      queue.shift();
      save('queue',queue);
    }
    settings.apiOnline = true;
  } catch (error) {
    if (error.status) message(sticky(),'有一条待同步记录需要处理：' + error.message,true);
  } finally {flushing=false;refreshNetwork();}
}

async function run(callback) {
  message(sticky(),'');
  try {await callback();}
  catch (error) {message(sticky(), error.status ? error.message : (error instanceof TypeError ? '服务暂不可用，请检查连接或稍后重试。' : error.message), true);}
}

$('search').addEventListener('submit',event => {event.preventDefault();run(() => search());});
document.querySelectorAll('[data-query]').forEach(button => button.onclick = () => {$('query').value=button.dataset.query;$('search-field').value='all';run(() => search());});
$('previous-page').onclick = () => run(() => search(catalogPage-1));
$('next-page').onclick = () => run(() => search(catalogPage+1));
$('choose-book').onclick = () => run(async () => {
  edition = pendingBook;
  copy = $('copies').value || null;
  task = null;
  sessionKey = uid();
  lastOrder = null;
  $('check-receipt').hidden = true;
  message($('feedback-result'),'');
  resetPhoto();
  showTarget();
  saveTask();
  progress(1,markerPath.length ? '' : 'arrival');
  if (scanned && actual === face) await arrive(scanned,true);
});
$('route-start').onchange = () => {start=$('route-start').value;showMap();};
$('route-face').onchange = () => selectFace($('route-face').value);
$('manual-face-form').onsubmit = event => {event.preventDefault();run(() => selectFace(parseMarker($('manual-face').value.trim())));};
$('shelf-form').onsubmit = event => {event.preventDefault();run(() => arrive($('shelf-marker').value.trim()));};
$('adopt-face').onclick = () => run(() => {selectFace(actual);return arrive(scanned,true);});
for (const [input, destination, submit] of [['scan-shelf','shelf-marker',value => arrive(value,true)],['scan-book','barcode',verify]]) {
  $(input).onchange = () => run(async () => {
    const file=$(input).files[0]; if (!file) return;
    try {const value=await readBarcode(file);$(destination).value=value;await submit(value);}
    finally {$(input).value='';}
  });
}
$('photo-file').onchange = () => run(() => {
  const file = $('photo-file').files[0];
  if (!file) return;
  if (file.size > 8*1024*1024) throw new Error('照片超过 8 MB，请缩小图片或重新拍摄。');
  resetPhoto();
  progress(2);
  $('upload-consent').checked = false;
  photo = {file,url:URL.createObjectURL(file)};
  displayPhoto(photo.url);
});
document.querySelectorAll('[data-sample]').forEach(button => button.onclick = () => {
  resetPhoto(); progress(2); sample=button.dataset.sample; displayPhoto('/assets/samples/'+sample+'.jpg');
});
$('clear-photo').onclick = () => {resetPhoto();progress(2);};
$('recognize').onclick = () => run(recognize);
$('manual-verify').onclick = () => progress(3,'verify');
$('verify-form').onsubmit = event => {event.preventDefault();run(() => verify($('barcode').value.trim()));};
$('verify-pending').onclick = () => run(() => verify(null));
$('feedback-form').onsubmit = event => {event.preventDefault();run(sendFeedback);};
$('check-receipt').onclick = () => run(async () => {
  const data=await api('/workorders/'+lastOrder+'?'+new URLSearchParams({session_id:task.session_id}));
  message($('feedback-result'),[status[data.status],findings[data.finding],data.receipt].filter(Boolean).join(' · '));
});
window.addEventListener('offline',refreshNetwork);
window.addEventListener('online',() => {flush();refreshNetwork();});
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

run(async () => {
  try {settings={...await api('/config'),apiOnline:true};save('config',settings);}
  catch {settings.apiOnline=false;}
  configUi();
  const saved=read('active-task',null);
  if (saved?.library === settings.library && !markerPath.length && !prefilled) {
    ({edition,copy,face,actual,scanned,task,sessionKey,lastOrder}=saved);
    showTarget();
    if (edition) progress(task ? 2 : 1);
    if (task?.face?.direction) $('face-direction').textContent = `架面朝${task.face.direction}，位置按读者面对书架的视角编号。`;
    if (lastOrder) {
      $('check-receipt').hidden=false;
      message($('feedback-result'),'上次提交的工单 ' + lastOrder);
    }
  }
  refreshNetwork();
  const initial = await Promise.allSettled([loadMap(),search(),cacheCatalog(),flush()]);
  if (initial[1].status === 'rejected') throw initial[1].reason;
  if (cached.at) $('catalog-time').textContent = '缓存于 ' + stamp(cached.at);
  if (markerPath.length && markerPath[0] === settings.library) {
    $('shelf-marker').value = face;
    message(sticky(), `已从架面 ${face} 开始，请选择图书版本。`);
  }
});
