#!/usr/bin/env python3
"""
Frontend markup for the AnimePahe downloader: the HTML/CSS/JS single-page UI
rendered inside the pywebview window. Kept as a module-level string so
PyInstaller bundles it with the code (no extra data file to ship).
"""
# ── Frontend (HTML/CSS/JS served into the pywebview window) ──────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PAHE DL</title>
<style>
  :root{
    --bg:#0a0a14; --bg2:#0d0d1c; --surface:#12122150; --card:#16162a;
    --card2:#1c1c34; --border:#2d2b50; --border2:#3a3866;
    --magenta:#e94aff; --magenta-dim:#a21caf; --purple:#a855f7;
    --deep:#6d28d9; --cyan:#22d3ee; --text:#f5f3ff; --sub:#b6b3da;
    --muted:#6e6c9c; --success:#34d399; --error:#fb7185; --warn:#fcd34d;
  }
  *{box-sizing:border-box; margin:0; padding:0}
  html,body{height:100%}
  body{
    font-family:"Inter","Segoe UI",system-ui,sans-serif;
    background:
      radial-gradient(1200px 600px at 80% -10%, #2a0b4e55, transparent 60%),
      radial-gradient(900px 500px at -10% 110%, #0a3b4a55, transparent 60%),
      var(--bg);
    color:var(--text); overflow:hidden; user-select:none;
  }
  /* faint synthwave grid floor */
  body::before{
    content:""; position:fixed; inset:0; pointer-events:none; opacity:.05;
    background-image:linear-gradient(var(--magenta) 1px,transparent 1px),
      linear-gradient(90deg,var(--magenta) 1px,transparent 1px);
    background-size:42px 42px;
    mask-image:linear-gradient(transparent 55%, #000 130%);
  }
  .app{display:flex; flex-direction:column; height:100vh}

  /* ── top bar ─────────────────────────────────────────── */
  .topbar{
    display:flex; align-items:center; gap:16px; padding:14px 18px;
    background:linear-gradient(180deg,#13132488,#0d0d1c88);
    backdrop-filter:blur(8px);
    border-bottom:1px solid var(--border);
    box-shadow:0 1px 0 #e94aff33, 0 8px 30px #00000060;
  }
  .logo{display:flex; align-items:center; gap:9px; font-weight:800; font-size:18px;
    letter-spacing:.5px; padding:8px 16px; border-radius:12px;
    background:linear-gradient(135deg,var(--deep),var(--magenta-dim));
    box-shadow:0 0 18px #e94aff55, inset 0 0 12px #ffffff15;}
  .logo .dot{color:var(--cyan); text-shadow:0 0 10px var(--cyan)}
  .logo .dl{color:var(--cyan); text-shadow:0 0 8px #22d3ee99}

  .searchwrap{flex:1; display:flex; gap:8px; align-items:center}
  .searchbox{flex:1; position:relative; display:flex; align-items:center;
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:0 14px; transition:.2s; box-shadow:inset 0 1px 2px #00000040;}
  .searchbox:focus-within{border-color:var(--magenta);
    box-shadow:0 0 0 3px #e94aff22, 0 0 22px #e94aff44;}
  .searchbox svg{width:17px; height:17px; color:var(--muted); flex:none}
  .searchbox input{flex:1; background:none; border:none; outline:none;
    color:var(--text); font-size:14px; padding:12px 10px; user-select:text}
  .searchbox input::placeholder{color:var(--muted)}

  .btn{border:none; cursor:pointer; border-radius:11px; font-size:13px;
    font-weight:600; padding:11px 16px; transition:.18s; white-space:nowrap;
    font-family:inherit;}
  .btn-primary{background:linear-gradient(135deg,var(--deep),var(--magenta));
    color:#fff; box-shadow:0 0 16px #e94aff44;}
  .btn-primary:hover{filter:brightness(1.12); box-shadow:0 0 22px #e94aff77;
    transform:translateY(-1px)}
  .btn-ghost{background:var(--card); color:var(--magenta);
    border:1px solid var(--border)}
  .btn-ghost.cyan{color:var(--cyan)}
  .btn-ghost:hover{border-color:var(--magenta); color:#fff;
    box-shadow:0 0 14px #e94aff33}
  .btn-ghost.cyan:hover{border-color:var(--cyan); box-shadow:0 0 14px #22d3ee33}
  .btn-ghost.active{background:#241a3d; border-color:var(--magenta); color:#fff}
  .btn-ghost.cyan.active{border-color:var(--cyan)}

  .engine{display:flex; align-items:center; gap:10px; flex:none}
  .dot{display:flex; align-items:center; gap:6px; font-size:12px; color:var(--sub)}
  .dot .led{width:8px; height:8px; border-radius:50%; background:var(--warn);
    box-shadow:0 0 8px currentColor; animation:pulse 1.6s infinite}
  .dot.ready .led{background:var(--success); animation:none}
  .dot.error .led{background:var(--error); animation:none}
  @keyframes pulse{50%{opacity:.35}}

  /* ── main split ──────────────────────────────────────── */
  .main{flex:1; display:grid; grid-template-columns:1.4fr 1fr; gap:14px;
    padding:14px 18px; min-height:0}
  .panel{background:linear-gradient(180deg,#14142688,#10101e88);
    border:1px solid var(--border); border-radius:16px; display:flex;
    flex-direction:column; min-height:0; overflow:hidden;
    box-shadow:0 10px 40px #00000050}
  .panel-head{display:flex; align-items:center; justify-content:space-between;
    padding:13px 16px; border-bottom:1px solid var(--border);
    font-size:11px; font-weight:700; letter-spacing:1.5px}
  .panel-head .magenta{color:var(--magenta); text-shadow:0 0 10px #e94aff66}
  .panel-head .cyan{color:var(--cyan); text-shadow:0 0 10px #22d3ee66}
  .panel-head .meta{color:var(--muted); font-weight:500; letter-spacing:.3px}

  /* ── results card grid ──────────────────────────────── */
  .grid{flex:1; overflow-y:auto; padding:14px; display:grid;
    grid-template-columns:repeat(auto-fill,minmax(132px,1fr));
    gap:13px; align-content:start}
  /* Poster tile. Height is a FIXED pixel value, not a ratio — this WebKitGTK
     mis-resolves both `aspect-ratio` and percentage padding on grid items when
     the column is narrow, collapsing the tiles. A fixed height can't collapse;
     the column width still flexes to fill, and the poster background cover-crops
     to fit. The poster and caption are absolute overlays. */
  .card{position:relative; border-radius:13px; overflow:hidden; cursor:pointer;
    background:var(--card); border:1px solid var(--border); transition:.18s;
    height:232px}
  .card:hover{border-color:var(--magenta); transform:translateY(-3px);
    box-shadow:0 8px 26px #00000070, 0 0 18px #e94aff44}
  .card.sel{border-color:var(--cyan); box-shadow:0 0 0 2px #22d3ee55,0 0 18px #22d3ee44}
  .card .thumb{position:absolute; top:0; left:0; right:0; bottom:0;
    background-size:cover; background-position:center;
    background-color:var(--card2)}
  .card .thumb.fallback{background:linear-gradient(135deg,#1c1c34,#241a3d)}
  .card .thumb.fallback span{position:absolute; top:0; left:0; right:0; bottom:0;
    display:flex; align-items:center; justify-content:center; font-size:30px;
    opacity:.5; filter:drop-shadow(0 0 8px #e94aff88)}
  .card .cap{position:absolute; left:0; right:0; bottom:0; padding:9px;
    font-size:11.5px; line-height:1.3; font-weight:600;
    background:linear-gradient(180deg,transparent,#0d0d1ce0 42%,#0d0d1c);
    display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical;
    overflow:hidden}
  .card .badge{position:absolute; top:7px; left:7px; font-size:9.5px;
    font-weight:700; padding:3px 7px; border-radius:7px;
    background:#0a0a14cc; color:var(--cyan); border:1px solid #22d3ee55;
    backdrop-filter:blur(4px)}
  /* Results pulled from the widened catalog search (no cover art) get a muted
     "catalog" chip and a slightly dimmer tile so they're distinct from the
     API's art results. */
  .card .badge.cat{color:var(--muted); border-color:var(--border2);
    text-transform:uppercase; letter-spacing:.5px}
  .card.cat{opacity:.9}
  .card.cat:hover{opacity:1}

  /* ── catalog list view (no artwork — dense title list) ── */
  .listview{flex:1; overflow-y:auto; padding:8px; display:flex;
    flex-direction:column; gap:2px}
  .list-row{display:flex; align-items:center; gap:11px; padding:9px 13px;
    border-radius:9px; cursor:pointer; transition:.12s; font-size:13.5px;
    border:1px solid transparent}
  .list-row:hover{background:var(--card); border-color:var(--border2)}
  .list-row.sel{background:#241a3d; border-color:var(--cyan);
    box-shadow:0 0 12px #22d3ee33}
  .list-row .li-dot{color:var(--deep); font-size:11px; flex:none;
    transition:.12s}
  .list-row:hover .li-dot{color:var(--magenta)}
  .list-row.sel .li-dot{color:var(--cyan)}
  .list-row .li-title{color:var(--text); overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; font-weight:500}

  .empty{margin:auto; text-align:center; color:var(--muted); font-size:13px;
    padding:40px}
  .empty .big{font-size:34px; opacity:.4; margin-bottom:10px}

  /* ── episodes / download panel ──────────────────────── */
  .rightpanel{display:flex; flex-direction:column; min-height:0}
  .ep-meta{padding:8px 16px; font-size:11px; color:var(--sub);
    border-bottom:1px solid var(--border); min-height:30px}
  .ep-list{flex:1; overflow-y:auto; padding:8px 6px}
  .ep-row{display:flex; align-items:center; gap:10px; padding:8px 11px;
    border-radius:9px; cursor:pointer; transition:.12s; font-size:13px}
  .ep-row:hover{background:var(--border)}
  .ep-row.sel{background:#241a3d}
  .ep-check{width:17px; height:17px; border-radius:5px; flex:none;
    border:1.5px solid var(--border2); display:flex; align-items:center;
    justify-content:center; transition:.12s}
  .ep-row.sel .ep-check{background:var(--magenta); border-color:var(--magenta);
    box-shadow:0 0 8px #e94aff88}
  .ep-check svg{width:11px; height:11px; color:#fff; opacity:0}
  .ep-row.sel .ep-check svg{opacity:1}
  .ep-num{color:var(--cyan); font-weight:700; min-width:46px; font-size:12px}
  .ep-title{color:var(--sub); overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; flex:1}
  /* per-episode status chip (on disk / downloading / done / failed) */
  .ep-tag{margin-left:auto; font-size:9px; font-weight:700; letter-spacing:.4px;
    padding:2px 7px; border-radius:6px; text-transform:uppercase; flex:none;
    display:none; border:1px solid transparent; white-space:nowrap}
  .ep-tag.show{display:inline-block}
  .ep-tag.t-disk,.ep-tag.t-skip{color:var(--muted); border-color:var(--border2)}
  .ep-tag.t-active{color:var(--warn); border-color:#fcd34d55;
    animation:pulse 1.4s infinite}
  .ep-tag.t-done{color:var(--success); border-color:#34d39955}
  .ep-tag.t-failed{color:var(--error); border-color:#fb718555}
  .ep-row.on-disk .ep-title,.ep-row.dl-done .ep-title{color:var(--muted)}

  .progtext{font-size:11px; color:var(--cyan); min-height:14px; font-weight:600;
    letter-spacing:.2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}

  /* batch summary + retry (shown under the progress bar during/after a run) */
  .batchbar{display:none; align-items:center; gap:10px; font-size:11.5px;
    color:var(--sub)}
  .batchbar.show{display:flex}
  .batchbar .bcount{color:var(--text); font-weight:600}
  .batchbar .bfail{color:var(--error); font-weight:600}
  .batchbar .bsize{color:var(--muted); margin-left:auto}
  .btn-retry{border:1px solid #fb718566; background:var(--card); color:var(--error);
    padding:5px 11px; border-radius:9px; font-size:11.5px; font-weight:600;
    cursor:pointer; display:none}
  .btn-retry.show{display:inline-block}
  .btn-retry:hover{background:#fb71851a}

  /* update-available chip in the top bar */
  .update-chip{display:none; align-items:center; gap:6px; cursor:pointer;
    font-size:11.5px; font-weight:600; color:#0a0a14; padding:7px 12px;
    border-radius:11px; background:linear-gradient(135deg,var(--success),#22d3ee);
    box-shadow:0 0 14px #34d39955; white-space:nowrap}
  .update-chip.show{display:flex}
  .ver{font-size:10px; color:var(--muted); font-weight:600; letter-spacing:.3px;
    align-self:center; margin-left:2px}

  /* ── controls ───────────────────────────────────────── */
  .controls{border-top:1px solid var(--border); padding:12px 14px;
    display:flex; flex-direction:column; gap:10px;
    background:linear-gradient(180deg,transparent,#0d0d1c80)}
  .ctl-row{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
  .seg{display:flex; gap:4px; align-items:center}
  .seg label{font-size:11px; color:var(--muted); font-weight:600}
  select,.mini-input{background:var(--card); color:var(--text);
    border:1px solid var(--border); border-radius:8px; padding:6px 9px;
    font-size:12px; outline:none; font-family:inherit;
    -webkit-appearance:none; appearance:none; cursor:pointer;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%239d9bc4' stroke-width='3'><path d='M6 9l6 6 6-6'/></svg>");
    background-repeat:no-repeat; background-position:right 8px center; padding-right:26px}
  select option{background:#16162a; color:var(--text)}
  select:focus,.mini-input:focus{border-color:var(--magenta)}
  .mini-input{width:54px; text-align:center; user-select:text}
  .chk{display:flex; align-items:center; gap:7px; cursor:pointer; font-size:12px;
    color:var(--sub)}
  .chk input{accent-color:var(--magenta); width:15px; height:15px}
  .range-in{width:48px}
  .quick{font-size:11px; color:var(--cyan); cursor:pointer; padding:5px 9px;
    border:1px solid var(--border); border-radius:7px}
  .quick:hover{border-color:var(--cyan)}

  .dlbar{display:flex; gap:8px; align-items:center}
  .btn-dl{flex:1; background:linear-gradient(135deg,var(--deep),var(--magenta));
    color:#fff; font-weight:700; padding:13px; border-radius:11px;
    box-shadow:0 0 18px #e94aff55}
  .btn-dl:hover{filter:brightness(1.12); box-shadow:0 0 26px #e94aff88}
  .btn-dl:disabled{opacity:.4; cursor:default; filter:none; box-shadow:none}
  .btn-stop{background:#2a1322; color:var(--error); border:1px solid #fb718555;
    padding:13px 16px; border-radius:11px; font-weight:700}
  .btn-stop:disabled{opacity:.35; cursor:default}

  .folder{display:flex; gap:8px; align-items:center}
  .folder .path{flex:1; font-size:11.5px; color:var(--text); background:var(--card);
    border:1px solid var(--border); border-radius:8px; padding:9px 11px;
    white-space:nowrap; user-select:text; outline:none; font-family:inherit;
    transition:.15s}
  .folder .path::placeholder{color:var(--muted)}
  .folder .path:focus{border-color:var(--magenta); box-shadow:0 0 0 2px #e94aff22}
  .folder .path.ok{border-color:var(--success)}
  .folder .path.bad{border-color:var(--error)}
  .folder-hint{font-size:10.5px; line-height:1.5; white-space:pre-wrap;
    color:var(--muted); padding:0 2px; max-height:0; overflow:hidden;
    transition:max-height .2s}
  .folder-hint.ok{color:var(--success); max-height:60px}
  .folder-hint.bad{color:var(--warn); max-height:140px;
    font-family:ui-monospace,monospace; user-select:text}

  /* ── progress + status ──────────────────────────────── */
  .prog{height:8px; background:var(--card); border-radius:6px; overflow:hidden;
    border:1px solid var(--border)}
  .prog .fill{height:100%; width:0%;
    background:linear-gradient(90deg,var(--deep),var(--magenta),var(--cyan));
    box-shadow:0 0 12px #e94aff88; transition:width .3s; border-radius:6px}
  .statusbar{padding:9px 18px; font-size:12px; border-top:1px solid var(--border);
    background:#0d0d1c; display:flex; align-items:center; gap:9px; min-height:34px}
  .statusbar .sled{width:7px;height:7px;border-radius:50%;background:var(--sub);flex:none}
  .statusbar.info .sled{background:var(--cyan)} .statusbar.info{color:var(--text)}
  .statusbar.warn .sled{background:var(--warn)} .statusbar.warn{color:var(--warn)}
  .statusbar.success .sled{background:var(--success)} .statusbar.success{color:var(--success)}
  .statusbar.error .sled{background:var(--error)} .statusbar.error{color:var(--error)}

  ::-webkit-scrollbar{width:10px; height:10px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--border2); border-radius:6px;
    border:2px solid transparent; background-clip:content-box}
  ::-webkit-scrollbar-thumb:hover{background:var(--purple)}
</style>
</head>
<body>
<div class="app">
  <!-- top bar -->
  <div class="topbar">
    <div class="logo"><span class="dot">◆</span>PAHE<span class="dl">DL</span></div>
    <span class="ver" id="verLabel"></span>
    <div class="searchwrap">
      <div class="searchbox">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="q" placeholder="Search anime…  (empty = Latest)" autocomplete="off">
      </div>
      <button class="btn btn-primary" onclick="doSearch()">Search</button>
      <button class="btn btn-ghost active" id="btnCatalog" onclick="loadCatalog()">Browse all</button>
      <button class="btn btn-ghost cyan" id="btnLatest" onclick="loadLatest()">Latest</button>
    </div>
    <div class="engine">
      <div class="update-chip" id="updateChip" onclick="openRelease()">⬆ Update</div>
      <div class="dot" id="engineDot"><span class="led"></span><span id="engineTxt">starting…</span></div>
      <button class="btn btn-ghost cyan" onclick="runDiagnostics()">Self-test</button>
      <button class="btn btn-ghost" onclick="showBrowser()">Show Browser</button>
    </div>
  </div>

  <!-- main -->
  <div class="main">
    <!-- results -->
    <div class="panel">
      <div class="panel-head">
        <span class="magenta" id="resTitle">RESULTS</span>
        <span class="meta" id="resMeta"></span>
      </div>
      <div class="grid" id="grid">
        <div class="empty"><div class="big">◆</div>Waiting for the browser engine…</div>
      </div>
    </div>

    <!-- episodes + download -->
    <div class="panel rightpanel">
      <div class="panel-head">
        <span class="cyan" id="epHead">EPISODES</span>
        <span class="meta" id="epSel"></span>
      </div>
      <div class="ep-meta" id="epMeta">Pick an anime on the left.</div>
      <div class="ep-list" id="epList"></div>

      <div class="controls">
        <div class="ctl-row">
          <div class="seg"><label>Range</label>
            <input class="mini-input range-in" id="rFrom" placeholder="1">
            <span style="color:var(--muted)">–</span>
            <input class="mini-input range-in" id="rTo" placeholder="12"></div>
          <span class="quick" onclick="applyRange()">Apply</span>
          <span class="quick" onclick="selectAll(true)">All</span>
          <span class="quick" onclick="selectAll(false)">None</span>
          <span class="quick" onclick="selectMissing()" title="Select only episodes you don't already have">Missing</span>
        </div>
        <div class="ctl-row">
          <div class="seg"><label>Quality</label>
            <select id="quality"><option>1080</option><option>720</option><option>480</option><option>360</option></select></div>
          <div class="seg"><label>Audio</label>
            <select id="audio"><option value="jpn">jpn</option><option value="eng">eng</option></select></div>
          <div class="seg"><label>Season</label>
            <input class="mini-input" id="season" value="1"></div>
          <div class="seg"><label>Parallel</label>
            <select id="concurrency" title="How many episodes to download at once. 2–3 is faster but risks a Cloudflare/Kwik block."><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></div>
        </div>
        <div class="ctl-row">
          <label class="chk"><input type="checkbox" id="jellyfin" checked>Jellyfin naming</label>
          <label class="chk"><input type="checkbox" id="skipExisting" checked>Skip existing</label>
        </div>
        <div class="folder">
          <input class="path" id="folderPath" placeholder="Paste a folder path or smb:// share, or use Folder…">
          <button class="btn btn-ghost" onclick="chooseFolder()">Folder…</button>
        </div>
        <div class="folder-hint" id="folderHint"></div>
        <div class="prog"><div class="fill" id="progFill"></div></div>
        <div class="progtext" id="progText"></div>
        <div class="batchbar" id="batchBar">
          <span id="batchSummary"></span>
          <span class="bsize" id="batchSize"></span>
          <button class="btn-retry" id="btnRetry" onclick="retryFailed()">Retry failed</button>
        </div>
        <div class="dlbar">
          <button class="btn btn-dl" id="btnDl" onclick="startDownload()" disabled>Download</button>
          <button class="btn btn-stop" id="btnStop" onclick="stopDownload()" disabled>Stop</button>
        </div>
      </div>
    </div>
  </div>

  <!-- status -->
  <div class="statusbar warn" id="status"><span class="sled"></span><span id="statusTxt">Starting…</span></div>
</div>

<script>
  let RESULTS=[], EPISODES=[], SELECTED=new Set(), FOLDER="", CUR_TITLE="";
  let apiReady=false;

  function api(){ return window.pywebview.api; }

  window.addEventListener('pywebviewready', ()=>{ apiReady=true; restoreSettings(); });

  // ── settings persistence ─────────────────────────────
  async function restoreSettings(){
    try{
      const s = await api().load_settings();
      if(!s) return;
      if(s.quality!==undefined) document.getElementById('quality').value = s.quality;
      if(s.audio!==undefined) document.getElementById('audio').value = s.audio;
      if(s.jellyfin!==undefined) document.getElementById('jellyfin').checked = !!s.jellyfin;
      if(s.skip_existing!==undefined) document.getElementById('skipExisting').checked = !!s.skip_existing;
      if(s.concurrency!==undefined) document.getElementById('concurrency').value = String(s.concurrency);
      if(s.folder){ document.getElementById('folderPath').value = s.folder; setFolder(s.folder); }
    }catch(e){}
  }
  function currentSettings(){
    return {
      quality:document.getElementById('quality').value,
      audio:document.getElementById('audio').value,
      jellyfin:document.getElementById('jellyfin').checked,
      skip_existing:document.getElementById('skipExisting').checked,
      concurrency:parseInt(document.getElementById('concurrency').value)||1,
      folder:document.getElementById('folderPath').value.trim()
    };
  }

  // ── rendering ────────────────────────────────────────
  window.renderResults = (data)=>{
    RESULTS = data.results || [];
    document.getElementById('resMeta').textContent =
      RESULTS.length ? RESULTS.length+" titles" : "";
    const v = data.view;
    document.getElementById('btnCatalog').classList.toggle('active', v==='catalog');
    document.getElementById('btnLatest').classList.toggle('active', v==='latest');
    const g = document.getElementById('grid');
    if(!RESULTS.length){
      g.className='grid';
      g.innerHTML = '<div class="empty"><div class="big">∅</div>Nothing to show.</div>';
      return;
    }
    if(v==='catalog'){
      // Catalog has no artwork — render a clean, dense title list instead of
      // image cards. With thousands of entries, putting every row in the DOM at
      // once makes scrolling sluggish, so we render in chunks and append more as
      // the user scrolls near the bottom (infinite scroll).
      g.className='listview';
      g.innerHTML='';
      _catalogShown = 0;
      renderCatalogChunk(g);
      g.onscroll = ()=>{
        if(g.scrollTop + g.clientHeight >= g.scrollHeight - 400){
          renderCatalogChunk(g);
        }
      };
      return;
    }
    // Load catalog posters for tiles as they scroll into view (debounced).
    let _scrollT=null;
    g.onscroll = ()=>{ clearTimeout(_scrollT);
      _scrollT=setTimeout(queueVisibleCatalogPosters, 120); };
    // Search / Latest → poster card grid
    g.className='grid';
    g.innerHTML = '';
    RESULTS.forEach((r,i)=>{
      const card = document.createElement('div');
      card.className = 'card' + (r.type==='catalog' ? ' cat' : '');
      card.onclick=()=>pickAnime(i);
      let badge = '';
      if(r.type==='airing' && r.status) badge = `<div class="badge">${escapeHtml(r.status)}</div>`;
      else if(r.type==='catalog') badge = `<div class="badge cat">catalog</div>`;
      // The tile has a fixed height (see .card CSS). Start on a
      // fallback tile, then fetch the real image via Python (the site's image
      // host blocks direct cross-origin loads) and swap it into .thumb.
      card.innerHTML =
        `<div class="thumb fallback" data-i="${i}"><span>◆</span>${badge}</div>`+
        `<div class="cap">${escapeHtml(r.title)}</div>`;
      g.appendChild(card);
    });
    lazyLoadPosters();
  };

  let _posterQueue=[];
  let _catalogPosterQueue=[];
  let _catalogQueued=new Set();
  let _catalogPumping=false;
  let epLoading=false;          // true while an episode list is being fetched
  let _catalogShown=0;
  const CATALOG_CHUNK=300;
  function renderCatalogChunk(g){
    const end = Math.min(_catalogShown + CATALOG_CHUNK, RESULTS.length);
    const frag = document.createDocumentFragment();
    for(let i=_catalogShown; i<end; i++){
      const r=RESULTS[i];
      const row=document.createElement('div');
      row.className='list-row'; row.dataset.i=i; row.onclick=()=>pickAnime(i);
      row.innerHTML=`<span class="li-dot">◆</span><span class="li-title">${escapeHtml(r.title)}</span>`;
      frag.appendChild(row);
    }
    g.appendChild(frag);
    _catalogShown = end;
  }
  function applyPoster(i, data){
    if(!data) return;
    const thumb = document.querySelector(`.thumb[data-i='${i}']`);
    if(!thumb) return;
    thumb.classList.remove('fallback');
    thumb.style.backgroundImage = `url('${data}')`;
    const span = thumb.querySelector('span'); if(span) span.remove();
  }
  function lazyLoadPosters(){
    // Fast path: results that already carry a poster URL (from the search API) —
    // load a few at a time so we don't hammer the engine thread.
    _posterQueue = RESULTS.map((r,i)=>({i, url:r.poster}))
                          .filter(x=>x.url && x.url.length>4);
    pumpPosters();
    // Slow path: catalog results have no poster URL, so we fetch each title's
    // cover from its anime page. Every fetch is a real page load through the
    // browser engine, so we only queue tiles that are on/near screen and let
    // more load as the user scrolls — this keeps a 50-result search from tying
    // up the engine, while still filling in art for everything you actually see.
    _catalogQueued = new Set();
    _catalogPosterQueue = [];
    queueVisibleCatalogPosters();
  }
  function queueVisibleCatalogPosters(){
    const g = document.getElementById('grid');
    if(!g) return;
    const gr = g.getBoundingClientRect();
    const margin = 500;   // pre-load a bit beyond the viewport
    g.querySelectorAll('.card').forEach(card=>{
      const thumb = card.querySelector('.thumb.fallback');
      if(!thumb) return;                       // already has art
      const i = +thumb.dataset.i;
      if(_catalogQueued.has(i)) return;
      const r = RESULTS[i];
      if(!r || r.type!=='catalog' || !r.id) return;
      const cr = card.getBoundingClientRect();
      if(cr.bottom >= gr.top - margin && cr.top <= gr.bottom + margin){
        _catalogQueued.add(i);
        _catalogPosterQueue.push({i, id:r.id});
      }
    });
    pumpCatalogPosters();
  }
  async function pumpPosters(){
    if(!apiReady || !_posterQueue.length) return;
    const batch = _posterQueue.splice(0, 4);
    await Promise.all(batch.map(async ({i,url})=>{
      try{ applyPoster(i, await api().get_image(url)); }catch(e){}
    }));
    if(_posterQueue.length) setTimeout(pumpPosters, 60);
  }
  async function pumpCatalogPosters(){
    if(_catalogPumping || !apiReady) return;
    // Yield to user actions: don't hold up an episode load with poster fetches.
    if(epLoading){ setTimeout(pumpCatalogPosters, 500); return; }
    if(!_catalogPosterQueue.length) return;
    _catalogPumping = true;
    const {i,id} = _catalogPosterQueue.shift();
    try{ applyPoster(i, await api().get_poster(id)); }catch(e){}
    _catalogPumping = false;
    if(_catalogPosterQueue.length) setTimeout(pumpCatalogPosters, 180);
  }

  window.renderEpisodes = (data)=>{
    epLoading = false;   // episode load finished → catalog posters may resume
    clearTimeout(window._epGate);
    pumpCatalogPosters();
    EPISODES = data.episodes || [];
    CUR_TITLE = data.title || "";
    SELECTED.clear();
    // Auto-detected season (from the title) — prefill so multi-season shows get
    // the right SxxExx tag; the user can still override it.
    if(data.season) document.getElementById('season').value = data.season;
    document.getElementById('epHead').textContent = 'EPISODES';
    document.getElementById('epMeta').textContent =
      CUR_TITLE + " · " + EPISODES.length + " episodes";
    const l = document.getElementById('epList');
    l.innerHTML='';
    EPISODES.forEach((ep,i)=>{
      const row=document.createElement('div');
      row.className='ep-row'; row.dataset.i=i; row.dataset.ep=ep.episode;
      row.onclick=()=>toggleEp(i);
      row.innerHTML =
        `<div class="ep-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 13l4 4L19 7"/></svg></div>`+
        `<div class="ep-num">EP ${ep.episode}</div>`+
        `<div class="ep-title">${escapeHtml(ep.title||'—')}</div>`+
        `<span class="ep-tag"></span>`;
      l.appendChild(row);
    });
    updateSel(); updateDlState();
    refreshDownloaded();   // grey out episodes already on disk
  };

  // Mark episodes already present on disk (uses the current folder/season/naming).
  async function refreshDownloaded(){
    if(!apiReady || !EPISODES.length) return;
    if(!FOLDER){ document.querySelectorAll('.ep-row').forEach(r=>{
      r.classList.remove('on-disk'); const t=r.querySelector('.ep-tag');
      if(t && t.classList.contains('t-disk')){ t.className='ep-tag'; t.textContent=''; }
    }); return; }
    let have;
    try{
      have = await api().downloaded_episodes({
        episodes: EPISODES.map(e=>e.episode), dest: FOLDER, title: CUR_TITLE,
        season: parseInt(document.getElementById('season').value)||1,
        jellyfin: document.getElementById('jellyfin').checked });
    }catch(e){ return; }
    const set = new Set((have||[]).map(String));
    document.querySelectorAll('.ep-row').forEach(r=>{
      const onDisk = set.has(String(r.dataset.ep));
      const t = r.querySelector('.ep-tag');
      // Don't override a live download status tag.
      if(r.classList.contains('dl-active')||r.classList.contains('dl-done')||
         r.classList.contains('dl-failed')) return;
      r.classList.toggle('on-disk', onDisk);
      if(t){ if(onDisk){ t.className='ep-tag show t-disk'; t.textContent='on disk'; }
             else if(t.classList.contains('t-disk')){ t.className='ep-tag'; t.textContent=''; } }
    });
  }
  function selectMissing(){
    SELECTED.clear();
    document.querySelectorAll('.ep-row').forEach(r=>{
      const i=+r.dataset.i;
      const miss = !r.classList.contains('on-disk');
      r.classList.toggle('sel', miss);
      if(miss) SELECTED.add(i);
    });
    updateSel(); updateDlState();
  }

  // ── actions ──────────────────────────────────────────
  function doSearch(){ if(!apiReady)return; api().search(document.getElementById('q').value); }
  function loadLatest(){ if(!apiReady)return; api().load_latest(); }
  function loadCatalog(){ if(!apiReady)return; api().load_catalog(); }
  function showBrowser(){ if(!apiReady)return; api().show_browser(); }
  function runDiagnostics(){ if(!apiReady)return; api().run_diagnostics(); }
  function openRelease(){ if(apiReady) api().open_release(); }
  function retryFailed(){ if(apiReady) api().retry_failed(); }

  document.getElementById('q').addEventListener('keydown',e=>{ if(e.key==='Enter')doSearch(); });

  function pickAnime(i){
    const r=RESULTS[i]; if(!r)return;
    // Pause catalog poster fetching so the episode load isn't stuck behind it.
    // Safety timeout clears the gate even if the load errors out.
    epLoading = true;
    clearTimeout(window._epGate);
    window._epGate = setTimeout(()=>{ epLoading=false; pumpCatalogPosters(); }, 20000);
    document.querySelectorAll('.card,.list-row').forEach((c,j)=>c.classList.toggle('sel',j===i));
    document.getElementById('epMeta').textContent='Loading episodes…';
    api().get_episodes(r.id, r.title);
  }

  function toggleEp(i){
    if(SELECTED.has(i))SELECTED.delete(i); else SELECTED.add(i);
    document.querySelector(`.ep-row[data-i='${i}']`).classList.toggle('sel',SELECTED.has(i));
    updateSel(); updateDlState();
  }
  function selectAll(on){
    SELECTED.clear();
    if(on)EPISODES.forEach((_,i)=>SELECTED.add(i));
    document.querySelectorAll('.ep-row').forEach(r=>r.classList.toggle('sel',on));
    updateSel(); updateDlState();
  }
  function applyRange(){
    const lo=parseFloat(document.getElementById('rFrom').value);
    const hi=parseFloat(document.getElementById('rTo').value);
    if(isNaN(lo)||isNaN(hi))return;
    SELECTED.clear();
    EPISODES.forEach((ep,i)=>{ const n=parseFloat(ep.episode);
      if(!isNaN(n)&&n>=lo&&n<=hi)SELECTED.add(i); });
    document.querySelectorAll('.ep-row').forEach(r=>{
      const i=+r.dataset.i; r.classList.toggle('sel',SELECTED.has(i)); });
    updateSel(); updateDlState();
  }
  function updateSel(){
    document.getElementById('epSel').textContent = SELECTED.size? SELECTED.size+" selected":"";
  }
  function updateDlState(){
    document.getElementById('btnDl').disabled = SELECTED.size===0 || !FOLDER;
  }

  async function chooseFolder(){
    if(!apiReady)return;
    const f = await api().choose_folder();
    if(f){
      document.getElementById('folderPath').value = f;
      setFolder(f);
    }
  }

  // Validate / resolve a typed/pasted/picked path. Handles smb:// URLs by
  // finding their mount (or showing guidance).
  let _folderTimer=null;
  async function setFolder(path){
    const el = document.getElementById('folderPath');
    const hintEl = document.getElementById('folderHint');
    const raw = (path||'').trim();
    if(!raw){ FOLDER=''; el.classList.remove('ok','bad'); hintEl.textContent=''; hintEl.className='folder-hint'; updateDlState(); return; }
    if(!apiReady){ updateDlState(); return; }
    try{
      const res = await api().resolve_folder(raw);
      FOLDER = res.ok ? res.path : '';
      el.classList.toggle('ok', res.ok);
      el.classList.toggle('bad', !res.ok);
      // If we resolved an smb:// URL to a real mounted path, show it in the box.
      if(res.ok && res.path && res.path!==raw){ el.value = res.path; }
      hintEl.textContent = res.hint || '';
      hintEl.className = 'folder-hint' + (res.ok ? ' ok' : (res.hint ? ' bad' : ''));
    }catch(e){}
    updateDlState();
    refreshDownloaded();   // folder changed → re-check what's already on disk
  }
  // debounce typing on the folder field (script runs at end of <body>, so the
  // element already exists)
  (function(){
    const el=document.getElementById('folderPath');
    if(el) el.addEventListener('input',()=>{
      clearTimeout(_folderTimer);
      _folderTimer=setTimeout(()=>setFolder(el.value), 350);
    });
    // Re-check the on-disk state when the season or naming scheme changes.
    ['season','jellyfin'].forEach(id=>{
      const x=document.getElementById(id);
      if(x) x.addEventListener('change', ()=>refreshDownloaded());
    });
  })();

  function startDownload(){
    if(!apiReady || SELECTED.size===0 || !FOLDER) return;
    const eps=[...SELECTED].sort((a,b)=>a-b).map(i=>({
      ep:EPISODES[i].episode, title:CUR_TITLE,
      session:EPISODES[i].session, anime_id:EPISODES[i].anime_id }));
    // Remember these choices for next launch.
    try{ api().save_settings(currentSettings()); }catch(e){}
    api().start_download({
      episodes:eps, dest:FOLDER,
      quality:document.getElementById('quality').value,
      audio:document.getElementById('audio').value,
      season:parseInt(document.getElementById('season').value)||1,
      jellyfin:document.getElementById('jellyfin').checked,
      skip_existing:document.getElementById('skipExisting').checked,
      concurrency:parseInt(document.getElementById('concurrency').value)||1
    });
    document.getElementById('btnDl').disabled=true;
    document.getElementById('btnStop').disabled=false;
  }
  function stopDownload(){ if(apiReady)api().stop_download();
    document.getElementById('btnStop').disabled=true; }

  function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,c=>(
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  // ── batch queue rendering ────────────────────────────
  const _TAG = {downloading:['t-active','downloading'], done:['t-done','done'],
                failed:['t-failed','failed'], skipped:['t-skip','skipped']};
  function applyBatch(b){
    const items = b.items || [];
    const bar = document.getElementById('batchBar');
    if(!items.length){ bar.classList.remove('show'); return; }
    // Only paint per-episode status onto the list when we're actually looking at
    // the anime this batch belongs to — otherwise a previous show's statuses
    // would bleed onto a different anime's episodes (e.g. showing EP1-12 "done").
    const curAnime = EPISODES.length ? EPISODES[0].anime_id : null;
    const sameAnime = b.anime && curAnime && b.anime === curAnime;
    let done=0, failed=0, active=0;
    items.forEach(it=>{
      if(it.status==='done'||it.status==='skipped') done++;
      else if(it.status==='failed') failed++;
      else if(it.status==='downloading') active++;
      if(!sameAnime) return;   // count totals, but don't tag another anime's rows
      const row = document.querySelector(`.ep-row[data-ep="${CSS.escape(String(it.ep))}"]`);
      if(!row) return;
      row.classList.remove('dl-active','dl-done','dl-failed','dl-skipped','on-disk');
      const t = row.querySelector('.ep-tag'); if(!t) return;
      const map = _TAG[it.status];
      if(it.status==='downloading') row.classList.add('dl-active');
      else if(it.status==='done') row.classList.add('dl-done');
      else if(it.status==='failed') row.classList.add('dl-failed');
      else if(it.status==='skipped') row.classList.add('dl-skipped');
      if(map){ t.className='ep-tag show '+map[0]; t.textContent=map[1]; }
      else { t.className='ep-tag'; t.textContent=''; }
    });
    // Show the summary bar while downloading (global progress) or while viewing
    // the batch's own anime; hide a finished batch when browsing a different show.
    if(!sameAnime && !b.downloading){ bar.classList.remove('show'); return; }
    bar.classList.add('show');
    document.getElementById('batchSummary').innerHTML =
      `<span class="bcount">${done}/${items.length}</span> done`
      + (failed?` · <span class="bfail">${failed} failed</span>`:'')
      + (active?` · ${active} downloading`:'');
    const sz = b.bytes||0, est = b.estimate||0;
    document.getElementById('batchSize').textContent =
      sz>0 ? (fmtGB(sz) + (est>sz ? ' / ~'+fmtGB(est) : '')) : '';
    document.getElementById('btnRetry').classList.toggle('show', failed>0 && !b.downloading);
  }
  function fmtGB(bytes){ const gb=bytes/1073741824;
    return gb>=1 ? gb.toFixed(2)+' GB' : (bytes/1048576).toFixed(0)+' MB'; }

  let _updateShown=false, _verShown=false;
  async function refreshAppInfo(){
    try{
      const a = await api().app_info();
      if(!_verShown && a.version){ document.getElementById('verLabel').textContent='v'+a.version; _verShown=true; }
      if(!_updateShown && a.update && a.update.newer){
        const c=document.getElementById('updateChip');
        c.textContent='⬆ v'+a.update.latest+' available';
        c.classList.add('show'); _updateShown=true;
      }
    }catch(e){}
  }

  // ── polling for status / progress / engine ───────────
  async function poll(){
    if(apiReady){
      try{
        const s=await api().poll_status();
        const bar=document.getElementById('status');
        bar.className='statusbar '+(s.kind||'info');
        document.getElementById('statusTxt').textContent=s.msg;

        const p=await api().poll_progress();
        document.getElementById('progFill').style.width=(p.value||0)+'%';
        document.getElementById('progText').textContent = p.label || '';

        const e=await api().engine_state();
        const dot=document.getElementById('engineDot');
        dot.className='dot'+(e.ready?' ready':'')+(e.dot==='error'?' error':'');
        document.getElementById('engineTxt').textContent=
          e.dot==='minimized'?'ready':e.dot;

        const b=await api().poll_batch();
        applyBatch(b);

        // re-enable download button when a run finishes
        if(p.value>=100 || (s.msg&&s.msg.startsWith('Done'))){
          document.getElementById('btnStop').disabled=true;
          updateDlState();
        }
      }catch(err){}
    }
    setTimeout(poll, 600);
  }
  poll();
  setInterval(refreshAppInfo, 3000); refreshAppInfo();
</script>
</body>
</html>
"""
