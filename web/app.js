'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
 speaker:'<rect x="5" y="2" width="14" height="20" rx="3"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="6" r="1"/>',
 tv:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>',
 home:'<path d="m3 11 9-8 9 8v10h-7v-7h-4v7H3z"/>',
 house:'<path d="m3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8"/>',
 overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
 grid:'<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M3 10h18M10 10v11"/>',
 chart:'<path d="M3 3v18h18M6 15l5-5 4 3 6-8"/>',
 network:'<rect x="8" y="2" width="8" height="6" rx="1.5"/><rect x="2" y="16" width="8" height="6" rx="1.5"/><rect x="14" y="16" width="8" height="6" rx="1.5"/><path d="M12 8v4M6 16v-4h12v4"/>',
 settings:'<path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 2 3h3l1-3 3-1 2-3-2-2 1-3-2-3-3 1-2-3z"/><circle cx="12" cy="12" r="3"/>',
 shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6"/>',
 refresh:'<path d="M20 7a9 9 0 0 0-15-2L2 8m0-6v6h6M4 17a9 9 0 0 0 15 2l3-3m0 6v-6h-6"/>',
 chevrons:'<path d="m8 8 4-4 4 4m-8 8 4 4 4-4"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
 chevron:'<path d="m9 5 7 7-7 7"/>',
 down:'<path d="m6 9 6 6 6-6"/>',
 plug:'<path d="M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0zm6 10v4"/>',
 sensor:'<rect x="5" y="3" width="14" height="18" rx="5"/><circle cx="12" cy="11" r="3"/><path d="M10 17h4"/>',
 wifi:'<path d="M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a6 6 0 0 1 7 0M12 19h.01"/>',
 bolt:'<path d="m13 2-9 12h7l-1 8 10-13h-7z"/>',
 activity:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
 scan:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M7 12h10M12 7v10"/>',
 download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
 close:'<path d="m6 6 12 12M6 18 18 6"/>',
 temp:'<path d="M9 14.5V5a3 3 0 0 1 6 0v9.5a5 5 0 1 1-6 0Z"/><path d="M12 8v10"/>',
 dots:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
 globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
 pin:'<path d="m9 3 10 10-4 1-2 4-3-3-6 6m3-13 2-5m-2 5-3 2 9 8"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 moon:'<path d="M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10Z"/>',
 server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M12 6.5h5M12 17.5h5"/>',
 leaf:'<path d="M20 3C8 2 2 8 5 16c8 6 16 0 15-13ZM5 20l10-11"/>',
 battery:'<rect x="2" y="6" width="18" height="12" rx="2"/><path d="M23 10v4M6 10v4m4-4v4m4-4v4"/>',
 copy:'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>'
};
const icon = (name, extra='') => `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
function hydrateIcons(root=document) { $$('[data-icon]', root).forEach(el => { el.innerHTML = icon(el.dataset.icon); el.removeAttribute('data-icon'); }); }
hydrateIcons();
const state = {devices:[], rooms:[], collector:{network:{}}, events:[], samples:[], page:'overview', filter:'all', query:'', range:24, metric:'power_w', connected:false, initial:true, unit:localStorage.getItem('hearth-unit') || 'F', chartDevices:new Set(), lastHistory:null, historyError:null, energy:null, energyDays:1, energyError:null, lastEnergyPoll:null, heatingDraft:null};
const colors = ['#2e6a51','#9eac58','#d09460','#7895b0','#9b86ae','#c17e85','#528f90','#b5a384'];
let loading = false, toastTimer, detailIP = null;
const timeAgo = (value) => { if (!value) return 'Not yet'; const s = Math.max(0, (Date.now()-Date.parse(value))/1000); return s<15?'Just now':s<60?`${Math.floor(s)}s ago`:s<3600?`${Math.floor(s/60)}m ago`:s<86400?`${Math.floor(s/3600)}h ago`:`${Math.floor(s/86400)}d ago`; };
const dateTime = value => value ? new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : 'Not yet';
const fmt = (n, digits=1) => Number.isFinite(n) ? n.toLocaleString([], {maximumFractionDigits:digits}) : '—';
const metric = (d,k) => d.metrics?.[k]?.value;
const reporting = () => state.devices.filter(d=>d.status==='online');
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
  const data=await api('/api/state'); Object.assign(state, data); state.connected=true; state.initial=false;
  if(force || state.lastHistory!==state.collector.last_poll || !state.lastHistory) await loadHistory();
  if(force || state.lastEnergyPoll!==state.collector.last_poll || !state.energy) await loadEnergy();
  updateChrome(); render();
 } catch(e) { state.connected=false; updateChrome(); if(state.initial) $('#main').innerHTML=`<div class="empty-state large">${icon('server')}<h1>The collector is taking a moment.</h1><p>Keep the Hearth server running on this Mac, then refresh the page.</p><button class="button primary" id="retry-load">Try again</button></div>`; }
 finally { loading=false; }
}
async function loadHistory() {
 try { const data=await api(`/api/history?hours=${state.range}`); state.samples=data.samples; state.lastHistory=state.collector.last_poll; state.historyError=null; }
 catch(e) { state.historyError=e.message; }
}
function updateChrome() {
 const delayed=state.collector.health==='delayed';
 $('#collector-status').innerHTML=`<i class="dot"></i>${state.connected?delayed?'Collection delayed':state.collector.health==='starting'?'Collector starting':'Collector online':'Collector disconnected'}`;
 $('#collector-status').classList.toggle('disconnected',!state.connected||delayed);
 $('#nav-count').textContent=state.devices.length || '0';
 $('#footer-status').textContent=state.connected?`Collected locally · Updated ${timeAgo(state.collector.last_poll)}`:'Collector disconnected · Reconnecting…';
 $('#refresh-button').classList.toggle('spinning',!!state.collector.polling);
}
function heading(title, subtitle, action=true, eyebrow='YOUR CONNECTED HOME') {
 return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${action?`<button class="button secondary scan-button" ${state.collector.scanning?'disabled':''}>${icon('scan', state.collector.scanning?'rotate':'')}${state.collector.scanning?'Discovering…':'Scan network'}</button><button class="button primary add-button">${icon('plus')}Add device</button>`:''}</div></div>`;
}
function connectionBanner() { return !state.connected?`<div class="notice warning">${icon('info')}The collector is disconnected. Displayed readings are from the last connection. Reconnecting automatically.</div>`:state.collector.health==='delayed'?`<div class="notice warning">${icon('info')}Collection is delayed. Last completed ${timeAgo(state.collector.last_poll)}. ${esc(state.collector.collection_error||'Waiting for the next collection cycle.')}</div>`:''; }
function scanBanner() {
 const c=state.collector;
 if(c.scanning) return `<div class="notice">${icon('scan','rotate')}${c.naming?'Discovering device names on':'Discovering devices on'} ${esc(c.network.subnet||'your home network')}<span class="notice-right">${c.scan_progress} / ${c.scan_total||'—'} addresses</span></div>`;
 return c.scan_error?`<div class="notice warning">${icon('info')}${esc(c.scan_error)}</div>`:'';
}
function statCard(label,value,unit,sub,ico,cls='') {
 return `<article class="stat-card ${cls}"><div class="stat-top"><span>${label}</span><span class="stat-icon">${icon(ico)}</span></div><div class="stat-value">${value}<span>${unit}</span></div><div class="stat-sub">${sub}</div></article>`;
}
function overview() {
 const live=reporting(), online=reachable(), power=live.filter(d=>Number.isFinite(metric(d,'power_w'))), sum=power.reduce((s,d)=>s+metric(d,'power_w'),0), known=state.devices.filter(d=>d.pinned).sort((a,b)=>(b.status==='online')-(a.status==='online')||a.name.localeCompare(b.name)), unavailable=known.filter(d=>d.status!=='online');
 const date=new Date().toLocaleDateString([], {weekday:'long',month:'long',day:'numeric'});
 return `${heading('Your home, in view.', 'A little clarity for everything connected.')} ${connectionBanner()} ${scanBanner()}
 <div class="welcome-strip"><span>${icon('leaf')} A closer connection to your home.</span><span>${date}</span></div>
 <section class="stats-grid" aria-label="Home summary">
 ${statCard('Devices reachable', online.length,`/ ${state.devices.length}`,`<i class="dot"></i>Responding on your network`,'wifi','highlight')}
 ${statCard('Live telemetry',live.length,'devices',`${live.length?icon('check'):icon('clock')} ${live.length?'Reporting directly to this Mac':'Waiting for first readings'}`,'activity')}
 ${statCard('Power right now',power.length?fmt(sum):'—','W',`Across ${power.length} reporting power ${power.length===1?'meter':'meters'}`,'bolt')}
 ${statCard('Awaiting telemetry',unavailable.length,'saved devices',`${icon('info')} Reachable doesn’t always mean readable`,'clock')}
 </section>
 ${heatingOverview()}${roomTemperatures()}${mediaOverview()}<div class="overview-middle">${chartPanel()}${networkPanel()}</div>
 <section class="section devices-section"><div class="section-heading"><div><h2>My devices <span class="count-badge">${known.length}</span></h2><p>Your saved devices, at a glance.</p></div><a href="#devices" class="text-button">View all devices ${icon('arrow')}</a></div><div class="devices-grid">${known.length?known.map(deviceCard).join(''):`<div class="empty-state">${icon('plug')}<p>Add your first device to get started.</p></div>`}</div></section>
 <div class="overview-bottom">${activityPanel()}<article class="panel insight-panel"><span class="small-kicker">GOOD TO KNOW</span><span class="insight-icon">${icon('shield')}</span><h3>Local by nature.<br>Yours by design.</h3><p>Readings go straight from your devices to this Mac. Your history stays here, too.</p><a href="#settings" class="text-button">About your collector ${icon('arrow')}</a></article></div>`;
}
function mediaOverview() {
 const devices=reporting().filter(d=>d.kind==='media');
 if(!devices.length)return '';
 const visible=[...devices].sort((a,b)=>(a.metrics?.playback_state?.value==='Playing'?-1:0)-(b.metrics?.playback_state?.value==='Playing'?-1:0)||a.name.localeCompare(b.name)).slice(0,6);
 return `<section class="panel media-overview"><div class="panel-heading"><div><h2>Beyond the thermostat</h2><p>Live status from ${devices.length} local speakers and media players.</p></div><a href="#devices" class="text-button" data-filter="media">View all media ${icon('arrow')}</a></div><div class="table-scroll"><table><thead><tr><th>Device</th><th>Reported state</th><th>Reported level</th><th>App / details</th></tr></thead><tbody>${visible.map(d=>{const m=d.metrics||{},activity=mediaState(d);return `<tr><td><button class="table-device" data-device="${esc(d.ip)}">${icon(deviceIcon(d))}<span><strong>${esc(d.name)}</strong><small>${esc(deviceModel(d))}</small></span></button></td><td>${esc(activity.value)}<small class="media-state-label">${esc(activity.label)}</small></td><td>${mediaLevel(d)}</td><td class="muted">${esc(m.media_title?.value||m.active_app?.value||m.speaker_room?.value||protocolLabel(d.protocol))}</td></tr>`;}).join('')}</tbody></table></div></section>`;
}
function roomTemperatures() {
 const rooms=state.rooms||[];
 if(!rooms.length)return '';
 return `<section class="room-temperature-section" aria-label="Room temperatures"><div class="section-heading"><div><h2>How home feels</h2><p>Measured room temperatures from your floor sensors.</p></div><a href="#settings" class="text-button">Manage sensors ${icon('arrow')}</a></div><div class="room-temperature-grid">${rooms.map(room=>{
  const t=room.temperature, humidity=room.humidity, battery=room.battery, sensor=room.sensor;
  const sleepy=sensor?.expected_sleep && sensor.status!=='online';
  const relay=room.heaters?.find(h=>h.relay_on!==null), power=room.heaters?.some(h=>Number.isFinite(h.power_w?.value))?room.heaters.reduce((sum,h)=>sum+(h.power_w?.value||0),0):null;
  return `<article class="room-temperature-card ${t?.stale?'older-reading':''}"><div class="room-card-top"><span class="room-floor-icon">${icon('house')}</span><div><h3>${esc(room.name)}</h3><span>Room temperature</span></div><span class="room-report-state ${t?.stale?'older':''}"><i class="dot"></i>${t?t.stale?'Older reading':sleepy?'Last reported':'Recent report':'Not linked'}</span></div><div class="room-reading">${t?`${fmt(state.unit==='F'?t.value*9/5+32:t.value)}<span>°${state.unit}</span>`:`—<span>No sensor reading</span>`}</div><div class="room-secondary"><span>${icon('activity')}${humidity?`${fmt(humidity.value)}% humidity`:'Humidity unavailable'}</span><span>${icon('battery')}${battery?`${fmt(battery.value,0)}% battery`:'Battery unavailable'}</span></div><div class="room-report-footer"><span>${icon('clock')}${t?`Reported ${timeAgo(t.reported_at)}`:'Link a sensor in Settings'}</span>${sensor?`<button class="text-button" data-device="${esc(sensor.ip)}">${esc(sensor.ip)} ${icon('chevron')}</button>`:''}</div><div class="room-heater-state">${icon('plug')}<span>${relay?relay.relay_on?Number.isFinite(power)?`Heater drawing ${fmt(power)} W`:'Heater relay on · power unavailable':'Heater relay off':'Heater state unavailable'}</span><span class="room-source">${t?esc(t.source||'HTTP'):''}</span></div></article>`;
 }).join('')}</div><div class="room-sensor-note">${icon('info')}Battery sensors sleep between reports. These are the latest measured values, not continuously sampled temperatures.${state.collector.sensor_listener?' Local sensor listener is active.':state.collector.sensor_listener_error?' Automatic updates are unavailable; polling remains active.':''}</div></section>`;
}
function roomSensorSettings() {
 const sensors=[];
 for(const d of state.devices) {
  const readings={...(d.last_metrics||{}),...(d.latest_metrics||{}),...(d.metrics||{})};
  for(const [key,m] of Object.entries(readings)) if(m.unit==='°C' && (key==='temperature_c'||(key.startsWith('temperature:')||key.startsWith('external_temperature_'))&&key.endsWith('_c'))) sensors.push({device:d,key,reading:m});
 }
 return `<section class="panel room-sensor-settings"><div class="panel-heading"><div><h2>Room temperature sources</h2><p>Link a measured sensor to each floor. Heater board temperatures are excluded.</p></div>${icon('temp')}</div><div class="room-link-forms">${(state.rooms||[]).map(room=>`<form class="room-link-form" data-room="${room.id}"><label>${esc(room.name)}<select name="sensor" aria-label="${esc(room.name)} temperature sensor"><option value="">No sensor linked</option>${sensors.map(s=>`<option value="${esc(s.device.ip+'|'+s.key)}" ${room.sensor?.mac===s.device.mac?'selected':''}>${esc(s.device.name)} · ${esc(s.device.ip)} · ${esc(s.reading.label)}</option>`).join('')}</select></label><button class="button secondary" type="submit">Save source</button></form>`).join('')}</div><div class="notice">${icon('wifi')}${state.collector.sensor_listener?'Receiving existing Shelly H&T broadcasts locally. Device settings are unchanged.':esc(state.collector.sensor_listener_error||'The local sensor listener starts with the collector.')}</div></section>`;
}
function chartPanel(full=false) {
 const options=[['power_w','Power'],['internal_temperature_c','Internal temp.'],['temperature_c','Room temp.'],['humidity_pct','Humidity'],['rssi_dbm','Wi-Fi signal'],['latency_ms','Latency'],['volume_pct','Volume'],['component_volume_pct','Component level'],['uptime_s','Uptime']];
 // Include Gen2 component sensor metrics as distinct selectable series.
 const extra=new Map(); for(const d of state.devices) for(const [k,v] of Object.entries(d.metrics||{})) if(!options.some(x=>x[0]===k) && /^(temperature:|humidity:|external_temperature|external_humidity|switch:.*internal)/.test(k)) extra.set(k,v.label);
 const select=options.concat([...extra]);
 return `<section class="panel chart-panel ${full?'full-chart':''}"><div class="panel-heading"><div><h2>Telemetry over time</h2><p>Real readings. A clearer picture.</p></div><a class="icon-button export-link" href="/api/export.csv?hours=${state.range}" title="Export this time range" aria-label="Export this time range">${icon('download')}</a></div><div class="chart-controls"><label class="select-wrap">${icon(state.metric.includes('temperature')?'temp':state.metric==='power_w'?'bolt':'chart')}<select id="metric-select" aria-label="Telemetry metric">${select.map(([key,label])=>`<option value="${key}" ${key===state.metric?'selected':''}>${label}</option>`).join('')}</select>${icon('down')}</label><div class="segmented range-select" aria-label="Time range">${[[1,'1h'],[6,'6h'],[24,'24h'],[168,'7d']].map(([n,s])=>`<button class="${state.range===n?'selected':''}" data-range="${n}" aria-pressed="${state.range===n}">${s}</button>`).join('')}</div></div><div class="chart-wrap">${buildChart(full)}</div><div class="chart-legend">${chartLegend()}</div><div class="chart-note" title="Charts average readings across long ranges. CSV exports contain every collected sample.">${icon('info')} ${state.metric.includes('internal')?'Internal temperature is the device’s own temperature, not room temperature.':state.metric==='power_w'?'Power covers reporting meters only. History starts when the collector is running.':'Gaps mean no measurement was collected. History is saved while the collector runs.'}</div></section>`;
}
function seriesData() {
 const grouped=new Map();
 for(const sample of state.samples) if(sample.metric===state.metric) { if(!grouped.has(sample.ip)) grouped.set(sample.ip,[]); grouped.get(sample.ip).push(sample); }
 return state.devices.filter(d=>grouped.has(d.ip)).map((d,i)=>({device:d, points:grouped.get(d.ip), color:colors[state.devices.filter(d=>grouped.has(d.ip)).indexOf(d)%colors.length]}));
}
function chartLegend() {
 const series=seriesData();
 return series.length?series.map(s=>`<button class="legend-item ${state.chartDevices.has(s.device.ip)?'dimmed':''}" data-series="${esc(s.device.ip)}" aria-pressed="${!state.chartDevices.has(s.device.ip)}"><i class="legend-dot" data-color="${s.color}"></i>${esc(s.device.name)}<strong>${metricFormat(s.points.at(-1))}</strong></button>`).join(''):`<span class="muted legend-empty">No ${state.metric.replaceAll('_',' ')} readings collected yet</span>`;
}
function buildChart(full) {
 if(state.historyError) return `<div class="chart-empty">${icon('info')}<strong>History couldn’t load</strong><p>${esc(state.historyError)}</p></div>`;
 const series=seriesData().filter(s=>!state.chartDevices.has(s.device.ip));
 const all=series.flatMap(s=>s.points);
 if(!all.length) return `<div class="chart-empty">${icon('chart')}<strong>${state.chartDevices.size?'No visible readings':'A little history starts here.'}</strong><p>${state.chartDevices.size?'Select a device in the legend to show its readings.':'Readings will appear as compatible devices report this metric.'}</p></div>`;
 const w=760,h=full?330:218,left=48,right=18,top=14,bottom=32;
 const converted=n=>state.metric.endsWith('_c')&&state.unit==='F'?n*9/5+32:n;
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
 const unit=state.metric.endsWith('_c')?`°${state.unit}`:all[0].unit;
 let svg=`<svg class="telemetry-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(state.metric.replaceAll('_',' '))} telemetry chart, ${esc(unit)}"><defs>${series.map((s,i)=>`<linearGradient id="fill-${i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${s.color}" stop-opacity=".12"/><stop offset="100%" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`).join('')}</defs>`;
 for(let i=0;i<5;i++){const v=min+(max-min)*(4-i)/4, yy=top+(h-top-bottom)*i/4;svg+=`<line x1="${left}" y1="${yy}" x2="${w-right}" y2="${yy}" stroke="#e9ece5" stroke-dasharray="3 5"/><text x="${left-12}" y="${yy+4}" text-anchor="end" class="axis-label">${fmt(v,1)}</text>`;}
 for(let i=0;i<5;i++){const t=start+(end-start)*i/4,xx=left+(w-left-right)*i/4;svg+=`<text x="${xx}" y="${h-7}" text-anchor="${i===0?'start':i===4?'end':'middle'}" class="axis-label">${(span>86400000?new Date(t).toLocaleString([],{month:'short',day:'numeric',hour:'numeric'}):new Date(t).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}))}</text>`;}
 series.forEach((s,i)=>{
  const step=Math.max(1,Math.floor(s.points.length/300)), points=s.points.filter((_,j)=>j%step===0||j===s.points.length-1);
  const gap=Math.max(90000,((Math.max(...timestamps)-Math.min(...timestamps))/300)*2.5),segments=[];let segment=[];
  for(const point of points) {if(segment.length && Date.parse(point.at)-Date.parse(segment.at(-1).at)>gap){segments.push(segment);segment=[];} segment.push(point);}if(segment.length) segments.push(segment);
  for(const seg of segments){const line=seg.map((p,j)=>`${j?'L':'M'}${x(p.at).toFixed(2)},${y(p.value).toFixed(2)}`).join(' ');if(seg.length>1)svg+=`<path d="${line} L${x(seg.at(-1).at)},${h-bottom} L${x(seg[0].at)},${h-bottom} Z" fill="url(#fill-${i})"/><path d="${line}" fill="none" stroke="${s.color}" stroke-width="2.4" stroke-linejoin="round"/>`;}
  const latest=points.at(-1); svg+=`<circle cx="${x(latest.at)}" cy="${y(latest.value)}" r="5" fill="${s.color}" stroke="#fff" stroke-width="2"><title>${esc(s.device.name)}: ${esc(metricFormat(latest))} at ${esc(dateTime(latest.at))}</title></circle>`;
  // Hover dots expose the actual historical reading without inventing interpolated values.
  if(points.length<160) for(const p of points) svg+=`<circle class="chart-hover-dot" cx="${x(p.at)}" cy="${y(p.value)}" r="6" fill="${s.color}" opacity="0"><title>${esc(s.device.name)} · ${esc(metricFormat(p))} · ${esc(dateTime(p.at))}</title></circle>`;
 });
 svg+=`<text x="${left}" y="${top-3}" class="unit-label">${esc(unit)}</text></svg>`;return svg;
}
function networkPanel() {
 const net=state.collector.network, online=reachable().length, total=state.devices.length;
 return `<article class="network-panel"><div class="network-panel-top"><span class="dark-kicker">THE LOCAL PICTURE</span><span class="subtle-icon">${icon('network')}</span></div><h2>A connected home.<br>A private connection.</h2><div class="network-visual"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><div class="orbit orbit-three"></div><span class="orbit-node node-one">${icon('plug')}</span><span class="orbit-node node-two">${icon('sensor')}</span><span class="orbit-node node-three">${icon('grid')}</span><div class="network-center">${icon('home')}</div><span class="network-pulse"></span></div><div class="network-summary"><div><i class="dot"></i><strong>${online}</strong><span>reachable</span></div><div><strong>${total}</strong><span>in inventory</span></div></div><div class="network-panel-footer"><span>${esc(net.subnet||'Detecting network')}</span><a href="#network" aria-label="View your network">${icon('arrow')}</a></div></article>`;
}
function deviceCard(d) {
 const [status,cls]=statusInfo(d), m=d.kind==='sensor'?{...(d.last_metrics||{}),...(d.latest_metrics||{}),...(d.metrics||{})}:d.metrics||{}, hasPower=Number.isFinite(metric(d,'power_w')), temperature=Object.entries(m).find(([k])=>k==='temperature_c'||k.startsWith('temperature:'));
 let main,second;
 if(d.kind==='media'){const activity=mediaState(d);main=`<span class="card-reading media-reading" title="${esc(activity.value)}">${esc(activity.value)}</span><span class="reading-label">${esc(activity.label)}</span>`;second=`${icon('speaker')}<span>${mediaLevel(d)}</span>`;}
 else if(hasPower){main=`<span class="card-reading">${fmt(m.power_w.value)}<small>W</small></span><span class="reading-label">Current power</span>`;const relay=m.relay_0;second=`<span class="relay-indicator ${relay?.value?'on':''}"></span><span>Relay ${relay?relay.value?'on':'off':'unavailable'}</span>`;}
 else if(temperature){main=`<span class="card-reading">${metricFormat(temperature[1])}</span><span class="reading-label">${d.status==='online'?'Room temperature':'Last reported temperature'}</span>`;second=`${icon(d.status==='online'?'activity':'moon')}<span>${d.status==='online'?'Recent sensor report':'Last reading · sensor may sleep'}</span>`;}
 else {main=`<span class="card-reading unavailable">—<small>${d.status==='reachable'?'No API yet':'No readings yet'}</small></span><span class="reading-label">${d.status==='reachable'?'Network connection detected':'Waiting for telemetry'}</span>`;second=`${icon(d.expected_sleep?'moon':'info')}<span>${d.expected_sleep?'Battery device · may sleep':d.name.startsWith('Heater candidate')?'Heater candidate · unconfirmed':'Device type unconfirmed'}</span>`;}
 const rssi=m.rssi_dbm;
 return `<button class="device-card ${cls}" data-device="${esc(d.ip)}"><div class="device-card-top"><span class="device-type-icon ${d.kind==='switch'?'sage':''}">${icon(deviceIcon(d))}</span>${badge(d)}</div><div class="device-card-title"><h3 title="${esc(d.name)}">${esc(d.name)}</h3>${icon('chevron')}</div><div class="device-meta">${esc(d.room)} <span>·</span> ${esc(deviceModel(d))}</div><div class="device-readout">${main}</div><div class="device-card-bottom"><span>${second}</span><span class="signal" title="${rssi?`${rssi.value} dBm`:'Network latency'}">${icon('wifi')}${rssi?`${rssi.value}`:Number.isFinite(metric(d,'latency_ms'))?`${fmt(metric(d,'latency_ms'),0)} ms`:'—'}</span></div><div class="device-ip">${esc(d.ip)}<span>${d.last_telemetry?`Updated ${timeAgo(d.last_telemetry)}`:d.last_seen?`Seen ${timeAgo(d.last_seen)}`:'Not yet seen'}</span></div></button>`;
}
function activityPanel(full=false) {
 const events=state.events.slice(0,full?50:4);
 return `<section class="panel activity-panel"><div class="panel-heading"><div><h2>Recent activity</h2><p>The small things happening around your home.</p></div><span class="activity-label">${icon('clock')} Live log</span></div><div class="activity-list">${events.length?events.map(e=>`<div class="activity-row"><span class="event-icon ${e.type==='online'?'positive':''}">${icon(e.type==='discovered'?'plus':e.type==='online'?'activity':e.type==='unreachable'?'moon':'wifi')}</span><div><p>${esc(e.message)}</p><small>${esc(e.ip||'Local collector')}</small></div><time title="${esc(dateTime(e.at))}">${timeAgo(e.at)}</time></div>`).join(''):`<div class="empty-state small"><p>Your home’s activity will appear here.</p></div>`}</div></section>`;
}
function devicesPage() {
 const filters=[['all','All devices',state.devices.length],['saved','My devices',state.devices.filter(d=>d.pinned).length],['switch','Switches',state.devices.filter(d=>d.kind==='switch').length],['sensor','Sensors',state.devices.filter(d=>d.kind==='sensor').length],['media','Media',state.devices.filter(d=>d.kind==='media').length],['unidentified','Other devices',state.devices.filter(d=>!d.kind).length]];
 const query=state.query.toLowerCase();
 const devices=state.devices.filter(d=>(state.filter==='all'||state.filter==='saved'&&d.pinned||state.filter==='unidentified'&&!d.kind||d.kind===state.filter)&&`${d.name} ${d.ip} ${d.room} ${d.model||''} ${d.advertised_model||''} ${d.hostname||''} ${d.discovered_name||''}`.toLowerCase().includes(query));
 return `${heading('Every connection. One place.', 'Your saved devices and everything discovered on your network.',true,'DEVICE INVENTORY')}${connectionBanner()}${scanBanner()}<div class="inventory-toolbar"><div class="filter-tabs">${filters.map(([key,label,n])=>`<button class="filter-tab ${key===state.filter?'active':''}" data-filter="${key}">${label}<span>${n}</span></button>`).join('')}</div><label class="search-field">${icon('search')}<input id="device-search" type="search" placeholder="Find a device…" value="${esc(state.query)}" aria-label="Find a device"></label></div><div class="inventory-caption"><span>${devices.length} ${devices.length===1?'device':'devices'}</span><span>Network devices may include phones, computers, and your router.</span></div><div class="devices-grid inventory-grid">${devices.map(deviceCard).join('')||`<div class="panel empty-state wide">${icon('search')}<h3>No devices here yet.</h3><p>Try another filter, add an IP address, or scan your home network.</p></div>`}</div><div class="notice lower-notice">${icon('info')}Names come from device broadcasts and your router’s DNS. Some devices publish only an ID or no name. Sleeping and isolated devices may need to be added manually.</div>`;
}
function telemetryPage() {
 const live=reporting();
 return `${heading('Let your home tell its story.', 'Explore collected readings, spot changes, and take your data with you.',false,'TELEMETRY & HISTORY')}${connectionBanner()}${roomTemperatures()}${chartPanel(true)}<section class="section"><div class="section-heading"><div><h2>Latest readings</h2><p>Direct from each reporting device.</p></div><a class="button secondary" href="/api/export.csv?hours=${state.range}">${icon('download')}Export CSV</a></div><div class="panel table-panel"><table><thead><tr><th>Device</th><th>Metric</th><th>Latest value</th><th>Last telemetry</th></tr></thead><tbody>${live.flatMap(d=>Object.entries(d.metrics||{}).filter(([k])=>!/^power_\d+_w$/.test(k)&&!k.startsWith('relay_')).map(([k,m],i)=>`<tr><td>${i===0?`<button class="table-device" data-device="${esc(d.ip)}">${icon(deviceIcon(d))}${esc(d.name)}</button>`:''}</td><td>${esc(m.label)}</td><td class="value-cell">${metricFormat(m)}</td><td class="muted">${timeAgo(d.last_telemetry)}</td></tr>`)).join('')||'<tr><td colspan="4" class="table-empty">Readings will appear when compatible devices respond.</td></tr>'}</tbody></table></div></section>`;
}
function networkPage() {
 const net=state.collector.network;
 return `${heading('A view beneath the surface.', 'Discover connections and see which devices are ready to share data.',true,'HOME NETWORK')}${connectionBanner()}${scanBanner()}<section class="network-info-grid">${statCard('Home subnet',esc(net.subnet||'Unknown'),'','Discovery limited to this /24 network','network')}${statCard('Gateway',esc(net.gateway||'Unknown'),'','Your local network’s front door','wifi')}${statCard('Collector IP',esc(net.local_ip||'Unknown'),' ',`This Mac · ${esc(net.interface||'unknown interface')}`,'server')}</section><section class="panel"><div class="panel-heading"><div><h2>Network inventory</h2><p>${state.devices.length} devices saved · Last scan ${timeAgo(state.collector.last_scan)} · Names ${timeAgo(state.collector.last_name_discovery)}</p></div><div class="network-actions"><button class="text-button names-button" ${state.collector.naming?'disabled':''}>${icon('search')}${state.collector.naming?'Finding names…':'Refresh names'}</button><button class="text-button scan-button" ${state.collector.scanning?'disabled':''}>${icon('scan')}Scan again</button></div></div><div class="table-scroll"><table><thead><tr><th>Device</th><th>IP address</th><th>Hardware address</th><th>Connection</th><th>Telemetry API</th><th>Last seen</th><th></th></tr></thead><tbody>${state.devices.map(d=>`<tr><td><button class="table-device" data-device="${esc(d.ip)}">${icon(deviceIcon(d))}<span><strong>${esc(d.name)}</strong><small>${esc(deviceModel(d))}</small></span></button></td><td class="mono">${esc(d.ip)}</td><td class="mono muted">${esc(d.mac||'Not observed')}</td><td>${badge(d)}</td><td><span class="protocol-tag ${d.status==='online'?'supported':''}">${esc(protocolLabel(d.protocol))}</span></td><td class="muted">${timeAgo(d.last_seen)}</td><td><button class="icon-button" data-device="${esc(d.ip)}" aria-label="View ${esc(d.name)}">${icon('chevron')}</button></td></tr>`).join('')}</tbody></table></div></section><div class="notice lower-notice">${icon('shield')}Discovery uses ping, the local ARP cache, and read-only Shelly requests. A cached address alone is never marked online.</div>`;
}
function settingsPage() {
 const c=state.collector;
 return `${heading('Make yourself at home.', 'A few simple settings for your local workspace.',false,'WORKSPACE SETTINGS')}<div class="settings-layout"><section class="panel settings-panel"><div class="panel-heading"><div><h2>Your local collector</h2><p>A direct connection to your home.</p></div><span class="status-badge ${state.connected?'online':'offline'}"><i class="dot"></i>${state.connected?'Running':'Disconnected'}</span></div><dl class="settings-list"><div><dt>Collection interval</dt><dd>Every ${c.poll_seconds||30} seconds</dd></div><div><dt>History retention</dt><dd>${c.retention_days||30} days</dd></div><div><dt>Data storage</dt><dd>SQLite on this Mac</dd></div><div><dt>Started</dt><dd>${dateTime(c.started_at)}</dd></div><div><dt>Last collection</dt><dd>${dateTime(c.last_poll)}</dd></div><div><dt>Device access</dt><dd>Read-only telemetry</dd></div></dl><div class="notice">${icon('info')}${c.background?'Background collection starts at login and recovers after failures.':'Keep the server running to collect readings.'} This Mac must be awake. Closing the browser doesn’t stop collection.</div></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>A little more personal</h2><p>Preferences for this browser.</p></div>${icon('settings')}</div><div class="setting-choice"><div><strong>Temperature unit</strong><p>Applies to charts and device readings.</p></div><div class="segmented">${['F','C'].map(u=>`<button class="${state.unit===u?'selected':''}" data-unit="${u}" aria-pressed="${state.unit===u}">°${u}</button>`).join('')}</div></div><div class="setting-choice export-setting"><div><strong>Your data, to go</strong><p>Export all readings from the past 30 days.</p></div><a href="/api/export.csv?hours=720" class="button secondary">${icon('download')}Export CSV</a></div><div class="supported-apis"><h3>Connected locally</h3><div><span class="api-logo">S</span><span><strong>Shelly Gen 1 & Gen 2+</strong><small>Power, sensors, relays, signal & uptime when exposed</small></span>${icon('check')}</div><p class="muted">Local readers also support Roku, Sonos, WiiM UPnP, and Google Cast receivers, including RIVA. Other paired or cloud-managed devices need their own integration. Authentication-protected APIs are flagged for setup.</p></div></section></div>${heatingSettings()}${collectionStatusPanel()}${roomSensorSettings()}${activityPanel(true)}`;
}
function render() {
 const focused=document.activeElement, focusID=focused?.id, selection=focused?.selectionStart;
 const views={overview,heating:heatingPage,devices:devicesPage,telemetry:telemetryPage,network:networkPage,settings:settingsPage};
 $('#main').innerHTML=(views[state.page]||overview)();
 $$('[data-color]').forEach(el=>{el.style.backgroundColor=el.dataset.color;});
 $$('[data-bar-height]').forEach(el=>{el.style.height=el.dataset.barHeight+'%';});
 $$('[data-energy-count]').forEach(el=>{el.style.setProperty('--days',el.dataset.energyCount);});
 if(focusID && $(`#${focusID}`)) {const input=$(`#${focusID}`);input.focus({preventScroll:true});if(typeof selection==='number'&&input.setSelectionRange&&input.type==='search')input.setSelectionRange(selection,selection);}
}
function route() {
 const page=location.hash.slice(1)||'overview';state.page=['overview','heating','devices','telemetry','network','settings'].includes(page)?page:'overview';
 $$('[data-page]').forEach(a=>{const active=a.dataset.page===state.page;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
 $('#crumb').textContent=state.page[0].toUpperCase()+state.page.slice(1);document.title=`${$('#crumb').textContent} · Hearth`;render();window.scrollTo(0,0);
}
function openAdd() { $('#add-form').reset();$('#add-error').textContent='';$('#device-dialog').showModal(); }
function openDetail(ip) {
 const d=state.devices.find(d=>d.ip===ip);if(!d)return;detailIP=ip;
 const entries=Object.entries(d.metrics||{}).filter(([k])=>!/^power_\d+_w$/.test(k));
 const lastEntries=d.status!=='online'?Object.entries(d.last_metrics||{}).filter(([k])=>!/^power_\d+_w$/.test(k)&&k!=='latency_ms'):[];
 const [label, cls]=statusInfo(d);
 $('#detail-content').innerHTML=`<div class="modal-heading"><div class="detail-header"><span class="device-type-icon sage">${icon(deviceIcon(d))}</span><div><span class="eyebrow">${esc(d.model||d.advertised_model||(d.discovered_name?'NETWORK DEVICE':'UNIDENTIFIED DEVICE'))}</span><h2 id="detail-title">${esc(d.name)}</h2></div></div><button class="icon-button close-detail" aria-label="Close device details">${icon('close')}</button></div><div class="detail-status">${badge(d)}<span class="mono muted">${esc(d.ip)}</span></div>${d.discovered_name?`<div class="discovered-identity"><div>${icon('wifi')}<strong>Discovered identity</strong><span>${esc(d.discovered_name_source)}</span></div><p>${esc(d.discovered_name)}</p>${d.hostname?`<small class="mono">${esc(d.hostname)}</small>`:''}<small>Observed ${timeAgo(d.name_discovered_at)}${d.name_mode==='manual'?' · Custom display name preserved':' · Used as the display name'}</small></div>`:''}${d.error?`<div class="notice warning">${icon('info')}${esc(d.error)}</div>`:''}${d.telemetry_note?`<div class="notice">${icon('info')}${esc(d.telemetry_note)}</div>`:''}<div class="detail-metrics">${entries.map(([k,m])=>`<div><span>${esc(m.label)}</span><strong>${k.startsWith('relay_')?m.value?'On':'Off':metricFormat(m)}</strong></div>`).join('')||'<div><span>Telemetry</span><strong>No readings yet</strong></div>'}</div>${lastEntries.length?`<details class="last-readings"><summary>Last successful telemetry · ${dateTime(d.last_telemetry)}</summary><p class="muted">Historical readings, not current device state.</p><div class="detail-metrics stale">${lastEntries.map(([k,m])=>`<div><span>${esc(m.label)}</span><strong>${metricFormat(m)}</strong></div>`).join('')}</div></details>`:''}<dl class="device-info-list"><div><dt>Last seen on network</dt><dd>${dateTime(d.last_seen)}</dd></div><div><dt>Last successful telemetry</dt><dd>${dateTime(d.last_telemetry)}</dd></div><div><dt>MAC address</dt><dd class="mono">${esc(d.mac||'Not observed')}</dd></div><div><dt>Firmware</dt><dd class="mono">${esc(d.firmware||'Not identified')}</dd></div></dl>${Object.keys(d.metrics||{}).some(k=>k.includes('internal_temperature'))?`<div class="notice">${icon('temp')}Internal device temperature is not the room temperature.</div>`:''}<form id="edit-form"><div class="detail-edit-heading"><h3>Make it yours</h3><span>Workspace labels only</span></div><div class="form-row"><label>Device name<input name="name" value="${esc(d.name)}" maxlength="100" required></label><label>Room<input name="room" value="${esc(d.room)}" maxlength="100" required></label></div><label>Notes<textarea name="notes" rows="2" maxlength="1000">${esc(d.notes)}</textarea></label><div class="detail-checks"><label class="check-label"><input name="auto_name" type="checkbox" ${d.name_mode==='auto'?'checked':''}>Use discovered name when available</label><label class="check-label"><input name="pinned" type="checkbox" ${d.pinned?'checked':''}>Show in My devices</label><label class="check-label"><input name="expected_sleep" type="checkbox" ${d.expected_sleep?'checked':''}>Battery device that sleeps</label></div><p id="edit-error" class="form-error" role="alert"></p><div class="detail-actions"><a class="button secondary" href="/api/export.csv?hours=${state.range}&ip=${esc(d.ip)}">${icon('download')}Export history</a><button class="button primary" type="submit">${icon('check')}Save details</button></div></form>${d.raw_status?`<details class="raw-details"><summary>Last API snapshot <span>${dateTime(d.raw_status_at||d.last_telemetry)}</span></summary><pre>${esc(JSON.stringify(d.raw_status,null,2))}</pre></details>`:''}`;
 if(!$('#detail-dialog').open)$('#detail-dialog').showModal();
}
async function scan() {
 try{await api('/api/scan',{method:'POST',body:'{}'});toast('Discovering devices on your home network…');await load();}catch(e){toast(e.message,true);}
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
 if(target.dataset.filter){state.filter=target.dataset.filter;render();}
 if(target.dataset.range){state.range=Number(target.dataset.range);await loadHistory();render();}
 if(target.dataset.energyDays){state.energyDays=Number(target.dataset.energyDays);await loadEnergy();render();}
 if(target.dataset.series){state.chartDevices.has(target.dataset.series)?state.chartDevices.delete(target.dataset.series):state.chartDevices.add(target.dataset.series);render();}
 if(target.dataset.unit){state.unit=target.dataset.unit;localStorage.setItem('hearth-unit',state.unit);render();}
});
document.addEventListener('change',e=>{if(e.target.name==='auto_name'&&e.target.checked){const d=state.devices.find(d=>d.ip===detailIP);$('#edit-form input[name="name"]').value=d.discovered_name||`Device .${detailIP.split('.').at(-1)}`;}if(e.target.id==='metric-select'){state.metric=e.target.value;state.chartDevices.clear();render();}});
document.addEventListener('input',e=>{if(e.target.name==='name'&&e.target.closest('#edit-form'))$('#edit-form input[name="auto_name"]').checked=false;if(e.target.id==='device-search'){state.query=e.target.value;render();}});
$('#add-form').addEventListener('submit',async e=>{
 e.preventDefault();const form=e.target,button=$('button[type="submit"]',form);button.disabled=true;$('#add-error').textContent='';
 try{const fields=new FormData(form);await api('/api/devices',{method:'POST',body:JSON.stringify({name:fields.get('name'),ip:fields.get('ip'),room:fields.get('room'),expected_sleep:fields.has('expected_sleep')})});$('#device-dialog').close();toast('Device added. Checking for telemetry…');await load();}catch(err){$('#add-error').textContent=err.message;}finally{button.disabled=false;}
});
document.addEventListener('submit',async e=>{
 if(e.target.id==='heating-settings') {
  e.preventDefault();const form=e.target,fields=new FormData(form),button=$('button[type="submit"]',form);button.disabled=true;
  const rate=String(fields.get('rate_per_kwh')).trim();
  try{await api('/api/settings/heating',{method:'POST',body:JSON.stringify({rate_per_kwh:rate===''?null:Number(rate),timezone:fields.get('timezone'),meter_macs:fields.getAll('meter_macs')})});state.heatingDraft=null;toast('Heating settings saved.');await load(true);}catch(err){toast(err.message,true);button.disabled=false;}
  return;
 }
 if(e.target.matches('.room-link-form')) {
  e.preventDefault();const form=e.target,button=$('button',form),selected=new FormData(form).get('sensor');button.disabled=true;
  const [ip,key]=String(selected||'').split('|');
  try{await api(`/api/rooms/${form.dataset.room}/sensor`,{method:'POST',body:JSON.stringify({ip:ip||null,metric:key||'temperature_c'})});toast('Room temperature source saved.');await load();}catch(err){toast(err.message,true);}finally{button.disabled=false;}
  return;
 }
 if(e.target.id!=='edit-form')return;e.preventDefault();const form=e.target,fields=new FormData(form),button=$('button[type="submit"]',form);button.disabled=true;
 try{await api(`/api/devices/${detailIP}`,{method:'PATCH',body:JSON.stringify({name:fields.get('name'),name_mode:fields.has('auto_name')?'auto':'manual',room:fields.get('room'),notes:fields.get('notes'),pinned:fields.has('pinned'),expected_sleep:fields.has('expected_sleep')})});$('#detail-dialog').close();toast('Device details saved.');await load();}catch(err){$('#edit-error').textContent=err.message;button.disabled=false;}
});
$$('dialog').forEach(dialog=>dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}}));
window.addEventListener('hashchange',route);
document.addEventListener('input',rememberHeatingDraft);
document.addEventListener('change',rememberHeatingDraft);
route();load(true);setInterval(load,5000);setInterval(updateChrome,15000);

async function loadEnergy() {
 try{state.energy=await api(`/api/energy?days=${state.energyDays}`);state.energyError=null;state.lastEnergyPoll=state.collector.last_poll;}
 catch(err){state.energyError=err.message;}
}
function money(value) {return Number.isFinite(value)?new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(value):'—';}
function heatingOverview() {
 const e=state.energy,today=e?.daily.at(-1);
 if(!e?.sources.length)return '';
 return `<section class="panel heating-overview"><div><span class="eyebrow">HEATING TODAY</span><h2>${Number.isFinite(today?.kwh)?fmt(today.kwh,3):'—'} <small>kWh observed</small></h2><p>${today?.cost!==null?`${money(today?.cost)} estimated cost`:'Add your electricity rate in Settings'} · ${fmt(today?.coverage_pct)}% of meter time covered${state.energyError?' · Summary update unavailable':''}</p></div><a href="#heating" class="button secondary">${icon('bolt')}View heating</a></section>`;
}
function heatingPage() {
 const e=state.energy;
 let result=`${heading('Understand your heating.', 'Measured energy, estimated cost, and a clear view of what was collected.',false,'HEATING & ENERGY')}${connectionBanner()}`;
 if(state.energyError)result+=`<div class="notice warning">${icon('info')}Heating summary could not refresh: ${esc(state.energyError)}. Any displayed totals are from the last successful update.</div>`;
 if(!e)return result+`<div class="empty-state"><p>Waiting for heating readings…</p></div>`;
 const total=e.total,rate=e.settings.rate_per_kwh,label=e.days===1?'Today':`Last ${e.days} calendar days`;
 result+=`<div class="energy-toolbar"><div class="segmented">${[[1,'Today'],[7,'7 days'],[30,'30 days']].map(([days,text])=>`<button data-energy-days="${days}" class="${e.days===days?'selected':''}" aria-pressed="${e.days===days}">${text}</button>`).join('')}</div><span>${esc(e.settings.timezone)}</span><a class="button secondary" href="/api/energy.csv?days=${e.days}">${icon('download')}Export heating</a></div>`;
 if(!e.sources.length)return result+`<div class="notice">${icon('info')}Choose your heating energy meters in <a href="#settings">Settings</a> to begin.</div>`;
 result+=`<section class="stats-grid energy-stats">${statCard('Observed heating energy',Number.isFinite(total.kwh)?fmt(total.kwh,3):'—','kWh',label,'bolt','highlight')}${statCard('Estimated energy cost',money(total.cost),'',rate===null?'Set a rate in Settings':`At $${fmt(rate,6)} / kWh`,'leaf')}${statCard('Meter time covered',fmt(total.coverage_pct),'%',`${e.sources.length} selected heating meters`,'activity')}${statCard('Excluded intervals',e.sources.reduce((sum,s)=>sum+s.gaps+s.resets,0),'',`Long gaps and counter resets`,'clock')}</section>`;
 result+=`<div class="notice ${total.coverage_pct<95?'warning':''}">${icon('info')}Totals cover observed intervals only, not whole-home usage. Missing time, gaps over two minutes, and detected meter resets are excluded. ${rate===null?'Enter an electricity rate in Settings to see cost estimates.':'Costs use your saved rate and exclude fixed monthly charges.'}</div>`;
 result+=`<section class="panel energy-panel"><div class="panel-heading"><div><h2>Heating by floor</h2><p>${label} · Hardware-linked meter history</p></div><a class="text-button" href="#settings">Manage meters ${icon('arrow')}</a></div><div class="table-scroll"><table><thead><tr><th>Heating meter</th><th>Observed kWh</th><th>Est. cost</th><th>Coverage</th><th>Latest reading</th></tr></thead><tbody>${e.sources.map(s=>`<tr><td><strong>${esc(s.room)}</strong><small class="energy-source-name">${esc(s.name)}</small></td><td>${Number.isFinite(s.kwh)?fmt(s.kwh,3):'—'}</td><td>${money(s.cost)}</td><td>${fmt(s.coverage_pct)}%${s.gaps+s.resets?`<small class="energy-source-name">${s.gaps} gaps · ${s.resets} resets</small>`:''}</td><td>${timeAgo(s.last_reading)}</td></tr>`).join('')}</tbody></table></div></section>`;
 const max=Math.max(.001,...e.daily.map(d=>d.kwh||0));
 result+=`<section class="panel energy-panel"><div class="panel-heading"><div><h2>Daily observed energy</h2><p>Partial days show the energy we could measure.</p></div></div><div class="energy-chart-scroll"><div class="energy-bars" data-energy-count="${e.daily.length}">${e.daily.map(d=>`<div class="energy-day" title="${esc(d.date)}: ${d.kwh===null?'No observations':`${fmt(d.kwh,3)} kWh, ${fmt(d.coverage_pct)}% coverage`}"><span class="energy-bar-value">${d.kwh===null?'—':fmt(d.kwh,2)}</span><div class="energy-bar-track"><div class="energy-bar ${d.kwh===null?'missing':''}" data-bar-height="${d.kwh===null?2:Math.max(2,(d.kwh/max)*100)}"></div></div><span>${new Date(d.date+'T12:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'})}</span></div>`).join('')}</div></div><div class="table-scroll"><table><thead><tr><th>Local date</th><th>Observed kWh</th><th>Est. cost</th><th>Coverage</th></tr></thead><tbody>${[...e.daily].reverse().map(d=>`<tr><td>${esc(d.date)}</td><td>${d.kwh===null?'—':fmt(d.kwh,3)}</td><td>${money(d.cost)}</td><td>${fmt(d.coverage_pct)}%</td></tr>`).join('')}</tbody></table></div><p class="energy-method">Usage is the change between consecutive meter counters. The first reading establishes a baseline. Intervals crossing midnight are split in proportion to time, over at most two minutes. Raw readings and energy history are retained for 30 days. ${esc(e.rate_note||'')}</p></section>`;
 return result;
}
function rememberHeatingDraft(event) {
 const form=event.target.closest('#heating-settings');if(!form)return;
 const fields=new FormData(form);state.heatingDraft={rate_per_kwh:fields.get('rate_per_kwh'),timezone:fields.get('timezone'),meter_macs:fields.getAll('meter_macs')};
}
function heatingSettings() {
 const e=state.energy;if(!e)return '';
 const settings=state.heatingDraft||e.settings;
 return `<section class="panel energy-panel"><div class="panel-heading"><div><h2>Heating energy & cost</h2><p>Shared settings for this home. Rates are in US dollars.</p></div>${icon('bolt')}</div><form id="heating-settings" class="energy-settings"><div class="form-row"><label>Electricity rate ($ / kWh)<input id="heating-rate" type="number" name="rate_per_kwh" min="0" max="10" step="0.000001" placeholder="Enter your rate" value="${esc(settings.rate_per_kwh??'')}"></label><label>Reporting time zone<input id="heating-timezone" name="timezone" maxlength="100" required value="${esc(settings.timezone)}"></label></div><p class="energy-method">Include usage-based supply and delivery charges. Leave blank to hide cost estimates. Fixed monthly fees are excluded. Changing this rate recalculates all displayed estimates. ${esc(e.rate_note||'')}</p><fieldset><legend>Heating energy meters</legend>${e.available_meters.map(m=>`<label class="check-label"><input type="checkbox" name="meter_macs" value="${esc(m.source_id)}" ${settings.meter_macs.includes(m.source_id)?'checked':''}>${esc(m.name)} · ${esc(m.room)}</label>`).join('')||'<p>No identified energy meters yet.</p>'}</fieldset><button type="submit" class="button primary">${icon('check')}Save heating settings</button></form></section>`;
}
function collectionStatusPanel() {
 const c=state.collector,b=c.backup,gap=c.last_gap;
 return `<section class="panel energy-panel"><div class="panel-heading"><div><h2>Background collection & backups</h2><p>${c.background?'Managed on this Mac · starts at login and restarts after failures.':'Manual session · install background collection to start at login.'}</p></div>${icon('server')}</div><dl class="settings-list"><div><dt>Collection health</dt><dd>${esc(c.health||'Connecting')}${c.collection_error?` · ${esc(c.collection_error)}`:''}</dd></div><div><dt>Latest daily backup</dt><dd>${b?dateTime(b.at):'Waiting for first backup'}</dd></div><div><dt>Backup retention</dt><dd>Seven daily snapshots</dd></div><div><dt>Latest collection gap</dt><dd>${gap?`${dateTime(gap.start)} → ${dateTime(gap.end)}`:'None recorded'}</dd></div></dl>${c.backup_error?`<div class="notice warning">${icon('info')}Backup needs attention: ${esc(c.backup_error)}</div>`:''}<p class="energy-method">${c.background?'The installed collector and history live on this Mac’s internal drive.':'Run install-background.command from the project folder to install a managed local copy.'} This Mac must be awake and connected to your home network. Collection resumes after sleep; missed intervals remain visible as gaps.</p></section>`;
}
