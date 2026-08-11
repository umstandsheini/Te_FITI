const $=s=>document.querySelector(s);
const CAMS=[{k:"front",a:"a-front",l:"Front"},{k:"back",a:"a-back",l:"Rear"},
  {k:"left_repeater",a:"a-left",l:"Left"},{k:"right_repeater",a:"a-right",l:"Right"},
  {k:"left_pillar",a:"a-lp",l:"Pillar L"},{k:"right_pillar",a:"a-rp",l:"Pillar R"}];
let allClips=[], videos=[], master=null, raf=0, tele=null, activeId=null, curEvent=null;
let lmap=null, lline=null, lmark=null, gpsPts=[];
let initialSeek=null;
let gpsFilter=null, _gpsMarkers=[];
let eventSegs=[], eventSegIdx=-1;   // this event's other 1-min segments, for the player's cross-clip nav

const BM="(async()=>{const pick=document.createElement('input');pick.type='file';pick.accept='application/json,.json';pick.onchange=async()=>{try{const job=JSON.parse(await pick.files[0].text());const items=job.items||job;let raw=sessionStorage.getItem('ROCP_token'),token=raw;try{const p=JSON.parse(raw);token=(typeof p==='string')?p:(p.access_token||p.token||p.accessToken||raw);}catch(e){}if(!token){alert('No Tesla token – log in to dashcam.tesla.com first.');return;}const out=[],CH=30;for(let i=0;i<items.length;i+=CH){const r=await fetch('/api/1/decrypt/batch',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token,'Accept':'application/json'},body:JSON.stringify({items:items.slice(i,i+CH)})});if(!r.ok){alert('API error '+r.status+' at chunk '+i);return;}const j=await r.json();(j.results||[]).forEach(x=>{if(x.key)out.push({id:x.id,key:x.key});});}const blob=new Blob([JSON.stringify({results:out})],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='keys.json';a.click();alert('Done: '+out.length+' keys -> keys.json');}catch(e){alert('Error: '+e.message);}};pick.click();})();";

// ---------- Progress bars ----------
// One renderer for the index scan and both batch jobs. total<=0 renders as
// indeterminate — the folder walk only learns its total once it is done.
function renderBar(el,{label,done,total,note}){
  if(!el) return;
  const known=total>0;
  const pct=known?Math.min(100,Math.round(done/total*100)):0;
  el.innerHTML=`<div class="pbar${known?"":" indet"}">`
    +`<div class="pbLabel"><span>${label}</span><b>${known?done+" / "+total+" · "+pct+"%":(note||"")}</b></div>`
    +`<div class="pbTrack"><div class="pbFill"></div></div>`
    +(known&&note?`<div class="pbLabel"><span>${note}</span></div>`:"")
    +`</div>`;
  if(known) el.querySelector(".pbFill").style.width=pct+"%";
}

const SCAN_PHASES={
  walk:  j=>({label:"📂 Indexing folders on the NAS…", done:j.done, total:0,
              note:j.done+" folders"}),
  // "index" only counts files never seen before — everything else is a cache
  // hit and costs no NAS access at all.
  index: j=>({label:"🔍 Inspecting new clips…", done:j.done, total:j.total,
              note:"one file read per clip"}),
  meta:  j=>({label:"📊 Reading telemetry & events…", done:j.done, total:j.total,
              note:j.new?j.new+" new clips":"all from cache"})
};

let _scanWasRunning=false;
function renderScanBar(s){
  const bar=$("#scanbar"), j=(s&&s.scan_job)||{};
  const show=!!j.running;
  bar.classList.toggle("on",show);
  if(show){
    const f=SCAN_PHASES[j.phase]||SCAN_PHASES.walk;
    renderBar(bar,f(j));
  }else{
    bar.innerHTML="";
  }
  // A finished scan means the clip list changed under us — pull it in.
  if(_scanWasRunning&&!show) loadClips(true);
  _scanWasRunning=show;
  return show;
}

// Trips and analytics are built in the background too — the first build on a
// large library takes minutes. Reload each as soon as its build finishes.
let _wasBuilding={trips:false,analytics:false};
function trackDerivedBuilds(s){
  const b=(s&&s.building)||{};
  if(_wasBuilding.trips&&!b.trips) loadTrips().catch(()=>{});
  if(_wasBuilding.analytics&&!b.analytics&&currentView==="analytics") loadAnalytics();
  _wasBuilding={trips:!!b.trips,analytics:!!b.analytics};
  return b;
}

// ---------- Status ----------
async function refreshStatus(){
  try{
    const s=await fetch("api/status").then(r=>r.json());
    renderScanBar(s);
    trackDerivedBuilds(s);
    if(s.ready===false&&!$("#cliplist").querySelector(".cliprow")){
      $("#cliplist").innerHTML='<div class="loading">⏳ Building the clip index — the list appears as soon as it is done.</div>';
    }
    $("#status").innerHTML=`🎞️ <b>${s.clips}</b> Clips · 🔒 <b>${s.encrypted}</b> · 🔑 <b>${s.keyed}</b> · ✅ <b>${s.decrypted}</b> · ⏳ no key <b>${s.need_keys}</b>`+(s.busy?" · running…":"");
    const li=s.login||{};
    const api=s.last_api||{};
    // A token file exists (li.logged_in) but the refresh may still be dead —
    // the backend can't tell cheaply, but a failed fetch reports it in
    // last_api. Treat that as "session expired": red pill + show the login box
    // so there's an obvious way to recover, instead of a green "logged in ✓"
    // sitting next to keys that never arrive.
    const expired=api.ok===false && /log in again|login expired|not logged in/i.test(api.msg||"");
    const healthy=li.logged_in && !expired;
    $("#lpill").className="pill "+(healthy?"ok":"bad");
    $("#lpill").textContent=expired?"session expired — log in again"
      :(li.logged_in?(li.has_refresh?"logged in ✓":"logged in"):"not logged in");
    $("#loginbox").style.display=healthy?"none":"block";
    // Report every outcome, including "0 new keys". Previously the message was
    // only replaced when got>0, so a fetch that found nothing left the panel
    // reading "Fetching keys…" indefinitely.
    if(!s.busy&&api.ok!==null&&api.ok!==undefined){
      const nk=s.no_wrapped_key||0;
      $("#lmsg").textContent=(api.ok===false)
        ? "Direct API: "+api.msg
        : `Direct API: ${api.got||0} new key(s).`
          +(s.need_keys?` ${s.need_keys} still missing.`:"")
          +(nk?` ${nk} file(s) carry no key of their own and cannot be recovered.`:"");
    }
    $("#tstat").textContent=`Clips ${s.clips} · encrypted ${s.encrypted} · with key ${s.keyed} · no key ${s.need_keys}`;
    const dn=$("#decDeleteNote");
    if(dn) dn.innerHTML=s.delete_originals
      ? '<b style="color:var(--bad)">delete_originals is on: the encrypted originals are deleted afterwards. This cannot be undone.</b>'
      : 'The encrypted originals are kept (<code>delete_originals</code> is off).';
    return s;
  }catch(e){return null;}
}

// ---------- Clip browser ----------
async function loadClips(keepActive){
  if(!keepActive) $("#cliplist").innerHTML='<div class="loading">⏳ Loading clips…</div>';
  allClips=await fetch("api/clips").then(r=>r.json()).catch(()=>[]);
  populateReasonFilter();
  populatePurgeReason();
  buildSidebar();
  // No-op until the map exists; keeps the markers in step when the list is
  // reloaded (scan finished, keys fetched) while the Map tab is open.
  renderEventMarkers();
  if(keepActive&&activeId){const c=allClips.find(x=>x.id===activeId); if(c) markActive(activeId);}
}
function clipState(c){ return c.has_locked&&!c.playable&&!c.needs_prepare ? "locked" : (c.needs_prepare?"key":"ready"); }
let _collapsed={};
const _lockSvg=(color,title)=>`<svg title="${title}" width="12" height="14" viewBox="0 0 24 28" style="vertical-align:middle"><path d="M6 12V8a6 6 0 1 1 12 0v4h1a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3v-8a3 3 0 0 1 3-3h1z" fill="${color}"/></svg>`;
function _lockBadge(c){
  if(c.has_nokey && !c.needs_prepare && !c.playable)
    return _lockSvg("#f87171","encrypted, but the file contains no key — Tesla stored none, so it cannot be decrypted");
  if(!c.has_locked && !c.needs_prepare) return "";
  if(c.needs_prepare) return _lockSvg("#34d399","encrypted – key available");
  return _lockSvg("#9aa7b4","encrypted – no key yet");
}
function _clipRow(c){
  const r=document.createElement("div"); r.className="cliprow"+(c.id===activeId?" active":"")+(c.is_trigger?" trigger":""); r.dataset.id=c.id;
  // Every segment of an event folder carries 📅, but only one of them contains
  // the moment the event fired — that one gets 🎯 and a highlight.
  const at=(c.event_at!=null)?` at ${Math.floor(c.event_at/60)}:${String(Math.round(c.event_at%60)).padStart(2,"0")}`:"";
  const evBadge=c.is_trigger
    ? `<span class="trigBadge" title="The event happens in this clip${at}">🎯</span>`
    : (c.has_event?'<span title="Part of an event – the trigger is in another segment">📅</span>':"");
  const keptBadge=c.protected?'<span class="keptBadge" title="Kept — protected from the storage cleanup">📌</span>':"";
  const badges=(c.has_tel?'<span title="Telemetry/HUD available">📊</span>':"")+evBadge+keptBadge+_lockBadge(c);
  const tm=c.timestamp.replace("_"," ").replace(/-/g,(m,i)=>i>9?":":"-");
  r.innerHTML=`<img class="thumb" loading="lazy" src="api/thumb?id=${encodeURIComponent(c.id)}" onerror="this.classList.add('noimg');this.removeAttribute('src')"><span class="cmid"><span class="cliptime">${tm}</span><span class="badges">${badges}</span></span>`;
  r.onclick=()=>openPlayerOverlay(c);
  return r;
}
// A Sentry/Saved event's segments (all sharing one event.json/folder) are
// otherwise indistinguishable rows in a row — clustering consecutive same-
// folder event clips lets them collapse to one summary row by default.
// _expandedEvents persists across rebuilds the same way _collapsed does for
// vehicle groups, so re-filtering doesn't re-collapse something you opened.
let _expandedEvents=new Set();
// Same idea for RecentClips loop-recording: a driving/awake-Sentry stretch
// writes one clip per minute into the SAME folder as every other stretch
// that day, so folder equality alone (the event rule above) would merge
// unrelated sessions together. A session is instead a run of consecutive,
// non-event, same-folder clips no more than 90s apart -- comfortably above
// the ~60s segment cadence, comfortably below the multi-minute gap that
// marks the car actually going back to sleep between wake-ups.
function _tsMs(ts){
  const m=ts.match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$/);
  return m?Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5],+m[6]):null;
}
function _tsGapOk(a,b){ const x=_tsMs(a),y=_tsMs(b); return x!=null&&y!=null&&Math.abs(x-y)<=90000; }
let _expandedSessions=new Set();
function clusterEvents(clips){
  const out=[];
  let i=0;
  while(i<clips.length){
    const c=clips[i];
    if(c.has_event && c.folder){
      let j=i+1;
      while(j<clips.length && clips[j].folder===c.folder && clips[j].has_event) j++;
      if(j-i>1){ out.push({type:"event",folder:c.folder,clips:clips.slice(i,j)}); i=j; continue; }
    }
    if(!c.has_event && c.folder){
      let j=i+1;
      while(j<clips.length && clips[j].folder===c.folder && !clips[j].has_event
            && _tsGapOk(clips[j-1].timestamp,clips[j].timestamp)) j++;
      if(j-i>1){ out.push({type:"session",clips:clips.slice(i,j)}); i=j; continue; }
    }
    out.push({type:"single",clip:c}); i++;
  }
  return out;
}
function appendClips(el,clips){
  for(const item of clusterEvents(clips)){
    if(item.type==="single"){ el.appendChild(_clipRow(item.clip)); continue; }
    const isEvent=item.type==="event";
    // clips within a cluster are in list order (newest first); the oldest
    // (last) one's id is a stable key across rebuilds even as a live session
    // grows a new segment onto its newest end.
    const key=(isEvent?item.folder:item.clips[item.clips.length-1].id);
    const expandedSet=isEvent?_expandedEvents:_expandedSessions;
    const expanded=expandedSet.has(key);
    if(!expanded){
      const head=isEvent?(item.clips.find(c=>c.is_trigger)||item.clips[0]):item.clips[0];
      const row=_clipRow(head);
      const badge=document.createElement("button");
      badge.className="evExpand"; badge.type="button";
      if(isEvent){
        badge.title=`Show all ${item.clips.length} clips of this event`;
      } else {
        const from=item.clips[item.clips.length-1].timestamp.slice(11).replace("-",":").slice(0,5);
        const to=item.clips[0].timestamp.slice(11).replace("-",":").slice(0,5);
        badge.title=`Show all ${item.clips.length} clips of this recording (${from}–${to})`;
      }
      badge.textContent="▸ "+item.clips.length;
      badge.onclick=(e)=>{ e.stopPropagation(); expandedSet.add(key); buildSidebar(); };
      row.querySelector(".badges").appendChild(badge);
      el.appendChild(row);
    } else {
      const head=document.createElement("div"); head.className="eventHead";
      head.innerHTML=isEvent?`<span>▾ Event · ${item.clips.length} clips</span>`
                             :`<span>▾ Recording · ${item.clips.length} clips</span>`;
      head.onclick=()=>{ expandedSet.delete(key); buildSidebar(); };
      el.appendChild(head);
      for(const c of item.clips){
        const row=_clipRow(c); row.classList.add("eventSeg");
        el.appendChild(row);
      }
    }
  }
}
function buildSidebar(){
  const q=$("#search").value.trim().toLowerCase(), fDrive=$("#filterDrive").checked, fEvent=$("#filterEvent").checked, fHonk=$("#filterHonk").checked;
  const filtered=[];
  for(const c of allClips){
    if(fDrive && !c.has_tel) continue;
    if(fEvent && !c.has_event) continue;
    if(fHonk && c.reason!=="user_interaction_honk") continue;
    if(reasonFilter && c.reason!==reasonFilter) continue;
    if(q && !(c.timestamp.toLowerCase().includes(q) || (c.folder||"").toLowerCase().includes(q))) continue;
    if(gpsFilter){
      const hasPtInBounds = c.gps_bounds && gpsFilter.contains([c.gps_bounds.center_lat, c.gps_bounds.center_lon]);
      if(!hasPtInBounds) continue;
    }
    if(tripFilter && !tripFilter.has(c.id)) continue;
    filtered.push(c);
  }
  updateFilterChips(filtered.length);
  filtered.sort((a,b)=>b.timestamp.localeCompare(a.timestamp));
  const el=$("#cliplist"); el.innerHTML="";
  const vehicles=new Set(filtered.map(c=>c.vehicle).filter(Boolean));
  if(vehicles.size>0){
    const groups={}; const ungrouped=[];
    for(const c of filtered){ if(c.vehicle)(groups[c.vehicle]||=[]).push(c); else ungrouped.push(c); }
    const sorted=[...vehicles].sort();
    for(const v of sorted){
      const head=document.createElement("div"); head.className="ghead";
      head.innerHTML=`<span>${(_collapsed[v]?"▸ ":"▾ ")+v}</span><span class="cnt">${groups[v].length}</span>`;
      head.onclick=()=>{_collapsed[v]=!_collapsed[v];buildSidebar();};
      el.appendChild(head);
      if(!_collapsed[v]) appendClips(el,groups[v]);
    }
    appendClips(el,ungrouped);
  } else {
    appendClips(el,filtered);
  }
}
function markActive(id){ [...document.querySelectorAll(".cliprow")].forEach(r=>r.classList.toggle("active",r.dataset.id===id)); }

// ---------- Filter chips (area / trip / reason) + result count ----------
let tripFilter=null;    // Set of clip ids, or null
let reasonFilter=null;  // event reason string, or null

// Options come from the clips actually present, newest counts included, so the
// dropdown never offers a reason that would yield an empty list.
function populateReasonFilter(){
  const sel=$("#filterReason"); if(!sel) return;
  const counts={};
  for(const c of allClips) if(c.reason) counts[c.reason]=(counts[c.reason]||0)+1;
  const entries=Object.entries(counts).sort((a,b)=>b[1]-a[1]);
  sel.innerHTML='<option value="">All reasons</option>'+entries.map(([r,n])=>
    `<option value="${r}">${REASON_LABELS[r]||r} (${n})</option>`).join("");
  // keep the current selection if it still exists
  sel.value=(reasonFilter&&counts[reasonFilter])?reasonFilter:"";
  if(reasonFilter&&!counts[reasonFilter]) reasonFilter=null;
}

function setReasonFilter(reason){
  reasonFilter=reason||null;
  const sel=$("#filterReason"); if(sel) sel.value=reasonFilter||"";
  buildSidebar();
}
function clearReasonFilter(){ setReasonFilter(null); }

function updateFilterChips(count){
  const ac=$("#areaChip");
  if(gpsFilter){ ac.style.display="inline-flex"; ac.innerHTML='📍 Area <span class="chipx" title="Clear area filter">✕</span>'; ac.querySelector(".chipx").onclick=clearAreaFilter; }
  else ac.style.display="none";
  const tc=$("#tripChip");
  if(tripFilter){ tc.style.display="inline-flex"; tc.innerHTML='🚗 Trip <span class="chipx" title="Clear trip filter">✕</span>'; tc.querySelector(".chipx").onclick=clearTripFilter; }
  else tc.style.display="none";
  const rc=$("#reasonChip");
  if(reasonFilter){
    rc.style.display="inline-flex";
    rc.innerHTML=`📅 ${REASON_LABELS[reasonFilter]||reasonFilter} <span class="chipx" title="Clear reason filter">✕</span>`;
    rc.querySelector(".chipx").onclick=clearReasonFilter;
  }
  else rc.style.display="none";
  $("#resultCount").textContent=count+" clip"+(count===1?"":"s");
}
function clearAreaFilter(){
  gpsFilter=null;
  if(_drawRect&&landingMap){ landingMap.removeLayer(_drawRect); _drawRect=null; }
  const ar=$("#areaResult"); if(ar) ar.style.display="none";
  buildSidebar();
}
function clearTripFilter(){ tripFilter=null; buildSidebar(); }

// ---------- Open / Playback ----------
function clearStage(){
  cancelAnimationFrame(raf); videos=[]; master=null; tele=null;
  [...$("#stage").querySelectorAll(".tile")].forEach(t=>t.remove());
  $("#stageaction").style.display="none"; $("#hud").style.display="none"; $("#nerd").style.display="none";
  $("#telctrl").style.display="none"; $("#telnone").style.display="none";
  $("#eventMark").style.display="none";
}
// The event-moment badge next to the HUD: content/color set once per clip
// (setupEventMark, from curEvent), visibility toggled every loop() tick by
// how close playback is to curEvent.seek (updateEventMark). Independent of
// telemetry/#hud so it still works on an event clip with no per-frame data.
function setupEventMark(){
  const em=$("#eventMark");
  if(!curEvent || curEvent.seek==null){ em.style.display="none"; em.dataset.seek=""; return; }
  em.style.background=EVENT_COLORS[curEvent.reason]||"#9aa7b4";
  em.innerHTML=reasonGlyph(curEvent.reason);
  em.title=(REASON_LABELS[curEvent.reason]||curEvent.reason||"Event")+" — this is the moment it happened";
  em.dataset.seek=curEvent.seek;
  em.style.display="none";
}
function updateEventMark(t){
  const em=$("#eventMark");
  if(!em.dataset.seek) return;
  em.style.display=Math.abs(t-(+em.dataset.seek))<1.5?"flex":"none";
}
function showAction(html){const a=$("#stageaction");a.innerHTML=html;a.style.display="flex";}
function setupMaster(){
  master=videos[0]; if(!master) return;
  master.onloadedmetadata=()=>{$("#seek").max=Math.floor(master.duration*1000)||1000; if(initialSeek!==null){master.currentTime=initialSeek; slaves(v=>v.currentTime=initialSeek);} updateEventMark(master.currentTime);};
  master.onplay=()=>{slaves(v=>v.play().catch(()=>{}));$("#play").textContent="⏸";loop();};
  master.onpause=()=>{slaves(v=>v.pause());$("#play").textContent="▶";cancelAnimationFrame(raf);};
  // Reaching the end of one segment continues straight into the next one of
  // the same event (autoplay) instead of just stopping -- this is what makes
  // a multi-segment event feel like one continuous recording.
  master.onended=()=>{ if(eventSegIdx>=0 && eventSegIdx<eventSegs.length-1) jumpToSegment(eventSegIdx+1,true); };
}
async function open(c, _prepared, _autoplay){
  activeId=c.id; markActive(c.id);
  updateKeepBtn(c.protected);
  $("#placeholder").style.display="none";
  clearStage(); $("#stage").style.display="grid"; $("#bar").style.display="none";
  $("#meta").textContent=(c.source?c.source+" · ":"")+c.timestamp.replace("_"," ");
  let anyPlay=false, anyKey=false, anyLocked=false;
  for(const cam of CAMS){
    const cm=c.cameras[cam.k];
    const t=document.createElement("div"); t.className="tile "+cam.a+(cm?"":" empty");
    const tag=document.createElement("span"); tag.className="tag"; tag.textContent=cam.l; t.appendChild(tag);
    if(cm&&cm.url){const v=document.createElement("video");v.src=cm.url;v.muted=true;v.playsInline=true;v.preload="auto";t.appendChild(v);videos.push(v);anyPlay=true;
      const dl=document.createElement("a");dl.className="dlcam";dl.href=cm.url;dl.download=cm.url.split("/").pop();dl.title="Download this camera";dl.textContent="⬇";t.appendChild(dl);
      const fs=document.createElement("button");fs.className="fscam";fs.title="Fullscreen";fs.textContent="⛶";fs.onclick=(ev)=>{ev.stopPropagation();const el=t.querySelector("video");if(el){if(el.requestFullscreen)el.requestFullscreen();else if(el.webkitEnterFullscreen)el.webkitEnterFullscreen();}};t.appendChild(fs);}
    else if(cm&&cm.state==="key"){anyKey=true;const o=document.createElement("div");o.className="camnote";o.textContent="🔒";t.appendChild(o);}
    else if(cm&&cm.state==="locked"){anyLocked=true;const o=document.createElement("div");o.className="camnote";o.textContent="🔒";t.appendChild(o);}
    $("#stage").appendChild(t);
  }
  // Telemetrie laden (falls vorhanden)
  tele = c.telemetry ? await fetch(c.telemetry).then(r=>r.json()).catch(()=>null) : null;
  buildGps();
  // Event-Daten laden (Seek, GPS-Fallback, Reason)
  initialSeek = null;
  curEvent = null;
  if(c.has_event){
    try{ curEvent=await fetch(`api/event?id=${encodeURIComponent(c.id)}`).then(r=>r.json()).catch(()=>null); }catch(e){}
    if(curEvent&&curEvent.seek>=0) initialSeek=curEvent.seek;
    if(!gpsPts.length && curEvent&&curEvent.lat&&curEvent.lon) gpsPts=[[curEvent.lat,curEvent.lon]];
  }
  updateEventNav(c);
  setupEventMark();
  if($("#t_map").checked) showMap(true);
  // key available -> decrypt transparently and play immediately
  if(anyKey && !_prepared){ return prepareAndOpen(c.id); }
  if(anyPlay){
    $("#bar").style.display="flex"; setupMaster(); applyHud(); updateTelControls();
    if(_autoplay) master.play().catch(()=>{});
    const frontCam=c.cameras.front;
    if(!tele && frontCam && frontCam.state==="plain"){
      fetch("api/prepare",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:c.id})})
        .then(r=>r.json()).then(async r=>{
          if(activeId!==c.id || !r || !r.clip || !r.clip.telemetry) return;
          const t=await fetch(r.clip.telemetry).then(r=>r.json()).catch(()=>null);
          if(t && activeId===c.id){ tele=t; buildGps(); applyHud(); updateTelControls(); }
        }).catch(()=>{});
    }
    return;
  }
  // encrypted WITHOUT key -> offer to fetch key and decrypt
  if(anyLocked){ showAction(`<div class="msg">🔒 Encrypted – no key yet.</div><button class="btn" id="getkey">🔑 Fetch key &amp; play</button><div class="msg" id="gkmsg"></div>`); $("#getkey").onclick=()=>fetchKeyAndOpen(c.id); }
  else if(anyKey){ showAction(`<div class="msg">⚠️ Decryption failed – try again in the 🔑 panel.</div>`); }
}
// ---------- Event nav: browse a multi-segment event without leaving the player ----------
// Looked up from allClips (not the filtered sidebar list), so it works
// regardless of whatever search/filter was active when the clip was opened.
// A loop-recording session (see clusterEvents) has no folder-wide marker like
// has_event to key off, so finding "the segments around c" means locating the
// contiguous same-folder, non-event, <=90s-gap run that contains c itself.
function _sessionSegs(c){
  const same=allClips.filter(x=>x.folder===c.folder && !x.has_event)
                      .sort((a,b)=>a.timestamp.localeCompare(b.timestamp));
  const idx=same.findIndex(x=>x.id===c.id);
  if(idx<0) return [];
  let lo=idx,hi=idx;
  while(lo>0 && _tsGapOk(same[lo-1].timestamp,same[lo].timestamp)) lo--;
  while(hi<same.length-1 && _tsGapOk(same[hi].timestamp,same[hi+1].timestamp)) hi++;
  return same.slice(lo,hi+1);
}
function updateEventNav(c){
  const nav=$("#eventNav");
  if(!c.folder){ nav.style.display="none"; eventSegs=[]; eventSegIdx=-1; return; }
  eventSegs=c.has_event
    ? allClips.filter(x=>x.folder===c.folder && x.has_event).sort((a,b)=>a.timestamp.localeCompare(b.timestamp))
    : _sessionSegs(c);
  eventSegIdx=eventSegs.findIndex(x=>x.id===c.id);
  if(eventSegs.length<2){ nav.style.display="none"; return; }
  nav.style.display="flex";
  $("#evPrev").disabled=eventSegIdx<=0;
  $("#evNext").disabled=eventSegIdx<0||eventSegIdx>=eventSegs.length-1;
  $("#evSegments").innerHTML=eventSegs.map((s,i)=>{
    const hm=s.timestamp.slice(11).replace("-",":").slice(0,5);   // "..._HH-MM-SS" -> "HH:MM"
    return `<button type="button" class="evSegBtn${i===eventSegIdx?" active":""}${s.is_trigger?" trig":""}" `
          +`data-i="${i}" title="${s.timestamp.replace('_',' ')}">${hm}</button>`;
  }).join("");
  [...$("#evSegments").querySelectorAll(".evSegBtn")].forEach(b=>b.onclick=()=>jumpToSegment(+b.dataset.i));
}
function jumpToSegment(i,autoplay){
  if(i<0||i>=eventSegs.length) return;
  open(eventSegs[i],false,autoplay);
}
async function prepareAndOpen(id){
  showAction(`<div class="msg">🔓 Decrypting…</div>`);
  const r=await fetch("api/prepare",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}).then(r=>r.json()).catch(()=>({ok:false,error:"Network error"}));
  refreshStatus();
  if(r&&r.clip&&(r.ok||r.clip.playable)){ updateClip(r.clip); open(r.clip, true); }
  else if(r&&r.errors&&r.errors.length){ showAction(`<div class="msg">Error: ${r.errors.join(", ")}</div>`); }
  else { showAction(`<div class="msg">Error: ${(r&&r.error)||"unknown"}</div>`); }
}
async function fetchKeyAndOpen(id){
  $("#gkmsg") && ($("#gkmsg").textContent="Fetching key…");
  await fetch("api/fetch",{method:"POST"});
  const t=setInterval(async()=>{
    const s=await fetch("api/status").then(r=>r.json()).catch(()=>null);
    if(s&&!s.busy){ clearInterval(t); await loadClips(true);
      const fresh=allClips.find(c=>c.id===id);
      if(fresh&&(fresh.needs_prepare||fresh.playable)) open(fresh);
      else $("#gkmsg")&&($("#gkmsg").textContent="No key received – try via 🔑 (bookmarklet).");
    }
  },1500);
}
function updateClip(fresh){ const i=allClips.findIndex(c=>c.id===fresh.id); if(i>=0){allClips[i]=fresh; buildSidebar();} }

function slaves(fn){videos.forEach(v=>{if(v!==master)fn(v);});}
function loop(){
  const t=master.currentTime;
  slaves(v=>{if(Math.abs(v.currentTime-t)>0.12)v.currentTime=t;});
  $("#seek").value=Math.floor(t*1000); $("#time").textContent=fmt(t)+" / "+fmt(master.duration||0);
  updateEventMark(t);
  if(tele&&tele.frame_count){const i=Math.min(tele.frame_count-1,Math.max(0,Math.round(t*tele.fps)));const fr=tele.frames[i];
    if($("#t_hud").checked)hud(fr);
    if($("#t_nerd").checked){nerd(fr);$("#nerd").style.display="block";}else $("#nerd").style.display="none";
    if($("#t_map").checked)mapMarker(fr);}
  raf=requestAnimationFrame(loop);
}
function hud(f){
  $("#h-gear").textContent=f.gear??"–"; $("#h-spd").textContent=Math.abs(Math.round(f.speed_kmh||0));
  $("#h-l").classList.toggle("on",!!f.blink_l); $("#h-r").classList.toggle("on",!!f.blink_r);
  const accelPct=Math.min(100,Math.max(0,f.accel||0)); $("#h-accel-fill").style.height=accelPct+"%";
  $("#h-brake").classList.toggle("on",!!f.brake); const ap=(f.autopilot||0)>0; $("#h-ap").classList.toggle("on",ap); $("#h-ap").style.display=ap?"flex":"none";
  $("#h-wheel").style.transform=`rotate(${f.steer||0}deg)`; $("#h-steer").classList.toggle("on",Math.abs(f.steer||0)>3);
}
function applyHud(){ $("#hud").style.display=(tele&&tele.frame_count&&$("#t_hud").checked)?"flex":"none"; }
function buildGps(){ gpsPts=(tele&&tele.frames)?tele.frames.filter(f=>f.lat&&f.lon).map(f=>[f.lat,f.lon]):[]; }
function ensureMap(){
  if(lmap||!window.L) return lmap;
  lmap=L.map("map",{attributionControl:false});
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19}).addTo(lmap);
  return lmap;
}
function drawTrack(){
  if(!ensureMap()) return;
  if(lline){lmap.removeLayer(lline);lline=null;} if(lmark){lmap.removeLayer(lmark);lmark=null;}
  if(!gpsPts.length) return;
  lline=L.polyline(gpsPts,{color:"#3b82f6",weight:4}).addTo(lmap);
  lmark=L.circleMarker(gpsPts[0],{radius:6,color:"#fff",weight:2,fillColor:"#34d399",fillOpacity:1}).addTo(lmap);
  lmap.fitBounds(lline.getBounds(),{padding:[20,20]});
}
function showMap(on){
  $("#map").style.display=on?"block":"none";
  if(!on) return;
  if(!window.L){ $("#map").innerHTML='<div style="padding:14px;color:var(--muted)">Map unavailable (no internet for map library/tiles).</div>'; return; }
  drawTrack(); setTimeout(()=>lmap&&lmap.invalidateSize(),60);
}
function mapMarker(f){ if(lmark&&f&&f.lat&&f.lon) lmark.setLatLng([f.lat,f.lon]); }
function updateTelControls(){
  const hasTel=!!(tele&&tele.frame_count);
  const hasGps=gpsPts.length>0;
  const hasEvent=!!curEvent;
  const hasNerd=hasTel||hasEvent;
  const hasAny=hasTel||hasGps||hasEvent;
  $("#telctrl").style.display=hasAny?"inline-flex":"none";
  $("#telnone").style.display=hasAny?"none":"inline";
  $("#t_hud").parentElement.style.display=hasTel?"inline-flex":"none";
  $("#t_nerd").parentElement.style.display=hasNerd?"inline-flex":"none";
  $("#t_map").parentElement.style.display=hasGps?"inline-flex":"none";
  if(!hasTel){ $("#hud").style.display="none"; }
  if(!hasNerd || !$("#t_nerd").checked){ $("#nerd").style.display="none"; }
  if(hasEvent && !hasTel && $("#t_nerd").checked){ nerdEvent(); $("#nerd").style.display="block"; }
  if(!hasGps){ $("#map").style.display="none"; $("#t_map").checked=false; }
}
// event.json's numeric "camera" field -> display label. Matches
// EVENT_CAMERA_INDEX in server.py (which drives the actual thumbnail camera
// selection) -- see its comment there for how 5/6 were verified against real
// footage. 2/3/4 are unverified carry-over guesses, never observed yet.
const CAM_LABELS=["Front","Rear","Left Repeater","Right Repeater","Pillar L","Left Repeater","Right Repeater"];
const REASON_LABELS={"sentry_aware_object_detection":"Object detected (Sentry)","sentry_aware_accel":"Acceleration (Sentry)","user_interaction_dashcam_icon_tapped":"Manual save","user_interaction_honk":"Honk","sentry_aware_intrusion":"Intrusion (Sentry)","sentry_locked_handle_pulled":"Door handle pulled (Sentry)","vehicle_auto_emergency_braking":"Emergency braking","user_interaction_dashcam_launcher_action_tapped":"Manual save (launcher)","user_interaction_dashcam_panel_save":"Manual save (panel)"};
// The server splits a measured magnitude off the reason (accelerometer
// triggers carry one), so the label matches and the value is shown alongside.
function reasonLabel(reason,value){
  const l=REASON_LABELS[reason]||reason;
  return (value==null)?l:`${l} (${(+value).toFixed(2)})`;
}
function nerd(f){
  const bl=((f.blink_l?"◀":"")+(f.blink_r?"▶":""))||"–";
  let lines=[
    "t "+f.t+" s","Speed "+f.speed_kmh+" km/h","Gear "+(f.gear??"–"),
    "Steering "+(f.steer??"–")+"°","Throttle "+(f.accel??"–"),"Brake "+(f.brake?"ON":"–"),
    "Blinker "+bl,"Autopilot "+(f.autopilot??0),
    "GPS "+(f.lat??"–")+", "+(f.lon??"–"),"Heading "+(f.heading??"–")+"°"
  ];
  if(curEvent) lines.push("","— Event —",...eventNerdLines());
  $("#nerd").innerHTML=lines.map(x=>"<div>"+x+"</div>").join("");
}
function eventNerdLines(){
  if(!curEvent) return [];
  const e=curEvent, lines=[];
  if(e.reason) lines.push("Reason: "+reasonLabel(e.reason,e.reason_value));
  if(e.city||e.street) lines.push("Location: "+[e.street,e.city].filter(Boolean).join(", "));
  if(e.lat&&e.lon) lines.push("GPS: "+e.lat+", "+e.lon);
  if(e.camera!=null) lines.push("Camera: "+(CAM_LABELS[+e.camera]||e.camera));
  if(e.seek!=null) lines.push("Event @ "+Math.round(e.seek)+" s");
  return lines;
}
function nerdEvent(){
  $("#nerd").innerHTML=eventNerdLines().map(x=>"<div>"+x+"</div>").join("");
}
const fmt=s=>{s=Math.max(0,s|0);return (s/60|0)+":"+String(s%60).padStart(2,"0");};
$("#play").onclick=()=>master&&(master.paused?master.play():master.pause());
$("#seek").oninput=e=>{const t=e.target.value/1000;if(master){master.currentTime=t;slaves(v=>v.currentTime=t);}};
$("#rate").onchange=e=>{const r=+e.target.value;if(master){master.playbackRate=r;slaves(v=>v.playbackRate=r);}};
$("#full").onclick=()=>document.fullscreenElement?document.exitFullscreen():$("#content").requestFullscreen();
addEventListener("keydown",e=>{if(!master)return;
  if(e.code==="Space"){e.preventDefault();master.paused?master.play():master.pause();}
  if(e.code==="ArrowRight"){const t=master.currentTime+5;master.currentTime=t;slaves(v=>v.currentTime=t);}
  if(e.code==="ArrowLeft"){const t=Math.max(0,master.currentTime-5);master.currentTime=t;slaves(v=>v.currentTime=t);}});

// ---------- Landing map (trips + event markers + rectangle select) ----------
let landingMap=null, tripLayerGroup=null, markerLayerGroup=null;
let tripsSorted=[], currentTripIdx=-1;
let currentView="clips";
let _drawMode=false, _drawRect=null, _drawStart=null;
const DRAW_TIP="tip: shift+drag on the map";

function initLandingMap(){
  if(landingMap||!window.L) return landingMap;
  try{
    landingMap=L.map("landingMap",{attributionControl:false});
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{maxZoom:19,attribution:''}).addTo(landingMap);
    tripLayerGroup=L.layerGroup().addTo(landingMap);
    markerLayerGroup=L.layerGroup().addTo(landingMap);
    landingMap.setView([0,0],2);
    wireRectSelect(landingMap);
  }catch(e){ console.error("Landing map init failed:", e); }
  return landingMap;
}

function wireRectSelect(map){
  const container=map.getContainer();
  function enterDraw(){
    _drawMode=true; map.dragging.disable();
    // Markers are their own DOM elements sitting on top of the map and they
    // swallow mousedown, so a drag starting on one never reached the map at
    // all. Turning off their pointer events for the duration of the drag is
    // what makes the selection reliably drawable.
    container.classList.add("drawing");
    $("#drawRectBtn").classList.add("active");
    $("#drawHint").textContent="Click & drag to select an area";
  }
  function exitDraw(){
    _drawMode=false; map.dragging.enable();
    container.classList.remove("drawing");
    $("#drawRectBtn").classList.remove("active");
    $("#drawHint").textContent=DRAW_TIP;   // restore, not clear: the hint is how
  }                                        // anyone learns shift+drag exists
  $("#drawRectBtn").onclick=()=>{ _drawMode ? exitDraw() : enterDraw(); };

  function begin(latlng){
    _drawStart=latlng;
    if(_drawRect){ map.removeLayer(_drawRect); _drawRect=null; }
  }
  function extend(latlng){
    if(!_drawStart) return;
    const b=L.latLngBounds(_drawStart,latlng);
    if(_drawRect) _drawRect.setBounds(b);
    else _drawRect=L.rectangle(b,{color:"#3b82f6",weight:2,fillOpacity:0.15}).addTo(map);
  }
  function finish(latlng){
    if(!_drawStart) return;
    const b=L.latLngBounds(_drawStart,latlng);
    _drawStart=null;
    // A click without movement is not a selection — leave everything as it was
    // instead of applying an empty filter.
    if(b.getNorthEast().equals(b.getSouthWest())){
      if(_drawRect){ map.removeLayer(_drawRect); _drawRect=null; }
      return;
    }
    if(_drawRect) _drawRect.setBounds(b);
    gpsFilter=b;
    exitDraw();
    buildSidebar();
    showAreaResult();
  }

  map.on("mousedown",e=>{
    // Shift+drag selects without arming the button first — the button was the
    // only way in, and nothing on the map hinted that it had to be pressed.
    if(!_drawMode && e.originalEvent && e.originalEvent.shiftKey) enterDraw();
    if(_drawMode) begin(e.latlng);
  });
  map.on("mousemove",e=>{ if(_drawMode) extend(e.latlng); });
  map.on("mouseup",e=>{ if(_drawMode) finish(e.latlng); });
  // Releasing outside the map would otherwise leave a half-drawn rectangle and
  // the map stuck in draw mode.
  window.addEventListener("mouseup",e=>{
    if(!_drawMode||!_drawStart) return;
    if(container.contains(e.target)) return;      // handled by the map above
    finish(map.mouseEventToLatLng(e));
  });
  // Reset also frames all events again, so there is always a way back to the
  // overview after zooming into a trip or a selection.
  $("#filterResetBtn").onclick=()=>{ exitDraw(); clearAreaFilter(); fitEventBounds(); };
}

function showAreaResult(){
  if(!gpsFilter) return;
  const count=allClips.filter(c=>c.gps_bounds && gpsFilter.contains([c.gps_bounds.center_lat,c.gps_bounds.center_lon])).length;
  const el=$("#areaResult");
  el.style.display="flex";
  el.innerHTML=`<span>📍 ${count} clip${count===1?"":"s"} in this area</span><button class="btn" id="areaViewBtn">View list</button>`;
  $("#areaViewBtn").onclick=()=>switchView("clips");
}

function groupMarkersByCoord(clips){
  const groups={};
  for(const c of clips){
    if(!c.gps_bounds) continue;
    const key=c.gps_bounds.center_lat.toFixed(4)+","+c.gps_bounds.center_lon.toFixed(4);
    (groups[key]=groups[key]||[]).push(c);
  }
  return groups;
}

const EVENT_COLORS={
  "user_interaction_honk":"#fbbf24",
  "sentry_aware_object_detection":"#f87171",
  "sentry_aware_accel":"#f87171",
  "sentry_aware_intrusion":"#f87171",
  "user_interaction_dashcam_icon_tapped":"#3b82f6"
};
const ICONS={
  sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  honk:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 10v4h4l5 4V6L7 10H3z"/></svg>',
  eye:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>',
  hand:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11V5a2 2 0 0 1 4 0v6M13 6a2 2 0 0 1 4 0v5M17 8a2 2 0 0 1 4 0v6a7 7 0 0 1-7 7h-1a7 7 0 0 1-6-3.5L4 13"/></svg>',
  pin:'<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>',
  grid:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  list:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>'
};
function reasonGlyph(reason){
  if(reason==="user_interaction_honk") return ICONS.honk;
  if(reason&&reason.startsWith("sentry_aware")) return ICONS.eye;
  if(reason&&reason.startsWith("user_interaction")) return ICONS.hand;
  return ICONS.pin;
}
function reasonIcon(reason,count){
  const color=EVENT_COLORS[reason]||"#9aa7b4";
  const content=count>1?count:reasonGlyph(reason);
  return L.divIcon({className:'',html:`<div class="evmarker" style="background:${color}">${content}</div>`,iconSize:[22,22],iconAnchor:[11,11]});
}

function applyThemeIcon(){
  const t=document.documentElement.dataset.theme||"dark";
  $("#themeToggle").innerHTML = t==="light" ? ICONS.moon : ICONS.sun;
}
function toggleTheme(){
  const next=(document.documentElement.dataset.theme||"dark")==="light" ? "dark" : "light";
  document.documentElement.dataset.theme=next;
  localStorage.setItem("theme",next);
  applyThemeIcon();
}

function applyViewIcon(){
  const v=localStorage.getItem("clipView")||"list";
  $("#viewToggle").innerHTML = v==="grid" ? ICONS.list : ICONS.grid;
  $("#viewToggle").title = v==="grid" ? "Switch to list view" : "Switch to grid view (bigger thumbnails)";
  $("#cliplist").classList.toggle("grid", v==="grid");
}
function toggleClipView(){
  const next=(localStorage.getItem("clipView")||"list")==="grid" ? "list" : "grid";
  localStorage.setItem("clipView",next);
  applyViewIcon();
}

// One marker per event, not per one-minute segment: an event folder holds up to
// eleven clips at practically the same spot, which used to stack eleven markers
// on top of each other. The triggering segment represents the event where it is
// known, otherwise the first segment of that folder.
function eventMarkerClips(){
  const byFolder=new Map();
  for(const c of allClips){
    if(!c.has_event||!c.gps_bounds) continue;
    const prev=byFolder.get(c.folder);
    if(!prev||(c.is_trigger&&!prev.is_trigger)) byFolder.set(c.folder,c);
  }
  return [...byFolder.values()];
}

function renderEventMarkers(){
  if(!landingMap||!markerLayerGroup) return;
  markerLayerGroup.clearLayers();
  const groups=groupMarkersByCoord(eventMarkerClips());
  for(const key in groups){
    const list=groups[key];
    const [lat,lon]=key.split(",").map(Number);
    const withReason=list.find(c=>c.reason);
    const m=L.marker([lat,lon],{icon:reasonIcon(withReason&&withReason.reason,list.length)}).addTo(markerLayerGroup);
    m.on("click",()=>{ list.length===1 ? openPlayerOverlay(list[0]) : showDisambiguation(m,list); });
  }
}

// The map opened on [0,0] at zoom 2 — a view of the whole planet, which also
// made the rectangle selection useless. Frame the events instead.
function fitEventBounds(){
  if(!landingMap) return false;
  const pts=eventMarkerClips().map(c=>[c.gps_bounds.center_lat,c.gps_bounds.center_lon]);
  if(!pts.length) return false;
  if(pts.length===1){ landingMap.setView(pts[0],15); return true; }
  landingMap.fitBounds(L.latLngBounds(pts),{padding:[40,40],maxZoom:16});
  return true;
}

function showDisambiguation(marker,list){
  const rows=list.slice().sort((a,b)=>b.timestamp.localeCompare(a.timestamp)).map(c=>
    `<div class="popRow" data-id="${c.id}"><img src="api/thumb?id=${encodeURIComponent(c.id)}" onerror="this.style.visibility='hidden'"><span>${c.timestamp.replace("_"," ")}${c.reason?" · "+reasonLabel(c.reason,c.reason_value):""}</span></div>`
  ).join("");
  L.popup({maxWidth:260}).setLatLng(marker.getLatLng()).setContent(`<div class="popList">${rows}</div>`).openOn(landingMap);
  setTimeout(()=>{
    document.querySelectorAll(".popRow").forEach(el=>{
      el.onclick=()=>{ const c=allClips.find(x=>x.id===el.dataset.id); if(c){ landingMap.closePopup(); openPlayerOverlay(c); } };
    });
  },0);
}

function fmtTs(ts){ return ts.replace("_"," "); }

function renderTripPolylines(){
  if(!landingMap||!tripLayerGroup) return;
  tripLayerGroup.clearLayers();
  tripsSorted.forEach((t,i)=>{
    if(!t.route||t.route.length<2) return;
    const active=i===currentTripIdx;
    const line=L.polyline(t.route,{color:active?"#3b82f6":"#5b6b7b",weight:active?5:3,opacity:active?0.95:0.5});
    line.on("click",()=>selectTrip(i));
    tripLayerGroup.addLayer(line);
  });
}

// fit=false selects a trip without moving the map — used for the automatic
// selection on first open, where the view should stay framed on all events
// rather than jumping to the newest drive.
function selectTrip(idx,fit=true){
  if(idx<0||idx>=tripsSorted.length) return;
  currentTripIdx=idx;
  const t=tripsSorted[idx];
  $("#tcVehicle").textContent=t.vehicle||"Vehicle";
  $("#tcRange").textContent=fmtTs(t.start)+" – "+fmtTs(t.end);
  $("#tcDist").textContent="📏 "+t.distance_km+" km";
  $("#tcClips").textContent="🎞️ "+t.clip_count+" clips";
  $("#tcEvents").textContent=t.event_total?("📅 "+t.event_total+" events"):"";
  $("#tripCard").style.display="block";
  renderTripPolylines();
  if(fit && t.bounds && landingMap){
    landingMap.fitBounds([[t.bounds.min_lat,t.bounds.min_lon],[t.bounds.max_lat,t.bounds.max_lon]],{padding:[40,40]});
  }
}
function prevTrip(){ if(currentTripIdx<tripsSorted.length-1) selectTrip(currentTripIdx+1); } // older
function nextTrip(){ if(currentTripIdx>0) selectTrip(currentTripIdx-1); } // newer

async function loadTrips(){
  tripsSorted=await fetch("api/trips").then(r=>r.json()).catch(()=>[]);
  if(tripsSorted.length) selectTrip(0);
}

// View a trip's clips: set the trip filter and jump to the Clips list.
function viewTripClips(){
  const t=tripsSorted[currentTripIdx]; if(!t) return;
  tripFilter=new Set(t.clip_ids);
  buildSidebar();
  switchView("clips");
}

// ---------- Player overlay ----------
function openPlayerOverlay(c){
  $("#playerOverlay").classList.add("open");
  open(c);
}
function closePlayerOverlay(){
  $("#playerOverlay").classList.remove("open");
  clearStage();
  $("#placeholder").style.display="flex";
  activeId=null;
  eventSegs=[]; eventSegIdx=-1; $("#eventNav").style.display="none";
}

// ---------- Tab views ----------
function switchView(name){
  currentView=name;
  document.querySelectorAll(".view").forEach(v=>v.classList.toggle("active",v.id==="view-"+name));
  document.querySelectorAll(".tabbtn").forEach(b=>b.classList.toggle("active",b.dataset.view===name));
  // The map is built on first use, not at boot: creating it immediately starts
  // downloading tiles from the CDN for a tab the user may never open.
  if(name==="map"){
    const fresh=!landingMap;
    initLandingMap();
    if(landingMap) setTimeout(()=>{
      landingMap.invalidateSize();
      if(fresh){
        renderEventMarkers();
        // Frame the events on first open. A selected trip zooms in further
        // below; without this the map would still be showing the whole world.
        fitEventBounds();
      }
      if(currentTripIdx>=0) selectTrip(currentTripIdx,!fresh);
      else if(tripsSorted.length) selectTrip(0,false);
    },60);
  }
  if(name==="analytics") loadAnalytics();
}
function fmtBytes(b){
  if(!b) return "0 B";
  if(b<1024) return b+" B";
  const units=["KB","MB","GB","TB"]; let i=-1;
  do{ b/=1024; i++; }while(b>=1024 && i<units.length-1);
  return b.toFixed(1)+" "+units[i];
}
async function loadAnalytics(){
  const el=$("#analyticsBody"); if(!el) return;
  el.innerHTML='<div class="loading">⏳ Loading analytics…</div>';
  const a=await fetch("api/analytics").then(r=>r.json()).catch(()=>null);
  if(!a){ el.innerHTML='<div class="loading">Failed to load analytics.</div>'; return; }
  if(a.pending){
    el.innerHTML='<div class="loading">⏳ Building the statistics — this reads the size of every clip once and can take a few minutes on a network share. They appear here automatically.</div>';
    return;
  }

  const c=a.clips, t=a.trips;
  // "Trips" counts every cluster of clips, and most are parked Sentry sessions
  // with no movement. The distance figures only cover the ones that moved, so
  // the tile says how many that is instead of leaving "410" next to "6 km avg".
  const moving=(t.moving!=null)?t.moving:null;
  const tiles=[
    ["🎞️ Clips",c.clips],["🔑 Decrypted",c.decrypted],["⏳ No key",c.need_keys],["📊 With telemetry",c.with_telemetry],
    ["🚗 Trips",t.total+(moving!=null?` <small>(${moving} driven)</small>`:"")],
    ["📏 Total distance",t.total_distance_km+" km"],
    ["📐 Avg driven trip",t.avg_distance_km+" km"],
    ["🏁 Longest trip",t.longest_km+" km"],
  ];
  const tilesHtml=`<div class="statGrid">${tiles.map(([l,v])=>`<div class="statTile"><div class="stNum">${v}</div><div class="stLbl">${l}</div></div>`).join("")}</div>`;

  const maxBytes=Math.max(1,...a.storage.by_folder.map(f=>f.bytes));
  const storageHtml=a.storage.by_folder.length?`<h3>Storage by vehicle/folder</h3><div class="barList">${
    a.storage.by_folder.map(f=>`<div class="barRow"><span class="barLbl">${f.folder}</span><div class="barTrack"><div class="barFill" style="width:${(f.bytes/maxBytes*100).toFixed(1)}%"></div></div><span class="barVal">${fmtBytes(f.bytes)} · ${f.clip_count} clips</span></div>`).join("")
  }</div>`:"";

  const reasons=Object.entries(a.events_by_reason).sort((x,y)=>y[1]-x[1]);
  const maxEv=Math.max(1,...reasons.map(r=>r[1]));
  // Rows are clickable: they filter the clip list to that reason and jump to it.
  const eventsHtml=reasons.length?`<h3>Events by reason</h3><div class="hint">Click a row to see only those clips.</div><div class="barList">${
    reasons.map(([r,n])=>`<div class="barRow clickable" data-reason="${r}" title="Show only these clips"><span class="barLbl">${REASON_LABELS[r]||r}</span><div class="barTrack"><div class="barFill" style="width:${(n/maxEv*100).toFixed(1)}%;background:var(--warn)"></div></div><span class="barVal">${n}</span></div>`).join("")
  }</div>`:"";

  const months=a.clips_by_month;
  const maxM=Math.max(1,...months.map(m=>m.count));
  const bw=24,gap=12,h=110;
  const w=Math.max(200,months.length*(bw+gap));
  const bars=months.map((m,i)=>{
    const bh=Math.round((m.count/maxM)*(h-30));
    const x=i*(bw+gap)+gap/2;
    const y=h-bh-18;
    return `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="3" fill="var(--accent)"/>`+
           `<text x="${x+bw/2}" y="${Math.max(10,y-4)}" font-size="9" fill="var(--txt)" text-anchor="middle">${m.count}</text>`+
           `<text x="${x+bw/2}" y="${h-4}" font-size="9" fill="var(--muted)" text-anchor="middle">${m.month.slice(2)}</text>`;
  }).join("");
  const monthHtml=months.length?`<h3>Clips by month</h3><svg viewBox="0 0 ${w} ${h}" style="width:100%;max-width:${w}px;height:${h}px">${bars}</svg>`:"";

  // The GPX section renders into its own container so it can be filled
  // asynchronously (it reads separate files on the NAS) without blocking the
  // rest of analytics, and stays absent entirely when no trips exist.
  el.innerHTML=tilesHtml+storageHtml+eventsHtml+monthHtml+'<div id="gpxSection"></div>';
  el.querySelectorAll(".barRow[data-reason]").forEach(row=>{
    row.onclick=()=>{ viewReasonClips(row.dataset.reason); };
  });
  loadGpxTrips();
}

// ---------- GPX trip viewer (te_usbhub recordings) ----------
let gpxMap=null, gpxLine=null, gpxTrips=[];
// te_usbhub's GPX carries only lat/lon/time (no speed of its own — see
// _derive_speeds_kmh in server.py), so speed is derived server-side per point
// and shown here as a color gradient along the track. Fixed 0-150 km/h domain
// (rather than each trip's own max) keeps a slow city drive and a highway run
// visually comparable instead of both maxing out their own color scale.
const GPX_SPEED_DOMAIN_KMH=150;
const GPX_SPEED_STOPS=[[0,[59,130,246]],[0.33,[16,185,129]],[0.66,[250,204,21]],[1,[239,68,68]]];
function gpxSpeedColor(kmh){
  const t=Math.max(0,Math.min(1,kmh/GPX_SPEED_DOMAIN_KMH));
  let i=0; while(i<GPX_SPEED_STOPS.length-2&&t>GPX_SPEED_STOPS[i+1][0]) i++;
  const [t0,c0]=GPX_SPEED_STOPS[i], [t1,c1]=GPX_SPEED_STOPS[i+1];
  const f=(t-t0)/(t1-t0||1);
  const c=c0.map((v,k)=>Math.round(v+(c1[k]-v)*f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
function initGpxMap(){
  if(gpxMap||!window.L) return gpxMap;
  try{
    // preferCanvas: a trip can be 1000+ points, drawn as one polyline per
    // segment so each can carry its own speed color — canvas rendering keeps
    // that many paths smooth where the default SVG renderer would bog down.
    gpxMap=L.map("gpxMap",{attributionControl:false,preferCanvas:true});
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{maxZoom:19,attribution:''}).addTo(gpxMap);
    gpxMap.setView([51.16,10.45],5);
  }catch(e){ console.error("GPX map init failed:", e); }
  return gpxMap;
}
async function loadGpxTrips(){
  const sec=$("#gpxSection"); if(!sec) return;
  let r;
  try{ r=await fetch("api/gpx_trips").then(x=>x.json()); }catch(e){ return; }
  // Hide the whole section unless the feature is configured AND has trips.
  if(!r||!r.enabled||!r.trips||!r.trips.length){ sec.innerHTML=""; return; }
  gpxTrips=r.trips;
  const total=gpxTrips.reduce((s,t)=>s+(t.distance_km||0),0);
  const opts=gpxTrips.map((t,i)=>{
    const km=t.distance_km!=null?` · ${t.distance_km} km`:"";
    return `<option value="${t.id}">${gpxLabel(t)}${km}</option>`;
  }).join("");
  const grad=GPX_SPEED_STOPS.map(([t,c])=>`rgb(${c[0]},${c[1]},${c[2]}) ${t*100}%`).join(",");
  sec.innerHTML=`<h3>Recorded drives (GPX) <small style="color:var(--muted);font-weight:400">`
    +`${gpxTrips.length} trip${gpxTrips.length===1?"":"s"} · ${total.toFixed(1)} km total</small></h3>`
    +`<div class="row" style="margin-bottom:8px"><select id="gpxPick">${opts}</select>`
    +`<span id="gpxInfo" style="color:var(--muted);font-size:12px"></span></div>`
    +`<div id="gpxMap" style="height:320px;border:1px solid var(--line);border-radius:8px;overflow:hidden"></div>`
    +`<div class="row" style="margin-top:6px;gap:8px;align-items:center;font-size:11px;color:var(--muted)">`
    +`<span>Speed</span><span>0</span>`
    +`<div style="flex:1;height:8px;border-radius:4px;background:linear-gradient(90deg,${grad})"></div>`
    +`<span>${GPX_SPEED_DOMAIN_KMH}+ km/h</span></div>`;
  $("#gpxPick").onchange=e=>showGpxTrip(e.target.value);
  setTimeout(()=>{ initGpxMap(); if(gpxMap){ gpxMap.invalidateSize(); showGpxTrip(gpxTrips[0].id); } },60);
}
// A trip id is an ISO-ish "2026-07-29T18-37-29"; show it as a readable date.
function gpxLabel(t){
  const m=/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})$/.exec(t.id);
  if(m) return `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}`;
  return t.name||t.id;
}
async function showGpxTrip(id){
  if(!gpxMap) return;
  let g;
  try{ g=await fetch("api/gpx?id="+encodeURIComponent(id)).then(x=>x.json()); }catch(e){ return; }
  if(!g||!g.track||!g.track.length){ $("#gpxInfo").textContent="No track points."; return; }
  // A LayerGroup holds the segments plus the start/end dots — a polyline is
  // not a container, so they can't be added to it directly.
  if(gpxLine) gpxMap.removeLayer(gpxLine);
  gpxLine=L.layerGroup().addTo(gpxMap);
  // Leaflet has no per-vertex color on a single polyline, so each [lat,lon,
  // speed] segment gets its own tiny 2-point line, colored by its speed.
  const track=g.track;
  for(let i=0;i<track.length-1;i++){
    const a=track[i], b=track[i+1];
    L.polyline([[a[0],a[1]],[b[0],b[1]]],
      {color:gpxSpeedColor((a[2]+b[2])/2),weight:4,opacity:0.9}).addTo(gpxLine);
  }
  // green = start, red = end, so the direction is readable
  L.circleMarker([track[0][0],track[0][1]],{radius:5,color:"#fff",weight:2,fillColor:"#34d399",fillOpacity:1}).addTo(gpxLine);
  L.circleMarker([track[track.length-1][0],track[track.length-1][1]],{radius:5,color:"#fff",weight:2,fillColor:"#f87171",fillOpacity:1}).addTo(gpxLine);
  if(g.bounds) gpxMap.fitBounds([[g.bounds.min_lat,g.bounds.min_lon],[g.bounds.max_lat,g.bounds.max_lon]],{padding:[30,30],maxZoom:16});
  const dur=gpxDuration(g.start,g.end);
  const speeds=(g.avg_speed_kmh!=null?` · ⌀ ${g.avg_speed_kmh} km/h`:"")+(g.max_speed_kmh!=null?` · ↑ ${g.max_speed_kmh} km/h`:"");
  $("#gpxInfo").textContent=`${g.point_count} points · ${g.distance_km} km`+(dur?` · ${dur}`:"")+speeds;
}
function gpxDuration(a,b){
  if(!a||!b) return "";
  const s=Date.parse(a), e=Date.parse(b);
  if(isNaN(s)||isNaN(e)||e<s) return "";
  const min=Math.round((e-s)/60000);
  return min<60?`${min} min`:`${Math.floor(min/60)} h ${min%60} min`;
}

// Filter the clip list to one event reason and show it — the same move as the
// trip card's "View clips".
function viewReasonClips(reason){
  tripFilter=null;
  setReasonFilter(reason);
  switchView("clips");
}

// ---------- Keep (protect) toggle ----------
function updateKeepBtn(on){
  const b=$("#keepbtn"); if(!b) return;
  b.textContent=on?"📌 Kept":"📌 Keep";
  b.classList.toggle("on",!!on);
  b.title=on?"Protected from the storage cleanup — click to unprotect"
            :"Protect this clip from the storage cleanup";
}

// ---------- Free up storage (purge) ----------
function purgeSpec(){
  return {category:$("#purgeCat").value,
          reason:$("#purgeReason").value||"",
          older_than_days:parseInt($("#purgeDays").value,10)||0,
          keep_telemetry:$("#purgeKeepTel").checked};
}
function purgeQuery(s){
  return `category=${encodeURIComponent(s.category)}&reason=${encodeURIComponent(s.reason)}`
        +`&older_than_days=${s.older_than_days}&keep_telemetry=${s.keep_telemetry?1:0}`;
}
async function purgePreview(){
  const s=purgeSpec();
  $("#purgeMsg").textContent="Counting…"; $("#purgeBtn").style.display="none";
  const p=await fetch("api/purge/preview?"+purgeQuery(s)).then(r=>r.json()).catch(()=>null);
  if(!p){ $("#purgeMsg").textContent="Preview failed."; return; }
  if(!p.clips){ $("#purgeMsg").textContent="No clips match — nothing to delete."; return; }
  const size=p.bytes_estimate?` (${p.exact?"":"about "}${fmtBytes(p.bytes_estimate)})`:"";
  const prot=p.protected_excluded?` · ${p.protected_excluded} kept clip(s) excluded`:"";
  $("#purgeMsg").innerHTML=`<b>${p.clips}</b> clip(s)${size} would be deleted${prot}.`;
  $("#purgeBtn").style.display="inline-block";
  $("#purgeBtn")._spec=s;
}
async function runPurge(){
  const s=$("#purgeBtn")._spec||purgeSpec();
  s.keep_telemetry=$("#purgeKeepTel").checked;  // doesn't affect candidacy — safe to refresh past preview
  const p=await fetch("api/purge/preview?"+purgeQuery(s)).then(r=>r.json()).catch(()=>null);
  if(!p||!p.clips){ $("#purgeMsg").textContent="Nothing to delete."; $("#purgeBtn").style.display="none"; return; }
  const label={no_event:"without an event",event:"with any event",
               reason:`with reason “${REASON_LABELS[s.reason]||s.reason}”`,all:"— ALL clips"}[s.category];
  const age=s.older_than_days>0?` older than ${s.older_than_days} days`:"";
  const telNote=s.keep_telemetry?"GPS/speed telemetry is kept.":"Telemetry data is deleted too.";
  if(!confirm(`Permanently DELETE ${p.clips} clip(s) ${label}${age}.\n\n`
    +`This removes the video files from the NAS and cannot be undone. `
    +`${telNote} Clips marked 📌 keep are not affected.\n\nContinue?`)) return;
  $("#purgeBtn").disabled=true; $("#purgeMsg").textContent="Starting…";
  $("#purgeCancel").style.display="inline-block"; $("#purgeCancel").disabled=false;
  await fetch("api/purge",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(s)});
  const poll=setInterval(async()=>{
    const st=await fetch("api/status").then(r=>r.json()).catch(()=>null);
    if(st&&st.dec_job){
      const j=st.dec_job;
      const mine=j.phase==="purge";
      $("#purgeBar").classList.toggle("on",!!j.running&&mine);
      if(j.running&&mine){
        $("#purgeMsg").textContent=`${j.done}/${j.total}…`;
        renderBar($("#purgeBar"),{label:"🗑️ Deleting clips…",done:j.done,total:j.total,
                                 note:(j.freed?fmtBytes(j.freed)+" freed":"")+(j.errors?` · ${j.errors} error(s)`:"")});
      } else if(!j.running){
        clearInterval(poll);
        $("#purgeBar").innerHTML=""; $("#purgeCancel").style.display="none";
        $("#purgeBtn").disabled=false; $("#purgeBtn").style.display="none";
        const ok=j.done-(j.skipped||0);
        $("#purgeMsg").textContent=(j.cancelled?"Cancelled — ":"")
          +`${ok} clip(s) deleted, ${fmtBytes(j.freed||0)} freed`+(j.errors?` · ${j.errors} error(s)`:"");
        refreshStatus(); loadClips(true);
      }
    }
  },1000);
}
function populatePurgeReason(){
  const sel=$("#purgeReason"); if(!sel) return;
  const counts={};
  for(const c of allClips) if(c.reason) counts[c.reason]=(counts[c.reason]||0)+1;
  const entries=Object.entries(counts).sort((a,b)=>b[1]-a[1]);
  sel.innerHTML=entries.map(([r,n])=>`<option value="${r}">${REASON_LABELS[r]||r} (${n})</option>`).join("")
    ||'<option value="">(no events found)</option>';
}

// ---------- Tools (fetch keys) ----------
async function onKeyFile(e){
  const f=e.target.files[0]; if(!f) return; $("#msg").textContent="Uploading…";
  let data; try{ data=JSON.parse(await f.text()); }catch(err){ $("#msg").textContent="Invalid JSON."; return; }
  const r=await fetch("api/keys",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}).then(r=>r.json()).catch(()=>({ok:false}));
  $("#msg").textContent=r.ok?`${r.stored} keys saved.`:"Error saving.";
  if(r.ok){ refreshStatus(); loadClips(true); }
}
async function boot(){
  $("#bmlink").href="javascript:"+BM;
  $("#toolsbtn").onclick=()=>$("#tools").style.display="flex";
  $("#toolsx").onclick=()=>$("#tools").style.display="none";
  $("#copybm").onclick=async()=>{try{await navigator.clipboard.writeText(BM);$("#copybm").textContent="copied ✓";setTimeout(()=>$("#copybm").textContent="Copy snippet",1500);}catch(e){alert("Copy failed.");}};
  $("#keyfile").onchange=onKeyFile;
  // Not awaited: this only fills the login link inside the Keys panel, so it
  // must not delay the status line and the clip list.
  fetch("api/login/url").then(r=>r.json()).then(({url})=>{
    $("#loginlink").href=url; $("#loginurl").value=url;
  }).catch(()=>{});
  $("#cbgo").onclick=async()=>{
    const r=await fetch("api/login/exchange",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({callback:$("#cb").value.trim()})}).then(r=>r.json());
    $("#lmsg").textContent=r.ok?("Login OK"+(r.refresh?" – Refresh token received.":".")):("Error: "+r.error); refreshStatus();
  };
  $("#fetchbtn").onclick=async()=>{
    $("#fetchbtn").disabled=true; $("#lmsg").textContent="Starting…";
    await fetch("api/fetch",{method:"POST"});
    const poll=setInterval(async()=>{
      const st=await fetch("api/status").then(r=>r.json()).catch(()=>null);
      if(st&&st.fetch_job){
        const j=st.fetch_job;
        $("#fetchbar").classList.toggle("on",!!j.running);
        if(j.running){
          $("#lmsg").textContent=j.total?`${j.done}/${j.total}…`:"Checking which clips still need a key…";
          renderBar($("#fetchbar"),{label:"🔑 Fetching keys from Tesla…",done:j.done,total:j.total});
        } else {
          clearInterval(poll);
          $("#fetchbar").innerHTML="";
          $("#fetchbtn").disabled=false;
          const la=st.last_api||{};
          $("#lmsg").textContent = la.ok===false ? `Error: ${la.msg||"failed"}`
            : la.got ? `${la.got} key(s) fetched.`
            : (la.msg || "Done.");
          refreshStatus(); loadClips(true);
        }
      }
    },1000);
  };
  $("#decryptbtn").onclick=async()=>{
    const s=await fetch("api/status").then(r=>r.json()).catch(()=>null);
    const n=s?s.need_decrypt:0, del=!!(s&&s.delete_originals);
    if(!n){ $("#decmsg").textContent="Nothing to decrypt — every keyed clip is already done."; return; }
    // Deleting the originals cannot be undone, so it is confirmed explicitly
    // and the message spells out what is about to happen.
    const msg=del
      ? `Decrypt ${n} file(s) and then DELETE the encrypted originals from the NAS.\n\n`
        +`The decrypted copy in the "decrypted" folder becomes your only copy. This cannot be undone.\n\nContinue?`
      : `Decrypt ${n} file(s)? The encrypted originals are kept.`;
    if(!confirm(msg)) return;
    $("#decryptbtn").disabled=true; $("#decmsg").textContent="Starting…";
    $("#deccancel").style.display="inline-block"; $("#deccancel").disabled=false;
    await fetch("api/decrypt",{method:"POST"});
    const poll=setInterval(async()=>{
      const st=await fetch("api/status").then(r=>r.json()).catch(()=>null);
      if(st&&st.dec_job){
        const j=st.dec_job;
        $("#decbar").classList.toggle("on",!!j.running);
        if(j.running){
          const notes=[];
          if(j.errors) notes.push(`${j.errors} error(s)`);
          if(j.skipped) notes.push(`${j.skipped} skipped (cancelling)`);
          $("#decmsg").textContent=`${j.done}/${j.total}…`;
          renderBar($("#decbar"),{label:j.deleting?"🔓 Decrypting and removing originals…":"🔓 Decrypting…",
                                 done:j.done,total:j.total,note:notes.join(" · ")});
        } else {
          clearInterval(poll);
          $("#decbar").innerHTML="";
          const ok=j.done-j.errors-(j.skipped||0);
          $("#decmsg").textContent=j.total>0
            ? (j.cancelled?`Cancelled — ${ok}/${j.total} decrypted`:`${ok}/${j.total} decrypted`)
              +(j.errors?` · ${j.errors} failed`:"")
              +(j.deleting&&ok?" · originals removed":"")
            : "nothing to do";
          $("#decryptbtn").disabled=false;
          $("#deccancel").style.display="none";
          refreshStatus(); loadClips(true);
        }
      }
    },1000);
  };
  // Separate from "Decrypt everything": these clips are already decrypted, only
  // their encrypted originals are still taking up space.
  async function refreshCleanup(){
    const info=$("#cleanupInfo"), btn=$("#cleanupbtn");
    if(!info||!btn) return;
    const s=await fetch("api/status").then(r=>r.json()).catch(()=>null);
    if(!s||!s.delete_originals){ info.textContent=""; btn.style.display="none"; return; }
    const p=await fetch("api/cleanup/preview").then(r=>r.json()).catch(()=>null);
    if(!p||!p.files){ info.textContent="No leftover encrypted originals — nothing to clean up."; btn.style.display="none"; return; }
    const size=p.bytes_estimate?` (${p.exact?"":"about "}${fmtBytes(p.bytes_estimate)})`:"";
    info.innerHTML=`<b>${p.files}</b> encrypted original(s)${size} belong to clips that are already decrypted. `
      +`Removing them frees that space; the decrypted copy stays and remains playable.`;
    btn.style.display="inline-block";
  }
  // Undecryptable files: quarantine moves them (reversible, target
  // configurable via broken_subpath); delete removes them for good and needs
  // no target, so it's offered even when broken_subpath isn't set.
  async function refreshQuarantine(){
    const info=$("#quarantineInfo"), btn=$("#quarantinebtn"), delBtn=$("#brokenDeleteBtn");
    if(!info||!btn||!delBtn) return;
    const p=await fetch("api/quarantine/preview").then(r=>r.json()).catch(()=>null);
    if(!p){ info.textContent=""; btn.style.display="none"; delBtn.style.display="none"; return; }
    if(!p.files){
      info.textContent="No undecryptable clips — nothing to clean up.";
      btn.style.display="none"; delBtn.style.display="none"; return;
    }
    const size=p.bytes_estimate?` (${p.exact?"":"about "}${fmtBytes(p.bytes_estimate)})`:"";
    info.innerHTML=`<b>${p.files}</b> file(s)${size} are encrypted but contain no key of their own, `
      +`so nothing can ever decrypt them — not this add-on, not Tesla's own app. `
      +(p.enabled?`Move them to <code>${p.target}</code> to get them out of the clip list without `
        +`deleting anything, or delete them permanently to free the space.`
        :`Delete them permanently to free the space (set <code>broken_subpath</code> in the add-on `
        +`options to move them aside instead).`);
    btn.style.display=p.enabled?"inline-block":"none";
    delBtn.style.display="inline-block";
  }
  // Both actions share the single bulk-job slot (_dec_job) and its bar/cancel
  // button with decrypt/purge/cleanup, so this just needs its own label text.
  function runBrokenJob(endpoint,label,doneWord){
    return async()=>{
      const p=await fetch("api/quarantine/preview").then(r=>r.json()).catch(()=>null);
      if(!p||!p.files){ $("#quarantinemsg").textContent="Nothing to do."; return; }
      const msg=doneWord==="deleted"
        ? `Permanently DELETE ${p.files} undecryptable file(s)`
          +(p.bytes_estimate?` (${p.exact?"":"about "}${fmtBytes(p.bytes_estimate)})`:"")+`.\n\n`
          +`This cannot be undone — they're unrecoverable anyway (no key ever existed for them), `
          +`but the files themselves will be gone.\n\nContinue?`
        : `Move ${p.files} undecryptable file(s) to:\n${p.target}\n\n`
          +`They leave the clip list but are not deleted — the folder structure is kept, `
          +`so you can move them back at any time.\n\nContinue?`;
      if(!confirm(msg)) return;
      $("#quarantinebtn").disabled=true; $("#brokenDeleteBtn").disabled=true;
      $("#quarantinemsg").textContent="Starting…";
      $("#deccancel").style.display="inline-block"; $("#deccancel").disabled=false;
      await fetch(endpoint,{method:"POST"});
      const poll=setInterval(async()=>{
        const st=await fetch("api/status").then(r=>r.json()).catch(()=>null);
        if(st&&st.dec_job){
          const j=st.dec_job;
          $("#decbar").classList.toggle("on",!!j.running);
          if(j.running){
            $("#quarantinemsg").textContent=`${j.done}/${j.total}…`;
            renderBar($("#decbar"),{label,done:j.done,total:j.total,
                                   note:(j.freed?fmtBytes(j.freed)+" freed":"")+(j.errors?` · ${j.errors} error(s)`:"")});
          } else {
            clearInterval(poll);
            $("#decbar").innerHTML="";
            const ok=j.done-j.errors-(j.skipped||0);
            $("#quarantinemsg").textContent=(j.cancelled?"Cancelled — ":"")
              +`${ok} ${doneWord}`+(j.freed?` · ${fmtBytes(j.freed)} freed`:"")+(j.errors?` · ${j.errors} error(s)`:"");
            $("#quarantinebtn").disabled=false; $("#brokenDeleteBtn").disabled=false;
            $("#deccancel").style.display="none";
            refreshStatus(); loadClips(true); refreshQuarantine().catch(()=>{});
          }
        }
      },1000);
    };
  }
  $("#quarantinebtn").onclick=runBrokenJob("api/quarantine","📦 Moving undecryptable files…","moved");
  $("#brokenDeleteBtn").onclick=runBrokenJob("api/quarantine/delete","🗑️ Deleting undecryptable files…","deleted");
  $("#toolsbtn").addEventListener("click",()=>{
    refreshCleanup().catch(()=>{}); refreshQuarantine().catch(()=>{});
  });
  $("#cleanupbtn").onclick=async()=>{
    const p=await fetch("api/cleanup/preview").then(r=>r.json()).catch(()=>null);
    if(!p||!p.files){ $("#cleanupmsg").textContent="Nothing to clean up."; return; }
    const size=p.bytes_estimate?` ${p.exact?"":"about "}${fmtBytes(p.bytes_estimate)},`:"";
    if(!confirm(`DELETE ${p.files} encrypted original file(s),${size} from the NAS.\n\n`
      +`These clips are already decrypted — the decrypted copy stays and remains playable, `
      +`but the encrypted source cannot be recovered.\n\nContinue?`)) return;
    $("#cleanupbtn").disabled=true; $("#cleanupmsg").textContent="Starting…";
    $("#deccancel").style.display="inline-block"; $("#deccancel").disabled=false;
    await fetch("api/cleanup",{method:"POST"});
    const poll=setInterval(async()=>{
      const st=await fetch("api/status").then(r=>r.json()).catch(()=>null);
      if(st&&st.dec_job){
        const j=st.dec_job;
        $("#decbar").classList.toggle("on",!!j.running);
        if(j.running){
          $("#cleanupmsg").textContent=`${j.done}/${j.total}…`;
          renderBar($("#decbar"),{label:"🗑️ Removing encrypted originals…",done:j.done,total:j.total,
                                 note:(j.freed?fmtBytes(j.freed)+" freed":"")+(j.errors?` · ${j.errors} skipped`:"")});
        } else {
          clearInterval(poll);
          $("#decbar").innerHTML="";
          const ok=j.done-j.errors-(j.skipped||0);
          $("#cleanupmsg").textContent=(j.cancelled?"Cancelled — ":"")
            +`${ok} removed, ${fmtBytes(j.freed||0)} freed`+(j.errors?` · ${j.errors} skipped`:"");
          $("#cleanupbtn").disabled=false; $("#deccancel").style.display="none";
          refreshStatus(); loadClips(true); refreshCleanup().catch(()=>{});
        }
      }
    },1000);
  };
  $("#deccancel").onclick=async()=>{
    $("#deccancel").disabled=true; $("#decmsg").textContent="Cancelling…";
    await fetch("api/decrypt/cancel",{method:"POST"}).catch(()=>{});
  };
  $("#thumbsbtn").onclick=async()=>{
    $("#thumbsbtn").disabled=true; $("#thumbmsg").textContent="Generating…";
    await fetch("api/thumbs_all",{method:"POST"});
    const poll=setInterval(async()=>{
      const s=await fetch("api/status").then(r=>r.json()).catch(()=>null);
      if(s&&s.thumb_job){
        const j=s.thumb_job;
        $("#thumbbar").classList.toggle("on",!!j.running);
        if(j.running){
          $("#thumbmsg").textContent=`${j.done}/${j.total}…`;
          renderBar($("#thumbbar"),{label:"🖼️ Generating thumbnails…",done:j.done,total:j.total});
        } else {
          clearInterval(poll);
          $("#thumbmsg").textContent=j.total>0?`${j.done}/${j.total} done ✓`:"none new";
          $("#thumbbar").innerHTML="";
          $("#thumbsbtn").disabled=false;
          loadClips(true);
        }
      }
    },800);
  };
  // Backfill (E) and resync (F) share one job slot on the server (_tel_job) —
  // starting either while the other runs is refused, so both buttons disable
  // together and either row's poll loop recognises a job it didn't start.
  function setTelButtonsBusy(busy){
    $("#telbtn").disabled=busy; $("#resyncbtn").disabled=busy;
  }
  async function pollTelJob(expectMode,onDone){
    const poll=setInterval(async()=>{
      const s=await fetch("api/status").then(r=>r.json()).catch(()=>null);
      if(!s||!s.tel_job) return;
      const j=s.tel_job;
      const mine=j.mode===expectMode||(!j.running&&_lastTelMode===expectMode);
      if(!mine && j.running) return;   // the other row's job is running; ignore
      if(j.running){
        _lastTelMode=j.mode;
        setTelButtonsBusy(true);
        onDone(j,true);
      } else {
        clearInterval(poll);
        setTelButtonsBusy(false);
        onDone(j,false);
      }
    },800);
    return poll;
  }
  let _lastTelMode=null;

  $("#telbtn").onclick=async()=>{
    setTelButtonsBusy(true); $("#telmsg").textContent="Extracting…";
    $("#telcancel").style.display="inline-block"; $("#telcancel").disabled=false;
    _lastTelMode="backfill";
    await fetch("api/telemetry_all",{method:"POST"});
    pollTelJob("backfill",(j,running)=>{
      $("#telbar").classList.toggle("on",running);
      if(running){
        $("#telmsg").textContent=`${j.done}/${j.total}…`;
        renderBar($("#telbar"),{label:"🛰️ Extracting telemetry…",done:j.done,total:j.total,
                               note:j.skipped?`${j.skipped} skipped (cancelling)`:""});
      } else {
        $("#telmsg").textContent=j.total>0
          ? (j.cancelled?"Cancelled — ":"")+`${j.done-(j.skipped||0)}/${j.total} done`
          : "none new";
        $("#telbar").innerHTML=""; $("#telcancel").style.display="none";
        loadClips(true);
      }
    });
  };
  $("#telcancel").onclick=async()=>{
    $("#telcancel").disabled=true; $("#telmsg").textContent="Cancelling…";
    await fetch("api/telemetry_all/cancel",{method:"POST"}).catch(()=>{});
  };

  // "Fix telemetry sync": re-extract telemetry for clips whose cached JSON
  // predates the 0.7.15 frame-timing fix. Only ever overwrites a
  // telemetry.json sidecar — the clip, its encrypted original and the key
  // store are untouched, so this needs no confirmation dialog.
  async function refreshResync(){
    const info=$("#resyncInfo"), btn=$("#resyncbtn");
    if(!btn) return;
    const p=await fetch("api/telemetry_resync/preview").then(r=>r.json()).catch(()=>null);
    if(!p||!p.files){
      $("#resyncmsg").textContent=p?"All telemetry is already on the current schema — nothing to fix.":"";
      btn.style.display="none";
      return;
    }
    $("#resyncmsg").textContent=`${p.files} clip(s) have telemetry from before this fix.`;
    btn.style.display="inline-block";
  }
  $("#toolsbtn").addEventListener("click",()=>refreshResync().catch(()=>{}));
  $("#resyncbtn").onclick=async()=>{
    setTelButtonsBusy(true); $("#resyncmsg").textContent="Starting…";
    $("#resynccancel").style.display="inline-block"; $("#resynccancel").disabled=false;
    _lastTelMode="resync";
    await fetch("api/telemetry_resync",{method:"POST"});
    pollTelJob("resync",(j,running)=>{
      $("#resyncbar").classList.toggle("on",running);
      if(running){
        const notes=[];
        if(j.errors) notes.push(`${j.errors} error(s)`);
        if(j.skipped) notes.push(`${j.skipped} skipped (cancelling)`);
        $("#resyncmsg").textContent=`${j.done}/${j.total}…`;
        renderBar($("#resyncbar"),{label:"🩹 Re-extracting telemetry…",done:j.done,total:j.total,
                                  note:notes.join(" · ")});
      } else {
        $("#resyncbar").innerHTML=""; $("#resynccancel").style.display="none";
        const ok=j.done-(j.errors||0)-(j.skipped||0);
        $("#resyncmsg").textContent=j.total>0
          ? (j.cancelled?"Cancelled — ":"")+`${ok}/${j.total} fixed`+(j.errors?` · ${j.errors} failed`:"")
          : "nothing to do";
        loadClips(true); refreshResync().catch(()=>{});
      }
    });
  };
  $("#resynccancel").onclick=async()=>{
    $("#resynccancel").disabled=true; $("#resyncmsg").textContent="Cancelling…";
    await fetch("api/telemetry_all/cancel",{method:"POST"}).catch(()=>{});
  };

  $("#t_hud").onchange=applyHud;
  $("#t_nerd").onchange=()=>{ if(!$("#t_nerd").checked){ $("#nerd").style.display="none"; } else if(!(tele&&tele.frame_count)&&curEvent){ nerdEvent(); $("#nerd").style.display="block"; } };
  $("#t_map").onchange=()=>showMap($("#t_map").checked);
  $("#purgeCat").onchange=()=>{
    $("#purgeReason").style.display=$("#purgeCat").value==="reason"?"inline-block":"none";
    $("#purgeBtn").style.display="none"; $("#purgeMsg").textContent="";
  };
  $("#purgeReason").onchange=()=>{ $("#purgeBtn").style.display="none"; $("#purgeMsg").textContent=""; };
  $("#purgeDays").oninput=()=>{ $("#purgeBtn").style.display="none"; $("#purgeMsg").textContent=""; };
  $("#purgePreviewBtn").onclick=()=>purgePreview().catch(()=>{});
  $("#purgeBtn").onclick=()=>runPurge().catch(()=>{});
  $("#purgeCancel").onclick=async()=>{
    $("#purgeCancel").disabled=true; $("#purgeMsg").textContent="Cancelling…";
    await fetch("api/decrypt/cancel",{method:"POST"}).catch(()=>{});
  };
  $("#keepbtn").onclick=async()=>{
    if(!activeId) return;
    const c=allClips.find(x=>x.id===activeId);
    const want=!(c&&c.protected);
    const r=await fetch("api/protect",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:activeId,protected:want})}).then(x=>x.json()).catch(()=>null);
    if(r){ if(c) c.protected=r.protected; updateKeepBtn(r.protected); buildSidebar(); markActive(activeId); }
  };
  $("#dlzip").onclick=()=>{ if(!activeId){return;} $("#vmsg").textContent="Creating ZIP…"; const a=document.createElement("a"); a.href="api/zip?id="+encodeURIComponent(activeId); a.click(); setTimeout(()=>$("#vmsg").textContent="",5000); };
  $("#permdec").onclick=async()=>{ if(!activeId){return;} $("#vmsg").textContent="Decrypting & saving…"; const r=await fetch("api/prepare",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:activeId})}).then(r=>r.json()).catch(()=>({ok:false})); $("#vmsg").textContent=(r&&(r.ok||r.clip))?"✓ permanently saved (folder decrypted/)":"Error"; refreshStatus(); loadClips(true); };
  $("#search").oninput=buildSidebar;
  $("#filterReason").onchange=e=>setReasonFilter(e.target.value);
  $("#filterDrive").onchange=buildSidebar;
  $("#filterEvent").onchange=buildSidebar;
  $("#filterHonk").onchange=buildSidebar;

  document.querySelectorAll(".tabbtn").forEach(b=>b.onclick=()=>switchView(b.dataset.view));
  $("#tripPrev").onclick=prevTrip;
  $("#tripNext").onclick=nextTrip;
  $("#tcViewClips").onclick=viewTripClips;
  $("#evPrev").onclick=()=>jumpToSegment(eventSegIdx-1);
  $("#evNext").onclick=()=>jumpToSegment(eventSegIdx+1);
  $("#playerClose").onclick=closePlayerOverlay;
  $("#themeToggle").onclick=toggleTheme;
  applyThemeIcon();
  $("#viewToggle").onclick=toggleClipView;
  applyViewIcon();

  // If the very first load takes unusually long, say so instead of sitting
  // silently on "loading". An index scan in progress is NOT unexpected — that
  // one has its own progress bar — so only complain when nothing is running.
  const slowHint=setTimeout(()=>{
    if($("#status").textContent.includes("loading")&&!$("#scanbar").classList.contains("on")){
      $("#status").textContent="⏳ still loading… (taking longer than expected — check the add-on log, or enable debug_logging)";
    }
  },8000);

  // No initLandingMap() here — switchView("map") builds it on first use, so
  // boot does not pull map tiles for a tab that may never be opened.
  try { await refreshStatus(); } catch(e){ console.error("status failed:", e); }
  clearTimeout(slowHint);
  await loadClips(false);
  try { await loadTrips(); } catch(e){ console.error("trips failed:", e); }
  // Poll faster while an index scan is running, so the bar actually moves.
  let pollMs=5000;
  (function poll(){
    setTimeout(async()=>{
      try{
        const s=await refreshStatus();
        pollMs=(s&&s.scan_job&&s.scan_job.running)?1500:5000;
      }catch(e){}
      poll();
    },pollMs);
  })();
}
boot();
