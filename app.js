const $ = (q) => document.querySelector(q);
const $$ = (q) => [...document.querySelectorAll(q)];
let currentUser = null;
let csrfToken = "";
let cachedAdmin = null;
let latestDashboard = null;
let accountFilter = "all";
let accountLayout = "list";
let toastTimer;
const escapeHtml = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const currencyDigits={KES:2,USD:2,CDF:2,UGX:0,XOF:0,XAF:0,RWF:0,ZMW:2,SLE:2,USDT:6,BTC:8,ETH:8,BNB:8};
const fmtMoney = (n, currency = "USD") => `${currency} ${Number(n || 0).toLocaleString("en-US", {minimumFractionDigits:currencyDigits[currency]??2, maximumFractionDigits:currencyDigits[currency]??2})}`;
const fmtMulti = (m) => Object.entries(m||{}).filter(([,v])=>Number(v)!==0).map(([c,v])=>fmtMoney(v,c)).join(" · ")||"—";
const fmtDate = (v) => v ? new Date(v).toLocaleString(undefined, {dateStyle:"medium",timeStyle:"short"}) : "—";
const statusPill = s => `<span class="status-pill status-${escapeHtml((s || "pending").toLowerCase())}">${escapeHtml((s || "pending").replaceAll("_"," ").toUpperCase())}</span>`;

async function api(path, options={}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type","application/json");
  if (csrfToken && options.method && options.method !== "GET") headers.set("X-CSRF-Token",csrfToken);
  const res = await fetch(path,{...options,headers,credentials:"same-origin"});
  let data = {};
  try { data = await res.json(); } catch (_) { data = {}; }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
function toast(message, kind="") {
  const el=$("#toast"); el.textContent=message; el.className=`toast show ${kind}`;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.className="toast",3500);
}
function setMessage(el,msg,ok=false){el.textContent=msg||"";el.classList.toggle("success",!!ok)}
function showAuth(view){$("#login-view").classList.toggle("hidden",view!=="login");$("#register-view").classList.toggle("hidden",view!=="register");$("#app-view").classList.add("hidden")}
function setSignedIn(user,csrf){currentUser=user;csrfToken=csrf||"";$("#login-view").classList.add("hidden");$("#register-view").classList.add("hidden");$("#app-view").classList.remove("hidden");$("#user-name").textContent=user.name;$("#user-role").textContent=user.role==="admin"?"Operations administrator":"Investor account";$("#user-avatar").textContent=(user.name||"X").trim().slice(0,1).toUpperCase();$("#admin-nav").classList.toggle("hidden",user.role!=="admin");$("#admin-overview").classList.toggle("hidden",user.role!=="admin");$("#admin-queue-panel").classList.toggle("hidden",user.role!=="admin");$("#kyc-notice").classList.toggle("hidden",user.role==="admin"||user.kyc_status==="verified");refreshApp()}
async function boot(){
  try { const state=await api("/api/me"); if(state.user){setSignedIn(state.user,state.csrf_token);return;} }
  catch (_) {}
  showAuth("login");
}
async function refreshApp(){
  try {
    const data=await api("/api/dashboard");
    latestDashboard=data;
    $("#my-balance").textContent=fmtMulti(data.balance_by_currency);
    $("#portfolio-deposits").textContent=fmtMulti(data.total_deposits_by_currency);
    $("#portfolio-withdrawals").textContent=fmtMulti(data.total_withdrawals_by_currency);
    $("#portfolio-balance").textContent=fmtMulti(data.balance_by_currency);
    renderAccounts(data);
    renderIntegrations(data.integrations||[]);
    $("#kyc-notice").classList.toggle("hidden",currentUser.role==="admin"||data.kyc_status==="verified");
    renderActivity(data.recent || []);
    renderLedger(data.ledger || []);
    renderRequests(data.requests || []);
    if(currentUser.role==="admin") await refreshAdmin();
  } catch(e) { toast(e.message,"error"); }
}
function renderActivity(rows){
  const el=$("#recent-activity");
  if(!rows.length){el.innerHTML='<div class="activity-empty">No records yet. Submitted requests will appear here.</div>';return;}
  el.innerHTML=rows.slice(0,6).map(r=>`<div class="activity-row"><span class="activity-badge">${r.entry_type?.startsWith("withdrawal")?"↥":"↧"}</span><span class="activity-copy"><strong>${escapeHtml(r.description||r.entry_type||"Account event")}</strong><small>${fmtDate(r.created_at)} · ${escapeHtml(r.reference||"No external reference")}</small></span><span class="activity-amount">${r.amount!=null?fmtMoney(r.amount,r.currency):statusPill(r.status)}</span></div>`).join("");
}
function renderAccounts(data){
  const el=$("#account-list");if(!el)return;
  const provider=data.pool?.provider||{};
  $("#provider-summary").textContent=provider.connected?`${provider.name} provider connected`:`${provider.name||"MT5"} provider not connected`;
  $("#pool-member-id").textContent=`MEMBER · ${data.pool?.member_reference||"PENDING"}`;
  const isDemo=accountFilter==="demo";
  if(isDemo){el.innerHTML='<div class="account-empty"><span>◷</span><strong>No demo provider account connected</strong><p>No sample account, balance, or trades are shown here. Demo accounts will appear after an actual provider sandbox is connected.</p></div>';return}
  const balance=fmtMulti(data.balance_by_currency);const shownBalance=balance==="—"?"KES 0.00":balance;
  el.classList.toggle("account-grid-layout",accountLayout==="grid");
  el.innerHTML=`<article class="account-card"><div class="account-card-head"><div class="account-title"><span class="account-mode-tag">REAL · POOL</span><div><strong>Xdigitex Managed Pool</strong><small>Member reference ${escapeHtml(data.pool?.member_reference||"pending")}</small></div></div><div class="account-provider-state"><span class="status-dot ${provider.connected?"status-dot-green":"status-dot-amber"}"></span>${provider.connected?"Provider connected":"Provider pending"}</div></div><div class="account-card-body"><div class="account-balance"><span>Recorded member ledger</span><strong>${escapeHtml(shownBalance)}</strong><small>Customer funds confirmed by Xdigitex Pay. Broker equity is reported separately.</small></div><div class="account-owner"><span>ACCOUNT HOLDER</span><strong>${escapeHtml(currentUser?.name||"")}</strong><small>${escapeHtml(provider.name||"MT5")} · organization-owned pool connection</small></div></div><div class="account-card-actions"><button class="button button-primary" data-action="deposit">↧ <span>Deposit</span></button><button class="button button-secondary" data-action="withdraw">↥ <span>Withdraw</span></button><button class="button button-secondary transfer-disabled" type="button" disabled title="Transfers are unavailable until the provider and pool allocation ledgers are connected">⇄ <span>Transfer</span></button><button class="icon-button account-more" type="button" data-action="pool-details" aria-label="Pool account details">⋮</button></div><div class="account-card-foot"><span>Trading status</span><strong>${provider.connected?"Awaiting first provider sync":"Not connected"}</strong><span>Lots and closed-trade stats remain unavailable until provider data arrives.</span></div></article>`;
}
function renderLedger(rows){const el=$("#ledger-table");if(!rows.length){el.innerHTML='<div class="activity-empty">No confirmed ledger entries. Pending requests are not included.</div>';return;}el.innerHTML=`<table class="data-table"><thead><tr><th>Date</th><th>Entry</th><th>Reference</th><th>Amount</th><th>Balance after</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${fmtDate(r.created_at)}</td><td>${escapeHtml(r.description)}</td><td>${escapeHtml(r.reference||"—")}</td><td>${fmtMoney(r.amount,r.currency)}</td><td>${fmtMoney(r.balance_after,r.currency)}</td></tr>`).join("")}</tbody></table>`}
function renderRequests(rows){const el=$("#funding-requests-table");const activity=$("#activity-table");const html=rows.length?`<table class="data-table"><thead><tr><th>Created</th><th>Type</th><th>Amount</th><th>Destination / Method</th><th>Reference</th><th>Status</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${fmtDate(r.created_at)}</td><td>${escapeHtml(r.kind)}</td><td>${fmtMoney(r.amount,r.currency)}</td><td>${escapeHtml(r.destination||r.method||"—")}</td><td>${escapeHtml(r.reference||"—")}</td><td>${statusPill(r.status)} ${r.status==="pending"&&r.provider_reference?`<button class="text-button" data-status="${escapeHtml(r.id)}">Check</button>`:""}</td></tr>`).join("")}</tbody></table>`:'<div class="activity-empty">No deposit or withdrawal requests yet.</div>';el.innerHTML=html;activity.innerHTML=html}
async function refreshAdmin(){
  const d=await api("/api/admin/overview");cachedAdmin=d;
  $("#admin-users").textContent=d.metrics.users;
  $("#pool-contributions").textContent=fmtMulti(d.metrics.confirmed_contributions_by_currency);
  $("#admin-pending-withdrawals").textContent=String(d.metrics.pending_withdrawal_count);
  $("#queue-summary").innerHTML=`<div class="queue-tile"><span>Deposits to verify</span><strong>${d.metrics.pending_deposit_count}</strong></div><div class="queue-tile"><span>Withdrawals to review</span><strong>${d.metrics.pending_withdrawal_count}</strong></div><div class="queue-tile"><span>Identity checks</span><strong>${d.metrics.pending_kyc_count}</strong></div>`;
  renderInvestors(d.users||[]);renderAdminRequests(d.requests||[]);renderAudit(d.audit||[]);renderIntegrations(d.integrations||[]);
}
function renderInvestors(users){const el=$("#investors-table");if(!users.length){el.innerHTML='<div class="activity-empty">No registered users.</div>';return;}el.innerHTML=`<table class="data-table"><thead><tr><th>Name</th><th>Email</th><th>Registered</th><th>Role</th><th>Identity status</th><th>Action</th></tr></thead><tbody>${users.map(u=>`<tr><td><strong>${escapeHtml(u.name)}</strong></td><td>${escapeHtml(u.email)}</td><td>${fmtDate(u.created_at)}</td><td>${escapeHtml(u.role)}</td><td>${statusPill(u.kyc_status)}</td><td>${u.role==="investor"&&u.kyc_status!=="verified"?`<button class="button button-secondary button-small" data-kyc="${escapeHtml(u.id)}">Review</button>`:"—"}</td></tr>`).join("")}</tbody></table>`}
function renderAdminRequests(rows){const dep=rows.filter(r=>r.kind==="deposit"&&r.status==="pending");const wd=rows.filter(r=>r.kind==="withdrawal"&&r.status==="pending");$("#admin-deposits-table").innerHTML=renderAdminRequestTable(dep,"deposit");$("#admin-withdrawals-table").innerHTML=renderAdminRequestTable(wd,"withdrawal")}
function renderAdminRequestTable(rows,kind){if(!rows.length)return '<div class="activity-empty">No pending '+escapeHtml(kind)+' requests.</div>';return `<table class="data-table"><thead><tr><th>Customer</th><th>Created</th><th>Amount</th><th>${kind==="deposit"?"Gateway / provider reference":"Xdigitex Pay destination"}</th><th>Action</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${escapeHtml(r.user_name)}</strong><br><span class="muted">${escapeHtml(r.user_email)}</span></td><td>${fmtDate(r.created_at)}</td><td>${fmtMoney(r.amount,r.currency)}</td><td>${escapeHtml(`${r.method||"—"} · ${r.provider_reference||r.destination||"awaiting provider reference"} · ${r.provider_status||"pending"}`)}</td><td><button class="button button-secondary button-small" data-refresh="${kind}:${escapeHtml(r.id)}">Refresh</button></td></tr>`).join("")}</tbody></table>`}
function renderAudit(rows){const el=$("#audit-table");if(!rows.length){el.innerHTML='<div class="activity-empty">No audit events recorded yet.</div>';return;}el.innerHTML=`<table class="data-table"><thead><tr><th>Time</th><th>Actor</th><th>Event</th><th>Target</th><th>Details</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${fmtDate(r.created_at)}</td><td>${escapeHtml(r.actor_email||"System")}</td><td>${escapeHtml(r.action)}</td><td>${escapeHtml(r.target_type||"—")} ${escapeHtml(r.target_id||"")}</td><td>${escapeHtml(r.summary||"—")}</td></tr>`).join("")}</tbody></table>`}
function renderIntegrations(rows){const html=rows.map(x=>`<div><span class="readiness-icon">${x.name==="broker"?"⌁":x.name==="xdigitex_pay"?"XP":"◉"}</span><span><strong>${escapeHtml(x.label)}</strong><small>${escapeHtml(x.detail)}</small></span><span class="status-text ${x.connected?"amber-text":"red-text"}">${x.connected?"CONFIGURED":"NOT CONFIGURED"}</span></div>`).join("");$("#integration-checklist").innerHTML=html;$("#overview-integrations").innerHTML=html}
function showPage(page){$$('.page-section').forEach(s=>s.classList.add("hidden"));const target=$("#page-"+page);if(target)target.classList.remove("hidden");$$('.nav-item').forEach(b=>b.classList.toggle("active",b.dataset.page===page));const selected=$(`.nav-item[data-page="${page}"]`);$("#page-title").textContent=selected?selected.textContent.trim():page;$("#sidebar").classList.remove("open");if(page==="requests"||page==="investors"||page==="audit")refreshAdmin().catch(e=>toast(e.message,"error"));}
function openRequest(kind){const dlg=$("#request-dialog");const dep=kind==="deposit";$("#request-title").textContent=dep?"Deposit with Xdigitex Pay":"Withdraw through Xdigitex Pay";$("#request-eyebrow").textContent=dep?"Xdigitex Pay deposit":"Xdigitex Pay withdrawal";$("#deposit-fields").classList.toggle("hidden",!dep);$("#deposit-fields").disabled=!dep;$("#withdraw-fields").classList.toggle("hidden",dep);$("#withdraw-fields").disabled=dep;$("#request-message").textContent="";dlg.dataset.kind=kind;$("#sidebar").classList.remove("open");const gateway=$("[name='gateway']");const phone=$("[name='deposit_phone']");const setPhone=()=>{phone.required=["mobile","safaricom","airtel"].includes(gateway.value)};gateway.onchange=setPhone;setPhone();const currency=dep?$("[name=deposit_currency]"):$("[name=withdraw_currency]");const amount=dep?$("[name=deposit_amount]"):$("[name=withdraw_amount]");const setStep=()=>{const d=currencyDigits[currency.value]??2;amount.step=d?`0.${"0".repeat(d-1)}1`:"1"};currency.onchange=setStep;setStep();dlg.showModal()}
async function submitRequest(e){e.preventDefault();const form=e.currentTarget;const kind=$("#request-dialog").dataset.kind;const fd=new FormData(form);const body={};try{let d;if(kind==="deposit"){body.amount=Number(fd.get("deposit_amount"));body.currency=fd.get("deposit_currency");body.gateway=fd.get("gateway");body.phone=fd.get("deposit_phone")||"";d=await api("/api/requests/deposit",{method:"POST",body:JSON.stringify(body)});const checkout=d.redirect_url||d.checkout_url||d.qrcode_link;if(checkout){window.location.assign(checkout);return;}}else{body.amount=Number(fd.get("withdraw_amount"));body.currency=fd.get("withdraw_currency");body.phone=fd.get("withdraw_phone");d=await api("/api/requests/withdrawal",{method:"POST",body:JSON.stringify(body)});}$("#request-dialog").close();form.reset();toast(d.message||"Request submitted to Xdigitex Pay.","success");refreshApp();}catch(err){setMessage($("#request-message"),err.message)}}
async function refreshProvider(kind,id){try{await api(`/api/admin/requests/${kind}/${id}/refresh`,{method:"POST",body:"{}"});toast("Xdigitex Pay status refreshed.","success");await refreshApp()}catch(e){toast(e.message,"error")}}
async function refreshOwnRequest(id){try{const d=await api(`/api/requests/${id}/status`);toast(`Provider status: ${d.provider_status||d.status}`,"success");await refreshApp()}catch(e){toast(e.message,"error")}}
function openPoolDetails(){const d=$("#pool-details-dialog");if(d) d.showModal()}
async function reviewKyc(id){const u=(cachedAdmin?.users||[]).find(x=>x.id===id);if(!u)return;const choice=confirm(`Mark identity review as verified for ${u.name} (${u.email})? Only do this after completing checks outside this app.`);if(!choice)return;try{await api(`/api/admin/users/${id}/kyc`,{method:"POST",body:JSON.stringify({status:"verified"})});toast("Identity status updated.","success");refreshApp()}catch(e){toast(e.message,"error")}}

$("#login-form").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);try{const d=await api("/api/auth/login",{method:"POST",body:JSON.stringify({email:fd.get("email"),password:fd.get("password")})});setMessage($("#login-message"),"");setSignedIn(d.user,d.csrf_token)}catch(err){setMessage($("#login-message"),err.message)}});
$("#register-form").addEventListener("submit",async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);try{const d=await api("/api/auth/register",{method:"POST",body:JSON.stringify({name:fd.get("name"),email:fd.get("email"),password:fd.get("password"),accepted_disclosures:fd.get("terms")==="on"})});setMessage($("#register-message"),"");setSignedIn(d.user,d.csrf_token);toast("Account created. Identity review is pending.","success")}catch(err){setMessage($("#register-message"),err.message)}});
$("#show-register").addEventListener("click",()=>showAuth("register"));$("#show-login").addEventListener("click",()=>showAuth("login"));
$("#logout-button").addEventListener("click",async()=>{try{await api("/api/auth/logout",{method:"POST",body:"{}"})}catch(_){}currentUser=null;csrfToken="";showAuth("login")});
$("#request-form").addEventListener("submit",submitRequest);
document.addEventListener("click",e=>{const nav=e.target.closest("[data-page]");if(nav)showPage(nav.dataset.page);const action=e.target.closest("[data-action]");if(action){if(action.dataset.action==="pool-details")openPoolDetails();else openRequest(action.dataset.action)}const goto=e.target.closest("[data-goto]");if(goto)showPage(goto.dataset.goto);const close=e.target.closest("[data-close-dialog]");if(close)$("#request-dialog").close();const closePool=e.target.closest("[data-close-pool]");if(closePool)$("#pool-details-dialog").close();const refresh=e.target.closest("[data-refresh]");if(refresh){const[k,id]=refresh.dataset.refresh.split(":");refreshProvider(k,id)}const check=e.target.closest("[data-status]");if(check)refreshOwnRequest(check.dataset.status);const filter=e.target.closest("[data-account-filter]");if(filter){accountFilter=filter.dataset.accountFilter;$$("[data-account-filter]").forEach(b=>b.classList.toggle("active",b===filter));renderAccounts(latestDashboard||{})}const layout=e.target.closest("[data-account-layout]");if(layout){accountLayout=layout.dataset.accountLayout;$$("[data-account-layout]").forEach(b=>b.classList.toggle("active",b===layout));renderAccounts(latestDashboard||{})}const kyc=e.target.closest("[data-kyc]");if(kyc)reviewKyc(kyc.dataset.kyc)});
$("#menu-toggle").addEventListener("click",()=>$("#sidebar").classList.toggle("open"));
boot();
