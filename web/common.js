export const $ = id => document.getElementById(id);
export const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export const stamp = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '暂无';
export const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2,'0')).join('');
export const status = {available:'可借', loaned:'已借出', unavailable:'当前不可借', unknown:'状态未知',
  open:'待接单', assigned:'已接单', investigating:'现场核查', closed:'已关闭'};
export const findings = {on_shelf:'馆员已核实在架', misplaced:'馆员已核实位置异常', marker_damaged:'馆员已核实架标损坏', unresolved:'核查后仍无法判断'};
export const reasons = {unrecognized:'本次未识别', retake:'连续重拍', wrong_edition:'版本或复本不符', marker_damage:'架标损坏', suspected_misplaced:'疑似错架'};
export const bookMeta = book => [book.author, book.publisher, book.year ? book.year + ' 年' : '', book.volume, book.isbn ? 'ISBN ' + book.isbn : ''].filter(Boolean).join(' · ');
export function read(key, fallback) {
  try {return JSON.parse(localStorage.getItem('diw:' + key)) ?? fallback;} catch {return fallback;}
}
export function save(key, value) {localStorage.setItem('diw:' + key, JSON.stringify(value));}
export function message(element, text, error = false) {
  element.hidden = !text;
  element.className = 'notice' + (error ? ' error' : '');
  element.textContent = text;
}

export class ApiError extends Error {
  constructor(status, text) {super(text); this.status = status;}
}

export async function api(path, {method='GET', body, key, token, raw=false, signal} = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (key) headers['Idempotency-Key'] = key;
  if (token) headers.Authorization = 'Bearer ' + token;
  const response = await fetch(path, {method, headers, body:body !== undefined ? JSON.stringify(body) : undefined, signal});
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))).detail;
    const text = Array.isArray(detail) ? detail.map(item => item.loc.slice(1).join('.') + ' ' + item.msg).join('；') : detail;
    throw new ApiError(response.status, text || '请求失败（' + response.status + '）');
  }
  return raw ? response : response.json();
}

export function route(document, start, faceID) {
  const nodes = new Map(document.nodes.map(point => [point.id, point]));
  const target = document.faces.find(face => face.id === faceID && face.marker_status !== 'retired')?.node_id;
  const origin = document.faces.find(face => face.id === start)?.node_id || start;
  if (!nodes.has(origin) || !nodes.has(target)) return [];
  const distance = (a, b) => Math.hypot(nodes.get(a).x-nodes.get(b).x, nodes.get(a).y-nodes.get(b).y);
  const neighbors = new Map(document.nodes.map(node => [node.id, []]));
  for (const {a,b,accessible} of document.edges) if (accessible && nodes.has(a) && nodes.has(b)) {
    neighbors.get(a).push(b);
    neighbors.get(b).push(a);
  }
  const open = new Set([origin]), costs = new Map([[origin,0]]), previous = new Map();
  while (open.size) {
    const current = [...open].sort((a,b) => costs.get(a)+distance(a,target)-costs.get(b)-distance(b,target))[0];
    if (current === target) {
      const result = [current];
      while (previous.has(result.at(-1))) result.push(previous.get(result.at(-1)));
      return result.reverse().map(id => nodes.get(id));
    }
    open.delete(current);
    for (const next of neighbors.get(current)) {
      const cost = costs.get(current) + distance(current,next);
      if (cost >= (costs.get(next) ?? Infinity)) continue;
      costs.set(next,cost);
      previous.set(next,current);
      open.add(next);
    }
  }
  return [];
}

export function mapSvg(document, {start='', face='', path=[], interactive=false, showNodes=false} = {}) {
  const nodes = new Map(document.nodes.map(point => [point.id,point]));
  const actualStart = document.faces.find(item => item.id === start)?.node_id || start;
  const u = n => Number(n)*3.6, v = n => Number(n)*2.48;
  const line = (a,b,add='') => `<line x1="${u(a.x)}" x2="${u(b.x)}" y1="${v(a.y)}" y2="${v(b.y)}" ${add}/>`;
  const guide = document.edges.filter(edge => edge.accessible && nodes.has(edge.a) && nodes.has(edge.b)).map(edge =>
    line(nodes.get(edge.a), nodes.get(edge.b), 'stroke="#aabfb0" stroke-width="1.2" stroke-dasharray="3 4"')).join('');
  const blocks = (document.blocks || []).map(block => `<rect x="${u(block.x)}" y="${v(block.y)}" width="${u(block.width)}" height="${v(block.height)}" rx="3" fill="#dce8dd" stroke="#bccfc0"/><text x="${u(Number(block.x)+Number(block.width)/2)}" y="${v(Number(block.y)+Number(block.height)/2)+2.5}" text-anchor="middle" fill="#4c6b5c" font-size="8">${esc(block.label)}</text>`).join('');
  const places = document.nodes.filter(node => showNodes || node.kind === 'entrance' || node.kind === 'service' || node.id === actualStart).map(node => {
    const color = node.kind === 'entrance' || node.id === actualStart ? '#537aa4' : '#739480';
    return `<circle cx="${u(node.x)}" cy="${v(node.y)}" r="4" fill="${color}"/><text x="${u(node.x)}" y="${v(node.y)+14}" text-anchor="middle" fill="#334a41" font-size="8.5" paint-order="stroke" stroke="#f3f6f0" stroke-width="2">${esc(showNodes ? node.id : node.label)}</text>`;
  }).join('');
  const faces = document.faces.filter(item => item.marker_status !== 'retired' && nodes.has(item.node_id)).map(item => {
    const point = nodes.get(item.node_id);
    const selected = item.id === face;
    return `<g ${interactive ? 'role="button" tabindex="0"' : ''} aria-label="${esc(item.label)}" data-face="${esc(item.id)}"><circle cx="${u(point.x)}" cy="${v(point.y)}" r="13" fill="transparent"/><circle cx="${u(point.x)}" cy="${v(point.y)}" r="${selected ? 6.1 : 4}" fill="${selected ? '#d37b3e' : '#fff'}" stroke="${selected ? '#a05e2c' : '#86a691'}" stroke-width="1.4"/><text x="${u(point.x)}" y="${v(point.y)-8}" text-anchor="middle" fill="${selected ? '#8d481a' : '#3a5648'}" font-size="8" font-weight="700" paint-order="stroke" stroke="#f3f6f0" stroke-width="2">${esc(item.id)}</text></g>`;
  }).join('');
  const background = document.image && /^[A-Za-z0-9+/=]+$/.test(document.image)
    ? `<image href="data:image/jpeg;base64,${document.image}" x="0" y="0" width="360" height="248" preserveAspectRatio="none"/>`
    : `<rect x="14" y="16" width="332" height="213" rx="7" fill="#f6f8f1" stroke="#d8e4d7"/><path d="M14 205h332M14 47h332" stroke="#e5ece2" stroke-dasharray="2 5"/><text x="24" y="34" font-size="8" fill="#526b5c">开架借阅区</text>`;
  return `<svg viewBox="0 0 360 248" ${interactive ? 'aria-label="楼层地图，可选择架面"' : 'role="img" aria-label="楼层地图"'} xmlns="http://www.w3.org/2000/svg">${background}${blocks}${guide}<polyline points="${path.map(point => u(point.x)+','+v(point.y)).join(' ')}" fill="none" stroke="#16825e" stroke-width="3.4" stroke-linejoin="round" stroke-linecap="round"/>${places}${faces}</svg>`;
}

export function asBase64(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export async function readBarcode(file) {
  if (!('BarcodeDetector' in window)) throw new Error('此浏览器未提供扫码功能，请手动输入编号。');
  const bitmap = await createImageBitmap(file);
  try {
    const result = await new BarcodeDetector().detect(bitmap);
    if (!result.length) throw new Error('未读到条码，请重新拍摄或手动输入。');
    const values = [...new Set(result.map(item => item.rawValue))];
    if (values.length !== 1) throw new Error('画面中有多个条码，请只拍需要核验的一个。');
    return values[0];
  } finally {bitmap.close();}
}
