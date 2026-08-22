
const API_BASE = localStorage.getItem("AITRADE_API_BASE") || "http://127.0.0.1:8765";
let engineRunning = true;
let entriesPaused = false;
let uptimeSeconds = 3*3600 + 17*60 + 22;

const $ = id => document.getElementById(id);
const safeStartDialog = $("safeStartDialog");
const confirmDialog = $("confirmDialog");

const chartSeries = {
  "1D": {points:[12,18,22,45,31,72,66,95,110,104,129,121,140,154,170,161,176,184],labels:["09:30","10:30","11:30","12:30","13:30","14:30"],net:"+$184.25",high:"+$236.80",low:"-$48.10"},
  "1W": {points:[20,42,28,65,89,74,116,96,134,155,148,171],labels:["Mon","Tue","Wed","Thu","Fri","Now"],net:"+$612.40",high:"+$728.10",low:"-$91.40"},
  "1M": {points:[45,30,62,54,88,76,91,109,101,138,143,160,151,177,190],labels:["W1","W2","W3","W4","Now"],net:"+$1,942.80",high:"+$2,106.30",low:"-$242.50"},
  "ALL": {points:[6,8,12,20,24,31,44,61,74,88,103,119,143,171,198],labels:["P1","P5","P10","P15","P20","Now"],net:"+$8,426.00",high:"+$8,910.40",low:"-$642.00"}
};

function nowTime(){ return new Date().toLocaleTimeString([], {hour12:false}); }
setInterval(()=> $("clock").textContent = nowTime(),500); $("clock").textContent = nowTime();
setInterval(()=>{
  uptimeSeconds++;
  const h=String(Math.floor(uptimeSeconds/3600)).padStart(2,"0");
  const m=String(Math.floor((uptimeSeconds%3600)/60)).padStart(2,"0");
  const s=String(uptimeSeconds%60).padStart(2,"0");
  $("heartbeat").textContent=`${(Math.random()*.8+.1).toFixed(1)}s ago`;
},1000);

function log(level,message){
  const row=document.createElement("div");
  row.innerHTML=`<time>${nowTime()}</time><span class="log-level ${level==="WARN"?"warn":level==="BLOCK"?"block":""}">${level}</span><p>${message}</p>`;
  $("logs").prepend(row);
}

async function api(path,method="POST",body={}){
  try{
    const res=await fetch(`${API_BASE}${path}`,{
      method,headers:{"Content-Type":"application/json"},
      body:method==="GET"?undefined:JSON.stringify(body)
    });
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  }catch(err){
    log("WARN",`Control API unavailable (${err.message}); prototype mock used`);
    return {ok:true,mock:true};
  }
}

function refreshState(){
  $("engineStatus").textContent=engineRunning?"Running":"Stopped";
  $("startBtn").textContent=engineRunning?"Run safe start":"Start engine";
}
refreshState();

function renderChart(range="1D"){
  const {points,labels,net,high,low}=chartSeries[range];
  const width=960,height=300,padX=10,padTop=18,padBottom=20;
  const min=Math.min(...points),max=Math.max(...points),xStep=(width-padX*2)/(points.length-1);
  const coords=points.map((value,i)=>{
    const norm=(value-min)/((max-min)||1);
    return [padX+i*xStep,height-padBottom-norm*(height-padTop-padBottom)];
  });
  const line=coords.map((c,i)=>`${i===0?"M":"L"} ${c[0].toFixed(2)} ${c[1].toFixed(2)}`).join(" ");
  const area=`${line} L ${coords.at(-1)[0].toFixed(2)} ${height-padBottom} L ${coords[0][0].toFixed(2)} ${height-padBottom} Z`;
  $("equityPath").setAttribute("d",line); $("equityPathGlow").setAttribute("d",line); $("equityArea").setAttribute("d",area);
  const last=coords.at(-1); $("equityDot").setAttribute("cx",last[0]); $("equityDot").setAttribute("cy",last[1]);
  $("chartNet").textContent=net; $("chartHigh").textContent=high; $("chartLow").textContent=low;
  $("chartXAxis").innerHTML=labels.map(x=>`<span>${x}</span>`).join("");
  document.querySelectorAll(".range-btn").forEach(btn=>btn.classList.toggle("active",btn.dataset.range===range));
}
document.querySelectorAll(".range-btn").forEach(btn=>btn.addEventListener("click",()=>renderChart(btn.dataset.range)));
renderChart("1D");

$("modeSelect").addEventListener("change", async e=>{
  const mode=e.target.value;
  await api("/control/mode","POST",{mode:mode.replace(" ","_")});
  log("INFO",`Execution mode set to ${mode}. Execution permission remains disabled.`);
});

const checks = [
  ["Fresh market data","PASS"],
  ["FundedNext authenticated","PASS"],
  ["Correct account ID","PASS"],
  ["Equity / MLL available","PASS"],
  ["Broker positions reconciled","PASS"],
  ["Prop rules loaded","PASS"],
  ["Frozen NQ hash verified","PASS"],
  ["No stale orders","PASS"],
  ["Risk limits valid","PASS"],
  ["News gate valid","PASS"],
  ["Execution permission checked","DISABLED"]
];

function renderChecks(){
  $("safeChecks").innerHTML=checks.map(([name,status])=>
    `<div class="check"><span>${name}</span><span class="${status==="DISABLED"?"blocked-text":"pass"}">${status}</span></div>`
  ).join("");
  $("confirmStart").disabled=false;
}

$("startBtn").addEventListener("click",()=>{renderChecks();safeStartDialog.showModal();});

$("confirmStart").addEventListener("click",async()=>{
  const result=await api("/control/start","POST",{safe_start:true,execution_permission:false});
  if(result.ok){
    engineRunning=true; entriesPaused=false; refreshState();
    log("INFO","Safe Start passed — engine running");
    log("BLOCK","Order execution remains disabled · PROP_EXECUTION=false");
    safeStartDialog.close();
  }
});

$("pauseBtn").addEventListener("click",async()=>{
  entriesPaused=!entriesPaused;
  await api("/control/pause-entries","POST",{paused:entriesPaused});
  $("pauseBtn").textContent=entriesPaused?"Resume new entries":"Pause new entries";
  log("INFO",entriesPaused?"New entries paused; reconciliation and position management remain active":"New entries resumed; execution still blocked");
});

function showConfirm({title,text,kicker="Confirm",typed=false,onConfirm}){
  $("confirmTitle").textContent=title;$("confirmText").textContent=text;$("confirmKicker").textContent=kicker;
  $("confirmInputWrap").classList.toggle("hidden",!typed);$("confirmInput").value="";$("confirmAction").disabled=typed;
  $("confirmAction").onclick=()=>{onConfirm();confirmDialog.close();};
  if(typed){$("confirmInput").oninput=e=>$("confirmAction").disabled=e.target.value.trim()!=="CONFIRM";}
  confirmDialog.showModal();
}

$("stopBtn").addEventListener("click",()=>showConfirm({
  title:"Stop execution engine?",
  text:"This stops the engine through the AITRADE Control API. FundedNext remains read-only and order execution remains disabled.",
  onConfirm:async()=>{await api("/control/stop");engineRunning=false;refreshState();log("WARN","Execution engine stopped by operator");}
}));

$("killBtn").addEventListener("click",()=>showConfirm({
  title:"Emergency flatten + stop",kicker:"Danger",
  text:"Requests working-order cancellation, position flattening if applicable, disables entries, then stops the engine. Typed confirmation is required.",
  typed:true,
  onConfirm:async()=>{await api("/control/emergency-kill","POST",{confirm:"CONFIRM"});engineRunning=false;entriesPaused=true;refreshState();log("WARN","Emergency kill requested");}
}));

$("clearLogs").addEventListener("click",()=> $("logs").innerHTML="");
