const API_BASE = location.protocol === "file:" ? "http://127.0.0.1:8765" : "";
const $ = id => document.getElementById(id);
const safeStartDialog = $("safeStartDialog");
const confirmDialog = $("confirmDialog");
let lastChecks = null;
let lastEventKey = "";

function nowTime(){ return new Date().toLocaleTimeString([], {hour12:false}); }
setInterval(()=> $("clock").textContent = nowTime(), 500);
$("clock").textContent = nowTime();

function money(n, signed=true){
  if(n==null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const abs = Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  if(!signed) return "$"+abs;
  if(v>0) return "+$"+abs;
  if(v<0) return "-$"+abs;
  return "$"+abs;
}

function setDot(id, kind){
  const el=$(id); if(!el) return;
  el.className = "indicator " + (kind||"");
}

function ageLabel(sec){
  if(sec==null || Number.isNaN(Number(sec))) return "—";
  const n = Number(sec);
  if(n < 10) return n.toFixed(1)+"s ago";
  if(n < 120) return Math.round(n)+"s ago";
  if(n < 7200) return Math.round(n/60)+"m ago";
  return (n/3600).toFixed(1)+"h ago";
}

function ageFromTs(ts){
  if(!ts) return null;
  const t = Date.parse(ts);
  if(Number.isNaN(t)) return null;
  return Math.max(0, (Date.now()-t)/1000);
}

function setOps(cellId, text, kind){
  const cell=$(cellId);
  if(cell) cell.className = "ops-cell" + (kind ? " "+kind : "");
}

function genuineLiveDvp(s){
  const d = s.decision||{};
  const liveSig = (d.last_live_signal) || (s.live_dvp && s.live_dvp.live_signal) || {};
  const source = liveSig.source || d.signal_source || "";
  const kind = d.signal && d.signal.kind;
  const dir = liveSig.direction || (kind==="LIVE" && d.signal.direction);
  const ok = !!(dir && (source==="phase54_live" || (d.signal_source==="LIVE" && kind==="LIVE")));
  return {ok, dir: ok?dir:null, sig: liveSig, source: ok ? (liveSig.source || "phase54_live") : null};
}

async function api(path, method="GET", body){
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {"Content-Type":"application/json"},
    body: method==="GET" ? undefined : JSON.stringify(body||{})
  });
  const data = await res.json().catch(()=>({}));
  if(!res.ok){
    const msg = data.detail && data.detail.message ? data.detail.message : (typeof data.detail==="string"?data.detail:`HTTP ${res.status}`);
    throw Object.assign(new Error(msg), {payload:data, status:res.status});
  }
  return data;
}

function renderChart(series){
  const points = (series && series.points && series.points.length) ? series.points : [0];
  const labels = (series && series.labels && series.labels.length) ? series.labels : ["now"];
  const width=960,height=300,padX=10,padTop=18,padBottom=20;
  const min=Math.min(...points), max=Math.max(...points);
  const xStep = points.length>1 ? (width-padX*2)/(points.length-1) : 0;
  const coords=points.map((value,i)=>{
    const norm = (max-min)===0 ? 0.5 : (value-min)/(max-min);
    return [padX+i*xStep, height-padBottom-norm*(height-padTop-padBottom)];
  });
  const line=coords.map((c,i)=>`${i===0?"M":"L"} ${c[0].toFixed(2)} ${c[1].toFixed(2)}`).join(" ");
  const area=`${line} L ${coords.at(-1)[0].toFixed(2)} ${height-padBottom} L ${coords[0][0].toFixed(2)} ${height-padBottom} Z`;
  $("equityPath").setAttribute("d", line);
  $("equityPathGlow").setAttribute("d", line);
  $("equityArea").setAttribute("d", area);
  const last=coords.at(-1);
  $("equityDot").setAttribute("cx", last[0]);
  $("equityDot").setAttribute("cy", last[1]);
  $("chartNet").textContent = money(series && series.net);
  $("chartHigh").textContent = money(series && series.high);
  $("chartLow").textContent = money(series && series.low);
  $("chartXAxis").innerHTML = labels.map(x=>`<span>${x}</span>`).join("");
  const top = Math.max(max, 0);
  const bot = Math.min(min, 0);
  document.querySelector(".chart-overlay.top").textContent = money(top, false);
  document.querySelector(".chart-overlay.mid").textContent = money((top+bot)/2, false);
  document.querySelector(".chart-overlay.bottom").textContent = money(bot, false);
}

function renderChecks(display){
  const order = [
    ["fresh_market_data","Fresh market data"],
    ["fundednext_authenticated","FundedNext authenticated"],
    ["correct_account_id","Correct account ID"],
    ["equity_mll_available","Equity / MLL available"],
    ["broker_positions_reconciled","Broker positions reconciled"],
    ["prop_rules_loaded","Prop rules loaded"],
    ["frozen_nq_hash_verified","Frozen NQ hash verified"],
    ["no_stale_orders","No stale orders"],
    ["risk_limits_valid","Risk limits valid"],
    ["news_gate_valid","News gate valid"],
    ["execution_permission_checked","Execution permission checked"]
  ];
  $("safeChecks").innerHTML = order.map(([k,name])=>{
    const status = (display&&display[k]) || "—";
    const cls = status==="DISABLED" ? "blocked-text" : (status==="PASS"?"pass":"blocked-text");
    return `<div class="check"><span>${name}</span><span class="${cls}">${status}</span></div>`;
  }).join("");
  $("machineChecks").innerHTML = order.map(([k,name])=>{
    const status = (display&&display[k]) || "—";
    const fail = status==="FAIL";
    const perm = k==="execution_permission_checked";
    return `<div class="${perm?"permission-check":""} ${fail?"fail":""}"><i></i><span>${name}</span><strong>${status}</strong></div>`;
  }).join("");
  const passN = order.filter(([k])=> (display&&display[k])==="PASS" || k==="execution_permission_checked").length;
  $("safeCount").textContent = `${passN} / ${order.length}`;
}

function renderLogs(events){
  if(!events || !events.length){ return; }
  const key = events.map(e=>e.ts+"|"+e.message).join("|");
  if(key===lastEventKey) return;
  lastEventKey = key;
  $("logs").innerHTML = events.slice(0,30).map(e=>{
    const t = (e.ts||"").substring(11,19) || nowTime();
    const level = e.level||"INFO";
    const cls = level==="WARN"?"warn":level==="BLOCK"?"block":"";
    return `<div><time>${t}</time><span class="log-level ${cls}">${level}</span><p>${e.message||""}</p></div>`;
  }).join("");
}

function applySnapshot(s){
  const running = s.engine==="RUNNING";
  const pc = s.prop_canary || {};
  $("engineStatus").textContent = running ? "RUNNING" : "STOPPED";
  $("engineStatusSide").textContent = running ? "RUNNING" : "STOPPED";
  setDot("engineDot", running?"healthy":"blocked");
  const hd = $("engineHealthDot");
  if(hd) hd.className = "health-dot " + (running ? "running" : "stopped");
  const hd = $("engineHealthDot");
  if(hd) hd.className = "health-dot " + (running ? "running" : "stopped");
  const u = s.unattended || {};
  const uState = u.state || "UNATTENDED_DISABLED";
  const uLabel = uState.replace("UNATTENDED_","").replaceAll("_"," ");
  if($("unattState")) $("unattState").textContent = uLabel;
  if($("unattHint")) $("unattHint").textContent = u.enabled ? "operator-enabled for today" : "1 account · 1 MNQ · 1 day latch";
  if($("unattAcct")) $("unattAcct").textContent = u.account || "FNFTCHTANATSWAPHILMU92044";
  if($("unattLatch")) $("unattLatch").textContent = u.daily_attempt_used ? "USED · LOCKED" : "UNUSED";
  if($("unattMkt")) $("unattMkt").textContent = u.market_live ? "LIVE" : (mdLabel || "STALE");
  if($("unatt55b")) $("unatt55b").textContent = u.automated_phase_55b ? "AUTOMATED_PHASE_55B_PASS" : "WAITING";
  if($("unattPos")) $("unattPos").textContent = (u.position || "FLAT");
  if($("unattRecon")) $("unattRecon").textContent = u.recon || "—";
  if($("unattMll")) $("unattMll").textContent = (u.mll==null?"—":u.mll) + " / " + (u.remaining_dd==null?"—":u.remaining_dd);
  if($("unattProt")) $("unattProt").textContent = (u.stop_confirmed?"STOP OK":"STOP —") + " · " + (u.target_confirmed?"TGT OK":"TGT —");
  if($("unattWd")) $("unattWd").textContent = u.watchdog || "—";
  if($("unattAlert")){
    const n = s.notifications || {};
    $("unattAlert").textContent = n.delivery_status || (n.configured ? "READY" : "NOT CONFIGURED");
  }
  if($("unattTs")) $("unattTs").textContent = u.last_change || "—";
  if($("unattProp")) $("unattProp").textContent = "LOCKED";
  const strip = $("unattendedStrip");
  if(strip){
    strip.classList.toggle("blocked", uState.indexOf("BLOCKED")>=0);
    strip.classList.toggle("open", uState.indexOf("POSITION")>=0 || uState.indexOf("ENTRY")>=0);
    strip.classList.toggle("ready", uState.indexOf("WAITING")>=0 || uState.indexOf("COMPLETE")>=0);
  }
  $("startBtn").textContent = "Run safe start";
  const mdObj = (s.market_data && typeof s.market_data === "object") ? s.market_data : {};
  const mdStatus = s.market_data_status || mdObj.freshness || s.market_data_freshness || (typeof s.market_data === "string" ? s.market_data : "");
  const mdQuality = s.market_data_quality || mdObj.quality || "";
  const mdLive = mdStatus==="LIVE" && (mdQuality==="LIVE" || !mdQuality) && mdQuality!=="SIMULATED" && mdQuality!=="DELAYED" && mdQuality!=="PLAYBACK";
  let mdLabel = mdStatus || "UNAVAILABLE";
  if (mdStatus==="CONNECTED_STALE" || mdStatus==="STALE") mdLabel = "STALE";
  else if (mdStatus==="DISCONNECTED") mdLabel = "DISCONNECTED";
  else if (mdStatus==="LIVE") mdLabel = mdLive ? "LIVE" : ("LIVE · "+(mdQuality||"CHECK QUALITY"));
  else if (mdStatus==="DELAYED" || mdQuality==="DELAYED") mdLabel = "DELAYED";
  else if (mdStatus==="PLAYBACK" || mdQuality==="PLAYBACK") mdLabel = "PLAYBACK";
  else if (mdStatus==="SIMULATED" || mdQuality==="SIMULATED") mdLabel = "SIMULATED";
  $("mdStatus").textContent = mdLabel + (mdQuality && mdQuality!==mdStatus ? (" · "+mdQuality) : "");
  setDot("mdDot", mdLive?"healthy":((mdStatus==="STALE"||mdStatus==="CONNECTED_STALE"||mdStatus==="DELAYED"||mdStatus==="SIMULATED"||mdStatus==="PLAYBACK")?"amber":"blocked"));
  const fnConnected = s.fundednext_connection==="CONNECTED" || (s.fundednext && s.fundednext.connected);
  const fnRo = s.fundednext_permission==="READ_ONLY" || (s.fundednext && s.fundednext.permission==="READ_ONLY");
  $("fnStatus").textContent = fnConnected
    ? ("CONNECTED · GENERAL PROP LOCKED · CANARY " + (pc.state || pc.label || "LOCKED"))
    : ((s.fundednext_connection==="DISCONNECTED" ? "DISCONNECTED" : (s.fundednext_connection||"UNAVAILABLE"))
        + " · GENERAL PROP LOCKED");
  setDot("fnDot", fnConnected && fnRo ? "amber" : (s.fundednext_connection==="DISCONNECTED"?"blocked":"amber"));
  const canaryState = pc.state || pc.label || "PROP_LOCKED";
  if($("canaryBar")) $("canaryBar").textContent = canaryState.replace("PROP_CANARY_","").replace("PROP_","");
  if($("canaryDot")) setDot("canaryDot", canaryState.indexOf("ARMED")>=0 || canaryState.indexOf("IN_FLIGHT")>=0 ? "amber" : (canaryState.indexOf("BLOCKED")>=0 ? "blocked" : (canaryState.indexOf("READY")>=0 ? "healthy" : "blocked")));
  if($("canarySide")) $("canarySide").textContent = canaryState;
  $("policyStatus").textContent = s.policy_engine==="ACTIVE"?"Active":"Degraded";
  setDot("policyDot", s.policy_engine==="ACTIVE"?"healthy":"amber");
  const propOff = s.PROP_EXECUTION===false || s.prop_execution===false;
  if($("propExecBar")) $("propExecBar").textContent = propOff ? "LOCKED" : "UNLOCKED";


  $("pauseBtn").textContent = s.entries_paused ? "Resume new entries" : "Pause new entries";
  const mode = (s.mode||"DRY_RUN").replace("_"," ");
  const sel = $("modeSelect");
  [...sel.options].forEach(o=>{ if(o.value.replace(" ","_")=== (s.mode||"DRY_RUN")) sel.value=o.value; });

  const r = s.risk||{};
  const a = s.account||{};
  const p = s.position||{};
  $("todayPnl").textContent = money(r.today_pnl);
  $("todayPnl").className = (r.today_pnl||0)>=0?"positive":"";
  $("currentDd").textContent = money(-(r.current_dd||0));
  $("remainingBuf").textContent = a.remaining_dd==null ? "UNAVAILABLE" : money(a.remaining_dd, false);
  $("remainingBufHint").textContent = `${(r.buffer_remaining_pct||0).toFixed(1)}% remaining`;
  $("openRisk").textContent = money(r.open_risk, false);
  $("openRiskHint").textContent = (p.quantity? `${p.quantity} active` : "no position");
  $("policyLane").textContent = r.lane||"—";
  $("policyLaneHint").textContent = r.permitted_label||"";
  document.querySelectorAll("#policyScale > div").forEach(el=>{
    el.classList.toggle("active", el.dataset.lane===(r.lane||""));
  });
  $("bufferUsed").textContent = `${(r.buffer_used_pct||0).toFixed(1)}%`;
  $("bufferBar").style.width = `${Math.min(100, Math.max(0, r.buffer_used_pct||0))}%`;

  $("reconBadge").textContent = `Broker position reconciled · ${p.reconciled?"YES":"NO"}`;
  $("reconBadge").classList.toggle("warn", !p.reconciled);
  $("reconBanner").classList.toggle("show", !p.reconciled);
  $("reconBanner").textContent = p.note || "Broker position and engine state disagree.";
  $("posInstrument").textContent = p.instrument||"MNQ";
  $("posSide").textContent = p.side||"FLAT";
  $("posQty").textContent = p.quantity??0;
  $("posEntry").textContent = p.entry==null?"—":Number(p.entry).toLocaleString();
  $("posLast").textContent = p.last==null?"—":Number(p.last).toLocaleString();
  $("posUnreal").textContent = money(p.unrealized);
  $("posStop").textContent = p.stop==null?"—":Number(p.stop).toLocaleString();
  $("posRisk").textContent = money(p.open_risk, false);

  const d = s.decision||{};
  const live = genuineLiveDvp(s);
  if($("lastLiveSignal")){
    $("lastLiveSignal").textContent = live.ok ? ("LIVE DVP · " + live.dir) : "NO LIVE DVP";
    $("lastLiveSignalDetail").textContent = live.ok
      ? ((live.source || "phase54_live") + " · " + (live.sig.bar_identity || live.sig.ts || ""))
      : "no phase54_live event";
  }
  const liveCard = $("liveSignalCard");
  if(liveCard){
    liveCard.className = "decision-card live-dvp-card " + (live.ok ? "live" : "empty");
  }
  if($("lastSignal")){
    $("lastSignal").textContent = live.ok ? ("LIVE DVP · " + live.dir) : "NO LIVE DVP";
    $("lastSignalDetail").textContent = live.ok ? (live.source || "phase54_live") : "";
  }
  $("policyDecision").textContent = d.policy&&d.policy.label || "—";
  $("policyDecisionDetail").textContent = d.policy&&d.policy.detail || "";
  $("policyCard").classList.toggle("approved", d.policy&&d.policy.verdict==="ALLOW");
  $("execDecision").textContent = (d.execution && d.execution.label) || (pc.state && pc.state.indexOf("ARMED")>=0 ? pc.state : (s.execution_arm || "DISARMED"));
  $("execDetail").textContent = (d.execution && d.execution.detail) || ("GENERAL PROP LOCKED · CANARY " + (pc.state || "LOCKED"));

  $("heartbeat").textContent = d.heartbeat&&d.heartbeat.label || "—";
  $("heartbeatDetail").textContent = d.heartbeat&&d.heartbeat.detail || "";
  if($("signalSource")) $("signalSource").textContent = live.ok ? (live.source || "phase54_live") : (d.signal_source || "NONE");
  if($("liveStrategyStatus")){
    const st = s.live_strategy_status || (s.live_dvp && s.live_dvp.strategy_status) || "—";
    const td0 = s.telemetry_dump || {};
    const nqWaiting = (td0.nq_bars_1m_status==="WAITING" || td0.nq_bars_1m_count===0) && !td0.last_nq_bar_ts;
    $("liveStrategyStatus").textContent = (st==="WARMING_UP" && nqWaiting) ? "WARMING_UP · NO 1m BARS" : st;
  }
  if($("sim101Recovery")) $("sim101Recovery").textContent = s.sim101_recovery || "—";
  const shadow = d.last_shadow_signal || s.last_shadow_signal || {};
  if($("lastShadowSignal")){
    $("lastShadowSignal").textContent = shadow.direction
      ? ("SHADOW · " + shadow.direction + " · " + (shadow.ts || shadow.source || "history"))
      : "none";
  }

  const sim = s.sim101 || {};
  const simPresent = !!sim.present;
  const simFlat = sim.flat===true || (String(sim.side||"").toUpperCase()==="FLAT" && Number(sim.quantity||0)===0);
  if($("sim101Badge")) $("sim101Badge").textContent = simPresent ? (sim.known===false ? "PRESENT · UNKNOWN" : "CONNECTED") : "MISSING";
  if($("sim101Side")) $("sim101Side").textContent = sim.side || (simFlat ? "FLAT" : "—");
  if($("sim101Account")) $("sim101Account").textContent = sim.account || sim.name || (simPresent ? "Sim101" : "MISSING");
  if($("sim101Qty")) $("sim101Qty").textContent = sim.quantity==null ? "—" : String(sim.quantity);
  if($("sim101RecoveryCard")) $("sim101RecoveryCard").textContent = s.sim101_recovery || "—";
  if($("sim101Arm")) $("sim101Arm").textContent = s.execution_arm || "DISARMED";
  if($("sim101Source")) $("sim101Source").textContent = sim.source || "—";
  if($("sim101Known")) $("sim101Known").textContent = sim.known===true ? "YES" : (sim.known===false ? "NO" : "—");

  if(s.hashes){
    $("nqHash").textContent = (s.hashes.nq||"").slice(0,8)+"…";
    $("gcHash").textContent = (s.hashes.gc||"").slice(0,8)+"…";
  }
  $("fnConn").textContent = (s.connection && s.connection.account_id)
    ? ((s.connection.authenticated ? "Connected · " : "Detected · ") + s.connection.account_id)
    : (s.connection && s.connection.authenticated ? "Authenticated" : "Not detected");
  if($("fnEnv")){
    const env = (s.account_environment || (s.market_data && s.market_data.account_environment) || "").toUpperCase();
    $("fnEnv").textContent = env === "SIMULATION" ? "Simulation" : (env || "—");
  }
  $("fnPerm").textContent = "Read-only";
  if($("fnGeneralProp")) $("fnGeneralProp").textContent = "LOCKED";
  if($("fnCanary")) $("fnCanary").textContent = pc.state || pc.label || "PROP_LOCKED";
  $("fnEquity").textContent = (a.equity==null ? "UNAVAILABLE" : money(a.equity,false))
    + " / "
    + (a.mll==null ? "UNAVAILABLE" : money(a.mll,false));
  if($("fnMll")) $("fnMll").textContent = (a.mll==null ? "UNAVAILABLE" : money(a.mll,false))
    + " / "
    + (a.remaining_dd==null ? "UNAVAILABLE" : money(a.remaining_dd,false));
  if($("fnPos")) $("fnPos").textContent = (p.side || "FLAT") + " · qty " + (p.quantity==null ? "—" : p.quantity);
  if($("fnWorking")) $("fnWorking").textContent = (s.checks && s.checks.checks && s.checks.checks.no_stale_orders===false) ? "WORKING" : "NONE";
  $("fnRecon").textContent = (pc.recon || (p.reconciled ? "Matched" : "Mismatch"));
  if($("fnSource")) $("fnSource").textContent = (s.fundednext && s.fundednext.source) || a.equity_source || "—";
  if($("fnBalance")) $("fnBalance").textContent = a.balance==null ? "UNAVAILABLE" : money(a.balance,false);
  if($("fnProfit")) $("fnProfit").textContent = a.realized_pnl==null ? "UNAVAILABLE" : money(a.realized_pnl);
  if($("fnStatusAcct")) $("fnStatusAcct").textContent = a.account_status || "—";
  if($("fnBreached")) $("fnBreached").textContent = a.breached===true ? "YES" : (a.breached===false ? "NO" : "—");

  const h = s.health||{};
  $("statWr").textContent = h.wr==null ? "—" : `${(h.wr*100).toFixed(1)}%`;
  $("statRealized").textContent = money(a.realized_pnl);
  $("statUnreal").textContent = money(p.unrealized);
  $("statMaxDd").textContent = money(-(r.current_dd||0));

  if(s.checks&&s.checks.display){
    lastChecks = s.checks;
    renderChecks(s.checks.display);
    $("safeSummary").textContent = (s.checks.safe_start_result || (s.checks.ok_to_run_engine ? "ENGINE_MAY_RUN" : "SAFE_START_FAILED"))
      + " · ORDERS MAY NOT";
  }

  const dump = s.telemetry_dump || {};
  const dumpTs = dump.timestamp || (s.market && s.market.snapshot_timestamp);
  const dumpAge = dump.age_sec!=null ? Number(dump.age_sec)
    : (s.market && s.market.addon_heartbeat_age_sec!=null ? Number(s.market.addon_heartbeat_age_sec) : ageFromTs(dumpTs));
  const ntAlive = dump.alive===true || (s.market && s.market.addon_heartbeat_alive===true) || (dumpAge!=null && dumpAge<=5);
  setOps("opsDeskCell", "HEALTHY", "ok");
  if($("opsDesk")) $("opsDesk").textContent = "HEALTHY";
  if($("opsDeskHint")) $("opsDeskHint").textContent = "snapshot loaded";
  setOps("opsNtCell", ntAlive ? "CONNECTED" : "STALE", ntAlive ? "ok" : "bad");
  if($("opsNt")) $("opsNt").textContent = ntAlive ? "CONNECTED" : "STALE";
  if($("opsNtHint")) $("opsNtHint").textContent = dumpAge!=null ? ("dump "+ageLabel(dumpAge)) : "dump age unknown";
  const mktKind = mdLive ? "ok" : ((mdStatus==="STALE"||mdStatus==="CONNECTED_STALE") ? "warn" : "bad");
  setOps("opsMarketCell", mdLabel, mktKind);
  if($("opsMarket")) $("opsMarket").textContent = mdLabel;
  if($("opsMarketHint")){
    const qAge = s.market_age_seconds;
    $("opsMarketHint").textContent = (qAge!=null ? ("quotes "+ageLabel(qAge)) : "quote age unknown")
      + " · telemetry " + (ntAlive ? "alive" : "stale");
  }
  setOps("opsEngineCell", running ? "RUNNING" : "STOPPED", running ? "ok" : "warn");
  if($("opsEngine")) $("opsEngine").textContent = running ? "RUNNING" : "STOPPED";
  if($("opsEngineHint")) $("opsEngineHint").textContent = running ? "loop active" : "not API health";
  setOps("opsSim101Cell", simPresent ? "CONNECTED" : "MISSING", simPresent ? "ok" : "bad");
  if($("opsSim101")) $("opsSim101").textContent = simPresent ? "CONNECTED" : "MISSING";
  if($("opsSim101Hint")) $("opsSim101Hint").textContent = sim.account || sim.name || "Sim101";
  const posTxt = (sim.side || (simFlat?"FLAT":"—")) + " qty " + (sim.quantity==null?"—":sim.quantity);
  setOps("opsPosCell", posTxt, simFlat ? "idle" : "warn");
  if($("opsPos")) $("opsPos").textContent = posTxt;
  const rec = s.sim101_recovery || "—";
  setOps("opsRecoveryCell", rec, rec==="FLAT_SAFE" ? "ok" : (rec==="ORPHAN_POSITION" ? "bad" : "warn"));
  if($("opsRecovery")) $("opsRecovery").textContent = rec;
  const arm = s.execution_arm || "DISARMED";
  const armed = String(arm).indexOf("ARMED")>=0 && String(arm).indexOf("DISARMED")<0;
  setOps("opsArmCell", arm, armed ? "warn" : "ok");
  if($("opsArm")) $("opsArm").textContent = arm;
  const fnTxt = fnConnected
    ? "CONNECTED"
    : (s.fundednext_connection || "UNAVAILABLE");
  setOps("opsFnCell", fnTxt, fnConnected ? "warn" : "bad");
  if($("opsFn")) $("opsFn").textContent = fnTxt;
  if($("opsFnHint")) $("opsFnHint").textContent = (a.account_id || "Flex 50K") + " · MCP money";
  setOps("opsPropCell", propOff ? "LOCKED" : "UNLOCKED", propOff ? "ok" : "bad");
  if($("opsProp")) $("opsProp").textContent = propOff ? "LOCKED" : "UNLOCKED";
  const cState = (pc.state || "PROP_LOCKED").replace("PROP_CANARY_","").replace("PROP_","");
  const cWarn = cState==="ARMED" || cState==="IN_FLIGHT";
  const cBad = cState==="BLOCKED";
  const cOk = cState==="READY" || cState==="DISARMED" || cState==="LOCKED" || cState==="COMPLETE";
  setOps("opsCanaryCell", cState, cBad ? "bad" : (cWarn ? "warn" : (cOk ? "ok" : "idle")));
  if($("opsCanary")) $("opsCanary").textContent = cState;
  if($("opsCanaryHint")) $("opsCanaryHint").textContent = pc.account ? "1 MNQ · exact account" : "one-shot 1 MNQ";
  const safe = (s.checks && s.checks.safe_start_result) || "—";
  setOps("opsSafeCell", safe, safe==="ENGINE_MAY_RUN" ? "ok" : "warn");
  if($("opsSafe")) $("opsSafe").textContent = safe;
  setOps("opsDvpCell", live.ok ? ("LIVE · "+live.dir) : "NO LIVE DVP", live.ok ? "ok" : "idle");
  if($("opsDvp")) $("opsDvp").textContent = live.ok ? ("LIVE · "+live.dir) : "NO LIVE DVP";
  if($("opsDvpHint")) $("opsDvpHint").textContent = live.ok ? (live.source || "phase54_live") : "shadow excluded";

  if($("opsDumpTs")) $("opsDumpTs").textContent = dumpTs || "—";
  if($("opsDumpAge")) $("opsDumpAge").textContent = dumpAge!=null ? ageLabel(dumpAge) : "—";
  const barTs = dump.last_nq_bar_ts
    || (s.live_dvp && s.live_dvp.last_finalized_5m && s.live_dvp.last_finalized_5m.iso_et)
    || (s.live_dvp && s.live_dvp.last_live_bar_ts);
  const barStatus = dump.nq_bars_1m_status || (s.live_dvp && s.live_dvp.nq_bars_1m_status) || (barTs ? "LIVE" : "WAITING");
  const barCount = dump.nq_bars_1m_count!=null ? dump.nq_bars_1m_count : (s.live_dvp && s.live_dvp.nq_bars_1m_count);
  if($("opsNqBar")) $("opsNqBar").textContent = barTs || (barStatus+" · count "+(barCount==null?"—":barCount));
  if($("opsNqBarAge")) $("opsNqBarAge").textContent = barTs ? ageLabel(ageFromTs(barTs)) : (barStatus+" · no 1m bar");
  const acctTs = (s.fundednext_account_state && s.fundednext_account_state.timestamp)
    || (s.fundednext_mcp && s.fundednext_mcp.timestamp)
    || (s.fundednext && s.fundednext.timestamp);
  if($("opsAcctTs")) $("opsAcctTs").textContent = acctTs || "—";
  if($("opsHb")) $("opsHb").textContent = s.heartbeat_ts || "—";
  if($("opsHbHint")) $("opsHbHint").textContent = running ? "engine loop" : "last start (engine STOPPED)";
  const n = s.notifications || {};
  if($("opsAlert")){
    if(!n.configured) $("opsAlert").textContent = "NOT CONFIGURED";
    else if(!n.enabled) $("opsAlert").textContent = "APPRISE · DISABLED";
    else $("opsAlert").textContent = "APPRISE · TELEGRAM · " + (n.delivery_status || "READY");
  }
  if($("opsAlertHint")) $("opsAlertHint").textContent = n.last_event_type || "outbound only";

  renderChart(s.telemetry||{points:[a.realized_pnl||0],net:a.realized_pnl||0,high:a.realized_pnl||0,low:a.realized_pnl||0,labels:["now"]});
  renderLogs(s.events||[]);
}

async function refresh(){
  try{
    const s = await api("/api/snapshot");
    applySnapshot(s);
  }catch(err){
    $("engineStatus").textContent = "STOPPED";
    if($("opsDesk")) $("opsDesk").textContent = "OFFLINE";
    setOps("opsDeskCell", "OFFLINE", "bad");
    if($("opsDeskHint")) $("opsDeskHint").textContent = "snapshot fetch failed";
  }
}

$("startBtn").addEventListener("click", async ()=>{
  try{
    const c = await api("/safe-start/checks");
    lastChecks = c;
    renderChecks(c.display||{});
    $("modalResult").textContent = c.ok
      ? "ENGINE RUNNING · ORDER EXECUTION DISABLED"
      : "SAFE START FAILED · ORDER EXECUTION DISABLED";
    $("confirmStart").disabled = !c.ok;
    safeStartDialog.showModal();
  }catch(err){
    $("confirmStart").disabled = true;
    safeStartDialog.showModal();
  }
});

$("confirmStart").addEventListener("click", async ()=>{
  try{
    await api("/control/start","POST",{safe_start:true,execution_permission:false});
    safeStartDialog.close();
    await refresh();
  }catch(err){
    $("modalResult").textContent = "SAFE START FAILED · ORDER EXECUTION DISABLED";
  }
});

$("pauseBtn").addEventListener("click", async ()=>{
  const paused = $("pauseBtn").textContent.indexOf("Resume")===-1;
  await api("/control/pause-entries","POST",{paused});
  await refresh();
});

$("modeSelect").addEventListener("change", async e=>{
  await api("/control/mode","POST",{mode:e.target.value.replace(" ","_")});
  await refresh();
});

function showConfirm({title,text,kicker="Confirm",typed=false,onConfirm}){
  $("confirmTitle").textContent=title;
  $("confirmText").textContent=text;
  $("confirmKicker").textContent=kicker;
  $("confirmInputWrap").classList.toggle("hidden",!typed);
  $("confirmInput").value="";
  $("confirmAction").disabled=typed;
  $("confirmAction").onclick=()=>{onConfirm();confirmDialog.close();};
  if(typed){$("confirmInput").oninput=e=>$("confirmAction").disabled=e.target.value.trim()!=="CONFIRM";}
  confirmDialog.showModal();
}

$("stopBtn").addEventListener("click",()=>showConfirm({
  title:"Stop execution engine?",
  text:"This stops the engine through the AITRADE Control API. FundedNext remains read-only and order execution remains disabled.",
  onConfirm:async()=>{ await api("/control/stop","POST"); await refresh(); }
}));

$("killBtn").addEventListener("click",()=>showConfirm({
  title:"Emergency flatten + stop", kicker:"Danger",
  text:"Logs a flatten request without transmitting orders (PROP_EXECUTION=false), then stops the engine.",
  typed:true,
  onConfirm:async()=>{ await api("/control/emergency-kill","POST",{confirm:"CONFIRM"}); await refresh(); }
}));

$("clearLogs").addEventListener("click",()=>{ $("logs").innerHTML=""; lastEventKey=""; });
document.querySelectorAll(".range-btn").forEach(btn=>btn.addEventListener("click",()=>{
  document.querySelectorAll(".range-btn").forEach(b=>b.classList.toggle("active", b===btn));
  refresh();
}));

refresh();
setInterval(refresh, 3000);
