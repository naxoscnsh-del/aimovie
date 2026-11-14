"use strict";
// AI 短片工作台 — 无构建步骤的单页应用

// ---------- 工具 ----------
const $ = (s, r = document) => r.querySelector(s);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* 非 JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `请求失败（${r.status}）`);
  return data;
}
let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), bad ? 6000 : 2500);
}
const fail = (e) => toast(e.message || String(e), true);

const P_STATUS = {
  draft: ["待生成脚本", "todo"], scripting: ["脚本生成中", "run"], script_review: ["待确认脚本", "todo"],
  imaging: ["场景图生成中", "run"], image_review: ["待选图", "todo"], image_selected: ["已选图", "todo"],
  generating: ["视频生成中", "run"], shot_review: ["待审核镜头", "todo"], rendering: ["合成中", "run"],
  done: ["已完成", "ok"], failed: ["失败", "bad"],
};
const S_STATUS = {
  pending: ["未出图", ""], imaging: ["出图中", "run"], image_ready: ["待选图", "todo"],
  image_selected: ["已选图", ""], video_running: ["视频生成中", "run"], succeeded: ["视频完成", "ok"], failed: ["失败", "bad"],
};
const badge = (map, key) => { const [t, c] = map[key] || [key, ""]; return h("span", { class: "badge " + c }, t); };
const usd = (n) => "$" + Number(n || 0).toFixed(2);
const ratioCss = (r) => (r || "9:16").replace(":", " / ");

// ---------- 全局状态 ----------
const S = { cfg: null, project: null, tab: null, draft: null, dirty: false, pollTimer: null, bodySig: "" };

// ---------- 路由 ----------
window.addEventListener("hashchange", route);
async function route() {
  clearTimeout(S.pollTimer);
  closeEvents();
  if (S.dirty && !confirm("脚本有未保存的修改，确定离开？")) return;
  S.project = null; S.tab = null; S.draft = null; S.dirty = false; S.bodySig = "";
  const m = location.hash.match(/^#\/p\/([\w-]+)/);
  if (m) return projectPage(m[1]);
  if (location.hash.startsWith("#/stats")) return statsPage();
  if (location.hash.startsWith("#/templates")) return templatesPage();
  if (location.hash.startsWith("#/library")) return libraryPage();
  if (location.hash.startsWith("#/members")) return membersPage();
  return homePage();
}

// ---------- 首页 ----------
async function homePage() {
  const app = $("#app");
  let templates = [], assets = [];
  try { [templates, assets] = await Promise.all([api("/api/templates"), api("/api/assets")]); } catch (e) { fail(e); }
  app.replaceChildren(newForm(templates, assets), h("h2", {}, "全部短片"), h("div", { id: "plist", class: "plist" }, h("div", { class: "muted" }, "加载中…")));
  try {
    const items = await api("/api/projects");
    const box = $("#plist");
    if (!items.length) return box.replaceChildren(h("div", { class: "empty" }, "还没有短片，先在上面描述一个场景"));
    const f = S.listFilter || (S.listFilter = { who: "all", status: "", q: "" });
    const q = h("input", { type: "text", placeholder: "搜索标题或成员", value: f.q, style: "max-width:220px" });
    const st = h("select", { style: "max-width:150px" }, h("option", { value: "" }, "全部状态"),
      Object.entries(P_STATUS).map(([k, [label]]) => h("option", { value: k, selected: f.status === k }, label)));
    const whoChips = h("div", { class: "chips" });
    const grid = h("div", { class: "plist" });
    const draw = () => {
      whoChips.replaceChildren(...[["all", "全部"], ["mine", "我的"]].map(([k, l]) => h("button", { class: "chip" + (f.who === k ? " on" : ""), onclick: () => { f.who = k; draw(); } }, l)));
      const kw = f.q.trim().toLowerCase();
      const list = items.filter((p) => (f.who === "all" || p.owner === S.cfg.user) && (!f.status || p.status === f.status)
        && (!kw || p.title.toLowerCase().includes(kw) || (p.owner || "").toLowerCase().includes(kw)));
      grid.replaceChildren(...(list.length ? list.map(card) : [h("div", { class: "muted" }, "没有符合条件的短片")]));
    };
    q.addEventListener("input", () => { f.q = q.value; draw(); });
    st.addEventListener("change", () => { f.status = st.value; draw(); });
    draw();
    box.className = "";
    box.replaceChildren(h("div", { class: "row", style: "margin-bottom:12px" }, S.cfg.user ? whoChips : null, st, q), grid);
  } catch (e) { fail(e); }
  function card(p) {
    return (h("a", { class: "pcard", href: `#/p/${p.id}` },
      h("div", { class: "row spread" }, badge(P_STATUS, p.status), h("span", { class: "muted small" }, p.ratio)),
      h("div", { class: "t" }, p.title),
      h("div", { class: "muted small" }, `${p.created_at.slice(5, 16)} · ${p.shots} 个镜头 · 已花 ${usd(p.cost.spent_usd)}`, p.owner ? ` · ${p.owner}` : ""),
    ));
  }
}

function newForm(templates = [], assets = []) {
  const ratios = S.cfg.ratios;
  let ratio = "9:16", duration = 30, tpl = null;
  const picked = new Set();
  const ratioChips = h("div", { class: "chips" });
  const durChips = h("div", { class: "chips" });
  const drawChips = () => {
    ratioChips.replaceChildren(...ratios.map((r) => h("button", { type: "button", class: "chip" + (r === ratio ? " on" : ""), onclick: () => { ratio = r; drawChips(); } }, r)));
    durChips.replaceChildren(...[15, 30, 45, 60].map((d) => h("button", { type: "button", class: "chip" + (d === duration ? " on" : ""), onclick: () => { duration = d; drawChips(); } }, `${d} 秒`)));
  };
  drawChips();
  const refsPreview = h("div", { class: "refs" });
  const fileInput = h("input", { type: "file", name: "refs", accept: "image/png,image/jpeg,image/webp", multiple: true,
    onchange: () => refsPreview.replaceChildren(...[...fileInput.files].slice(0, 4).map((f) => h("img", { src: URL.createObjectURL(f), alt: f.name }))) });
  const tplInfo = h("div", { class: "muted small" });
  const tplSelect = h("select", {}, h("option", { value: "" }, "不使用模板"), templates.map((x) => h("option", { value: x.id }, x.name)));
  const applyTpl = () => {
    tpl = templates.find((x) => String(x.id) === tplSelect.value) || null;
    if (tpl) {
      ratio = tpl.ratio; duration = [15, 30, 45, 60].includes(tpl.duration) ? tpl.duration : duration;
      style.value = tpl.style; drawChips();
      tplInfo.replaceChildren(
        tpl.characters.length ? h("div", {}, "固定角色：", tpl.characters.map((c) => `${c.name}（${c.appearance}）`).join("；")) : null,
        tpl.assets.length ? h("div", {}, "自带素材：", tpl.assets.map((a) => a.name).join("、")) : null);
      if (!desc.value.trim() && tpl.description) desc.placeholder = "参考：" + tpl.description;
    } else tplInfo.replaceChildren();
    drawAssets();
  };
  tplSelect.addEventListener("change", applyTpl);
  const assetGrid = h("div", { class: "pick-grid" });
  const drawAssets = () => {
    const fromTpl = new Set(tpl ? tpl.asset_ids : []);
    assetGrid.replaceChildren(...assets.map((a) => {
      const on = picked.has(a.id) || fromTpl.has(a.id);
      return h("button", { type: "button", class: "pick" + (on ? " on" : ""), title: `${a.kind} · ${a.name}`, disabled: fromTpl.has(a.id),
        onclick: () => { picked.has(a.id) ? picked.delete(a.id) : picked.add(a.id); drawAssets(); } },
        h("img", { src: a.url, alt: a.name, loading: "lazy" }), h("span", {}, a.name));
    }));
  };
  drawAssets();
  if (S.pendingTemplate) { setTimeout(() => { tplSelect.value = String(S.pendingTemplate); S.pendingTemplate = null; applyTpl(); }, 0); }
  const desc = h("textarea", { name: "description", rows: 3, placeholder: "例如：社区咖啡店的清晨，店主小林开门、做第一杯拿铁、递给熟客，温暖治愈" });
  const style = h("input", { type: "text", name: "style", placeholder: "可选，例如：日系胶片感、暖色调" });
  const submit = h("button", { class: "primary", type: "submit" }, "生成脚本");
  const warn = [];
  if (!S.cfg.demo && (!S.cfg.keys.deepseek || !S.cfg.keys.ark)) warn.push("服务器未配置 API Key，生成会失败。");
  if (!S.cfg.demo && !S.cfg.priced) warn.push("未配置模型单价，花费预估与 100 USD 熔断不生效。");
  return h("form", { class: "panel stack", onsubmit: async (ev) => {
      ev.preventDefault();
      const fd = new FormData();
      fd.set("description", desc.value.trim()); fd.set("style", style.value.trim());
      fd.set("ratio", ratio); fd.set("duration", duration);
      for (const f of [...fileInput.files].slice(0, 4)) fd.append("refs", f);
      if (tpl) fd.set("template_id", tpl.id);
      if (picked.size) fd.set("asset_ids", [...picked].join(","));
      submit.disabled = true;
      try { const p = await api("/api/projects", { method: "POST", form: fd }); location.hash = `#/p/${p.id}`; }
      catch (e) { fail(e); submit.disabled = false; }
    } },
    h("h2", {}, "新建短片"),
    warn.length ? h("div", { class: "warnline" }, warn.join(" ")) : null,
    templates.length ? h("div", { class: "grid2" }, h("div", {}, h("label", {}, "风格模板"), tplSelect), h("div", { style: "align-self:end" }, tplInfo)) : null,
    h("div", {}, h("label", {}, "场景描述"), desc),
    h("div", { class: "grid2" },
      h("div", {}, h("label", {}, "画幅"), ratioChips),
      h("div", {}, h("label", {}, "目标时长"), durChips)),
    h("div", {}, h("label", {}, "风格"), style),
    assets.length ? h("div", {}, h("label", {}, "从素材库选参考图"), assetGrid) : null,
    h("div", {}, h("label", {}, "上传参考图（角色 / 产品 / 风格；与素材库合计最多 4 张）"), fileInput, refsPreview),
    h("div", { class: "row spread" }, h("span", { class: "muted small" }, `单片花费上限 $${S.cfg.budget_usd}`), submit),
  );
}

// ---------- 项目页 ----------
async function projectPage(pid) {
  $("#app").replaceChildren(h("div", { id: "phead" }), h("div", { id: "pbanner" }), h("div", { id: "ptabs" }), h("div", { id: "pbody" }));
  openEvents(pid);
  await refresh(pid);
}

// 实时推送：服务端有变化就推 update；连接失败时退回 2 秒轮询
function openEvents(pid) {
  closeEvents();
  if (!window.EventSource) return;
  S.sseOk = false;
  let pending = null;
  const es = new EventSource(`/api/projects/${pid}/events`);
  es.onopen = () => { S.sseOk = true; };
  es.addEventListener("update", () => {
    clearTimeout(pending);
    pending = setTimeout(() => { if (location.hash.includes(pid)) refresh(pid); }, 150);
  });
  es.onerror = () => { S.sseOk = false; };
  S.es = es;
}
function closeEvents() { if (S.es) { S.es.close(); S.es = null; } S.sseOk = false; }

async function refresh(pid) {
  clearTimeout(S.pollTimer);
  try { S.project = await api(`/api/projects/${pid}`); }
  catch (e) { $("#app").replaceChildren(h("div", { class: "empty" }, e.message, h("p", {}, h("a", { href: "#/" }, "返回首页")))); return; }
  if (!location.hash.includes(pid)) return;
  drawProject();
  if (S.project.job && !S.sseOk) S.pollTimer = setTimeout(() => refresh(pid), 2000);
}

function defaultTab(p) {
  if (["imaging", "image_review", "image_selected"].includes(p.status)) return "images";
  if (["generating", "shot_review"].includes(p.status)) return "videos";
  if (["rendering", "done"].includes(p.status)) return "final";
  return "script";
}

function drawProject() {
  const p = S.project;
  if (!S.tab) S.tab = defaultTab(p);
  const busy = !!p.job || !p.can_edit;
  // 头部
  const c = p.cost, pct = Math.min(100, (c.spent_usd / c.budget_usd) * 100);
  const meterCls = "meter" + (c.spent_usd >= c.budget_usd ? " over" : c.spent_usd >= S.cfg.budget_warn_usd ? " warn" : "");
  $("#phead").replaceChildren(h("div", { class: "panel" },
    h("div", { class: "row spread" },
      h("div", { class: "grow" },
        h("div", { class: "row small" }, h("a", { href: "#/" }, "← 全部短片"),
          h("button", { class: "ghost small", onclick: duplicate }, "复制"),
          p.can_edit ? h("button", { class: "ghost small danger", disabled: !!p.job, onclick: remove }, "删除") : null),
        h("div", { class: "row", style: "margin-top:6px" }, h("h1", {}, p.title), badge(P_STATUS, p.status))),
      h("div", { style: "min-width:220px" },
        h("div", { class: "row spread small" }, h("span", {}, `已花 ${usd(c.spent_usd)}`), h("span", { class: "muted" }, `上限 ${usd(c.budget_usd)}`)),
        h("div", { class: meterCls }, h("span", { style: `width:${pct}%` }), h("i", { style: `left:${(S.cfg.budget_warn_usd / c.budget_usd) * 100}%` })),
        p.estimate ? h("div", { class: "muted small", style: "margin-top:4px" },
          `整片预估 ${usd(p.estimate.total_usd)}`, p.estimate.priced ? "" : "（未配置单价）") : null)),
    h("details", { style: "margin-top:10px" }, h("summary", {}, "需求与参考图"),
      h("p", {}, p.brief.description), h("p", { class: "muted small" }, `${p.ratio} · 约 ${p.brief.duration} 秒`, p.brief.style ? ` · ${p.brief.style}` : ""),
      h("div", { class: "refs" }, p.brief.refs.map((u) => h("img", { src: u, alt: "参考图" })))),
  ));
  // 任务横幅
  const j = p.job || (p.last_job && p.last_job.status === "failed" ? p.last_job : null);
  $("#pbanner").replaceChildren(!p.can_edit ? h("div", { class: "banner" }, h("div", {}, `只读：这是 ${p.owner} 的项目，只有本人或管理员可以修改。可以另存为模板或把场景图存入素材库。`)) : "", j ? h("div", { class: "banner" + (j.status === "failed" ? " bad" : "") },
    j.status === "failed" ? null : h("div", { class: "spin" }),
    h("div", { class: "grow" },
      h("div", { class: "row spread" },
        h("strong", {}, j.status === "failed" ? `${j.label}失败：${j.error || ""}` : `${j.label}${j.status === "queued" ? "（排队中）" : "中…"}`),
        j.status !== "failed" && p.can_edit ? h("button", { class: "ghost small danger", onclick: () => cancelJob(j.id) }, "取消") : null),
      h("pre", {}, (j.log || "").trim().split("\n").slice(-6).join("\n")))) : "");
  // 标签页
  const shots = p.shots, nSel = shots.filter((s) => s.state.selected).length, nVid = shots.filter((s) => s.state.video).length;
  const tabs = [["script", "① 脚本", shots.length ? `${shots.length} 镜` : ""], ["images", "② 场景图", shots.length ? `${nSel}/${shots.length}` : ""],
    ["videos", "③ 视频", shots.length ? `${nVid}/${shots.length}` : ""], ["final", "④ 成片", p.final ? "✓" : ""]];
  $("#ptabs").replaceChildren(h("div", { class: "tabs" }, tabs.map(([k, t, n]) =>
    h("button", { class: "tab" + (S.tab === k ? " on" : ""), onclick: () => { S.tab = k; drawProject(); } }, t, n ? h("span", { class: "n" }, n) : null))));
  // 主体（编辑中的脚本不被轮询覆盖；内容没变不重绘，避免视频重新加载）
  const sig = JSON.stringify([S.tab, busy, p.shots, p.final, p.script, p.has_bgm, p.status]);
  if (S.tab === "script" && S.dirty && S.bodyTab === "script") return;
  if (sig === S.bodySig) return;
  S.bodySig = sig; S.bodyTab = S.tab;
  const body = { script: scriptTab, images: imagesTab, videos: videosTab, final: finalTab }[S.tab](p, busy);
  $("#pbody").replaceChildren(body);
}

async function cancelJob(id) {
  if (!confirm("取消这个任务？已经完成的图和视频会保留；正在生成的视频会尝试在方舟侧取消（已开始生成的可能仍会计费）。")) return;
  try {
    const r = await api(`/api/jobs/${id}/cancel`, { method: "POST" });
    toast(r.status === "cancelled" ? "已取消" : "正在停止，当前步骤完成后结束");
    await refresh(S.project.id);
  } catch (e) { fail(e); }
}

async function duplicate() {
  const p = S.project;
  if (!confirm("复制这部短片的需求和脚本（不复制图和视频），用来做变体？")) return;
  try { const n = await api(`/api/projects/${p.id}/duplicate`, { method: "POST" }); toast("已复制"); location.hash = `#/p/${n.id}`; }
  catch (e) { fail(e); }
}
async function remove() {
  const p = S.project;
  if (!confirm(`删除「${p.title}」？会移到服务器回收站（data/trash），管理员可找回。`)) return;
  try { await api(`/api/projects/${p.id}`, { method: "DELETE" }); toast("已删除"); location.hash = "#/"; }
  catch (e) { fail(e); }
}

async function startJob(kind, extra = {}) {
  const p = S.project;
  try { await api(`/api/projects/${p.id}/jobs`, { method: "POST", body: { kind, ...extra } }); toast("已开始"); await refresh(p.id); }
  catch (e) { fail(e); }
}

// ---------- ① 脚本 ----------
function scriptTab(p, busy) {
  if (!p.script) {
    return h("div", { class: "panel empty" }, busy ? "DeepSeek 正在写脚本，通常需要 20–60 秒…" : "还没有脚本",
      !busy ? h("p", {}, h("button", { class: "primary", onclick: () => startJob("script") }, "生成脚本")) : null);
  }
  if (!S.draft) S.draft = JSON.parse(JSON.stringify({ ...p.script, shots: p.shots.map(({ state, index, ...s }) => s) }));
  const d = S.draft, dis = busy;
  const touch = () => { if (!S.dirty) { S.dirty = true; saveBtn.disabled = false; } };
  const field = (obj, key, el) => { el.value = obj[key] ?? ""; el.disabled = dis; el.addEventListener("input", () => { obj[key] = el.value; touch(); }); return el; };
  const ta = (rows) => h("textarea", { rows });
  const redraw = () => { const sig = S.dirty; $("#pbody").replaceChildren(scriptTab(p, busy)); S.dirty = sig; if (sig) saveBtnRef().disabled = false; };

  const chars = h("div", { class: "stack" }, d.characters.map((ch, i) => h("div", { class: "grid3" },
    h("div", { class: "stack" }, h("div", {}, h("label", {}, "角色名"), field(ch, "name", h("input", { type: "text" }))),
      h("div", {}, h("label", {}, "配音音色（可选，台词用）"), field(ch, "voice", h("input", { type: "text", placeholder: "voice_type" })))),
    h("div", { style: "grid-column: span 2" }, h("label", { class: "row spread" }, "固定外观描述",
      dis ? null : h("button", { class: "ghost danger small", type: "button", onclick: () => { d.characters.splice(i, 1); touch(); redraw(); } }, "删除")),
      field(ch, "appearance", ta(2))))));

  const shotCards = d.shots.map((s, i) => {
    const st = (p.shots.find((x) => x.id === s.id) || {}).state;
    const dur = h("select", {}, S.cfg.durations.map((v) => h("option", { value: v, selected: Number(s.duration) === v }, `${v} 秒`)));
    dur.disabled = dis; dur.addEventListener("change", () => { s.duration = Number(dur.value); touch(); });
    const charsInput = h("input", { type: "text", placeholder: "用逗号分隔，需与上方角色名一致" });
    charsInput.value = (s.characters || []).join("，"); charsInput.disabled = dis;
    charsInput.addEventListener("input", () => { s.characters = charsInput.value.split(/[,，、\s]+/).filter(Boolean); touch(); });
    const move = (delta) => { const j = i + delta; if (j < 0 || j >= d.shots.length) return; [d.shots[i], d.shots[j]] = [d.shots[j], d.shots[i]]; touch(); redraw(); };
    return h("div", { class: "shot" },
      h("div", { class: "shot-head" }, h("span", { class: "num" }, i + 1),
        h("div", { class: "grow" }, field(s, "description", h("input", { type: "text", placeholder: "镜头简述" }))),
        st ? badge(S_STATUS, st.status) : h("span", { class: "badge" }, "新镜头"),
        dis ? null : [h("button", { class: "ghost", type: "button", title: "上移", onclick: () => move(-1) }, "↑"),
          h("button", { class: "ghost", type: "button", title: "下移", onclick: () => move(1) }, "↓"),
          h("button", { class: "ghost danger", type: "button", onclick: () => { if (d.shots.length > 1) { d.shots.splice(i, 1); touch(); redraw(); } } }, "删除")]),
      h("div", { class: "grid2" },
        h("div", {}, h("label", {}, "场景图提示词（静态画面）"), field(s, "image_prompt", ta(3))),
        h("div", {}, h("label", {}, "视频提示词（怎么动）"), field(s, "video_prompt", ta(3)))),
      h("div", { class: "grid3", style: "margin-top:10px" },
        h("div", {}, h("label", {}, "时长"), dur),
        h("div", {}, h("label", {}, "出场角色"), charsInput),
        h("div", {}, h("label", {}, "旁白 / 台词（也作字幕）"), field(s, "narration", h("input", { type: "text" })),
          d.characters.length ? speakerSel(s) : null)),
      i < d.shots.length - 1 ? h("label", { class: "check" }, linkBox(s),
        "平滑过渡到下一镜（用下一镜的场景图做尾帧，适合同一场景的连续动作）") : null);
  });
  function speakerSel(s) {
    const sel = h("select", { style: "margin-top:6px" }, h("option", { value: "" }, "说话人：画外旁白"),
      d.characters.filter((c) => c.name).map((c) => h("option", { value: c.name, selected: s.speaker === c.name }, `说话人：${c.name}`)));
    sel.disabled = dis;
    sel.addEventListener("change", () => { s.speaker = sel.value; touch(); });
    return sel;
  }
  function linkBox(s) {
    const cb = h("input", { type: "checkbox", checked: !!s.link_next, disabled: dis });
    cb.addEventListener("change", () => { s.link_next = cb.checked; touch(); });
    return cb;
  }

  const saveBtn = h("button", { id: "saveBtn", disabled: true, onclick: () => save(false) }, "保存");
  const saveBtnRef = () => $("#saveBtn") || saveBtn;
  const total = d.shots.reduce((a, s) => a + Number(s.duration || 0), 0);
  async function saveTemplate() {
    if (S.dirty) return toast("先保存脚本修改，再另存为模板", true);
    const name = prompt("模板名称", p.title);
    if (!name) return;
    try { await api("/api/templates", { method: "POST", body: { from_project: p.id, name } }); toast("已存为模板，新建短片时可选用"); }
    catch (e) { fail(e); }
  }
  async function save(thenImages) {
    try {
      if (S.dirty) {
        S.project = await api(`/api/projects/${p.id}/script`, { method: "PUT", body: S.draft });
        S.dirty = false; S.draft = null; S.bodySig = "";
        toast("已保存");
      }
      if (thenImages) { S.tab = "images"; await startJob("images"); } else drawProject();
    } catch (e) { fail(e); }
  }
  return h("div", {},
    h("div", { class: "panel stack" },
      h("div", { class: "row spread" }, h("h2", {}, "整体设定"), h("div", { class: "row" },
        h("span", { class: "muted small" }, `${d.shots.length} 个镜头 · 共 ${total} 秒`),
        h("button", { class: "ghost", type: "button", onclick: saveTemplate }, "另存为模板"))),
      h("div", {}, h("label", {}, "标题"), field(d, "title", h("input", { type: "text" }))),
      h("div", {}, h("label", {}, "全片风格（拼接到每张场景图提示词前）"), field(d, "style", ta(2))),
      h("div", {}, h("label", {}, "角色"), chars,
        dis ? null : h("button", { class: "ghost", type: "button", onclick: () => { d.characters.push({ name: "", appearance: "" }); touch(); redraw(); } }, "+ 添加角色"))),
    h("h2", {}, "分镜"),
    shotCards,
    dis ? null : h("button", { class: "ghost", type: "button", style: "margin-top:10px",
      onclick: () => { d.shots.push({ description: "", characters: [], image_prompt: "", video_prompt: "", duration: S.cfg.durations[0], narration: "" }); touch(); redraw(); } }, "+ 添加镜头"),
    h("div", { class: "actions" },
      h("button", { disabled: dis, onclick: () => { if (confirm("重新生成会覆盖当前脚本和所有素材记录，确定？")) startJob("script"); } }, "重新生成脚本"),
      saveBtn,
      h("button", { class: "primary", disabled: dis, onclick: () => save(true) }, "保存并生成场景图")));
}

// ---------- ② 场景图 ----------
function imagesTab(p, busy) {
  if (!p.shots.length) return h("div", { class: "panel empty" }, "先完成脚本");
  const allSel = p.shots.every((s) => s.state.selected);
  const anyCand = p.shots.some((s) => s.state.candidates.length);
  const pick = async (s, n) => {
    try { S.project = await api(`/api/projects/${p.id}/select`, { method: "POST", body: { shot_id: s.id, candidate: n } }); S.bodySig = ""; drawProject(); }
    catch (e) { fail(e); }
  };
  const rows = p.shots.map((s) => {
    const st = s.state;
    const cands = st.candidates.length ? st.candidates.map((u, i) =>
      h("button", { class: "cand" + (st.selected === i + 1 ? " on" : ""), style: `aspect-ratio:${ratioCss(p.ratio)}`, disabled: busy,
        title: st.refined && st.refined[i] ? `修改：${st.refined[i]}` : `候选 ${i + 1}`, onclick: () => pick(s, i + 1) },
        h("img", { src: u, loading: "lazy", alt: `第 ${s.index} 镜候选 ${i + 1}` }),
        st.refined && st.refined[i] ? h("span", { class: "tag" }, "改") : null))
      : [h("div", { class: "placeholder", style: `aspect-ratio:${ratioCss(p.ratio)}` }, st.status === "imaging" ? "出图中…" : "未出图")];
    return h("div", { class: "shot" },
      h("div", { class: "shot-head" }, h("span", { class: "num" }, s.index), h("strong", { class: "grow" }, s.description || "（无简述）"),
        badge(S_STATUS, st.status),
        h("button", { disabled: busy, onclick: () => startJob("images", { shots: [s.index] }) }, st.candidates.length ? "重出这一镜" : "出图")),
      h("div", { class: "muted small", style: "margin-bottom:8px" }, s.image_prompt),
      st.image_stale ? h("div", { class: "warnline" }, "提示词已修改，建议重出这一镜") : null,
      st.error ? h("div", { class: "errline" }, st.error) : null,
      h("div", { class: "cands" }, cands),
      st.selected ? refineBox(s) : null);
  });
  function refineBox(s) {
    const input = h("input", { type: "text", placeholder: "例如：人物换成侧脸、背景加一盏暖色吊灯、去掉桌上的杯子", disabled: busy });
    const go = () => {
      const v = input.value.trim();
      if (v.length < 2) return toast("写一下要怎么改", true);
      startJob("refine", { shot: s.index, candidate: s.state.selected, instruction: v });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    return h("div", { class: "row", style: "margin-top:10px" },
      h("div", { class: "grow" }, input),
      h("button", { disabled: busy, onclick: go }, "基于选中图修改"),
      h("button", { class: "ghost small", onclick: () => saveAsset(s) }, "存入素材库"));
  }
  async function saveAsset(s) {
    const name = prompt("素材名称", s.description || `第 ${s.index} 镜`);
    if (!name) return;
    const kind = prompt(`类型（${S.cfg.asset_kinds.join(" / ")}）`, "场景");
    if (!kind) return;
    try { await api(`/api/projects/${p.id}/save-asset`, { method: "POST", body: { shot_id: s.id, name, kind } }); toast("已存入素材库"); }
    catch (e) { fail(e); }
  }
  return h("div", {},
    rows,
    h("div", { class: "actions" },
      h("button", { disabled: busy, onclick: () => startJob("images") }, anyCand ? "补齐未出图的镜头" : "生成全部场景图"),
      h("button", { class: "primary", disabled: busy || !allSel,
        title: allSel ? "" : "每个镜头都选好图后才能生成视频",
        onclick: () => { S.tab = "videos"; startJob("videos"); } }, allSel ? "生成镜头视频" : `还有 ${p.shots.filter((s) => !s.state.selected).length} 镜未选图`)));
}

// ---------- ③ 视频 ----------
function videosTab(p, busy) {
  if (!p.shots.length) return h("div", { class: "panel empty" }, "先完成脚本");
  const allVid = p.shots.every((s) => s.state.video);
  const rows = p.shots.map((s) => {
    const st = s.state;
    const frame = st.selected ? st.candidates[st.selected - 1] : null;
    return h("div", { class: "shot" },
      h("div", { class: "shot-head" }, h("span", { class: "num" }, s.index), h("strong", { class: "grow" }, s.description || "（无简述）"),
        s.link_next && s.index < p.shots.length ? h("span", { class: "badge", title: "首尾帧过渡" }, `→ 过渡到第 ${s.index + 1} 镜`) : null,
        h("span", { class: "muted small" }, `${s.duration} 秒`), badge(S_STATUS, st.status),
        h("button", { disabled: busy || !st.selected, onclick: () => startJob("videos", { shots: [s.index] }) }, st.video ? "重生成" : "生成")),
      h("div", { class: "row", style: "align-items:flex-start" },
        st.video ? h("video", { class: "vid", src: st.video, controls: true, playsinline: true, preload: "metadata", poster: frame || undefined })
          : frame ? h("img", { class: "vid", src: frame, alt: "首帧", style: "background:#eee" })
            : h("div", { class: "placeholder", style: `aspect-ratio:${ratioCss(p.ratio)}` }, "未选图"),
        h("div", { class: "grow small" },
          h("div", { class: "muted" }, "视频提示词"), h("div", {}, s.video_prompt),
          s.narration ? [h("div", { class: "muted", style: "margin-top:8px" }, "旁白"), h("div", {}, s.narration)] : null,
          st.tasks > 1 ? h("div", { class: "muted", style: "margin-top:8px" }, `已生成 ${st.tasks} 次`) : null,
          st.error ? h("div", { class: "errline", style: "margin-top:8px" }, st.error) : null)));
  });
  return h("div", {}, rows, h("div", { class: "actions" },
    h("button", { disabled: busy || allVid, onclick: () => startJob("videos") }, "补齐缺失的镜头"),
    h("button", { class: "primary", disabled: busy || !allVid, onclick: () => { S.tab = "final"; S.bodySig = ""; drawProject(); } }, "去合成成片")));
}

// ---------- ④ 成片 ----------
function finalTab(p, busy) {
  const allVid = p.shots.length && p.shots.every((s) => s.state.video);
  const bgmInput = h("input", { type: "file", accept: ".mp3,.m4a,.aac,.wav" });
  const upload = async () => {
    if (!bgmInput.files.length) return toast("先选择音频文件", true);
    const fd = new FormData(); fd.set("bgm", bgmInput.files[0]);
    try { S.project = await api(`/api/projects/${p.id}/bgm`, { method: "POST", form: fd }); toast("BGM 已上传"); S.bodySig = ""; drawProject(); }
    catch (e) { fail(e); }
  };
  const hasNarr = p.shots.some((s) => (s.narration || "").trim());
  const ttsOn = h("input", { type: "checkbox", checked: S.cfg.tts.enabled && hasNarr, disabled: !S.cfg.tts.enabled || !hasNarr || busy });
  const voice = h("input", { type: "text", value: S.cfg.tts.voice, disabled: !S.cfg.tts.enabled || busy, placeholder: "音色 voice_type" });
  const meta = p.final_meta;
  const burn = h("input", { type: "checkbox", checked: meta ? !!meta.burn_subs : false, disabled: !hasNarr || busy });
  const fade = h("input", { type: "checkbox", checked: meta ? !!meta.transition : false, disabled: busy || p.shots.length < 2 });
  const compose = () => startJob("compose", { bgm: p.has_bgm, tts: ttsOn.checked, voice: voice.value.trim(), burn_subs: burn.checked,
    transition: fade.checked ? 0.5 : 0 });
  const others = S.cfg.ratios.filter((r) => r !== p.ratio);
  const exportPanel = p.final ? h("div", { class: "panel stack" },
    h("h2", {}, "导出其他画幅"),
    h("div", { class: "muted small" }, "不重新生成：原画面完整保留，空白处用同一画面放大模糊填充。适合把竖屏片发到横屏平台。"),
    h("div", { class: "row" }, others.map((r) => p.exports && p.exports[r]
      ? h("a", { href: p.exports[r], download: `${p.title}_${r.replace(":", "x")}.mp4` }, h("button", {}, `下载 ${r}`))
      : h("button", { disabled: busy, onclick: () => startJob("export", { ratio: r }) }, `导出 ${r}`)))) : null;
  return h("div", {},
    h("div", { class: "panel" },
      meta && p.final ? h("div", { class: "muted small", style: "text-align:center;margin-bottom:8px" },
        [meta.tts ? `旁白配音（${meta.voice}）` : "无配音", meta.bgm ? "含 BGM" : "无 BGM", meta.burn_subs ? "字幕已烧录" : "软字幕",
          meta.transition ? "淡入淡出转场" : "硬切"].join(" · ")) : null,
      p.final ? [h("video", { class: "final", src: p.final, controls: true, playsinline: true }),
        h("div", { class: "row", style: "justify-content:center;margin-top:12px" },
          h("a", { href: p.final, download: `${p.title}.mp4` }, h("button", { class: "primary" }, "下载成片")),
          h("a", { href: p.final.replace(/\.mp4$/, ".srt"), download: `${p.title}.srt` }, h("button", {}, "下载字幕")))]
        : h("div", { class: "empty" }, busy ? "处理中…" : allVid ? "所有镜头已就绪，可以合成成片" : "还有镜头没有视频，先到「③ 视频」补齐")),
    h("div", { class: "panel stack" },
      h("h2", {}, "旁白配音"),
      !S.cfg.tts.enabled ? h("div", { class: "muted small" }, "服务器未配置 TTS_APPID / TTS_TOKEN，旁白只作为字幕。")
        : !hasNarr ? h("div", { class: "muted small" }, "脚本里没有旁白，可在「① 脚本」里填写。")
          : h("div", { class: "muted small" }, "用火山引擎语音合成为每镜旁白配音，按镜头起点对齐；同样的文字和音色只合成一次。"),
      h("label", { class: "check" }, ttsOn, "合成时加入旁白配音（设了说话人的镜头用该角色的音色）"),
      h("label", { class: "check" }, burn, "把字幕烧进画面（抖音、视频号等平台不显示软字幕时勾选）"),
      h("div", { style: "max-width:360px" }, h("label", {}, "音色（voice_type，见火山引擎音色列表）"), voice)),
    h("div", { class: "panel stack" },
      h("h2", {}, "剪辑"),
      h("label", { class: "check" }, fade, "镜头之间 0.5 秒淡入淡出（成片会比镜头总时长略短，字幕和配音自动对齐）")),
    exportPanel,
    h("div", { class: "panel stack" },
      h("h2", {}, "背景音乐（可选）"),
      h("div", { class: "muted small" }, p.has_bgm ? "已上传 BGM，合成时混入（有配音时自动压低到 25%）" : "不上传则只有配音或无声"),
      h("div", { class: "row" }, bgmInput, h("button", { onclick: upload, disabled: busy }, "上传"))),
    h("div", { class: "actions" },
      h("button", { class: "primary", disabled: busy || !allVid, onclick: compose }, p.final ? "重新合成" : "合成成片")));
}

// ---------- 成本看板 ----------
async function statsPage() {
  const app = $("#app");
  app.replaceChildren(h("div", { class: "muted" }, "加载中…"));
  let d;
  try { d = await api("/api/stats"); } catch (e) { return fail(e); }
  const t = d.totals;
  const tile = (label, value, note) => h("div", { class: "tile" }, h("div", { class: "muted small" }, label), h("div", { class: "big" }, value), note ? h("div", { class: "muted small" }, note) : null);
  const table = (cols, rows) => h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, cols.map(([, label, cls]) => h("th", { class: cls || "" }, label)))),
    h("tbody", {}, rows.length ? rows.map((r) => h("tr", {}, cols.map(([key, , cls, fmt]) => h("td", { class: cls || "" }, fmt ? fmt(r[key], r) : r[key]))))
      : h("tr", {}, h("td", { colspan: cols.length, class: "muted" }, "暂无数据"))));
  const maxMonth = Math.max(1, ...d.by_month.map((m) => m.spent_usd));
  const KIND = { script: "脚本 DeepSeek", image: "场景图 Seedream", video: "视频 Seedance", tts: "配音 TTS" };
  const rate = (r) => (r.total ? Math.round((r.done / r.total) * 100) + "%" : "–");
  app.replaceChildren(...[
    h("div", { class: "row spread", style: "margin-bottom:12px" }, h("h1", {}, "成本看板"), h("a", { href: "#/" }, "← 返回")),
    d.priced ? null : h("div", { class: "warnline", style: "margin-bottom:12px" }, "未配置模型单价，以下花费偏低，仅供参考。"),
    h("div", { class: "tiles" },
      tile("累计花费", usd(t.spent_usd), `¥${t.spent_cny}`),
      tile("短片", `${t.projects} 部`, `已完成 ${t.done} 部`),
      tile("每部成片平均", t.avg_usd_per_done == null ? "–" : usd(t.avg_usd_per_done), `上限 ${usd(d.budget_usd)}`),
      tile("本月花费", usd(d.month.spent_usd), d.month.team_budget_usd ? `团队月预算 ${usd(d.month.team_budget_usd)}${d.month.warn ? " · 已超 80%" : ""}` : "未设团队月预算"),
      tile("视频重生成", t.video_shots ? `${Math.round((t.video_regens / t.video_shots) * 100)}%` : "–", `${t.video_regens} 次 / ${t.video_shots} 个镜头`)),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", {}, "按月"), d.by_month.length ? d.by_month.map((m) =>
        h("div", { class: "bar-row" }, h("span", { class: "bar-label" }, m.month),
          h("span", { class: "bar" }, h("i", { style: `width:${(m.spent_usd / maxMonth) * 100}%` })), h("span", { class: "bar-val" }, usd(m.spent_usd))))
        : h("div", { class: "muted" }, "暂无数据")),
      h("div", { class: "panel" }, h("h2", {}, "按模型"), table([["kind", "类别", "", (k) => KIND[k] || k], ["spent_usd", "花费", "r", usd]], d.by_kind))),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", {}, "按成员"), table([["user", "成员"], ["projects", "短片", "r"], ["spent_usd", "花费", "r", usd]], d.by_user)),
      h("div", { class: "panel" }, h("h2", {}, "任务成功率"), table([["label", "任务"], ["total", "次数", "r"], ["done", "成功率", "r", (_v, r) => rate(r)],
        ["avg_s", "平均耗时", "r", (v) => (v == null ? "–" : v < 90 ? `${v} 秒` : `${(v / 60).toFixed(1)} 分`)]], d.jobs))),
    h("div", { class: "panel" }, h("h2", {}, "花费最多的短片"), table([["title", "短片", "", (v, r) => h("a", { href: `#/p/${r.id}` }, v)], ["owner", "成员"],
      ["status", "状态", "", (v) => badge(P_STATUS, v)], ["spent_usd", "花费", "r", usd]], d.top_projects))].filter(Boolean));
}

// ---------- 模板库 ----------
async function templatesPage() {
  const app = $("#app");
  app.replaceChildren(h("div", { class: "muted" }, "加载中…"));
  let list;
  try { list = await api("/api/templates"); } catch (e) { return fail(e); }
  const mine = (x) => S.cfg.role === "admin" || !x.owner || x.owner === S.cfg.user;
  app.replaceChildren(
    h("div", { class: "row spread", style: "margin-bottom:12px" }, h("h1", {}, "风格模板"), h("a", { href: "#/" }, "← 返回")),
    h("p", { class: "muted small" }, "模板保存全片风格、固定角色外观、画幅时长和参考素材。在项目「① 脚本」里点“另存为模板”即可创建；同系列短片套用同一模板，角色外观保持一致。"),
    list.length ? h("div", { class: "plist" }, list.map((x) => h("div", { class: "pcard stack" },
      h("div", { class: "row spread" }, h("strong", {}, x.name), h("span", { class: "muted small" }, `${x.ratio} · ${x.duration} 秒`)),
      x.style ? h("div", { class: "small" }, x.style) : null,
      x.characters.length ? h("div", { class: "muted small" }, x.characters.map((c) => `${c.name}：${c.appearance}`).join("；")) : null,
      x.assets.length ? h("div", { class: "refs" }, x.assets.map((a) => h("img", { src: a.url, alt: a.name, title: a.name }))) : null,
      h("div", { class: "row spread" }, h("span", { class: "muted small" }, x.owner || ""),
        h("div", { class: "row" },
          mine(x) ? h("button", { class: "ghost danger", onclick: async () => {
            if (!confirm(`删除模板「${x.name}」？`)) return;
            try { await api(`/api/templates/${x.id}`, { method: "DELETE" }); templatesPage(); } catch (e) { fail(e); } } }, "删除") : null,
          h("button", { class: "primary", onclick: () => { S.pendingTemplate = x.id; location.hash = "#/"; } }, "用它新建"))))))
      : h("div", { class: "panel empty" }, "还没有模板"));
}

// ---------- 素材库 ----------
async function libraryPage() {
  const app = $("#app");
  app.replaceChildren(h("div", { class: "muted" }, "加载中…"));
  let list;
  try { list = await api("/api/assets"); } catch (e) { return fail(e); }
  let filter = "全部";
  const grid = h("div", { class: "lib-grid" });
  const chips = h("div", { class: "chips" });
  const mine = (x) => S.cfg.role === "admin" || !x.owner || x.owner === S.cfg.user;
  const draw = () => {
    chips.replaceChildren(...["全部", ...S.cfg.asset_kinds].map((k) => h("button", { class: "chip" + (k === filter ? " on" : ""), onclick: () => { filter = k; draw(); } }, k)));
    const items = list.filter((x) => filter === "全部" || x.kind === filter);
    grid.replaceChildren(...(items.length ? items.map((x) => h("div", { class: "lib-item" },
      h("img", { src: x.url, alt: x.name, loading: "lazy" }),
      h("div", { class: "row spread", style: "padding:8px 10px" },
        h("div", {}, h("div", { class: "small" }, x.name), h("div", { class: "muted small" }, `${x.kind} · ${x.owner || ""}`)),
        mine(x) ? h("button", { class: "ghost danger", onclick: async () => {
          if (!confirm(`删除素材「${x.name}」？已用它的项目不受影响。`)) return;
          try { await api(`/api/assets/${x.id}`, { method: "DELETE" }); libraryPage(); } catch (e) { fail(e); } } }, "删除") : null)))
      : [h("div", { class: "muted" }, "暂无素材")]));
  };
  const name = h("input", { type: "text", placeholder: "例如：店主小林正面" });
  const kind = h("select", {}, S.cfg.asset_kinds.map((k) => h("option", { value: k }, k)));
  const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp" });
  const upload = async (ev) => {
    ev.preventDefault();
    if (!file.files.length) return toast("请选择图片", true);
    const fd = new FormData(); fd.set("name", name.value.trim()); fd.set("kind", kind.value); fd.set("file", file.files[0]);
    try { await api("/api/assets", { method: "POST", form: fd }); toast("已上传"); libraryPage(); } catch (e) { fail(e); }
  };
  draw();
  app.replaceChildren(
    h("div", { class: "row spread", style: "margin-bottom:12px" }, h("h1", {}, "素材库"), h("a", { href: "#/" }, "← 返回")),
    h("form", { class: "panel", onsubmit: upload },
      h("h2", {}, "上传素材"),
      h("div", { class: "grid3" }, h("div", {}, h("label", {}, "名称"), name), h("div", {}, h("label", {}, "类型"), kind),
        h("div", {}, h("label", {}, "图片"), file)),
      h("div", { class: "row spread", style: "margin-top:12px" },
        h("span", { class: "muted small" }, "角色、产品图会作为参考图传给 Seedream，新建短片时可直接选用。"),
        h("button", { class: "primary", type: "submit" }, "上传"))),
    chips, h("div", { style: "height:12px" }), grid);
}

// ---------- 成员 ----------
async function membersPage() {
  const app = $("#app");
  const pw = h("input", { type: "password", placeholder: "至少 8 位", autocomplete: "new-password" });
  const myPw = h("form", { class: "panel stack", onsubmit: async (ev) => {
      ev.preventDefault();
      try { await api(`/api/users/${encodeURIComponent(S.cfg.user)}`, { method: "PATCH", body: { password: pw.value } }); toast("密码已修改，浏览器会要求重新登录"); pw.value = ""; }
      catch (e) { fail(e); } } },
    h("h2", {}, "修改我的密码"), h("div", { class: "row" }, h("div", { class: "grow" }, pw), h("button", { class: "primary", type: "submit" }, "保存")));
  const head = h("div", { class: "row spread", style: "margin-bottom:12px" }, h("h1", {}, S.cfg.role === "admin" ? "成员管理" : "我的账号"), h("a", { href: "#/" }, "← 返回"));
  if (!S.cfg.accounts) {
    return app.replaceChildren(head, h("div", { class: "panel stack" },
      h("p", {}, "当前使用共用口令登录（APP_PASSWORD），所有人都是管理员。"),
      h("p", { class: "muted small" }, "要启用成员账号：在 .env 设置 ADMIN_USER 和 ADMIN_PASSWORD 后重启服务，用该账号登录后在这里添加成员。启用后共用口令失效。")),
      S.cfg.role === "admin" ? h("div", { class: "panel stack", id: "syspanel" }, h("h2", {}, "系统自检")) : null);
    if (S.cfg.role === "admin") loadSystem(false);
    return;
  }
  if (S.cfg.role !== "admin") return app.replaceChildren(head, myPw);
  let users;
  try { users = await api("/api/users"); } catch (e) { return fail(e); }
  const patch = async (name, body, msg) => {
    try { await api(`/api/users/${encodeURIComponent(name)}`, { method: "PATCH", body }); toast(msg); membersPage(); } catch (e) { fail(e); }
  };
  const nName = h("input", { type: "text", placeholder: "用户名（建议用真名）" });
  const nPw = h("input", { type: "password", placeholder: "初始密码，至少 8 位", autocomplete: "new-password" });
  const nRole = h("select", {}, h("option", { value: "member" }, "成员"), h("option", { value: "admin" }, "管理员"));
  app.replaceChildren(head,
    h("div", { class: "panel" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "成员"), h("th", {}, "角色"), h("th", {}, "状态"), h("th", { class: "r" }, "操作"))),
      h("tbody", {}, users.map((u) => h("tr", {},
        h("td", {}, u.name, u.name === S.cfg.user ? h("span", { class: "muted small" }, "（我）") : null),
        h("td", {}, u.role === "admin" ? "管理员" : "成员"),
        h("td", {}, u.disabled ? h("span", { class: "badge bad" }, "已停用") : h("span", { class: "badge ok" }, "正常")),
        h("td", { class: "r" },
          h("button", { class: "ghost", onclick: () => { const v = prompt(`为 ${u.name} 设置新密码（至少 8 位）`); if (v) patch(u.name, { password: v }, "密码已重置"); } }, "重置密码"),
          h("button", { class: "ghost", onclick: () => patch(u.name, { role: u.role === "admin" ? "member" : "admin" }, "角色已修改") }, u.role === "admin" ? "设为成员" : "设为管理员"),
          u.name === S.cfg.user ? null : h("button", { class: "ghost danger", onclick: () => patch(u.name, { disabled: !u.disabled }, u.disabled ? "已启用" : "已停用") }, u.disabled ? "启用" : "停用"))))))),
    h("form", { class: "panel stack", onsubmit: async (ev) => {
        ev.preventDefault();
        try { await api("/api/users", { method: "POST", body: { name: nName.value.trim(), password: nPw.value, role: nRole.value } }); toast("已添加"); membersPage(); }
        catch (e) { fail(e); } } },
      h("h2", {}, "添加成员"),
      h("div", { class: "grid3" }, nName, nPw, nRole),
      h("div", { class: "row spread" }, h("span", { class: "muted small" }, "成员可以看所有短片，只能修改自己创建的；管理员可以修改全部并管理成员。"),
        h("button", { class: "primary", type: "submit" }, "添加"))),
    myPw,
    h("div", { class: "panel stack", id: "syspanel" }, h("h2", {}, "系统自检"), h("div", { class: "muted small" }, "加载中…")));
  loadSystem(false);
}

async function loadSystem(ping) {
  const box = $("#syspanel");
  if (!box) return;
  let d;
  try { d = await api("/api/system" + (ping ? "?ping=1" : "")); } catch (e) { return fail(e); }
  const mark = { ok: ["✓", "ok"], warn: ["!", "todo"], fail: ["✗", "bad"] };
  box.replaceChildren(
    h("div", { class: "row spread" }, h("h2", {}, "系统自检"),
      h("button", { onclick: (ev) => { ev.target.disabled = true; ev.target.textContent = "测试中…"; loadSystem(true); } }, "测试接口连通")),
    h("table", { class: "tbl" }, h("tbody", {}, d.items.map((it) => h("tr", {},
      h("td", { style: "width:36px" }, h("span", { class: "badge " + mark[it.level][1] }, mark[it.level][0])),
      h("td", { style: "width:120px" }, it.name), h("td", { class: "small" }, it.detail))))),
    h("div", { class: "muted small" }, `回收站里有 ${d.trash} 个已删除项目（data/trash）。后台任务线程 ${d.workers} 个。`, ping ? "" : " 连通测试只读取模型/任务列表，不产生生成费用。"));
}

function drawDemoBar() {
  if (!S.cfg.demo || $("#demobar")) return;
  document.body.insertBefore(h("div", { id: "demobar", class: "demobar" },
    "演示模式：不调用真实接口、不花钱，图片和视频都是占位素材。正式使用前请关闭 DEMO_MODE。"), $("main"));
}

function drawNav() {
  drawDemoBar();
  const links = [["#/templates", "模板"], ["#/library", "素材库"], ["#/stats", "成本看板"]];
  if (S.cfg.accounts || S.cfg.role === "admin") links.push(["#/members", S.cfg.role === "admin" ? "成员" : "账号"]);
  $("#nav").replaceChildren(...links.map(([href, t]) => h("a", { href }, t)), ...(S.cfg.user ? [h("span", { class: "who" }, S.cfg.user)] : []));
}

// ---------- 启动 ----------
(async function boot() {
  try {
    S.cfg = await api("/api/config");
    drawNav();
    route();
  } catch (e) { $("#app").replaceChildren(h("div", { class: "empty" }, "无法连接服务：" + e.message)); }
})();
