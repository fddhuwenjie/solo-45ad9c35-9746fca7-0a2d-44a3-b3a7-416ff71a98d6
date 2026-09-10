/* 微孔板梯度稀释规划器 —— 前端逻辑（原生 JS，无第三方依赖） */
"use strict";

const state = {
  result: null,
  config: null,
  // 客户端布板：slot -> {role, level, replicate, locked, source, conc, vol} | null(空孔)
  board: new Map(),
  rows: 8, cols: 12, plateType: 96,
  experimentId: null,
  stepStates: {},   // order -> {done, done_at, note}
  logs: [],
  selectedSlot: null,
  dragSlot: null,
};

const $ = (sel) => document.querySelector(sel);
const fmt = (x, d = 4) => {
  if (x === null || x === undefined || x === "") return "–";
  const n = Number(x);
  if (!isFinite(n)) return String(x);
  if (n !== 0 && Math.abs(n) < 0.001) return n.toExponential(2);
  return Number(n.toFixed(d)).toString();
};
const unit = () => state.config?.concentration_unit || "";

// --------------------------------------------------------------------------- //
// 初始化
// --------------------------------------------------------------------------- //

function init() {
  $("#fPlate").addEventListener("change", onPlateChange);
  for (const id of ["fCapacity", "fStockConc", "fUnit", "fStockVol", "fMaxChain",
                    "fMinMix", "fDead", "fBlankVol"]) {
    $("#" + id).addEventListener("change", () => recompute(false));
  }
  $("#fBlanks").addEventListener("change", () => recompute(true));
  loadExamples();
  loadSavedList();
  // 默认表单
  onPlateChange(true);
  addRange(2, 20); addRange(20, 200);
  [10, 1, 0.1, 0.01, 0.001].forEach((c) => addTarget(c, 100, 2));
  $("#fBlanks").value = 2;
  recompute(true);
}

async function loadExamples() {
  const res = await fetch("/api/examples");
  const list = await res.json();
  const sel = $("#exampleSelect");
  for (const ex of list) {
    const opt = document.createElement("option");
    opt.value = ex.id;
    opt.textContent = ex.name;
    opt.title = ex.description;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", async () => {
    if (!sel.value) return;
    const r = await fetch(`/api/examples/${sel.value}`);
    const ex = await r.json();
    applyConfigToForm(ex.config);
    await recompute(true);
    toast(`已载入「${ex.name}」（未保存）`);
    sel.value = "";
  });
}

function onPlateChange(skipDefaultCap) {
  const t = Number($("#fPlate").value);
  state.plateType = t;
  if (!skipDefaultCap) $("#fCapacity").value = t === 96 ? 280 : 2900;
  else $("#fCapacity").value = t === 96 ? 280 : 2900;
  state.rows = t === 96 ? 8 : 4;
  state.cols = t === 96 ? 12 : 6;
  state.board = new Map();
  if (!skipDefaultCap) recompute(true);
}

// --------------------------------------------------------------------------- //
// 动态表单：量程 / 目标
// --------------------------------------------------------------------------- //

function addRange(lo, hi) {
  const div = document.createElement("div");
  div.className = "target-row";
  div.innerHTML = `
    <input type="number" min="0" step="any" placeholder="最小 µL" value="${lo ?? ""}">
    <input type="number" min="0" step="any" placeholder="最大 µL" value="${hi ?? ""}">
    <span></span><button type="button" class="icon-btn" title="删除">✕</button>`;
  div.querySelectorAll("input").forEach((i) => i.addEventListener("change", () => recompute(false)));
  div.querySelector("button").onclick = () => { div.remove(); recompute(false); };
  $("#rangeList").appendChild(div);
}

function addTarget(conc = "", vol = 100, reps = 1) {
  const div = document.createElement("div");
  div.className = "target-row";
  div.innerHTML = `
    <input class="t-conc" type="number" min="0" step="any" placeholder="浓度" value="${conc}">
    <input class="t-vol" type="number" min="0" step="any" value="${vol}">
    <input class="t-rep" type="number" min="1" step="1" value="${reps}">
    <button type="button" class="icon-btn" title="删除该行">✕</button>`;
  div.querySelector(".t-conc").addEventListener("change", () => recompute(false));
  div.querySelector(".t-vol").addEventListener("change", () => recompute(false));
  div.querySelector(".t-rep").addEventListener("change", () => recompute(true));
  div.querySelector("button").onclick = () => { div.remove(); recompute(true); };
  $("#targetList").appendChild(div);
}

function makeSerial() {
  const start = parseFloat(prompt("起始浓度（最高浓度点）：", "10"));
  if (!isFinite(start) || start <= 0) return;
  const factor = parseFloat(prompt("稀释倍数（如 10 表示 1:10 系列）：", "10"));
  if (!isFinite(factor) || factor <= 1) return;
  const n = parseInt(prompt("浓度点数量：", "6"), 10);
  if (!n || n < 1) return;
  const vol = parseFloat(prompt("每孔终体积 (µL)：", "100")) || 100;
  $("#targetList").innerHTML = "";
  for (let i = 0; i < n; i++) {
    addTarget(Number((start / Math.pow(factor, i)).toPrecision(6)), vol, 2);
  }
  recompute(true);
}

// --------------------------------------------------------------------------- //
// 表单 <-> 配置
// --------------------------------------------------------------------------- //

function readFormConfig() {
  const ranges = [...$("#rangeList").querySelectorAll(".target-row")].map((row) => ({
    min: parseFloat(row.children[0].value),
    max: parseFloat(row.children[1].value),
  })).filter((r) => isFinite(r.min) && isFinite(r.max) && r.min > 0 && r.max >= r.min);

  const targets = [...$("#targetList").querySelectorAll(".target-row")].map((row) => ({
    concentration: parseFloat(row.querySelector(".t-conc").value),
    volume: parseFloat(row.querySelector(".t-vol").value),
    replicates: parseInt(row.querySelector(".t-rep").value, 10) || 1,
  })).filter((t) => t.concentration > 0 && t.volume > 0);

  return {
    plate_type: Number($("#fPlate").value),
    well_capacity: parseFloat($("#fCapacity").value) || null,
    stock_concentration: parseFloat($("#fStockConc").value),
    stock_volume: parseFloat($("#fStockVol").value) || null,
    concentration_unit: $("#fUnit").value.trim() || "µM",
    pipette_ranges: ranges.length ? ranges : [{ min: 2, max: 200 }],
    min_mix: parseFloat($("#fMinMix").value) || 0,
    dead_volume: parseFloat($("#fDead").value) || 0,
    max_chain: parseInt($("#fMaxChain").value, 10) || 12,
    blank_count: parseInt($("#fBlanks").value, 10) || 0,
    blank_volume: parseFloat($("#fBlankVol").value) || 100,
    targets,
    wells: boardToPayload(),
  };
}

function boardToPayload() {
  // 仅在客户端有自定义布板时回传；空 Map 表示完全自动
  if (state.board.size === 0) return [];
  const out = [];
  for (const [slot, w] of state.board) {
    if (!w) continue;
    out.push({
      slot,
      role: w.role,
      level: w.level,
      replicate: w.replicate,
      locked: !!w.locked,
      source: w.source ?? null,
    });
  }
  return out;
}

function applyConfigToForm(cfg) {
  $("#fPlate").value = cfg.plate_type;
  onPlateChange(true);
  $("#fCapacity").value = cfg.well_capacity ?? (cfg.plate_type === 96 ? 280 : 2900);
  $("#fStockConc").value = cfg.stock_concentration;
  $("#fUnit").value = cfg.concentration_unit || "µM";
  $("#fStockVol").value = cfg.stock_volume ?? "";
  $("#fMaxChain").value = cfg.max_chain;
  $("#fMinMix").value = cfg.min_mix;
  $("#fDead").value = cfg.dead_volume;
  $("#fBlanks").value = cfg.blank_count ?? 0;
  $("#fBlankVol").value = cfg.blank_volume ?? 100;
  $("#rangeList").innerHTML = "";
  (cfg.pipette_ranges || [{ min: 2, max: 200 }]).forEach((r) => addRange(r.min, r.max));
  $("#targetList").innerHTML = "";
  (cfg.targets || []).forEach((t) => addTarget(t.concentration, t.volume, t.replicates));
  state.board = new Map();
  for (const w of cfg.wells || []) state.board.set(w.slot, { ...w });
}

// --------------------------------------------------------------------------- //
// 重算
// --------------------------------------------------------------------------- //

async function recompute(relayout) {
  let cfg;
  try {
    cfg = readFormConfig();
  } catch (e) {
    toast("表单输入有误：" + e.message, true);
    return;
  }
  if (relayout) {
    // 结构变化（目标/平行样/板型）→ 清除自定义布板
    state.board = new Map();
    cfg.wells = [];
  }
  state.config = cfg;
  const res = await fetch("/api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: cfg }),
  });
  const data = await res.json();
  if (!res.ok) {
    toast(data.error || "规划失败", true);
    return;
  }
  state.result = data;
  syncBoardFromResult(data, relayout);
  renderAll();
}

function syncBoardFromResult(data, relayout) {
  // 服务器返回孔位为权威；保留锁定标记与手动来源（它们已包含在结果中）
  const next = new Map();
  for (const w of data.wells) {
    next.set(w.slot, {
      role: w.role, level: w.level, replicate: w.replicate,
      locked: w.locked, source: w.manual_source ?? null,
      conc: w.target_conc, vol: w.final_volume,
    });
  }
  state.board = next;
}

// --------------------------------------------------------------------------- //
// 渲染：板图
// --------------------------------------------------------------------------- //

function renderPlate() {
  const { rows, cols, plateType } = state;
  const cont = $("#plateContainer");
  cont.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = `plate plate-${plateType}`;

  const corner = document.createElement("div");
  corner.className = "corner";
  grid.appendChild(corner);
  for (let c = 1; c <= cols; c++) {
    const h = document.createElement("div");
    h.className = "col-head";
    h.textContent = c;
    grid.appendChild(h);
  }

  const wells = new Map(state.result.wells.map((w) => [w.slot, w]));
  for (let r = 0; r < rows; r++) {
    const rh = document.createElement("div");
    rh.className = "row-head";
    rh.textContent = String.fromCharCode(65 + r);
    grid.appendChild(rh);
    for (let c = 0; c < cols; c++) {
      const slot = r * cols + c;
      grid.appendChild(renderWellCell(slot, wells.get(slot)));
    }
  }
  cont.appendChild(grid);
}

function firstOrderForSlot(slot) {
  const s = state.result.steps.find((st) => st.target_slot === slot);
  return s ? s.order : null;
}

function renderWellCell(slot, w) {
  const cell = document.createElement("div");
  if (!w) {
    cell.className = "well empty";
    cell.innerHTML = `<div class="wlabel"><span>${labelOf(slot)}</span></div>
      <div style="font-size:10px;color:#aab2bd;text-align:center;margin-top:8px">空孔 · 可拖入</div>`;
  } else {
    const codes = w.issue_codes || [];
    const hasErr = codes.some((c) => ["CAPACITY", "VOLUME_RANGE", "CONC_UNREACHABLE",
      "SOURCE_CYCLE", "SOURCE_INVALID"].includes(c));
    const hasWarn = codes.some((c) => ["MIN_MIX", "DEAD_VOLUME", "STEP_DEPTH", "STOCK_SHORT"].includes(c));
    const levelCls = w.role === "blank" ? "blank-well"
      : (w.level <= 7 ? `level-${w.level}` : "level-default");
    cell.className = ["well", levelCls,
      w.locked ? "locked" : "",
      w.role === "blank" ? "blank-well" : "",
      hasErr ? "has-error" : hasWarn ? "has-warn" : "",
      state.selectedSlot === slot ? "flash" : ""].join(" ");
    cell.draggable = !w.locked;
    const order = firstOrderForSlot(slot);
    const concText = w.role === "blank" ? "空白"
      : `${fmt(w.computed_conc ?? w.target_conc, 4)} ${unit()}`;
    const targetText = w.role === "blank" ? ""
      : `<div style="font-size:9px;color:var(--muted)">目标 ${fmt(w.target_conc)} · P${w.replicate}</div>`;
    cell.innerHTML = `
      <div class="wlabel">
        <span>${w.label}</span>
        ${w.locked ? '<span class="lock-badge">🔒</span>' : ""}
      </div>
      <div class="wconc">${concText}</div>
      <div class="wsrc">← ${w.source_label || "–"}</div>
      <div class="wvol">${fmt(w.final_volume, 0)} µL${targetText}</div>
      ${order ? `<span class="order-badge">${order}</span>` : ""}`;
  }
  cell.dataset.slot = slot;
  cell.addEventListener("click", () => selectWell(slot));
  cell.addEventListener("dblclick", (e) => { e.preventDefault(); toggleLock(slot); });
  cell.addEventListener("dragstart", onDragStart);
  cell.addEventListener("dragend", onDragEnd);
  cell.addEventListener("dragover", onDragOver);
  cell.addEventListener("dragleave", onDragLeave);
  cell.addEventListener("drop", onDrop);
  return cell;
}

function labelOf(slot) {
  const r = Math.floor(slot / state.cols);
  const c = slot % state.cols;
  return `${String.fromCharCode(65 + r)}${c + 1}`;
}

// --------------------------------------------------------------------------- //
// 拖拽
// --------------------------------------------------------------------------- //

function onDragStart(e) {
  const slot = Number(e.currentTarget.dataset.slot);
  const w = state.board.get(slot);
  if (!w || w.locked) { e.preventDefault(); return; }
  state.dragSlot = slot;
  e.currentTarget.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", String(slot));
}
function onDragEnd(e) {
  e.currentTarget.classList.remove("dragging");
  document.querySelectorAll(".drop-ok,.drop-deny")
    .forEach((el) => el.classList.remove("drop-ok", "drop-deny"));
  state.dragSlot = null;
}
function onDragOver(e) {
  e.preventDefault();
  const target = Number(e.currentTarget.dataset.slot);
  const tw = state.board.get(target);
  const cls = (!tw || !tw.locked) ? "drop-ok" : "drop-deny";
  e.currentTarget.classList.add(cls);
  e.dataTransfer.dropEffect = (!tw || !tw.locked) ? "move" : "none";
}
function onDragLeave(e) {
  e.currentTarget.classList.remove("drop-ok", "drop-deny");
}
function onDrop(e) {
  e.preventDefault();
  const from = state.dragSlot ?? Number(e.dataTransfer.getData("text/plain"));
  const to = Number(e.currentTarget.dataset.slot);
  if (from === null || from === to) return;
  const sw = state.board.get(from);
  const tw = state.board.get(to);
  if (sw?.locked) { toast("源孔已锁定，不可移动", true); return; }
  if (tw?.locked) { toast("目标孔已锁定，不能覆盖；请先双击解锁", true); return; }
  // 交换（空孔为 null）；交换后清除被移动孔的手动来源，避免指向失效
  if (sw && from !== to) sw.source = null;
  state.board.set(to, sw ? { ...sw } : null);
  state.board.set(from, tw ? { ...tw } : null);
  state.dragSlot = null;
  recompute(false);
}

function toggleLock(slot) {
  const w = state.board.get(slot);
  if (!w) { toast("空孔无需锁定"); return; }
  w.locked = !w.locked;
  if (!w.locked) w.source = null; // 解锁后恢复自动来源
  recompute(false);
}

function autoLayout() {
  state.board = new Map();
  recompute(true);
  toast("已恢复自动布板");
}

// --------------------------------------------------------------------------- //
// 孔详情
// --------------------------------------------------------------------------- //

function selectWell(slot) {
  state.selectedSlot = slot;
  renderPlate();
  const w = state.result.wells.find((x) => x.slot === slot);
  const box = $("#wellDetail");
  if (!w) {
    box.innerHTML = `<span style="color:var(--muted)">${labelOf(slot)} 为空孔。可将其他孔拖入此处。</span>`;
    return;
  }
  const a = w.analyte_transfer, d = w.diluent_transfer;
  const devHtml = (w.deviations || []).map((dv) => `<div class="deviation">⚠️ ${dv.message}</div>`).join("");
  const issueHtml = (w.issue_codes || []).length
    ? `<div style="margin:6px 0"><b>问题代码：</b>${w.issue_codes.join(", ")}</div>` : "";

  // 来源选择器（空白孔固定使用稀释液，不显示）
  let sourceSelector = "";
  if (w.role !== "blank") {
    const sourceOpts = [`<option value="">自动（由系统选择）</option>`,
      `<option value="stock" ${w.manual_source === "stock" ? "selected" : ""}>母液直接稀释</option>`,
      `<option value="diluent" ${w.manual_source === "diluent" ? "selected" : ""}>仅稀释液</option>`];
    for (const other of state.result.wells) {
      if (other.slot === w.slot || other.role === "blank") continue;
      sourceOpts.push(`<option value="${other.slot}" ${String(w.manual_source) === String(other.slot) ? "selected" : ""}>
        ${other.label}（${fmt(other.computed_conc)} ${unit()}）</option>`);
    }
    sourceSelector = `
    <div style="margin-top:8px">
      <label class="field" style="font-size:11px">手动指定来源（立即重算；设为母液可打断异常链）
        <select id="manualSource" onchange="setManualSource(${w.slot}, this.value)">
          ${sourceOpts.join("")}
        </select>
      </label>
    </div>`;
  }

  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <b style="font-size:15px">${w.label}</b>
      <span style="color:var(--muted)">${w.role === "blank" ? "空白孔" : `浓度点 ${w.level + 1} · 平行 ${w.replicate}`}</span>
      <button class="${w.locked ? "" : "primary"}" style="margin-left:auto;padding:3px 9px"
        onclick="toggleLock(${w.slot}); selectWell(${w.slot})">${w.locked ? "🔓 解锁" : "🔒 锁定"}</button>
    </div>
    <div class="kv"><span>目标浓度</span><b>${w.role === "blank" ? 0 : fmt(w.target_conc)} ${unit()}</b></div>
    <div class="kv"><span>实际终浓度</span><b>${fmt(w.computed_conc)} ${unit()}</b></div>
    <div class="kv"><span>终体积（保留）</span><b>${fmt(w.final_volume)} µL</b></div>
    <div class="kv"><span>建议配制体积</span><b>${fmt(w.prep_volume ?? w.final_volume)} µL${w.prep_volume > w.final_volume + 1e-6 ? "（含供出量+死体积）" : ""}</b></div>
    <div class="kv"><span>稀释级数</span><b>${w.chain_depth ?? "–"}</b></div>
    <div class="kv"><span>分析物加入</span><b>${a ? fmt(a.volume) + " µL（" + a.note + "）" : "–"}</b></div>
    <div class="kv"><span>稀释液补加</span><b>${d ? fmt(d.volume) + " µL（" + d.note + "）" : "–"}</b></div>
    ${sourceSelector}
    ${issueHtml}
    <div style="margin-top:6px">${devHtml || '<span style="color:var(--ok)">✓ 该孔无偏差</span>'}</div>`;
}

function setManualSource(slot, val) {
  const w = state.board.get(slot);
  if (!w) return;
  w.source = val === "" ? null : (val === "stock" || val === "diluent" ? val : Number(val));
  w.locked = true; // 指定来源意味着确认该孔
  recompute(false).then(() => selectWell(slot));
}

// --------------------------------------------------------------------------- //
// 问题面板
// --------------------------------------------------------------------------- //

function renderIssues() {
  const box = $("#issueList");
  const issues = state.result.issues;
  if (!issues.length) {
    box.innerHTML = `<div class="issue ok">✓ 全部检查通过：移液量、孔容量、浓度可达性、来源链与孔位均可行。</div>`;
    return;
  }
  box.innerHTML = issues.map((i) => {
    const labels = i.slots.map((s) =>
      `<span class="chip well-link" onclick="selectWell(${s})">${labelOf(s)}</span>`).join("");
    return `<div class="issue ${i.severity}" title="点击孔位编号定位">
      <span class="icode">${i.severity === "error" ? "⛔" : "⚠️"} ${i.code}</span>
      <span>${i.message} ${labels}</span></div>`;
  }).join("");
  // 偏差（最接近方案）
  const devs = state.result.wells.flatMap((w) =>
    (w.deviations || []).map((d) => ({ w, d })));
  if (devs.length) {
    box.innerHTML += `<h2 style="margin-top:10px">最接近可行方案及偏差</h2>` +
      devs.map(({ w, d }) => `<div class="deviation">
        <b class="well-link" onclick="selectWell(${w.slot})">${w.label}</b>：${d.message}</div>`).join("");
  }
}

// --------------------------------------------------------------------------- //
// 汇总与步骤
// --------------------------------------------------------------------------- //

function renderSummary() {
  const s = state.result.summary;
  $("#stWells").textContent = `${s.well_count}（${s.sample_count}/${s.blank_count}）`;
  $("#stStock").textContent = fmt(s.stock_used, 1);
  $("#stDiluent").textContent = fmt(s.diluent_used, 1);
  $("#stSteps").textContent = s.step_count;
  $("#stErr").textContent = s.errors;
  $("#stErr").style.color = s.errors ? "var(--err)" : "var(--ok)";
  $("#stWarn").textContent = s.warnings;
  $("#stWarn").style.color = s.warnings ? "var(--warn)" : "var(--ok)";
}

function renderSteps() {
  const box = $("#stepList");
  box.innerHTML = "";
  const groups = new Map();
  for (const s of state.result.steps) {
    if (!groups.has(s.phase)) {
      groups.set(s.phase, { title: s.phase_title, items: [] });
    }
    groups.get(s.phase).items.push(s);
  }
  for (const [, g] of groups) {
    const gdiv = document.createElement("div");
    gdiv.className = "phase-group";
    gdiv.innerHTML = `<div class="phase-title">${g.title}</div>`;
    for (const s of g.items) {
      const st = state.stepStates[s.order];
      const done = st && st.done;
      const item = document.createElement("div");
      item.className = "step-item" + (done ? " done" : "");
      item.innerHTML = `
        <input type="checkbox" ${done ? "checked" : ""}
          ${state.experimentId ? "" : "disabled title='保存实验后可勾选'"}
          onchange="toggleStep(${s.order}, this.checked)">
        <span class="num">${s.order}</span>
        <div>
          <div class="step-text">${s.text}</div>
          <div class="step-meta">目标孔
            <span class="well-link" onclick="selectWell(${s.target_slot})">${s.target_label}</span>
            ${s.locked ? " · 🔒 已确认孔" : ""}
            ${done && st.done_at ? ` · 完成于 ${st.done_at}` : ""}
          </div>
        </div>`;
      gdiv.appendChild(item);
    }
    box.appendChild(gdiv);
  }

  const total = state.result.steps.length;
  const doneN = state.result.steps.filter((s) => state.stepStates[s.order]?.done).length;
  const pct = total ? Math.round(doneN / total * 100) : 0;
  $("#progressFill").style.width = pct + "%";
  $("#progressText").textContent = state.experimentId
    ? `已完成 ${doneN} / ${total} 步（${pct}%）`
    : "尚未保存实验——点击左侧「💾 保存实验」后即可逐步勾选并保留执行记录";
}

async function toggleStep(order, done) {
  if (!state.experimentId) return;
  const r = await fetch(`/api/experiments/${state.experimentId}/steps/${order}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ done }),
  });
  state.stepStates[order] = await r.json();
  await refreshLogs();
  renderSteps();
}

async function resetSteps() {
  if (!state.experimentId) { toast("请先保存实验"); return; }
  await fetch(`/api/experiments/${state.experimentId}/reset`, { method: "POST" });
  const exp = await (await fetch(`/api/experiments/${state.experimentId}`)).json();
  state.stepStates = exp.step_states || {};
  await refreshLogs();
  renderSteps();
  toast("已重置全部勾选");
}

async function refreshLogs() {
  if (!state.experimentId) return;
  const exp = await (await fetch(`/api/experiments/${state.experimentId}`)).json();
  state.logs = exp.logs || [];
  state.stepStates = exp.step_states || state.stepStates;
  $("#logList").innerHTML = state.logs.slice().reverse().map((l) =>
    `<div class="log-row">${l.created_at} · ${l.detail || l.action}${l.step_order ? `（#${l.step_order}）` : ""}</div>`
  ).join("") || '<span style="color:var(--muted)">暂无记录</span>';
}

// --------------------------------------------------------------------------- //
// 保存 / 打开 / 删除
// --------------------------------------------------------------------------- //

async function saveExperiment() {
  const cfg = readFormConfig();
  cfg.wells = boardToPayload();
  const name = $("#expName").value.trim() || `稀释方案 ${new Date().toLocaleString()}`;
  const payload = { name, config: cfg };
  let r, data;
  if (state.experimentId) {
    r = await fetch(`/api/experiments/${state.experimentId}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } else {
    r = await fetch("/api/experiments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }
  data = await r.json();
  if (!r.ok) { toast(data.error || "保存失败", true); return; }
  if (!state.experimentId) state.experimentId = data.id;
  $("#expName").value = name;
  $("#savedState").textContent = `已保存 #${data.id}「${name}」`;
  state.result = data.result;
  syncBoardFromResult(data.result, false);
  await loadSavedList();
  await refreshLogs();
  renderAll();
  toast("实验已保存");
}

async function loadSavedList() {
  const sel = $("#savedSelect");
  const list = await (await fetch("/api/experiments")).json();
  sel.innerHTML = `<option value="">已保存实验…</option>` +
    list.map((e) => `<option value="${e.id}" ${state.experimentId === e.id ? "selected" : ""}>
      #${e.id} ${e.name}</option>`).join("");
  sel.onchange = async () => {
    if (!sel.value) return;
    await openExperiment(Number(sel.value));
  };
}

async function openExperiment(id) {
  const exp = await (await fetch(`/api/experiments/${id}`)).json();
  state.experimentId = id;
  $("#expName").value = exp.name;
  applyConfigToForm(exp.config);
  state.config = readFormConfig();
  state.result = exp.result;
  syncBoardFromResult(exp.result, false);
  state.stepStates = exp.step_states || {};
  state.logs = exp.logs || [];
  $("#savedState").textContent = `已保存 #${id}「${exp.name}」· 更新于 ${exp.updated_at}`;
  renderAll();
  $("#logList").innerHTML = state.logs.slice().reverse().map((l) =>
    `<div class="log-row">${l.created_at} · ${l.detail || l.action}${l.step_order ? `（#${l.step_order}）` : ""}</div>`
  ).join("");
  toast(`已打开 #${id}`);
}

async function deleteCurrent() {
  if (!state.experimentId) { toast("当前没有已保存实验"); return; }
  if (!confirm(`确定删除实验 #${state.experimentId}？执行记录将一并删除。`)) return;
  await fetch(`/api/experiments/${state.experimentId}`, { method: "DELETE" });
  state.experimentId = null;
  $("#expName").value = "";
  $("#savedState").textContent = "未保存（当前结果仅在内存中）";
  state.stepStates = {};
  state.logs = [];
  await loadSavedList();
  renderSteps();
  refreshLogsView();
  toast("已删除");
}

function refreshLogsView() {
  $("#logList").innerHTML = '<span style="color:var(--muted)">保存实验后，勾选/取消与重算都会留痕。</span>';
}

// --------------------------------------------------------------------------- //
// 导出
// --------------------------------------------------------------------------- //

async function currentConfigForExport() {
  const cfg = readFormConfig();
  cfg.wells = boardToPayload();
  return cfg;
}

async function exportCSV() {
  if (state.experimentId) {
    window.location.href = `/api/experiments/${state.experimentId}/export.csv`;
    return;
  }
  const r = await fetch("/api/export.csv", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: await currentConfigForExport() }),
  });
  if (!r.ok) { toast("导出失败：配置无效", true); return; }
  const blob = await r.blob();
  downloadBlob(blob, "dilution_plan.csv");
}

function openWorksheet() {
  if (state.experimentId) {
    window.open(`/api/experiments/${state.experimentId}/worksheet`, "_blank");
    return;
  }
  // 未保存：用当前配置 POST 获取工作单 HTML 并在新标签打开
  currentConfigForExport().then((cfg) =>
    fetch("/api/worksheet", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: cfg }),
    })).then(async (r) => {
      if (!r.ok) { toast("无法生成工作单", true); return; }
      const html = await r.text();
      const w = window.open("", "_blank");
      w.document.write(html);
      w.document.close();
    });
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// --------------------------------------------------------------------------- //

function renderAll() {
  renderPlate();
  renderIssues();
  renderSummary();
  renderSteps();
  if (state.selectedSlot !== null) {
    const exists = state.result.wells.some((w) => w.slot === state.selectedSlot);
    if (exists) selectWell(state.selectedSlot);
    else { state.selectedSlot = null; $("#wellDetail").innerHTML = ""; }
  }
}

let toastTimer;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = "toast", 2600);
}

// recompute 被 HTML onsubmit 调用，需要返回 Promise；包一层避免未捕获
window.recompute = recompute;

init();
