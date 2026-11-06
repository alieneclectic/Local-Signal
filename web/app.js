'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
 speaker:'<rect x="5" y="2" width="14" height="20" rx="3"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="6" r="1"/>',
 tv:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>',
 overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
 grid:'<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M3 10h18M10 10v11"/>',
 chart:'<path d="M3 3v18h18M6 15l5-5 4 3 6-8"/>',
 network:'<rect x="8" y="2" width="8" height="6" rx="1.5"/><rect x="2" y="16" width="8" height="6" rx="1.5"/><rect x="14" y="16" width="8" height="6" rx="1.5"/><path d="M12 8v4M6 16v-4h12v4"/>',
 settings:'<path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 2 3h3l1-3 3-1 2-3-2-2 1-3-2-3-3 1-2-3z"/><circle cx="12" cy="12" r="3"/>',
 shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6"/>',
 'circle-question':'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 1 1 5 2.2c-1.3 1-2 1.4-2 2.8m0 3h.01"/>',
 'circle-slash':'<circle cx="12" cy="12" r="8"/><path d="m6.35 6.35 11.3 11.3"/>',
 refresh:'<path d="M20 7a9 9 0 0 0-15-2L2 8m0-6v6h6M4 17a9 9 0 0 0 15 2l3-3m0 6v-6h-6"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
 chevron:'<path d="m9 5 7 7-7 7"/>',
 down:'<path d="m6 9 6 6 6-6"/>',
 plug:'<path d="M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0zm6 10v4"/>',
 sensor:'<rect x="5" y="3" width="14" height="18" rx="5"/><circle cx="12" cy="11" r="3"/><path d="M10 17h4"/>',
 wifi:'<path d="M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a6 6 0 0 1 7 0M12 19h.01"/>',
 activity:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
 scan:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M7 12h10M12 7v10"/>',
 download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
 close:'<path d="m6 6 12 12M6 18 18 6"/>',
 temp:'<path d="M9 14.5V5a3 3 0 0 1 6 0v9.5a5 5 0 1 1-6 0Z"/><path d="M12 8v10"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
 pin:'<path d="m9 3 10 10-4 1-2 4-3-3-6 6m3-13 2-5m-2 5-3 2 9 8"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 moon:'<path d="M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10Z"/>',
 server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M12 6.5h5M12 17.5h5"/>'
};
const icon = (name, extra='') => `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
function hydrateIcons(root=document) { $$('[data-icon]', root).forEach(el => { el.innerHTML = icon(el.dataset.icon); el.removeAttribute('data-icon'); }); }
hydrateIcons();
const state = {devices:[], firmware:[], firmware_events:[], collector:{network:{}}, events:[], samples:[], page:'overview', filter:'all', query:'', range:24, metric:'latency_ms', connected:false, initial:true, unit:localStorage.getItem('hearth-unit') || 'F', chartDevices:new Set(), lastHistory:null, historyError:null};
const colors = ['#2e6a51','#9eac58','#d09460','#7895b0','#9b86ae','#c17e85','#528f90','#b5a384']
 .map((color, i) => `var(--chart-series-${i+1}, ${color})`);
let loading = false, toastTimer, detailIP = null;
const openDeviceCards = new Set();
const deviceFilters = [
 ['all','All devices'], ['saved','My devices'], ['switch','Switches'], ['sensor','Sensors'],
 ['media','Media'], ['unidentified','Other devices'], ['firmware','Firmware'], ['updates','Updates needed']
];
const checkingFirmwareCatalogs = new Set(), firmwareCatalogRetryAt = new Map();
const firmwareCatalogLabels = {shelly:'Shelly catalog', wiim:'WiiM release notes', sonos:'Sonos release notes', nest:'Google production releases', cast:'Chromecast release notes'};
const firmwareGuides = {
 shelly: {label:'Shelly web interface guide', url:'https://www.shelly.com/blogs/documentation/web-interface-guides',
  steps:'Open the device’s web page and look for Firmware in Settings. Wake battery sensors first.'},
 wiim: {label:'WiiM update guide', url:'https://faq.wiimhome.com/en/support/solutions/articles/72000528782-how-to-update-firmware-on-your-wiim-device',
  steps:'In WiiM Home, go to Devices → Device Settings → Device Info → Check for Updates.'},
 sonos: {label:'Sonos update guide', url:'https://support.sonos.com/en-us/article/update-your-sonos-speakers',
  steps:'In the Sonos app, go to Settings → System → System Updates. Tap Update if it appears.'},
 roku: {label:'Roku update guide', url:'https://support.roku.com/en-us/article/update-the-software-on-your-streaming-device',
  steps:'On your Roku, go to Settings → System → Software update → Check Now.'},
 nest: {label:'Google firmware guide', url:'https://support.google.com/googlehome/answer/7365257?hl=en',
  steps:'In Google Home, touch and hold the device’s tile, then open Settings → Device information. Keep the device powered and online for automatic updates.'},
 cast: {label:'Google Cast firmware guide', url:'https://support.google.com/chromecast/answer/7124014?hl=en',
  steps:'In Google Home, touch and hold the device’s tile, then open Settings → Device information → Technical information.'},
 riva: {label:'RIVA Audio firmware guide', url:'https://www.rivaaudio.com/firmware-updates',
  steps:'To check for speaker updates, open RIVA Voice (iOS) or Riva Audio (Android) on the same Wi-Fi network. Select your Festival, open its settings, and look for Update Firmware at the bottom. The version shown here is the speaker’s Cast build.'},
 myq: {label:'myQ device settings guide', url:'https://support.chamberlaingroup.com/s/article/myQ-app-Navigating-to-Device-Settings',
  steps:'In the myQ app, open Settings or Menu → Devices. Under Hubs, choose your opener or hub, then select the paired opener if shown. Look for the firmware version in Device Settings.'},
};
const timeAgo = (value) => { if (!value) return 'Not yet'; const s = Math.max(0, (Date.now()-Date.parse(value))/1000); return s<15?'Just now':s<60?`${Math.floor(s)}s ago`:s<3600?`${Math.floor(s/60)}m ago`:s<86400?`${Math.floor(s/3600)}h ago`:`${Math.floor(s/86400)}d ago`; };
const dateTime = value => value ? new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : 'Not yet';
const fmt = (n, digits=1) => Number.isFinite(n) ? n.toLocaleString([], {maximumFractionDigits:digits}) : '—';
const metric = (d,k) => d.metrics?.[k]?.value;
const reporting = () => state.devices.filter(d=>d.status==='online');
const firmwareUpdateCount = () => state.firmware.filter(d=>d.stable_status==='update_needed').length;
const reachable = () => state.devices.filter(d=>['online','reachable','auth_required'].includes(d.status));
const metricFormat = (m) => { if (!m) return '—'; if(typeof m.value==='string')return esc(m.value);if(m.kind==='boolean')return m.value?'Yes':'No';if(m.unit==='s'&&m.label?.startsWith('Track')){const seconds=Math.round(m.value);return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;} if (m.unit==='°C' && state.unit==='F') return `${fmt(m.value*9/5+32)} °F`; if (m.unit==='s') { const h=m.value/3600; return h<24?`${fmt(h)} hours`:`${fmt(h/24)} days`; } return `${fmt(m.value, m.unit==='kWh'?4:1)} ${esc(m.unit)}`; };
function statusInfo(d) {
 if(d.status==='online') return ['Reporting','online'];
 if(d.status==='reachable') return ['Reachable','reachable'];
 if(d.status==='auth_required') return ['Auth required','attention'];
 if(d.status==='unreachable' && d.expected_sleep) return ['May be asleep','sleeping'];
 if(d.status==='unreachable') return ['Unreachable','offline'];
 return ['Checking','pending'];
}
function badge(d) { const [label, cls]=statusInfo(d); return `<span class="status-badge ${cls}"><i class="dot"></i>${label}</span>`; }
function deviceModel(d) { return d.model||d.advertised_model||(d.discovered_name?'Network device':'Unidentified device'); }
function protocolLabel(p) { return ({'shelly-gen1':'Shelly Gen 1','shelly-rpc':'Shelly RPC','sonos-upnp':'Sonos UPnP','upnp-av':'UPnP AV','roku-ecp':'Roku ECP','google-cast':'Google Cast'})[p]||p||'Not identified'; }
function mediaState(d) {const m=d.metrics||{};if(m.playback_state)return {value:m.playback_state.value,label:m.playback_state.label};if(m.power_mode)return {value:m.power_mode.value,label:'Player power mode'};if(m.player_role)return {value:m.player_role.value==='No independent transport'?'Component':m.player_role.value,label:'Player role'};if(m.receiver_state)return {value:`Cast ${String(m.receiver_state.value).toLowerCase()}`,label:'Cast receiver'};return {value:'Unavailable',label:'Device status'};}
function mediaLevel(d) {const m=d.metrics||{},v=m.volume_pct||m.component_volume_pct;if(!v)return '—';return `${m.component_volume_pct?'Component ':v.label?.startsWith('Cast')?'Cast ':''}${fmt(v.value)}%${m.muted?.value?' · Muted':''}`;}
function deviceIcon(d) { if(d.kind==='media')return d.protocol==='roku-ecp'||(d.advertised_model||d.model||'').toLowerCase()==='chromecast'?'tv':'speaker';return d.kind==='switch'?'plug':d.kind==='sensor'?'sensor':d.kind==='gateway'?'wifi':'grid'; }
function toast(message, error=false) { const el=$('#toast'); el.textContent=message; el.className=`toast visible ${error?'error':''}`; clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove('visible'),4500); }
async function api(path, options={}) {
 const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json','X-Hearth-Request':'1',...options.headers}});
 const data=await response.json(); if(!response.ok) throw new Error(data.error||'The request failed.'); return data;
}
async function load(force=false) {
 if(loading) return; loading=true;
 try {
  const data=await api('/api/state');
  if(state.dev_revision && data.dev_revision && state.dev_revision!==data.dev_revision) {
   location.reload();
   return;
  }
  const lastEventId=state.firmware_events[0]?.id ?? 0;
  const alerts=state.initial ? [] : (data.firmware_events||[]).filter(e=>e.id>lastEventId);
  Object.assign(state, data); state.connected=true; state.initial=false;
  if(state.page==='history' && (force || state.lastHistory?.poll!==state.collector.last_poll || state.lastHistory?.range!==state.range)) await loadHistory();
  updateChrome(); render();
  if(state.page==='devices') {
   const sources=new Set(state.firmware.filter(d=>d.catalog_id&&!d.catalog_checked_at).map(d=>d.catalog_id));
   for(const source of sources) refreshFirmwareCatalog(source);
  }
  if(alerts.length) toast(alerts.length===1?alerts[0].message:`${alerts.length} firmware updates reported. See Devices for details.`);
 } catch(e) { state.connected=false; updateChrome(); if(state.initial) $('#main').innerHTML=`<div class="empty-state large">${icon('server')}<h1>The collector is taking a moment.</h1><p>Keep the Local Signal server running, then refresh the page.</p><button class="button primary" id="retry-load">Try again</button></div>`; else if(state.page==='devices'||state.page==='network') render(); }
 finally { loading=false; }
}
async function refreshFirmwareCatalog(source) {
 if(checkingFirmwareCatalogs.has(source) || Date.now()<(firmwareCatalogRetryAt.get(source)||0)) return;
 checkingFirmwareCatalogs.add(source);
 try {
  const result=await api('/api/firmware/catalog/refresh', {method:'POST',body:JSON.stringify({source})});
  if(result.pending) return;
 }
 catch { firmwareCatalogRetryAt.set(source,Date.now()+60_000); }
 finally { checkingFirmwareCatalogs.delete(source); }
 await load();
}
async function loadHistory() {
 const range=state.range, poll=state.collector.last_poll;
 try {
  const {samples}=await api(`/api/history?hours=${range}`);
  if(range!==state.range)return;
  state.samples=samples;state.lastHistory={poll,range};state.historyError=null;
 } catch(e) { if(range===state.range)state.historyError=e.message; }
}
function updateChrome() {
 const delayed=state.collector.health==='delayed';
 $('#collector-status').innerHTML=`<i class="dot"></i>${state.connected?delayed?'Collection delayed':state.collector.health==='starting'?'Collector starting':'Collector online':'Collector disconnected'}`;
 $('#collector-status').classList.toggle('disconnected',!state.connected||delayed);
 $('#nav-count').textContent=state.devices.length || '0';
 $('#footer-status').textContent=state.connected?`Collected locally · Updated ${timeAgo(state.collector.last_poll)}`:'Collector disconnected · Reconnecting…';
 $('#refresh-button').classList.toggle('spinning',!!state.collector.polling);
}
function heading(title, subtitle, action=true, eyebrow='DEVICE MONITORING') {
 return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${action?`<button class="button secondary scan-button" ${state.collector.scanning?'disabled':''}>${icon('scan', state.collector.scanning?'rotate':'')}${state.collector.scanning?'Discovering…':'Scan network'}</button><button class="button primary add-button">${icon('plus')}Add device</button>`:''}</div></div>`;
}
function connectionBanner() { return !state.connected?`<div class="notice warning">${icon('info')}The collector is disconnected. Displayed readings are from the last connection. Reconnecting automatically.</div>`:state.collector.health==='delayed'?`<div class="notice warning">${icon('info')}Collection is delayed. Last completed ${timeAgo(state.collector.last_poll)}. ${esc(state.collector.collection_error||'Waiting for the next collection cycle.')}</div>`:''; }
function scanBanner() {
 const c=state.collector;
 if(c.scanning) return `<div class="notice">${icon('scan','rotate')}${c.naming?'Discovering device names on':'Discovering devices on'} ${esc(c.network.subnet||'the local network')}<span class="notice-right">${c.scan_progress} / ${c.scan_total||'—'} addresses</span></div>`;
 return c.scan_error?`<div class="notice warning">${icon('info')}${esc(c.scan_error)}</div>`:'';
}
function statCard(label,value,sub,ico,cls='') {
 return `<article class="stat-card ${cls}"><div class="stat-top"><span>${label}</span><span class="stat-icon">${icon(ico)}</span></div><div class="stat-value">${value}</div><div class="stat-sub">${sub}</div></article>`;
}
function overview() {
 const pinned=state.devices.filter(d=>d.pinned).sort((a,b)=>(b.status==='online')-(a.status==='online')||a.name.localeCompare(b.name));
 const live=state.connected?reporting().length:'—', online=state.connected?reachable().length:'—';
 const updates=state.connected?firmwareUpdateCount():'—';
 return `${heading('Overview', 'Discover and monitor devices on your network.')} ${connectionBanner()} ${scanBanner()}
 <section class="stats-grid" aria-label="Device summary">
 ${statCard('Devices in inventory',state.devices.length,`<a href="#devices" class="stat-link">View devices ${icon('arrow')}</a>`,'grid','highlight')}
 ${statCard('Devices reachable',online,`<a href="#network" class="stat-link">View network ${icon('arrow')}</a>`,'wifi')}
 ${statCard('Devices reporting',live,'Providing readings to the collector','activity')}
 ${statCard('Firmware updates',updates,`<a href="#devices/updates" class="stat-link">View updates ${icon('arrow')}</a>`,'shield')}
 </section>
 <section class="section devices-section"><div class="section-heading"><div><h2>My devices <span class="count-badge">${pinned.length}</span></h2><p>Your pinned devices, at a glance.</p></div><a href="#devices" class="text-button">View all devices ${icon('arrow')}</a></div><div class="devices-grid">${pinned.length?pinned.map(deviceCard).join(''):`<div class="panel empty-state wide">${icon('pin')}<p>Pin a device from its details to show it here.</p></div>`}</div></section>
 ${mediaOverview()}
 <div class="overview-bottom">${activityPanel()}<article class="panel insight-panel"><span class="small-kicker">LOCAL COLLECTION</span><span class="insight-icon">${icon('shield')}</span><h3>Your data stays local.</h3><p>Local Signal keeps device readings and history in a local database.</p><a href="#settings" class="text-button">Collector settings ${icon('arrow')}</a></article></div>`;
}
function mediaOverview() {
 const devices=reporting().filter(d=>d.kind==='media');
 if(!devices.length)return '';
 const visible=[...devices].sort((a,b)=>(a.metrics?.playback_state?.value==='Playing'?-1:0)-(b.metrics?.playback_state?.value==='Playing'?-1:0)||a.name.localeCompare(b.name)).slice(0,6);
 return `<section class="panel media-overview"><div class="panel-heading"><div><h2>Media devices</h2><p>Live status from ${devices.length} ${devices.length===1?'media device':'media devices'}.</p></div><a href="#devices/media" class="text-button">View all media ${icon('arrow')}</a></div><div class="table-scroll"><table><thead><tr><th>Device</th><th>Reported state</th><th>Reported level</th><th>App / details</th></tr></thead><tbody>${visible.map(d=>{const m=d.metrics||{},activity=mediaState(d);return `<tr><td><button class="table-device" data-device="${esc(d.ip)}">${icon(deviceIcon(d))}<span><strong>${esc(d.name)}</strong><small>${esc(deviceModel(d))}</small></span></button></td><td>${esc(activity.value)}<small class="media-state-label">${esc(activity.label)}</small></td><td>${mediaLevel(d)}</td><td class="muted">${esc(m.media_title?.value||m.active_app?.value||m.speaker_room?.value||protocolLabel(d.protocol))}</td></tr>`;}).join('')}</tbody></table></div></section>`;
}
function chartPanel() {
 const options=new Map([['latency_ms','Network latency'],['rssi_dbm','Wi-Fi signal'],['uptime_s','Uptime'],['power_w','Power'],['internal_temperature_c','Internal temperature'],['temperature_c','Sensor temperature'],['humidity_pct','Humidity'],['volume_pct','Volume'],['component_volume_pct','Component level']]);
 for(const sample of state.samples) if(!options.has(sample.metric)) options.set(sample.metric,sample.label);
 if(!options.has(state.metric)) options.set(state.metric,state.metric);
 return `<section class="panel"><div class="panel-heading"><div><h2>Readings over time</h2><p>Compare devices across a selected metric and time range.</p></div><a class="button secondary" href="/api/export.csv?hours=${state.range}">${icon('download')}Export CSV</a></div><div class="chart-controls"><label class="select-wrap">${icon('chart')}<select id="metric-select" aria-label="Device metric">${[...options].map(([key,label])=>`<option value="${esc(key)}" ${key===state.metric?'selected':''}>${esc(label)}</option>`).join('')}</select>${icon('down')}</label><div class="segmented" aria-label="Time range">${[[1,'1h'],[6,'6h'],[24,'24h'],[168,'7d']].map(([n,s])=>`<button id="history-range-${n}" class="${state.range===n?'selected':''}" data-range="${n}" aria-pressed="${state.range===n}">${s}</button>`).join('')}</div></div><div class="chart-wrap">${buildChart(options.get(state.metric))}</div><div class="chart-legend">${chartLegend()}</div><div class="chart-note" title="Charts average readings across long ranges. CSV exports contain every collected sample.">${icon('info')} ${state.metric.includes('internal_temperature')?'Internal temperature is measured inside the device.':'Gaps mean no reading was collected. Local Signal saves history while the collector runs.'}</div></section>`;
}
function seriesData() {
 const grouped=new Map();
 for(const sample of state.samples) if(sample.metric===state.metric) { if(!grouped.has(sample.ip)) grouped.set(sample.ip,[]); grouped.get(sample.ip).push(sample); }
 return state.devices.filter(d=>grouped.has(d.ip)).map((d,i)=>({device:d, points:grouped.get(d.ip), color:colors[i%colors.length]}));
}
function chartLegend() {
 const series=seriesData();
 return series.length?series.map(s=>`<button class="legend-item ${state.chartDevices.has(s.device.ip)?'dimmed':''}" data-series="${esc(s.device.ip)}" aria-pressed="${!state.chartDevices.has(s.device.ip)}"><i class="legend-dot" data-color="${s.color}"></i>${esc(s.device.name)}<strong>${metricFormat(s.points.at(-1))}</strong></button>`).join(''):'';
}
function buildChart(metricLabel) {
 if(!state.lastHistory&&!state.historyError) return `<div class="chart-empty">${icon('clock')}<strong>Loading history…</strong></div>`;
 if(state.historyError) return `<div class="chart-empty">${icon('info')}<strong>History couldn’t load</strong><p>${esc(state.historyError)}</p></div>`;
 const series=seriesData().filter(s=>!state.chartDevices.has(s.device.ip));
 const all=series.flatMap(s=>s.points);
 if(!all.length) return `<div class="chart-empty">${icon('chart')}<strong>${state.chartDevices.size?'No visible readings':'No readings for this metric'}</strong><p>${state.chartDevices.size?'Select a device in the legend to show its readings.':'Readings will appear as compatible devices report this metric.'}</p></div>`;
 const compact=window.matchMedia('(max-width: 530px)').matches;
 const w=compact?340:760,h=compact?220:330,left=44,right=18,top=14,bottom=32;
 const unit=all[0].unit==='°C'?`°${state.unit}`:all[0].unit;
 const converted=n=>all[0].unit==='°C'&&state.unit==='F'?n*9/5+32:n;
 const values=all.map(p=>converted(p.value)); let min=Math.min(...values),max=Math.max(...values);
 if(min===max){const pad=state.metric==='power_w'?10:Math.max(1,Math.abs(min)*.08);min-=pad;max+=pad;}
 else {const pad=(max-min)*.18;min-=pad;max+=pad;}
 if(state.metric==='power_w'||state.metric==='latency_ms'||state.metric==='humidity_pct') min=Math.max(0,min);
 const timestamps=all.map(p=>Date.parse(p.at));
 let end=Math.max(...timestamps), start=Math.min(...timestamps);
 // Start with a truthful short time window, then grow until the selected range is filled.
 const span=Math.max(5*60*1000,Math.min(state.range*3600000,end-start));
 start=end-span; end+=10000;
 const x=t=>left+(Date.parse(t)-start)/(end-start)*(w-left-right), y=v=>top+(max-converted(v))/(max-min)*(h-top-bottom);
 let svg=`<svg class="telemetry-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(metricLabel)} history chart, ${esc(unit)}"><defs>${series.map((s,i)=>`<linearGradient id="fill-${i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${s.color}" stop-opacity=".12"/><stop offset="100%" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`).join('')}</defs>`;
 for(let i=0;i<5;i++){const v=min+(max-min)*(4-i)/4, yy=top+(h-top-bottom)*i/4;svg+=`<line class="chart-gridline" x1="${left}" y1="${yy}" x2="${w-right}" y2="${yy}" stroke-dasharray="3 5"/><text x="${left-12}" y="${yy+4}" text-anchor="end">${fmt(v,1)}</text>`;}
 const ticks=compact?2:4;
 for(let i=0;i<=ticks;i++){const t=start+(end-start)*i/ticks,xx=left+(w-left-right)*i/ticks;svg+=`<text x="${xx}" y="${h-7}" text-anchor="${i===0?'start':i===ticks?'end':'middle'}">${(span>86400000?new Date(t).toLocaleString([],{month:'short',day:'numeric',hour:'numeric'}):new Date(t).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}))}</text>`;}
 series.forEach((s,i)=>{
  const step=Math.max(1,Math.floor(s.points.length/300)), points=s.points.filter((_,j)=>j%step===0||j===s.points.length-1);
  const gap=Math.max(90000,((Math.max(...timestamps)-Math.min(...timestamps))/300)*2.5),segments=[];let segment=[];
  for(const point of points) {if(segment.length && Date.parse(point.at)-Date.parse(segment.at(-1).at)>gap){segments.push(segment);segment=[];} segment.push(point);}if(segment.length) segments.push(segment);
  for(const seg of segments){const line=seg.map((p,j)=>`${j?'L':'M'}${x(p.at).toFixed(2)},${y(p.value).toFixed(2)}`).join(' ');if(seg.length>1)svg+=`<path d="${line} L${x(seg.at(-1).at)},${h-bottom} L${x(seg[0].at)},${h-bottom} Z" fill="url(#fill-${i})"/><path d="${line}" fill="none" stroke="${s.color}" stroke-width="2.4" stroke-linejoin="round"/>`;}
  const latest=points.at(-1); svg+=`<circle class="chart-latest" cx="${x(latest.at)}" cy="${y(latest.value)}" r="5" fill="${s.color}" stroke-width="2"><title>${esc(s.device.name)}: ${esc(metricFormat(latest))} at ${esc(dateTime(latest.at))}</title></circle>`;
  // Hover dots expose the actual historical reading without inventing interpolated values.
  if(points.length<160) for(const p of points) svg+=`<circle class="chart-hover-dot" cx="${x(p.at)}" cy="${y(p.value)}" r="6" fill="${s.color}" opacity="0"><title>${esc(s.device.name)} · ${esc(metricFormat(p))} · ${esc(dateTime(p.at))}</title></circle>`;
 });
 svg+=`<text x="${left}" y="${top-3}" class="unit-label">${esc(unit)}</text></svg>`;return svg;
}
function deviceReadout(d) {
 const current=d.metrics||{};
 const m=d.kind==='sensor'?{...(d.last_metrics||{}),...(d.latest_metrics||{}),...current}:current;
 const hasPower=Number.isFinite(metric(d,'power_w'));
 const sensorReading=metrics=>Object.entries(metrics).find(([k])=>k==='temperature_c'||k.startsWith('temperature:'));
 const currentTemperature=d.status==='online'?sensorReading(current):null, temperature=currentTemperature||sensorReading(m);
 let main,second;
 if(d.kind==='media'){const activity=mediaState(d);main=`<span class="card-reading media-reading" title="${esc(activity.value)}">${esc(activity.value)}</span><span class="reading-label">${esc(activity.label)}</span>`;second=`${icon('speaker')}<span>${mediaLevel(d)}</span>`;}
 else if(hasPower){main=`<span class="card-reading">${fmt(m.power_w.value)}<small>W</small></span><span class="reading-label">Current power</span>`;const relay=m.relay_0;second=`<span class="relay-indicator ${relay?.value?'on':''}"></span><span>Relay ${relay?relay.value?'on':'off':'unavailable'}</span>`;}
 else if(temperature){main=`<span class="card-reading">${metricFormat(temperature[1])}</span><span class="reading-label">${currentTemperature?'Sensor temperature':'Last reported temperature'}</span>`;second=`${icon(currentTemperature?'activity':'clock')}<span>${currentTemperature?'Recent sensor report':'Last reported reading'}</span>`;}
 else {main=`<span class="card-reading unavailable">—<small>${d.status==='reachable'?'No API yet':'No readings yet'}</small></span><span class="reading-label">${d.status==='reachable'?'Network connection detected':'Waiting for telemetry'}</span>`;second=`${icon(d.expected_sleep?'moon':'info')}<span>${d.expected_sleep?'Battery device · may sleep':'Device type unconfirmed'}</span>`;}
 const rssi=m.rssi_dbm;
 return `<div class="device-readout">${main}</div>
  <div class="device-card-bottom"><span>${second}</span><span class="signal" title="${rssi?`${esc(rssi.value)} dBm`:'Network latency'}">${icon('wifi')}${rssi?esc(rssi.value):Number.isFinite(metric(d,'latency_ms'))?`${fmt(metric(d,'latency_ms'),0)} ms`:'—'}</span></div>`;
}
function deviceRecency(device) {
 if(device.last_telemetry)return `Updated ${timeAgo(device.last_telemetry)}`;
 return device.last_seen?`Seen ${timeAgo(device.last_seen)}`:'Not yet seen';
}
function deviceCard(d) {
 const [,cls]=statusInfo(d);
 return `<button class="device-card ${cls}" data-device="${esc(d.ip)}">
  <div class="device-card-top"><span class="device-type-icon ${d.kind==='switch'?'sage':''}">${icon(deviceIcon(d))}</span>${badge(d)}</div>
  <div class="device-card-title"><h3 title="${esc(d.name)}">${esc(d.name)}</h3>${icon('chevron')}</div>
  <div class="device-meta">${esc(d.room)} <span>·</span> ${esc(deviceModel(d))}</div>
  ${deviceReadout(d)}
  <div class="device-ip">${esc(d.ip)}<span>${deviceRecency(d)}</span></div>
 </button>`;
}
function activityPanel(full=false, firmwareOnly=false) {
 const events=(firmwareOnly?state.firmware_events:state.events).slice(0,full?50:4);
 const title=firmwareOnly?'Firmware alerts':'Recent activity';
 const description=firmwareOnly?'Recent update notices from your devices.':'Recent changes across monitored devices.';
 const empty=firmwareOnly?'Firmware alerts will appear here when a device reports a new update.':'Device activity will appear here.';
 const icons={discovered:'plus',online:'activity',unreachable:'moon',firmware_update_available:'shield'};
 return `<section class="panel activity-panel"><div class="panel-heading"><div><h2>${title}</h2><p>${description}</p></div><span class="activity-label">${icon('clock')} Live log</span></div><div class="activity-list">${events.length?events.map(e=>`<div class="activity-row"><span class="event-icon ${e.type==='online'?'positive':''}">${icon(icons[e.type]||'wifi')}</span><div><p>${esc(e.message)}</p><small>${esc(e.ip||'Local collector')}</small></div><time title="${esc(dateTime(e.at))}">${timeAgo(e.at)}</time></div>`).join(''):`<div class="empty-state small"><p>${empty}</p></div>`}</div></section>`;
}
function manualFirmwareGuide(device) {
 const names=[device.model, device.advertised_model, device.hostname, device.discovered_name, device.name];
 const myqName=/(?:^|[^a-z0-9])(?:lift[ _-]?master|my[ _-]?q)(?=$|[^a-z0-9])/i;
 return names.some(name=>myqName.test(name||'')) ? firmwareGuides.myq : null;
}
function firmwareStatus(finding, device) {
 if(!finding)return manualFirmwareGuide(device) ? 'manual' : 'unsupported';
 if(finding.stable_status!=='unverified')return finding.stable_status;
 const source=finding.catalog_id;
 const waitingForReport=!finding.installed_version && !finding.checked_at;
 const waitingForCatalog=state.connected && state.page==='devices' && source && !finding.catalog_checked_at
   && (checkingFirmwareCatalogs.has(source) || Date.now()>=(firmwareCatalogRetryAt.get(source)||0));
 return waitingForReport || waitingForCatalog ? 'checking' : 'unverified';
}
function firmwareBadge(status, labelOverride) {
 const statuses={
  current:['Current','online'], update_needed:['Update needed','update-needed'],
  newer_published:['Newer release published','release-published'],
  checking:['Checking firmware…','pending'], unverified:['Unverified','pending','circle-question'],
  manual:['Manual check','pending','circle-question'],
  unsupported:['Unsupported','unsupported','circle-slash']
 };
 const [label,style,symbol='shield']=statuses[status]||statuses.unverified;
 const explanation=status==='unsupported' ? ' title="Local Signal has no supported firmware check for this device."' : '';
 return `<span class="status-badge firmware-badge ${style}"${explanation}>${icon(symbol)}<span class="visually-hidden">Firmware: </span>${labelOverride||label}</span>`;
}
function firmwareSummaryBadge(finding, device) {
 const status=firmwareStatus(finding,device);
 if(status!=='unsupported')return firmwareBadge(status);
 if(device.protocol||device.model||device.advertised_model||device.firmware)return firmwareBadge('unsupported','No firmware check');
 return '';
}
function firmwareGuideMarkup(guide, heading) {
 return `<div class="firmware-guide"><p><strong>${heading}</strong></p><p>${guide.steps}</p><a href="${guide.url}" target="_blank" rel="noopener noreferrer">${guide.label}</a></div>`;
}
function firmwareDetails(finding, device) {
 const reportedVersion=device.firmware;
 const manualGuide=!finding && manualFirmwareGuide(device);
 if(!finding)return `<section class="device-firmware">
  <div class="device-firmware-heading"><h3>Firmware</h3>${firmwareBadge(manualGuide?'manual':'unsupported')}</div>
  ${reportedVersion ? `<dl class="firmware-versions"><div><dt>Reported version</dt><dd>${esc(reportedVersion)}</dd></div></dl>` : ''}
  ${manualGuide ? firmwareGuideMarkup(manualGuide,'Where to check') : ''}
 </section>`;

 const status=firmwareStatus(finding);
 const sourceId=finding.catalog_id;
 const offered=status==='update_needed';
 const latest=offered ? finding.available_version : finding.latest_stable;
 const published=Boolean(finding.latest_stable) && !offered;
 const checked=published ? finding.catalog_checked_at : finding.checked_at;
 const source=published ? firmwareCatalogLabels[sourceId] : finding.adapter_id==='roku' ? 'Roku device report' : finding.adapter_id==='cast' ? 'Local Cast report' : 'Device report';
 const guide=firmwareGuides[finding.guide_id||finding.adapter_id];
 const guideHeading=status==='current' || status==='checking' ? 'Where to check' : 'Next step';
 const installedLabel={nest:'Installed build',cast:'Installed Cast firmware'}[finding.adapter_id] || 'Installed';
 const releaseLabel=['nest','cast'].includes(finding.adapter_id) ? 'Production release' : 'Latest stable';
 return `<section class="device-firmware">
  <h3>Firmware</h3>
  <dl class="firmware-versions">
   <div><dt>${installedLabel}</dt><dd>${esc(finding.installed_version||'Unknown')}</dd></div>
   ${!finding.installed_version && reportedVersion ? `<div><dt>Reported version</dt><dd>${esc(reportedVersion)}</dd></div>` : ''}
   ${offered || sourceId ? `<div><dt>${offered?'Offered update':releaseLabel}</dt><dd>${esc(latest||'—')}</dd></div>` : ''}
  </dl>
  ${checked ? `<p class="firmware-source muted">${source} · <time datetime="${esc(checked)}">${esc(dateTime(checked))}</time></p>` : ''}
  ${finding.status==='stale' ? '<p class="muted">The last device report is out of date.</p>' : ''}
  ${guide ? firmwareGuideMarkup(guide,guideHeading) : ''}
 </section>`;
}
function inventoryCard(device, finding) {
 const firmware = firmwareBadge(firmwareStatus(finding,device));
 return `<details class="panel inventory-card" data-inventory-ip="${esc(device.ip)}" ${openDeviceCards.has(device.ip)?'open':''}>
  <summary class="inventory-card-heading">
   <span class="inventory-identity"><strong>${esc(device.name)}</strong><span class="mono muted">${esc(device.ip)}</span></span>
   <span class="inventory-statuses">${badge(device)}${firmware}</span>
   ${icon('down','inventory-chevron')}
  </summary>
  <div class="inventory-card-details">
   <p class="device-meta">${esc(device.room)} · ${esc(deviceModel(device))}</p>
   ${deviceReadout(device)}
   <p class="muted inventory-recency">${deviceRecency(device)}</p>
   ${firmwareDetails(finding,device)}
   <button type="button" class="text-button" data-device="${esc(device.ip)}">Device details ${icon('arrow')}</button>
  </div>
 </details>`;
}
function matchesDeviceFilter({device, finding}, filter) {
 switch(filter) {
  case 'all': return true;
  case 'saved': return device.pinned;
  case 'unidentified': return !device.kind;
  case 'firmware': return Boolean(finding || manualFirmwareGuide(device));
  case 'updates': return finding?.stable_status==='update_needed';
  default: return device.kind===filter;
 }
}
function devicesPage() {
 const firmwareByIp=new Map(state.firmware.map(finding=>[finding.ip,finding]));
 const inventory=state.devices.map(device=>({device,finding:firmwareByIp.get(device.ip)}));
 const query=state.query.trim().toLowerCase();
 const firmwareView=state.filter==='firmware'||state.filter==='updates';
 const devices=inventory.filter(entry=>matchesDeviceFilter(entry,state.filter)).filter(({device})=>{
  const fields=[device.name,device.ip,device.room,device.model,device.advertised_model,device.hostname,device.discovered_name];
  return fields.join(' ').toLowerCase().includes(query);
 });
 const emptyTitle=state.filter==='updates'&&!query?'No reported updates':'No matching devices';
 const filters=deviceFilters.map(([key,label])=>{
  const active=key===state.filter;
  const count=inventory.filter(entry=>matchesDeviceFilter(entry,key)).length;
  return `<a id="device-filter-${key}" class="filter-tab ${active?'active':''}" href="${key==='all'?'#devices':`#devices/${key}`}" ${active?'aria-current="true"':''}>${label}<span>${count}</span></a>`;
 }).join('');
 return `${heading('Devices', 'Browse your devices, readings, and firmware status.',true,'DEVICE INVENTORY')}
 ${connectionBanner()}${scanBanner()}
 <div class="inventory-toolbar">
  <nav class="filter-tabs" aria-label="Device filters">${filters}</nav>
  <label class="search-field">${icon('search')}<input id="device-search" type="search" placeholder="Find a device…" value="${esc(state.query)}" aria-label="Find a device"></label>
 </div>
 <div class="inventory-caption">${devices.length} ${devices.length===1?'device':'devices'}</div>
 <div class="inventory-grid">${devices.map(({device,finding})=>inventoryCard(device,finding)).join('')||
   `<div class="panel empty-state wide">${icon('search')}<h3>${emptyTitle}</h3><p>Try another filter, add an IP address, or scan the local network.</p></div>`}</div>
 ${firmwareView
   ? `<p class="firmware-explainer muted">The Firmware filter shows devices with a supported check. Current means the last reported build matches the vendor’s published release. Update needed means the device confirmed an offer.</p>${activityPanel(true,true)}`
   : `<div class="notice lower-notice">${icon('info')}Names come from device broadcasts and your router’s DNS. Some devices publish only an ID or no name. Sleeping and isolated devices may need to be added manually.</div>`}
 `;
}
function historyPage() {
 return `${heading('History', 'Explore collected device readings and download the data.',false,'DEVICE READINGS')}${connectionBanner()}${chartPanel()}`;
}
function routerRate(bps) {
 if(!Number.isFinite(bps)) return 'Unknown';
 if(bps>=1e6) return `${fmt(bps/1e6,2)} Mbps`;
 if(bps>=1e3) return `${fmt(bps/1e3,1)} kbps`;
 return `${fmt(bps,1)} bps`;
}
const routerSupportLinks = '<a href="https://github.com/alieneclectic/Local-Signal/issues/new">Request support</a> or <a href="https://github.com/alieneclectic/Local-Signal/blob/main/CONTRIBUTING.md#add-a-router-adapter">contribute an adapter</a>.';
const routerConnectCommand = id => `<code>python3 server.py --router ${esc(id)} connect</code>`;
function routerPanel(router) {
 const ready=router.status==='ready';
 const titles={starting:'Waiting for the first reading',unavailable:'Readings unavailable',setup_required:'Setup required',unsupported:'Router unsupported'};
 const supported=field=>router.capabilities.includes(field);
 const rows=ready?router.devices:[];
 return `<section class="panel router-monitoring">
  <div class="panel-heading"><div><h2>${esc(router.name)}</h2><p>${ready?`Read-only router monitoring · Last checked ${dateTime(router.checked_at)}`:esc(titles[router.status])}</p></div>${icon('wifi')}</div>
  ${router.message?`<p class="collection-note">${esc(router.message)}</p>`:''}
  ${router.status==='setup_required'?`<p class="collection-note">On the collector computer, run ${routerConnectCommand(router.id)}.</p>`:''}
  ${router.status==='unsupported'?`<p class="collection-note">${routerSupportLinks}</p>`:''}
  ${rows.length?`<div class="table-scroll"><table>
   <thead><tr><th scope="col">Device</th><th scope="col">Upload</th><th scope="col">Download</th><th scope="col">Internet pause</th></tr></thead>
   <tbody>${rows.map(d=>`<tr>
    <th scope="row"><strong>${esc(d.name)}</strong>${d.ip?`<small class="mono muted">${esc(d.ip)}</small>`:''}</th>
    <td>${supported('upload_bps')?routerRate(d.upload_bps):'Unsupported'}</td>
    <td>${supported('download_bps')?routerRate(d.download_bps):'Unsupported'}</td>
    <td>${!supported('paused')?'Unsupported':d.paused===true?'Paused':d.paused===false?'Not paused':'Unknown'}</td>
   </tr>`).join('')}</tbody>
  </table></div>`:ready?`<p class="collection-note">The router returned no devices.</p>`:''}
 </section>`;
}
function routerMonitoring() {
 if(!state.connected) return '';
 const routers=state.router_monitoring;
 if(!routers) return `<div class="notice router-monitoring">${icon('wifi')}Router monitoring is available on the collector computer. Open the Network page using localhost.</div>`;
 const active=routers.filter(router=>router.status!=='disabled');
 if(active.length) return active.map(routerPanel).join('');
 return `<section class="panel router-monitoring">
  <div class="panel-heading"><div><h2>Router monitoring</h2><p>Optional · Off</p></div>${icon('wifi')}</div>
  <p class="collection-note">Internet traffic and device pause status require a compatible router adapter. Use the Network inventory below to discover local devices.</p>
  ${routers.length?`<ul class="collection-note router-options">${routers.map(router=>`<li><strong>${esc(router.name)}</strong> ${routerConnectCommand(router.id)}</li>`).join('')}</ul>`:`<p class="collection-note">This build has no router adapters.</p>`}
  <p class="collection-note">No adapter for your router? ${routerSupportLinks}</p>
 </section>`;
}
function networkPage() {
 const net=state.collector.network;
 return `${heading('Network', 'Discover devices and check how they connect to the collector.',true,'LOCAL NETWORK')}${connectionBanner()}${scanBanner()}<section class="network-info-grid">${statCard('Local subnet',esc(net.subnet||'Unknown'),'Discovery limited to this /24 network','network')}${statCard('Gateway',esc(net.gateway||'Unknown'),'Default route for this network','wifi')}${statCard('Collector IP',esc(net.local_ip||'Unknown'),`${esc(net.interface||'Local network address')}`,'server')}</section>${routerMonitoring()}<section class="panel"><div class="panel-heading"><div><h2>Network inventory</h2><p>${state.devices.length} devices saved · Last scan ${timeAgo(state.collector.last_scan)} · Names ${timeAgo(state.collector.last_name_discovery)}</p></div><div class="network-actions"><button class="text-button names-button" ${state.collector.naming?'disabled':''}>${icon('search')}${state.collector.naming?'Finding names…':'Refresh names'}</button><button class="text-button scan-button" ${state.collector.scanning?'disabled':''}>${icon('scan')}Scan again</button></div></div><div class="table-scroll"><table><thead><tr><th>Device</th><th>IP address</th><th>Hardware address</th><th>Connection</th><th>Telemetry API</th><th>Last seen</th><th></th></tr></thead><tbody>${state.devices.map(d=>`<tr><td><button class="table-device" data-device="${esc(d.ip)}">${icon(deviceIcon(d))}<span><strong>${esc(d.name)}</strong><small>${esc(deviceModel(d))}</small></span></button></td><td class="mono">${esc(d.ip)}</td><td class="mono muted">${esc(d.mac||'Not observed')}</td><td>${badge(d)}</td><td><span class="protocol-tag ${d.status==='online'?'supported':''}">${esc(protocolLabel(d.protocol))}</span></td><td class="muted">${timeAgo(d.last_seen)}</td><td><button class="icon-button" data-device="${esc(d.ip)}" aria-label="View ${esc(d.name)}">${icon('chevron')}</button></td></tr>`).join('')}</tbody></table></div></section><div class="notice lower-notice">${icon('shield')}Discovery uses ping, the local ARP cache, and read-only Shelly requests. A cached address alone is never marked online.</div>`;
}
function settingsPage() {
 const c=state.collector;
 const codeSource={checkout:'Working checkout',installed:'Installed snapshot'}[c.code_source];
 const theme=document.documentElement.dataset.theme;
 const themeChoice=`<div class="setting-choice">
  <div><strong id="theme-label">Theme</strong><p>Changes the dashboard appearance.</p></div>
  <div class="segmented theme-selector" role="group" aria-labelledby="theme-label">
   ${['Light','Dark'].map(label=>{
    const value=label.toLowerCase(), selected=theme===value;
    return `<button id="theme-${value}" class="${selected?'selected':''}" data-theme="${value}" aria-pressed="${selected}">${label}</button>`;
   }).join('')}
  </div>
 </div>`;
 return `${heading('Settings', 'Manage display preferences and check the collector.',false,'WORKSPACE SETTINGS')}<div class="settings-layout"><section class="panel settings-panel"><div class="panel-heading"><div><h2>Your local collector</h2><p>Collection and storage on the collector computer.</p></div><span class="status-badge ${state.connected?'online':'offline'}"><i class="dot"></i>${state.connected?'Running':'Disconnected'}</span></div><dl class="settings-list"><div><dt>Collection interval</dt><dd>Every ${c.poll_seconds||30} seconds</dd></div><div><dt>History retention</dt><dd>${c.retention_days||30} days</dd></div><div><dt>Data storage</dt><dd>Local SQLite database</dd></div>${codeSource?`<div><dt>Code source</dt><dd>${codeSource}</dd></div>`:''}<div><dt>Started</dt><dd>${dateTime(c.started_at)}</dd></div><div><dt>Last collection</dt><dd>${dateTime(c.last_poll)}</dd></div><div><dt>Device access</dt><dd>Read-only telemetry</dd></div></dl><div class="notice">${icon('info')}${c.background?'Background collection starts at login and recovers after failures.':'Keep the server running to collect readings.'} The collector computer must be awake. Closing the browser doesn’t stop collection.</div></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>Preferences</h2><p>Preferences for this browser.</p></div>${icon('settings')}</div>${themeChoice}<div class="setting-choice"><div><strong>Temperature unit</strong><p>Applies to charts and device readings.</p></div><div class="segmented">${['F','C'].map(u=>`<button class="${state.unit===u?'selected':''}" data-unit="${u}" aria-pressed="${state.unit===u}">°${u}</button>`).join('')}</div></div><div class="setting-choice export-setting"><div><strong>Export history</strong><p>Export all readings from the past 30 days.</p></div><a href="/api/export.csv?hours=720" class="button secondary">${icon('download')}Export CSV</a></div><div class="supported-apis"><h3>Connected locally</h3><div><span class="api-logo">S</span><span><strong>Shelly Gen 1 & Gen 2+</strong><small>Power, sensors, relays, signal & uptime when exposed</small></span>${icon('check')}</div><p class="muted">Local readers also support Roku, Sonos, WiiM UPnP, and Google Cast receivers, including RIVA. Other paired or cloud-managed devices need their own integration. Authentication-protected APIs are flagged for setup.</p></div></section></div>${collectionStatusPanel()}${activityPanel(true)}`;
}
const views={overview,devices:devicesPage,history:historyPage,network:networkPage,settings:settingsPage};
function render() {
 const main=$('#main'), focused=document.activeElement, focusID=focused?.id, selection=focused?.selectionStart;
 const cardSelector='details[data-inventory-ip]';
 const cardIP=focused?.closest(cardSelector)?.dataset.inventoryIp;
 // Keep disclosure state and focus when polling replaces the inventory.
 for(const card of $$(cardSelector,main)) {
  const ip=card.dataset.inventoryIp;
  if(card.open)openDeviceCards.add(ip);
  else openDeviceCards.delete(ip);
 }
 main.innerHTML=views[state.page]();
 $$('[data-color]',main).forEach(el=>{el.style.backgroundColor=el.dataset.color;});
 const card=cardIP && $$(cardSelector,main).find(el=>el.dataset.inventoryIp===cardIP);
 if(card) {
  const control=$(focused.tagName.toLowerCase(),card)||$('summary',card);
  control.focus({preventScroll:true});
 } else if(focusID) {
  const input=document.getElementById(focusID);
  input?.focus({preventScroll:true});
  if(input?.type==='search'&&typeof selection==='number')input.setSelectionRange(selection,selection);
 }
}
function route() {
 const requested=location.hash.slice(1);
 const legacyRoutes={firmware:'devices/firmware',telemetry:'history',heating:'overview'};
 const path=Object.hasOwn(legacyRoutes,requested)?legacyRoutes[requested]:requested;
 const [page,filter]=path.split('/');
 if(state.page!==page||requested==='firmware')state.query='';
 state.page=Object.hasOwn(views,page)?page:'overview';
 state.filter=deviceFilters.some(([key])=>key===filter)?filter:'all';
 if(path!==requested)history.replaceState(null,'',`#${path}`);
 $$('[data-page]').forEach(a=>{const active=a.dataset.page===state.page;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
 $('#crumb').textContent=state.page[0].toUpperCase()+state.page.slice(1);document.title=`${$('#crumb').textContent} · Local Signal`;render();window.scrollTo(0,0);
 if(!state.initial&&(state.page==='history'||state.page==='devices'))load();
}
function openAdd() { $('#add-form').reset();$('#add-error').textContent='';$('#device-dialog').showModal(); }
function openDetail(ip) {
 const d=state.devices.find(d=>d.ip===ip);if(!d)return;detailIP=ip;
 const finding=state.firmware.find(row=>row.ip===ip);
 const entries=Object.entries(d.metrics||{}).filter(([k])=>!/^power_\d+_w$/.test(k));
 const lastEntries=d.status!=='online'?Object.entries(d.last_metrics||{}).filter(([k])=>!/^power_\d+_w$/.test(k)&&k!=='latency_ms'):[];
 $('#detail-content').innerHTML=`<div class="modal-heading"><div class="detail-header"><span class="device-type-icon sage">${icon(deviceIcon(d))}</span><div><span class="eyebrow">${esc(d.model||d.advertised_model||(d.discovered_name?'NETWORK DEVICE':'UNIDENTIFIED DEVICE'))}</span><h2 id="detail-title">${esc(d.name)}</h2></div></div><button class="icon-button close-detail" aria-label="Close device details">${icon('close')}</button></div><div class="detail-status">${badge(d)}<span class="mono muted">${esc(d.ip)}</span>${firmwareSummaryBadge(finding,d)}</div>${d.discovered_name?`<div class="discovered-identity"><div>${icon('wifi')}<strong>Discovered identity</strong><span>${esc(d.discovered_name_source)}</span></div><p>${esc(d.discovered_name)}</p>${d.hostname?`<small class="mono">${esc(d.hostname)}</small>`:''}<small>Observed ${timeAgo(d.name_discovered_at)}${d.name_mode==='manual'?' · Custom display name preserved':' · Used as the display name'}</small></div>`:''}${d.error?`<div class="notice warning">${icon('info')}${esc(d.error)}</div>`:''}${d.telemetry_note?`<div class="notice">${icon('info')}${esc(d.telemetry_note)}</div>`:''}<div class="detail-metrics">${entries.map(([k,m])=>`<div><span>${esc(m.label)}</span><strong>${k.startsWith('relay_')?m.value?'On':'Off':metricFormat(m)}</strong></div>`).join('')||'<div><span>Telemetry</span><strong>No readings yet</strong></div>'}</div>${lastEntries.length?`<details class="last-readings"><summary>Last successful telemetry · ${dateTime(d.last_telemetry)}</summary><p class="muted">Historical readings, not current device state.</p><div class="detail-metrics stale">${lastEntries.map(([k,m])=>`<div><span>${esc(m.label)}</span><strong>${metricFormat(m)}</strong></div>`).join('')}</div></details>`:''}<dl class="device-info-list"><div><dt>Last seen on network</dt><dd>${dateTime(d.last_seen)}</dd></div><div><dt>Last successful telemetry</dt><dd>${dateTime(d.last_telemetry)}</dd></div><div><dt>MAC address</dt><dd class="mono">${esc(d.mac||'Not observed')}</dd></div></dl>${firmwareDetails(finding,d)}${Object.keys(d.metrics||{}).some(k=>k.includes('internal_temperature'))?`<div class="notice">${icon('temp')}Internal device temperature is not the room temperature.</div>`:''}<form id="edit-form"><div class="detail-edit-heading"><h3>Make it yours</h3><span>Workspace labels only</span></div><div class="form-row"><label>Device name<input name="name" value="${esc(d.name)}" maxlength="100" required></label><label>Room<input name="room" value="${esc(d.room)}" maxlength="100" required></label></div><label>Notes<textarea name="notes" rows="2" maxlength="1000">${esc(d.notes)}</textarea></label><div class="detail-checks"><label class="check-label"><input name="auto_name" type="checkbox" ${d.name_mode==='auto'?'checked':''}>Use discovered name when available</label><label class="check-label"><input name="pinned" type="checkbox" ${d.pinned?'checked':''}>Show in My devices</label><label class="check-label"><input name="expected_sleep" type="checkbox" ${d.expected_sleep?'checked':''}>Battery device that sleeps</label></div><p id="edit-error" class="form-error" role="alert"></p><div class="detail-actions"><a class="button secondary" href="/api/export.csv?hours=${state.range}&ip=${esc(d.ip)}">${icon('download')}Export history</a><button class="button primary" type="submit">${icon('check')}Save details</button></div></form>${d.raw_status?`<details class="raw-details"><summary>Last API snapshot <span>${dateTime(d.raw_status_at||d.last_telemetry)}</span></summary><pre>${esc(JSON.stringify(d.raw_status,null,2))}</pre></details>`:''}`;
 if(!$('#detail-dialog').open)$('#detail-dialog').showModal();
}
async function scan() {
 try{await api('/api/scan',{method:'POST',body:'{}'});toast('Discovering devices on the local network…');await load();}catch(e){toast(e.message,true);}
}
document.addEventListener('click',async e=>{
 const target=e.target.closest('button,a');if(!target)return;
 if(target.matches('.add-button'))openAdd();
 if(target.id==='close-add')$('#device-dialog').close();
 if(target.matches('.close-detail'))$('#detail-dialog').close();
 if(target.dataset.device)openDetail(target.dataset.device);
 if(target.matches('.scan-button'))await scan();
 if(target.matches('.names-button')){try{await api('/api/names/refresh',{method:'POST',body:'{}'});toast('Looking for advertised device names…');await load();}catch(err){toast(err.message,true);}}
 if(target.id==='refresh-button'||target.id==='retry-load') {try{await api('/api/refresh',{method:'POST',body:'{}'});toast('Refreshing device readings…');await load();}catch(err){toast(err.message,true);}}
 if(target.dataset.range){state.range=Number(target.dataset.range);await loadHistory();render();}
 if(target.dataset.series){state.chartDevices.has(target.dataset.series)?state.chartDevices.delete(target.dataset.series):state.chartDevices.add(target.dataset.series);render();}
 if(target.dataset.unit){state.unit=target.dataset.unit;localStorage.setItem('hearth-unit',state.unit);render();}
 if(target.dataset.theme){
  applyTheme(target.dataset.theme);
  localStorage.setItem(themeKey,target.dataset.theme);
  render();
 }
});
document.addEventListener('change',e=>{if(e.target.name==='auto_name'&&e.target.checked){const d=state.devices.find(d=>d.ip===detailIP);$('#edit-form input[name="name"]').value=d.discovered_name||`Device .${detailIP.split('.').at(-1)}`;}if(e.target.id==='metric-select'){state.metric=e.target.value;state.chartDevices.clear();render();}});
document.addEventListener('input',e=>{if(e.target.name==='name'&&e.target.closest('#edit-form'))$('#edit-form input[name="auto_name"]').checked=false;if(e.target.id==='device-search'){state.query=e.target.value;render();}});
$('#add-form').addEventListener('submit',async e=>{
 e.preventDefault();const form=e.target,button=$('button[type="submit"]',form);button.disabled=true;$('#add-error').textContent='';
 try{const fields=new FormData(form);await api('/api/devices',{method:'POST',body:JSON.stringify({name:fields.get('name'),ip:fields.get('ip'),room:fields.get('room'),expected_sleep:fields.has('expected_sleep')})});$('#device-dialog').close();toast('Device added. Checking for telemetry…');await load();}catch(err){$('#add-error').textContent=err.message;}finally{button.disabled=false;}
});
document.addEventListener('submit',async e=>{
 if(e.target.id!=='edit-form')return;e.preventDefault();const form=e.target,fields=new FormData(form),button=$('button[type="submit"]',form);button.disabled=true;
 try{await api(`/api/devices/${detailIP}`,{method:'PATCH',body:JSON.stringify({name:fields.get('name'),name_mode:fields.has('auto_name')?'auto':'manual',room:fields.get('room'),notes:fields.get('notes'),pinned:fields.has('pinned'),expected_sleep:fields.has('expected_sleep')})});$('#detail-dialog').close();toast('Device details saved.');await load();}catch(err){$('#edit-error').textContent=err.message;button.disabled=false;}
});
$$('dialog').forEach(dialog=>dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}}));
window.addEventListener('hashchange',route);
window.matchMedia('(max-width: 530px)').addEventListener('change',()=>{if(state.page==='history')render();});
route();load(true);setInterval(load,5000);setInterval(updateChrome,15000);

function collectionStatusPanel() {
 const c=state.collector,b=c.backup,gap=c.last_gap;
 return `<section class="panel settings-panel collection-panel"><div class="panel-heading"><div><h2>Background collection & backups</h2><p>${c.background?'Starts at sign-in and restarts after failures.':'Manual session · install background collection to start at login.'}</p></div>${icon('server')}</div><dl class="settings-list"><div><dt>Collection health</dt><dd>${esc(c.health||'Connecting')}${c.collection_error?` · ${esc(c.collection_error)}`:''}</dd></div><div><dt>Latest daily backup</dt><dd>${b?dateTime(b.at):'Waiting for first backup'}</dd></div><div><dt>Backup retention</dt><dd>Seven daily snapshots</dd></div><div><dt>Latest collection gap</dt><dd>${gap?`${dateTime(gap.start)} → ${dateTime(gap.end)}`:'None recorded'}</dd></div></dl>${c.backup_error?`<div class="notice warning">${icon('info')}Backup needs attention: ${esc(c.backup_error)}</div>`:''}<p class="collection-note">${c.background?'The collector stores history locally and backs it up daily.':'Set up background collection on the collector computer for automatic startup.'} The collector computer must be awake and connected to the local network. Collection resumes after sleep; missed intervals remain visible as gaps.</p></section>`;
}
