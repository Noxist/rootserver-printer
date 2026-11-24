# ui_html.py — Responsive Web UI with Batch Print, Image Preview, Queue Monitoring, and Guest Links
from fastapi.responses import HTMLResponse
from logic import PRINT_WIDTH_PX

# --- HTML Layout ---

_HTML_BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="csrf-token" content="{csrf}">
<title>{title}</title>
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<style>{css}</style>
</head>
<body>
 <header class="top">
  <div class="top-inner wrap">
    <a href="{header_href}" class="title" style="text-decoration:none; color:inherit;">Receipt Printer</a>
    <div class="status-pill" id="status-pill" role="status" aria-live="polite">
      <span class="status-dot"></span>
      <span class="status-text">Checking…</span>
    </div>
    <div class="spacer"></div>
    {nav}
  </div>
</header>
  <main class="wrap">{content}</main>
<script>{js}</script>
</body>
</html>
"""

# --- Main UI Content (Tabs) ---

HTML_UI = f"""
<div class="tabs" role="tablist" aria-label="Mode">
  <div class="tab" role="tab" id="tab-tpl" aria-controls="pane_tpl" aria-selected="true" tabindex="0">Template</div>
  <div class="tab" role="tab" id="tab-raw" aria-controls="pane_raw" aria-selected="false" tabindex="-1">Raw</div>
  <div class="tab" role="tab" id="tab-img" aria-controls="pane_img" aria-selected="false" tabindex="-1">Image</div>
  <div class="tab" role="tab" id="tab-queue" aria-controls="pane_queue" aria-selected="false" tabindex="-1">Queue Status</div>
</div>

<section id="pane_tpl" class="card" role="tabpanel" aria-labelledby="tab-tpl">
  <form method="post" action="/ui/print/template?csrf={{csrf}}">
    <input type="hidden" name="csrf_token" value="{{csrf}}">
    <label for="title">Title</label>
    <input id="title" type="text" name="title" placeholder="Title">

    <label for="lines">Text</label>
    <textarea id="lines" name="lines" placeholder="Text..."></textarea>

    <label><input type="checkbox" name="add_dt" checked> Add date/time</label>

    <div class="form-actions">
      <button type="submit">Print</button>
    </div>
  </form>
</section>

<section id="pane_raw" class="card" role="tabpanel" aria-labelledby="tab-raw" hidden>
  <form method="post" action="/ui/print/raw?csrf={{csrf}}">
    <input type="hidden" name="csrf_token" value="{{csrf}}">
    <label for="text">Raw text</label>
    <textarea id="text" name="text" placeholder="Any text..."></textarea>

    <label><input type="checkbox" name="add_dt"> Add date/time</label>

    <div class="form-actions">
      <button type="submit">Print</button>
    </div>
  </form>
</section>

<section id="pane_img" class="card" role="tabpanel" aria-labelledby="tab-img" hidden>
  <form method="post" action="/ui/print/image?csrf={{csrf}}" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{csrf}}">
    <div class="grid">
      <div>
        <label for="imgfile">Upload Image</label>
        <input id="imgfile" type="file" name="file" accept="image/*" hidden>
        <label for="imgfile" class="file-btn">Choose File</label>
        <span id="file-chosen">No file selected</span>
        
        <div id="img-preview-box" style="margin-top:12px; display:none; max-width:240px; border-radius:8px; overflow:hidden; border:1px solid var(--line);">
          <img id="img-preview" src="" alt="Preview" style="width:100%; display:block;">
        </div>
      </div>
      <div>
        <label for="img_title" style="margin-top:0">Title (optional)</label>
        <input id="img_title" type="text" name="img_title" placeholder="Title">
        <label for="img_subtitle" style="margin-top:8px">Subtitle (optional)</label>
        <input id="img_subtitle" type="text" name="img_subtitle" placeholder="Subtitle">
      </div>
    </div>
    
    <div class="form-actions">
      <button type="submit">Print Image</button>
    </div>
    <div id="drop-zone" class="drop-zone">
      Drag & Drop Image Here
    </div>
  </form>
</section>

<section id="pane_queue" class="card" role="tabpanel" aria-labelledby="tab-queue" hidden>
  <div class="row" style="justify-content:space-between; align-items:center; margin-bottom:12px;">
    <h3 class="title" style="margin:0">Print Queue Status</h3>
    <button type="button" class="secondary" id="btn-refresh-queue" style="padding:6px 14px; font-size:0.9rem;">Refresh</button>
  </div>

  <div class="grid" style="margin-bottom:16px;">
    <div style="padding:12px; border:1px solid var(--line); border-radius:12px; background:var(--card)">
      <div style="font-size:0.85rem; color:var(--muted)">Pending in Queue</div>
      <div id="stat-pending-count" style="font-size:1.6rem; font-weight:700">0</div>
    </div>
    <div style="padding:12px; border:1px solid var(--line); border-radius:12px; background:var(--card)">
      <div style="font-size:0.85rem; color:var(--muted)">Total Archived / Sent</div>
      <div id="stat-archived-count" style="font-size:1.6rem; font-weight:700">0</div>
    </div>
  </div>

  <div style="overflow-x:auto;">
    <table style="width:100%; border-collapse:collapse; font-size:0.9rem;">
      <thead>
        <tr style="border-bottom:1px solid var(--line); text-align:left;">
          <th style="padding:8px">Job ID</th>
          <th style="padding:8px">Status</th>
          <th style="padding:8px">Source</th>
          <th style="padding:8px">Time</th>
        </tr>
      </thead>
      <tbody id="queue-jobs-tbody">
        <tr><td colspan="4" style="padding:12px; color:var(--muted); text-align:center">Loading queue...</td></tr>
      </tbody>
    </table>
  </div>

  <div class="row" style="margin-top:16px; justify-content:flex-end; gap:10px;">
    <form method="post" action="/queue/clear?csrf={{csrf}}" onsubmit="return confirm('Clear pending print queue?');" style="display:inline;">
      <input type="hidden" name="csrf_token" value="{{csrf}}">
      <button type="submit" class="secondary" style="border-color:var(--err); color:var(--err);">Clear Pending</button>
    </form>
  </div>
</section>
"""

# --- Guest UI Content (Template & Image Only, No Queue, No Bulk) ---

GUEST_HTML_UI = f"""
<div class="tabs" role="tablist" aria-label="Mode">
  <div class="tab" role="tab" id="tab-tpl" aria-controls="pane_tpl" aria-selected="true" tabindex="0">Template</div>
  <div class="tab" role="tab" id="tab-img" aria-controls="pane_img" aria-selected="false" tabindex="-1">Image</div>
</div>

<section id="pane_tpl" class="card" role="tabpanel" aria-labelledby="tab-tpl">
  <form method="post" action="/ui/print/template?csrf={{csrf}}">
    <input type="hidden" name="csrf_token" value="{{csrf}}">
    <label for="title">Title</label>
    <input id="title" type="text" name="title" placeholder="Title">

    <label for="lines">Text</label>
    <textarea id="lines" name="lines" placeholder="Text..."></textarea>

    <label><input type="checkbox" name="add_dt" checked> Add date/time</label>

    <div class="form-actions">
      <button type="submit">Print</button>
    </div>
  </form>
</section>

<section id="pane_img" class="card" role="tabpanel" aria-labelledby="tab-img" hidden>
  <form method="post" action="/ui/print/image?csrf={{csrf}}" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{csrf}}">
    <div class="grid">
      <div>
        <label for="imgfile">Upload Image</label>
        <input id="imgfile" type="file" name="file" accept="image/*" hidden>
        <label for="imgfile" class="file-btn">Choose File</label>
        <span id="file-chosen">No file selected</span>
        
        <div id="img-preview-box" style="margin-top:12px; display:none; max-width:240px; border-radius:8px; overflow:hidden; border:1px solid var(--line);">
          <img id="img-preview" src="" alt="Preview" style="width:100%; display:block;">
        </div>
      </div>
      <div>
        <label for="img_title" style="margin-top:0">Title (optional)</label>
        <input id="img_title" type="text" name="img_title" placeholder="Title">
        <label for="img_subtitle" style="margin-top:8px">Subtitle (optional)</label>
        <input id="img_subtitle" type="text" name="img_subtitle" placeholder="Subtitle">
      </div>
    </div>
    
    <div class="form-actions">
      <button type="submit">Print Image</button>
    </div>
    <div id="drop-zone" class="drop-zone">
      Drag & Drop Image Here
    </div>
  </form>
</section>
"""

# --- Functions ---

def html_page(
    title: str,
    content: str,
    show_logout: bool = False,
    csrf_token: str = "",
    secure: bool = False,
    status_code: int = 200,
    show_nav: bool | None = None,
    header_url: str = "/ui",
) -> HTMLResponse:
    if show_nav is None:
        show_nav = show_logout
    nav_html = """
    <nav class="nav" id="main-nav">
      <a class="link" href="/ui">Printer</a>
      <a class="link" href="/ui/guests">Guests</a>
      <a class="link" href="/ui/images">Images</a>
      <a class="link" href="/ui/settings">Settings</a>
    </nav>
    """ if show_nav else ""

    content_rendered = content.replace("{csrf}", csrf_token)
    html = _HTML_BASE \
        .replace("{title}", title) \
        .replace("{content}", content_rendered) \
        .replace("{csrf}", csrf_token) \
        .replace("{css}", _CSS) \
        .replace("{js}", _JS) \
        .replace("{nav}", nav_html) \
        .replace("{header_href}", header_url)
    resp = HTMLResponse(html, status_code=status_code)
    if csrf_token:
        resp.set_cookie(
            key="printer_csrf",
            value=csrf_token,
            max_age=86400,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )
    return resp

def turnstile_challenge_page(site_key: str, next_url: str = "/ui/settings", error: str = None) -> HTMLResponse:
    return html_page("Cloudflare Access Required", "<div class='card'>Cloudflare Access required.</div>", show_logout=False)

def guest_ui_html(ignored_flag: str = "") -> str:
    return GUEST_HTML_UI

def settings_html_form() -> str:
    from logic import settings_effective, SET_KEYS
    eff = settings_effective()
    rows = []

    for key, default, typ, opts in SET_KEYS:
        val = eff.get(key, default)
        label = key.replace("RECEIPT_", "").replace("_", " ").title()
        
        if typ == "select":
            options = "".join([f'<option value="{o}"{" selected" if str(val)==str(o) else ""}>{o}</option>' for o in opts])
            field = f'<select name="{key}">{options}</select>'
        elif typ == "checkbox":
            checked = " checked" if str(val).lower() in ("1","true","yes","on","y","t") else ""
            field = f'<input type="checkbox" name="{key}" value="1"{checked}>'
        elif typ == "number":
            field = f'<input type="number" step="any" name="{key}" value="{val}">'
        else:
            field = f'<input type="text" name="{key}" value="{val}">'
            
        rows.append(f"<div><label>{label}</label>{field}</div>")

    form = f"""
    <section class="card">
      <form method="post" action="/ui/settings/save?csrf={{csrf}}">
        <input type="hidden" name="csrf_token" value="{{csrf}}">
        <div class="grid">
          {''.join(rows)}
        </div>
        <div class="row" style="margin-top:16px; gap:12px; justify-content:flex-end">
          <button type="submit">Save</button>
          <button type="submit" class="link" formaction="/ui/settings/test?csrf={{csrf}}" formmethod="post">Test print</button>
        </div>
      </form>
    </section>
    """
    return form

# --- Static Assets ---

_CSS = r"""
  :root{
    --bg:#f5f7fa; --card:#ffffff; --muted:#475467; --text:#0b1220; --line:#e7eaf0;
    --accent:#3b82f6; --accent-2:#8b5cf6; --err:#ef4444; --radius:18px;
    --shadow:0 4px 24px rgba(0,0,0,.12);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0b0f14; --card:#121821; --muted:#98a2b3; --text:#e6edf3; --line:#1e2a38;
      --shadow:0 6px 26px rgba(0,0,0,.35);
    }
  }
  *{box-sizing:border-box; -webkit-tap-highlight-color:transparent}
  html,body{height:100%}
  body{
    margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    color:var(--text); line-height:1.4;
    background:
      radial-gradient(1200px 800px at 20% -10%, rgba(59,130,246,.15), transparent 70%),
      radial-gradient(1000px 600px at 120% 10%, rgba(139,92,246,.12), transparent 70%),
      var(--bg);
    min-height:100vh;
    display:flex; flex-direction:column;
  }
  .wrap{max-width:920px; margin:0 auto; padding:clamp(16px,3vw,28px); flex:1; width:100%}
  header.top{position:sticky; top:0; backdrop-filter:saturate(1.2) blur(10px);
    background:color-mix(in srgb, var(--bg) 80%, transparent); border-bottom:1px solid var(--line); z-index:10}
  .top-inner{display:flex; align-items:center; gap:14px; padding:12px clamp(12px,3vw,24px)}
  .title{font-weight:700; font-size:1.1rem; letter-spacing:.2px}
  .status-pill{display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px;
    border:1px solid var(--line); background:color-mix(in srgb, var(--card) 92%, transparent);
    color:var(--muted); font-weight:600; font-size:.92rem; min-width:150px; justify-content:flex-start;}
  .status-dot{width:10px; height:10px; border-radius:999px; background:var(--muted); box-shadow:0 0 0 4px color-mix(in srgb, currentColor 12%, transparent);}
  .status-pill.ok{color:#16a34a; border-color:color-mix(in srgb, #16a34a 40%, var(--line)); background:color-mix(in srgb, #16a34a 12%, var(--card));}
  .status-pill.ok .status-dot{background:#16a34a;}
  .status-pill.busy{color:#2563eb; border-color:color-mix(in srgb, #2563eb 40%, var(--line)); background:color-mix(in srgb, #2563eb 12%, var(--card));}
  .status-pill.busy .status-dot{background:#2563eb; animation:pulse-dot 1.2s infinite ease-in-out;}
  @keyframes pulse-dot{0%,100%{transform:scale(0.85);opacity:0.7}50%{transform:scale(1.2);opacity:1}}
  .status-pill.err{color:#ef4444; border-color:color-mix(in srgb, #ef4444 40%, var(--line)); background:color-mix(in srgb, #ef4444 12%, var(--card));}
  .status-pill.err .status-dot{background:#ef4444;}

  .spacer{flex:1}
  .link{color:var(--muted); text-decoration:none; font-size:.95rem; margin-left:16px}
  .link:hover{color:var(--text); text-decoration:underline}
  .card{border:1px solid var(--line); background:color-mix(in srgb, var(--card) 95%, transparent);
        border-radius:var(--radius); box-shadow:var(--shadow);
        padding:clamp(16px,2.8vw,22px); margin:14px 0 22px; width:100%}
  .grid{display:grid; grid-template-columns:1fr 1fr; gap:16px}
  @media (max-width:760px){ .grid{grid-template-columns:1fr} }
  .tabs{display:flex; flex-wrap:wrap; gap:10px; padding:18px 0 10px}
  .tab{border:1px solid var(--line); border-radius:999px; padding:9px 18px; cursor:pointer;
       background:var(--card); font-weight:600; font-size:.95rem; user-select:none;
       transition:all .2s ease; box-shadow:0 2px 6px rgba(0,0,0,.04);}
  .tab[aria-selected="true"]{
       background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff;
       border-color:transparent; box-shadow:0 4px 14px rgba(59,130,246,.35);}
  .row{display:flex; flex-wrap:wrap; align-items:center; gap:12px}
  .grow{flex:1 1 auto}
  label{font-weight:600; color:var(--muted); display:block; margin:10px 0 6px}
  textarea, input[type=text], input[type=password], input[type=file], input[type=number], select{
    width:100%; border:1px solid var(--line); background:transparent; color:var(--text);
    padding:12px 14px; border-radius:12px; outline:none; transition:border-color .15s, box-shadow .15s
  }
  textarea{min-height:140px; resize:vertical}
  input:focus, textarea:focus, select:focus{
    border-color:var(--accent);
    box-shadow:0 0 0 4px color-mix(in srgb, var(--accent) 20%, transparent)
  }
  button{
    appearance:none; border:none; cursor:pointer; font-weight:700;
    padding:13px 22px; border-radius:12px;
    background:linear-gradient(135deg, var(--accent), var(--accent-2)); color:#fff;
    box-shadow:0 6px 18px rgba(59,130,246,.25);
    transition:transform .15s ease, box-shadow .15s ease;
  }
  button:hover{transform:translateY(-1px); box-shadow:0 8px 20px rgba(59,130,246,.3);}
  button.secondary{background:transparent; color:var(--text); border:1px solid var(--line); box-shadow:none}
  .hidden{display:none !important}

  .file-btn{display:inline-block; background:linear-gradient(135deg, var(--accent), var(--accent-2));
    color:#fff; padding:11px 18px; border-radius:12px; font-weight:600; cursor:pointer;
    box-shadow:0 6px 14px rgba(59,130,246,.25);}
  #file-chosen{margin-left:10px; color:var(--muted); font-size:.9rem}
  .drop-zone{
    margin-top:16px; padding:30px; border:2px dashed var(--line); 
    text-align:center; border-radius:12px; color:var(--muted); cursor:pointer
  }

  .form-actions{display:flex; justify-content:flex-end; margin-top:18px}
  @media (max-width:760px){
    .form-actions{justify-content:center}
    .form-actions button{width:100%; max-width:320px}
  }
  input[type="checkbox"]:focus {outline: none; box-shadow: none;}
"""

_JS = f"""
function getCsrfToken(){{
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.getAttribute('content') : '';
}}

// --- Tabs ---
const tabs=[
  {{id:"tpl",btn:"tab-tpl",pane:"pane_tpl"}},
  {{id:"raw",btn:"tab-raw",pane:"pane_raw"}},
  {{id:"img",btn:"tab-img",pane:"pane_img"}},
  {{id:"queue",btn:"tab-queue",pane:"pane_queue"}}
];

function selectTab(id){{
  const available = tabs.filter(t => document.getElementById(t.btn));
  if(!available.length) return;
  const target = available.some(t => t.id === id) ? id : available[0].id;
  available.forEach(t=>{{
    const btn=document.getElementById(t.btn),pane=document.getElementById(t.pane),active=(t.id===target);
    if(btn && pane){{
      btn.setAttribute("aria-selected",active?"true":"false");
      btn.tabIndex=active?0:-1; pane.hidden=!active;
    }}
  }});
  history.replaceState(null,"","#"+target);
  if(target === "queue") loadQueueView();
}}

function initFromHash(){{
  const h=(location.hash||"#tpl").slice(1);
  selectTab(h);
}}

tabs.forEach(t=>{{
  const el=document.getElementById(t.btn);
  if(el) {{
    el.addEventListener("click",()=>selectTab(t.id));
    el.addEventListener("keydown",e=>{{ if(e.key==="Enter"||e.key===" "){{ e.preventDefault(); selectTab(t.id); }} }});
  }}
}});
window.addEventListener("hashchange",initFromHash);
initFromHash();

// --- Live Queue Fetcher ---
async function loadQueueView(){{
  const tbody = document.getElementById("queue-jobs-tbody");
  const statPending = document.getElementById("stat-pending-count");
  const statArchived = document.getElementById("stat-archived-count");
  if(!tbody) return;

  try {{
    const resStatus = await fetch("/queue/status");
    if(resStatus.ok){{
      const d = await resStatus.json();
      if(statPending) statPending.textContent = d.queue_count >= 0 ? d.queue_count : 0;
      if(statArchived) statArchived.textContent = d.archive_count >= 0 ? d.archive_count : 0;
    }}

    const resList = await fetch("/queue/list");
    if(!resList.ok) throw new Error(resList.status);
    const data = await resList.json();
    const jobs = data.jobs || [];

    if(jobs.length === 0){{
      tbody.innerHTML = '<tr><td colspan="4" style="padding:14px; text-align:center; color:var(--muted)">No jobs currently waiting in queue.</td></tr>';
      return;
    }}

    let rowsHtml = "";
    jobs.forEach(j => {{
      const jid = j.job_id || j._id.slice(-8);
      const st = j.status || "queued";
      const src = (j.meta && j.meta.source) ? j.meta.source : "api";
      const tm = j.created_at_readable || (j.ts ? new Date(j.ts * 1000).toLocaleTimeString() : "-");
      rowsHtml += `
        <tr style="border-bottom:1px solid var(--line);">
          <td style="padding:8px; font-family:monospace">${{jid}}</td>
          <td style="padding:8px"><span style="display:inline-block; padding:2px 8px; border-radius:999px; background:rgba(59,130,246,0.15); color:var(--accent); font-weight:600; font-size:0.8rem">${{st}}</span></td>
          <td style="padding:8px">${{src}}</td>
          <td style="padding:8px; color:var(--muted)">${{tm}}</td>
        </tr>
      `;
    }});
    tbody.innerHTML = rowsHtml;
  }} catch(e){{
    tbody.innerHTML = `<tr><td colspan="4" style="padding:12px; color:var(--err)">Failed to load queue: ${{e}}</td></tr>`;
  }}
}}

const btnRefreshQueue = document.getElementById("btn-refresh-queue");
if(btnRefreshQueue){{
  btnRefreshQueue.addEventListener("click", loadQueueView);
}}

// --- File Upload, Preview & Drag/Drop ---
document.addEventListener("DOMContentLoaded",()=>{{
  const input=document.getElementById("imgfile");
  const dropZone=document.getElementById("drop-zone");
  const fileChosen=document.getElementById("file-chosen");
  const previewBox=document.getElementById("img-preview-box");
  const previewImg=document.getElementById("img-preview");
  const statusPill=document.getElementById("status-pill");
  const statusText=statusPill ? statusPill.querySelector(".status-text") : null;
  
  function handleFileSelect(file) {{
    if(!file) return;
    fileChosen.textContent = file.name;
    if(dropZone) dropZone.textContent = "Selected: " + file.name;
    
    // Live preview
    if(file.type.startsWith("image/") && previewBox && previewImg) {{
      const reader = new FileReader();
      reader.onload = e => {{
        previewImg.src = e.target.result;
        previewBox.style.display = "block";
      }};
      reader.readAsDataURL(file);
    }}
  }}

  if(input){{
    input.addEventListener("change", function(){{
      if(this.files.length) handleFileSelect(this.files[0]);
    }});
  }}
  
  if(dropZone && input){{
    dropZone.addEventListener("click", ()=>input.click());
    ["dragenter","dragover"].forEach(ev=>dropZone.addEventListener(ev,e=>{{
      e.preventDefault(); dropZone.style.background="rgba(59,130,246,0.08)";
    }}));
    ["dragleave","drop"].forEach(ev=>dropZone.addEventListener(ev,e=>{{
      e.preventDefault(); dropZone.style.background="";
    }}));
    dropZone.addEventListener("drop",e=>{{
      e.preventDefault();
      if(e.dataTransfer.files.length){{
        input.files = e.dataTransfer.files;
        handleFileSelect(e.dataTransfer.files[0]);
      }}
    }});
  }}

  function applyLiveStatus(data){{
    if(!statusPill || !statusText || !data) return;
    statusPill.classList.remove("ok","err","busy");

    const st = data.state || "idle";
    const label = data.label || (data.online ? "Printer online" : "Printer offline");
    const hint = data.detail || "";

    if(st === "printing" || st === "sending" || st === "queued"){{
      statusPill.classList.add("busy");
    }} else if(st === "printed" || st === "idle"){{
      statusPill.classList.add(data.online ? "ok" : "err");
    }} else {{
      statusPill.classList.add("err");
    }}
    statusText.textContent = label;
    statusPill.title = hint;
  }}

  let sse = null;
  let pollTimer = null;

  async function fetchLiveStatus(){{
    if(document.hidden) return;
    try{{
      const res = await fetch("/live-status", {{cache:"no-store"}});
      if(res.ok){{
        const data = await res.json();
        applyLiveStatus(data);
      }}
    }}catch(e){{}}
  }}

  function connectSSE(){{
    if(sse) {{ sse.close(); sse = null; }}
    if(document.hidden) return;

    try{{
      sse = new EventSource("/events");
      sse.addEventListener("status", e => {{

        try{{
          const data = JSON.parse(e.data);
          applyLiveStatus(data);
        }}catch(err){{}}
      }});
      sse.onerror = () => {{
        if(sse) {{ sse.close(); sse = null; }}
        setTimeout(connectSSE, 10000);
      }};
    }}catch(e){{
      // Fallback
    }}
  }}

  document.addEventListener("visibilitychange", () => {{
    if(document.hidden){{
      if(sse) {{ sse.close(); sse = null; }}
      if(pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
    }} else {{
      fetchLiveStatus();
      connectSSE();
      if(!pollTimer) pollTimer = setInterval(fetchLiveStatus, 25000);
    }}
  }});

  fetchLiveStatus();
  connectSSE();
  pollTimer = setInterval(fetchLiveStatus, 25000);
}});

""".replace("{w}", str(PRINT_WIDTH_PX))
