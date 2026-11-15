"use strict";
// AI 短片工作台 — Vue 3（浏览器版，无构建步骤）：左侧菜单 + 右侧页面
const { createApp, reactive, ref, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;

// ---------- 基础 ----------
const BASE = location.pathname.replace(/\/[^/]*$/, "");        // 子路径部署，如 /movie
const store = reactive({ cfg: null, toast: null, route: { name: "list", params: {} }, menuOpen: false });

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path.startsWith("/") ? BASE + path : path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* 非 JSON */ }
  if (r.status === 401 && data && data.login) { location.href = data.login; throw new Error("登录已过期，正在跳转登录…"); }
  if (!r.ok) throw new Error((data && data.error) || `请求失败（${r.status}）`);
  return data;
}
let toastTimer;
function toast(msg, bad = false) {
  store.toast = { msg, bad };
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (store.toast = null), bad ? 6000 : 2500);
}
const fail = (e) => toast(e.message || String(e), true);
const usd = (n) => "$" + Number(n || 0).toFixed(2);
const ratioCss = (r) => (r || "9:16").replace(":", " / ");
const go = (hash) => { location.hash = hash; store.menuOpen = false; };

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

// ---------- 路由（hash） ----------
function parseRoute() {
  const h = location.hash.replace(/^#/, "") || "/";
  let m;
  if ((m = h.match(/^\/p\/([\w-]+)/))) return { name: "project", params: { id: m[1] } };
  const map = { "/": "list", "/new": "new", "/templates": "templates", "/library": "library", "/stats": "stats",
    "/members": "members", "/system": "system" };
  return { name: map[h.split("?")[0]] || "list", params: {} };
}
window.addEventListener("hashchange", () => { store.route = parseRoute(); store.menuOpen = false; });

// ---------- 通用小组件 ----------
const Badge = {
  props: { map: Object, value: String },
  template: `<span class="badge" :class="(map[value] || [])[1]">{{ (map[value] || [value])[0] }}</span>`,
};
const Chips = {
  props: { options: Array, modelValue: [String, Number], label: Function },
  emits: ["update:modelValue"],
  template: `<div class="chips"><button v-for="o in options" :key="o" type="button" class="chip" :class="{ on: o === modelValue }"
    @click="$emit('update:modelValue', o)">{{ label ? label(o) : o }}</button></div>`,
};
const PageHead = {
  props: { title: String, sub: String },
  template: `<div class="page-head"><div><h1>{{ title }}</h1><div v-if="sub" class="muted small">{{ sub }}</div></div>
    <div class="row"><slot /></div></div>`,
};
const DataTable = {
  props: { cols: Array, rows: Array, empty: { type: String, default: "暂无数据" } },
  template: `<table class="tbl"><thead><tr><th v-for="c in cols" :key="c.key" :class="c.cls">{{ c.label }}</th></tr></thead>
    <tbody><tr v-if="!rows.length"><td :colspan="cols.length" class="muted">{{ empty }}</td></tr>
    <tr v-for="(r, i) in rows" :key="i"><td v-for="c in cols" :key="c.key" :class="c.cls"><slot :name="c.key" :row="r">{{ c.fmt ? c.fmt(r[c.key], r) : r[c.key] }}</slot></td></tr>
    </tbody></table>`,
};

// ---------- 新建短片 ----------
const NewPage = {
  components: { Chips, PageHead },
  setup() {
    const f = reactive({ description: "", style: "", ratio: "9:16", duration: 30, template: "" });
    const templates = ref([]), assets = ref([]), picked = ref(new Set()), files = ref([]), busy = ref(false);
    const tpl = computed(() => templates.value.find((t) => String(t.id) === String(f.template)) || null);
    const tplAssetIds = computed(() => new Set(tpl.value ? tpl.value.asset_ids : []));
    const previews = computed(() => files.value.map((x) => ({ name: x.name, url: URL.createObjectURL(x) })));
    const warn = computed(() => {
      const c = store.cfg, w = [];
      if (!c.demo && (!c.keys.deepseek || !c.keys.ark)) w.push("服务器未配置 API Key，生成会失败。");
      if (!c.demo && !c.priced) w.push("未配置模型单价，花费预估与 100 USD 熔断不生效。");
      return w.join(" ");
    });
    watch(tpl, (t) => {
      if (!t) return;
      f.ratio = t.ratio; if ([15, 30, 45, 60].includes(t.duration)) f.duration = t.duration; f.style = t.style;
    });
    onMounted(async () => {
      try { [templates.value, assets.value] = await Promise.all([api("/api/templates"), api("/api/assets")]); } catch (e) { fail(e); }
      if (store.pendingTemplate) { f.template = String(store.pendingTemplate); store.pendingTemplate = null; }
    });
    const toggle = (a) => {
      if (tplAssetIds.value.has(a.id)) return;
      const s = new Set(picked.value); s.has(a.id) ? s.delete(a.id) : s.add(a.id); picked.value = s;
    };
    const onFiles = (e) => { files.value = [...e.target.files].slice(0, 4); };
    async function submit() {
      if (f.description.trim().length < 4) return toast("请先描述一下场景", true);
      const fd = new FormData();
      fd.set("description", f.description.trim()); fd.set("style", f.style.trim());
      fd.set("ratio", f.ratio); fd.set("duration", f.duration);
      files.value.forEach((x) => fd.append("refs", x));
      if (tpl.value) fd.set("template_id", tpl.value.id);
      if (picked.value.size) fd.set("asset_ids", [...picked.value].join(","));
      busy.value = true;
      try { const p = await api("/api/projects", { method: "POST", form: fd }); go(`#/p/${p.id}`); }
      catch (e) { fail(e); busy.value = false; }
    }
    return { f, templates, assets, picked, tpl, tplAssetIds, previews, warn, toggle, onFiles, submit, busy, cfg: store.cfg };
  },
  template: `<div class="page">
    <PageHead title="新建短片" sub="描述场景，系统先写分镜脚本，确认后再出图、生成视频" />
    <div class="panel stack">
      <div v-if="warn" class="warnline">{{ warn }}</div>
      <div v-if="templates.length" class="grid2">
        <div><label>风格模板</label><select v-model="f.template"><option value="">不使用模板</option>
          <option v-for="t in templates" :key="t.id" :value="String(t.id)">{{ t.name }}</option></select></div>
        <div v-if="tpl" class="muted small" style="align-self:end">
          <div v-if="tpl.characters.length">固定角色：{{ tpl.characters.map(c => c.name + '（' + c.appearance + '）').join('；') }}</div>
          <div v-if="tpl.assets.length">自带素材：{{ tpl.assets.map(a => a.name).join('、') }}</div>
        </div>
      </div>
      <div><label>场景描述</label>
        <textarea v-model="f.description" rows="4" :placeholder="tpl && tpl.description ? '参考：' + tpl.description : '例如：社区咖啡店的清晨，店主小林开门、做第一杯拿铁、递给熟客，温暖治愈'"></textarea></div>
      <div class="grid2">
        <div><label>画幅</label><Chips :options="cfg.ratios" v-model="f.ratio" /></div>
        <div><label>目标时长</label><Chips :options="[15, 30, 45, 60]" v-model="f.duration" :label="d => d + ' 秒'" /></div>
      </div>
      <div><label>风格</label><input type="text" v-model="f.style" placeholder="可选，例如：日系胶片感、暖色调"></div>
      <div v-if="assets.length"><label>从素材库选参考图</label>
        <div class="pick-grid"><button v-for="a in assets" :key="a.id" type="button" class="pick"
          :class="{ on: picked.has(a.id) || tplAssetIds.has(a.id) }" :title="a.kind + ' · ' + a.name" @click="toggle(a)">
          <img :src="a.url" :alt="a.name" loading="lazy"><span>{{ a.name }}</span></button></div></div>
      <div><label>上传参考图（角色 / 产品 / 风格；与素材库合计最多 4 张）</label>
        <input type="file" accept="image/png,image/jpeg,image/webp" multiple @change="onFiles">
        <div class="refs" v-if="previews.length"><img v-for="p in previews" :key="p.url" :src="p.url" :alt="p.name"></div></div>
      <div class="row spread"><span class="muted small">单片花费上限 \${{ cfg.budget_usd }}</span>
        <button class="primary" :disabled="busy" @click="submit">{{ busy ? '提交中…' : '生成脚本' }}</button></div>
    </div></div>`,
};

// ---------- 短片列表 ----------
const ListPage = {
  components: { Badge, Chips, PageHead },
  setup() {
    const items = ref(null);
    const f = store.listFilter || (store.listFilter = reactive({ who: "all", status: "", q: "" }));
    onMounted(async () => { try { items.value = await api("/api/projects"); } catch (e) { fail(e); items.value = []; } });
    const shown = computed(() => {
      if (!items.value) return [];
      const kw = f.q.trim().toLowerCase();
      return items.value.filter((p) => (f.who === "all" || p.owner === store.cfg.user) && (!f.status || p.status === f.status)
        && (!kw || p.title.toLowerCase().includes(kw) || (p.owner || "").toLowerCase().includes(kw)));
    });
    return { items, shown, f, P_STATUS, usd, go, user: store.cfg.user };
  },
  template: `<div class="page">
    <PageHead title="全部短片" :sub="items ? items.length + ' 部' : ''"><button class="primary" @click="go('#/new')">新建短片</button></PageHead>
    <div class="toolbar">
      <Chips v-if="user" :options="['all', 'mine']" v-model="f.who" :label="v => v === 'all' ? '全部' : '我的'" />
      <select v-model="f.status" style="max-width:160px"><option value="">全部状态</option>
        <option v-for="(v, k) in P_STATUS" :key="k" :value="k">{{ v[0] }}</option></select>
      <input type="text" v-model="f.q" placeholder="搜索标题或成员" style="max-width:240px">
    </div>
    <div v-if="items === null" class="muted">加载中…</div>
    <div v-else-if="!items.length" class="panel empty">还没有短片。<a href="#/new">新建一部</a></div>
    <div v-else-if="!shown.length" class="muted">没有符合条件的短片</div>
    <div v-else class="plist">
      <a v-for="p in shown" :key="p.id" class="pcard" :href="'#/p/' + p.id">
        <div class="row spread"><Badge :map="P_STATUS" :value="p.status" /><span class="muted small">{{ p.ratio }}</span></div>
        <div class="t">{{ p.title }}</div>
        <div class="muted small">{{ p.created_at.slice(5, 16) }} · {{ p.shots }} 个镜头 · 已花 {{ usd(p.cost.spent_usd) }}<span v-if="p.owner"> · {{ p.owner }}</span></div>
      </a>
    </div></div>`,
};

// ---------- 项目：四个步骤 ----------
const ScriptTab = {
  props: { p: Object, busy: Boolean },
  emits: ["job", "saved"],
  setup(props, { emit }) {
    const draft = ref(null), dirty = ref(false);
    const build = () => {
      draft.value = JSON.parse(JSON.stringify({ ...props.p.script,
        shots: props.p.shots.map(({ state, index, ...s }) => ({ ...s, characters: s.characters || [] })) }));
      draft.value.characters.forEach((c) => { if (c.voice === undefined) c.voice = ""; });
      nextTick(() => (dirty.value = false));
    };
    watch(() => props.p, () => { if (props.p.script && (!dirty.value || !draft.value)) build(); }, { immediate: true });
    watch(draft, () => (dirty.value = true), { deep: true });
    const total = computed(() => draft.value ? draft.value.shots.reduce((a, s) => a + Number(s.duration || 0), 0) : 0);
    const stateOf = (s) => (props.p.shots.find((x) => x.id === s.id) || {}).state;
    const move = (i, d) => { const a = draft.value.shots, j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; };
    const addShot = () => draft.value.shots.push({ description: "", characters: [], image_prompt: "", video_prompt: "",
      duration: store.cfg.durations[0], narration: "", speaker: "", link_next: false });
    const charsText = (s) => (s.characters || []).join("，");
    const setChars = (s, v) => { s.characters = v.split(/[,，、\s]+/).filter(Boolean); };
    async function save(thenImages) {
      try {
        if (dirty.value) {
          const p = await api(`/api/projects/${props.p.id}/script`, { method: "PUT", body: draft.value });
          dirty.value = false; emit("saved", p); toast("已保存");
        }
        if (thenImages) emit("job", "images", {}, "images");
      } catch (e) { fail(e); }
    }
    async function saveTemplate() {
      if (dirty.value) return toast("先保存脚本修改，再另存为模板", true);
      const name = prompt("模板名称", props.p.title);
      if (!name) return;
      try { await api("/api/templates", { method: "POST", body: { from_project: props.p.id, name } }); toast("已存为模板"); }
      catch (e) { fail(e); }
    }
    const regen = () => { if (confirm("重新生成会覆盖当前脚本和所有素材记录，确定？")) emit("job", "script"); };
    return { draft, dirty, total, stateOf, move, addShot, charsText, setChars, save, saveTemplate, regen, S_STATUS, cfg: store.cfg };
  },
  components: { Badge },
  template: `<div v-if="!p.script" class="panel empty">{{ busy ? 'DeepSeek 正在写脚本，通常需要 20–60 秒…' : '还没有脚本' }}
      <p v-if="!busy"><button class="primary" @click="$emit('job', 'script')">生成脚本</button></p></div>
    <div v-else-if="draft">
      <div class="panel stack">
        <div class="row spread"><h2>整体设定</h2><div class="row"><span class="muted small">{{ draft.shots.length }} 个镜头 · 共 {{ total }} 秒</span>
          <button class="ghost" @click="saveTemplate">另存为模板</button></div></div>
        <div><label>标题</label><input type="text" v-model="draft.title" :disabled="busy"></div>
        <div><label>全片风格（拼接到每张场景图提示词前）</label><textarea v-model="draft.style" rows="2" :disabled="busy"></textarea></div>
        <div><label>角色</label>
          <div v-for="(c, i) in draft.characters" :key="i" class="grid3" style="margin-bottom:10px">
            <div class="stack"><div><label>角色名</label><input type="text" v-model="c.name" :disabled="busy"></div>
              <div><label>配音音色（可选，台词用）</label><input type="text" v-model="c.voice" placeholder="voice_type" :disabled="busy"></div></div>
            <div style="grid-column: span 2"><label class="row spread">固定外观描述
              <button v-if="!busy" class="ghost danger small" @click="draft.characters.splice(i, 1)">删除</button></label>
              <textarea v-model="c.appearance" rows="3" :disabled="busy"></textarea></div>
          </div>
          <button v-if="!busy" class="ghost" @click="draft.characters.push({ name: '', appearance: '', voice: '' })">+ 添加角色</button></div>
      </div>
      <h2>分镜</h2>
      <div v-for="(s, i) in draft.shots" :key="s.id || 'new' + i" class="shot">
        <div class="shot-head"><span class="num">{{ i + 1 }}</span>
          <div class="grow"><input type="text" v-model="s.description" placeholder="镜头简述" :disabled="busy"></div>
          <Badge v-if="stateOf(s)" :map="S_STATUS" :value="stateOf(s).status" /><span v-else class="badge">新镜头</span>
          <template v-if="!busy"><button class="ghost" title="上移" @click="move(i, -1)">↑</button>
            <button class="ghost" title="下移" @click="move(i, 1)">↓</button>
            <button class="ghost danger" @click="draft.shots.length > 1 && draft.shots.splice(i, 1)">删除</button></template>
        </div>
        <div class="grid2">
          <div><label>场景图提示词（静态画面）</label><textarea v-model="s.image_prompt" rows="3" :disabled="busy"></textarea></div>
          <div><label>视频提示词（怎么动）</label><textarea v-model="s.video_prompt" rows="3" :disabled="busy"></textarea></div>
        </div>
        <div class="grid3" style="margin-top:10px">
          <div><label>时长</label><select v-model.number="s.duration" :disabled="busy">
            <option v-for="d in cfg.durations" :key="d" :value="d">{{ d }} 秒</option></select></div>
          <div><label>出场角色</label><input type="text" :value="charsText(s)" @input="setChars(s, $event.target.value)"
            placeholder="用逗号分隔，需与上方角色名一致" :disabled="busy"></div>
          <div><label>旁白 / 台词（也作字幕）</label><input type="text" v-model="s.narration" :disabled="busy">
            <select v-if="draft.characters.length" v-model="s.speaker" style="margin-top:6px" :disabled="busy">
              <option value="">说话人：画外旁白</option>
              <option v-for="c in draft.characters.filter(c => c.name)" :key="c.name" :value="c.name">说话人：{{ c.name }}</option></select></div>
        </div>
        <label v-if="i < draft.shots.length - 1" class="check"><input type="checkbox" v-model="s.link_next" :disabled="busy">
          平滑过渡到下一镜（用下一镜的场景图做尾帧，适合同一场景的连续动作）</label>
      </div>
      <button v-if="!busy" class="ghost" style="margin-top:10px" @click="addShot">+ 添加镜头</button>
      <div class="actions">
        <button :disabled="busy" @click="regen">重新生成脚本</button>
        <button :disabled="busy || !dirty" @click="save(false)">保存</button>
        <button class="primary" :disabled="busy" @click="save(true)">保存并生成场景图</button>
      </div>
    </div>`,
};

const ImagesTab = {
  props: { p: Object, busy: Boolean },
  emits: ["job", "update"],
  components: { Badge },
  setup(props, { emit }) {
    const edits = reactive({});
    const allSel = computed(() => props.p.shots.every((s) => s.state.selected));
    const anyCand = computed(() => props.p.shots.some((s) => s.state.candidates.length));
    const unselected = computed(() => props.p.shots.filter((s) => !s.state.selected).length);
    async function pick(s, n) {
      try { emit("update", await api(`/api/projects/${props.p.id}/select`, { method: "POST", body: { shot_id: s.id, candidate: n } })); }
      catch (e) { fail(e); }
    }
    function refine(s) {
      const v = (edits[s.id] || "").trim();
      if (v.length < 2) return toast("写一下要怎么改", true);
      emit("job", "refine", { shot: s.index, candidate: s.state.selected, instruction: v });
      edits[s.id] = "";
    }
    async function saveAsset(s) {
      const name = prompt("素材名称", s.description || `第 ${s.index} 镜`); if (!name) return;
      const kind = prompt(`类型（${store.cfg.asset_kinds.join(" / ")}）`, "场景"); if (!kind) return;
      try { await api(`/api/projects/${props.p.id}/save-asset`, { method: "POST", body: { shot_id: s.id, name, kind } }); toast("已存入素材库"); }
      catch (e) { fail(e); }
    }
    return { edits, allSel, anyCand, unselected, pick, refine, saveAsset, ratioCss, S_STATUS };
  },
  template: `<div v-if="!p.shots.length" class="panel empty">先完成脚本</div>
    <div v-else>
      <div v-for="s in p.shots" :key="s.id" class="shot">
        <div class="shot-head"><span class="num">{{ s.index }}</span><strong class="grow">{{ s.description || '（无简述）' }}</strong>
          <Badge :map="S_STATUS" :value="s.state.status" />
          <button :disabled="busy" @click="$emit('job', 'images', { shots: [s.index] })">{{ s.state.candidates.length ? '重出这一镜' : '出图' }}</button></div>
        <div class="muted small" style="margin-bottom:8px">{{ s.image_prompt }}</div>
        <div v-if="s.state.image_stale" class="warnline">提示词已修改，建议重出这一镜</div>
        <div v-if="s.state.error" class="errline">{{ s.state.error }}</div>
        <div class="cands">
          <template v-if="s.state.candidates.length">
            <button v-for="(u, i) in s.state.candidates" :key="u" class="cand" :class="{ on: s.state.selected === i + 1 }"
              :style="{ aspectRatio: ratioCss(p.ratio) }" :disabled="busy" @click="pick(s, i + 1)"
              :title="s.state.refined && s.state.refined[i] ? '修改：' + s.state.refined[i] : '候选 ' + (i + 1)">
              <img :src="u" loading="lazy" :alt="'候选 ' + (i + 1)"><span v-if="s.state.refined && s.state.refined[i]" class="tag">改</span></button>
          </template>
          <div v-else class="placeholder" :style="{ aspectRatio: ratioCss(p.ratio) }">{{ s.state.status === 'imaging' ? '出图中…' : '未出图' }}</div>
        </div>
        <div v-if="s.state.selected" class="row" style="margin-top:10px">
          <div class="grow"><input type="text" v-model="edits[s.id]" @keydown.enter="refine(s)" :disabled="busy"
            placeholder="例如：人物换成侧脸、背景加一盏暖色吊灯、去掉桌上的杯子"></div>
          <button :disabled="busy" @click="refine(s)">基于选中图修改</button>
          <button class="ghost small" @click="saveAsset(s)">存入素材库</button></div>
      </div>
      <div class="actions">
        <button :disabled="busy" @click="$emit('job', 'images')">{{ anyCand ? '补齐未出图的镜头' : '生成全部场景图' }}</button>
        <button class="primary" :disabled="busy || !allSel" @click="$emit('job', 'videos', {}, 'videos')">
          {{ allSel ? '生成镜头视频' : '还有 ' + unselected + ' 镜未选图' }}</button>
      </div>
    </div>`,
};

const VideosTab = {
  props: { p: Object, busy: Boolean },
  emits: ["job", "tab"],
  components: { Badge },
  setup(props) {
    const allVid = computed(() => props.p.shots.length && props.p.shots.every((s) => s.state.video));
    const frame = (s) => (s.state.selected ? s.state.candidates[s.state.selected - 1] : null);
    return { allVid, frame, ratioCss, S_STATUS };
  },
  template: `<div v-if="!p.shots.length" class="panel empty">先完成脚本</div>
    <div v-else>
      <div v-for="s in p.shots" :key="s.id" class="shot">
        <div class="shot-head"><span class="num">{{ s.index }}</span><strong class="grow">{{ s.description || '（无简述）' }}</strong>
          <span v-if="s.link_next && s.index < p.shots.length" class="badge">→ 过渡到第 {{ s.index + 1 }} 镜</span>
          <span class="muted small">{{ s.duration }} 秒</span><Badge :map="S_STATUS" :value="s.state.status" />
          <button :disabled="busy || !s.state.selected" @click="$emit('job', 'videos', { shots: [s.index] })">{{ s.state.video ? '重生成' : '生成' }}</button></div>
        <div class="row" style="align-items:flex-start">
          <video v-if="s.state.video" class="vid" :src="s.state.video" controls playsinline preload="metadata" :poster="frame(s) || undefined"></video>
          <img v-else-if="frame(s)" class="vid" :src="frame(s)" alt="首帧" style="background:#eee">
          <div v-else class="placeholder" :style="{ aspectRatio: ratioCss(p.ratio) }">未选图</div>
          <div class="grow small">
            <div class="muted">视频提示词</div><div>{{ s.video_prompt }}</div>
            <template v-if="s.narration"><div class="muted" style="margin-top:8px">旁白</div><div>{{ s.narration }}</div></template>
            <div v-if="s.state.tasks > 1" class="muted" style="margin-top:8px">已生成 {{ s.state.tasks }} 次</div>
            <div v-if="s.state.error" class="errline" style="margin-top:8px">{{ s.state.error }}</div>
          </div>
        </div>
      </div>
      <div class="actions">
        <button :disabled="busy || allVid" @click="$emit('job', 'videos')">补齐缺失的镜头</button>
        <button class="primary" :disabled="busy || !allVid" @click="$emit('tab', 'final')">去合成成片</button>
      </div>
    </div>`,
};

const FinalTab = {
  props: { p: Object, busy: Boolean },
  emits: ["job", "update"],
  setup(props, { emit }) {
    const meta = computed(() => props.p.final_meta);
    const hasNarr = computed(() => props.p.shots.some((s) => (s.narration || "").trim()));
    const allVid = computed(() => props.p.shots.length && props.p.shots.every((s) => s.state.video));
    const opt = reactive({ tts: store.cfg.tts.enabled && hasNarr.value, voice: store.cfg.tts.voice,
      burn: meta.value ? !!meta.value.burn_subs : false, fade: meta.value ? !!meta.value.transition : false });
    const others = computed(() => store.cfg.ratios.filter((r) => r !== props.p.ratio));
    const bgm = ref(null);
    const metaText = computed(() => meta.value ? [meta.value.tts ? `旁白配音（${meta.value.voice}）` : "无配音",
      meta.value.bgm ? "含 BGM" : "无 BGM", meta.value.burn_subs ? "字幕已烧录" : "软字幕",
      meta.value.transition ? "淡入淡出转场" : "硬切"].join(" · ") : "");
    const compose = () => emit("job", "compose", { bgm: props.p.has_bgm, tts: opt.tts, voice: opt.voice.trim(),
      burn_subs: opt.burn, transition: opt.fade ? 0.5 : 0 });
    async function upload() {
      const f = bgm.value && bgm.value.files[0];
      if (!f) return toast("先选择音频文件", true);
      const fd = new FormData(); fd.set("bgm", f);
      try { emit("update", await api(`/api/projects/${props.p.id}/bgm`, { method: "POST", form: fd })); toast("BGM 已上传"); }
      catch (e) { fail(e); }
    }
    return { meta, hasNarr, allVid, opt, others, bgm, metaText, compose, upload, cfg: store.cfg };
  },
  template: `<div>
    <div class="panel">
      <div v-if="meta && p.final" class="muted small" style="text-align:center;margin-bottom:8px">{{ metaText }}</div>
      <template v-if="p.final">
        <video class="final" :src="p.final" controls playsinline></video>
        <div class="row" style="justify-content:center;margin-top:12px">
          <a :href="p.final" :download="p.title + '.mp4'"><button class="primary">下载成片</button></a>
          <a :href="p.final.replace(/\\.mp4$/, '.srt')" :download="p.title + '.srt'"><button>下载字幕</button></a></div>
      </template>
      <div v-else class="empty">{{ busy ? '处理中…' : allVid ? '所有镜头已就绪，可以合成成片' : '还有镜头没有视频，先到「视频」补齐' }}</div>
    </div>
    <div class="grid2">
      <div class="panel stack"><h2>旁白配音</h2>
        <div class="muted small">{{ !cfg.tts.enabled ? '服务器未配置 TTS，旁白只作为字幕。' : !hasNarr ? '脚本里没有旁白，可在「脚本」里填写。'
          : '为每镜旁白配音，按镜头起点对齐；设了说话人的镜头用该角色的音色。' }}</div>
        <label class="check"><input type="checkbox" v-model="opt.tts" :disabled="!cfg.tts.enabled || !hasNarr || busy">合成时加入旁白配音</label>
        <div><label>旁白音色（voice_type）</label><input type="text" v-model="opt.voice" :disabled="!cfg.tts.enabled || busy"></div>
      </div>
      <div class="panel stack"><h2>剪辑</h2>
        <label class="check"><input type="checkbox" v-model="opt.burn" :disabled="!hasNarr || busy">把字幕烧进画面（抖音、视频号等平台需要）</label>
        <label class="check"><input type="checkbox" v-model="opt.fade" :disabled="busy || p.shots.length < 2">镜头之间 0.5 秒淡入淡出</label>
        <div><label>背景音乐（可选）</label><div class="row"><input type="file" ref="bgm" accept=".mp3,.m4a,.aac,.wav" style="max-width:260px">
          <button :disabled="busy" @click="upload">上传</button></div>
          <div class="muted small" style="margin-top:4px">{{ p.has_bgm ? '已上传，合成时混入（有配音时自动压低）' : '未上传' }}</div></div>
      </div>
    </div>
    <div v-if="p.final" class="panel stack"><h2>导出其他画幅</h2>
      <div class="muted small">不重新生成：原画面完整保留，空白处模糊填充。</div>
      <div class="row"><template v-for="r in others" :key="r">
        <a v-if="p.exports && p.exports[r]" :href="p.exports[r]" :download="p.title + '_' + r.replace(':', 'x') + '.mp4'"><button>下载 {{ r }}</button></a>
        <button v-else :disabled="busy" @click="$emit('job', 'export', { ratio: r })">导出 {{ r }}</button></template></div>
    </div>
    <div class="actions"><button class="primary" :disabled="busy || !allVid" @click="compose">{{ p.final ? '重新合成' : '合成成片' }}</button></div>
  </div>`,
};

const ProjectPage = {
  props: { id: String },
  components: { Badge, ScriptTab, ImagesTab, VideosTab, FinalTab },
  setup(props) {
    const p = ref(null), tab = ref(null), notFound = ref(""), sseOk = ref(false);
    let es = null, timer = null, pending = null;
    const busy = computed(() => !!(p.value && (p.value.job || !p.value.can_edit)));
    const defaultTab = (x) => (["imaging", "image_review", "image_selected"].includes(x.status) ? "images"
      : ["generating", "shot_review"].includes(x.status) ? "videos" : ["rendering", "done"].includes(x.status) ? "final" : "script");
    async function refresh() {
      clearTimeout(timer);
      try { p.value = await api(`/api/projects/${props.id}`); }
      catch (e) { notFound.value = e.message; return; }
      if (!tab.value) tab.value = defaultTab(p.value);
      if (p.value.job && !sseOk.value) timer = setTimeout(refresh, 2000);
    }
    function openEvents() {
      if (!window.EventSource) return;
      es = new EventSource(`${BASE}/api/projects/${props.id}/events`);
      es.onopen = () => (sseOk.value = true);
      es.onerror = () => (sseOk.value = false);
      es.addEventListener("update", () => { clearTimeout(pending); pending = setTimeout(refresh, 150); });
    }
    onMounted(() => { openEvents(); refresh(); });
    onBeforeUnmount(() => { if (es) es.close(); clearTimeout(timer); });
    watch(() => props.id, () => { tab.value = null; p.value = null; if (es) es.close(); openEvents(); refresh(); });

    async function job(kind, extra = {}, toTab) {
      try {
        await api(`/api/projects/${p.value.id}/jobs`, { method: "POST", body: { kind, ...extra } });
        if (toTab) tab.value = toTab;
        toast("已开始"); await refresh();
      } catch (e) { fail(e); }
    }
    async function cancelJob() {
      if (!confirm("取消这个任务？已完成的图和视频会保留；正在生成的视频会尝试在方舟侧取消（已开始生成的可能仍会计费）。")) return;
      try { const r = await api(`/api/jobs/${p.value.job.id}/cancel`, { method: "POST" });
        toast(r.status === "cancelled" ? "已取消" : "正在停止，当前步骤完成后结束"); await refresh(); } catch (e) { fail(e); }
    }
    async function duplicate() {
      if (!confirm("复制这部短片的需求和脚本（不复制图和视频），用来做变体？")) return;
      try { const n = await api(`/api/projects/${p.value.id}/duplicate`, { method: "POST" }); toast("已复制"); go(`#/p/${n.id}`); } catch (e) { fail(e); }
    }
    async function remove() {
      if (!confirm(`删除「${p.value.title}」？会移到服务器回收站，管理员可找回。`)) return;
      try { await api(`/api/projects/${p.value.id}`, { method: "DELETE" }); toast("已删除"); go("#/"); } catch (e) { fail(e); }
    }
    const banner = computed(() => p.value && (p.value.job || (p.value.last_job && p.value.last_job.status === "failed" ? p.value.last_job : null)));
    const logTail = computed(() => banner.value ? (banner.value.log || "").trim().split("\n").slice(-6).join("\n") : "");
    const tabs = computed(() => {
      if (!p.value) return [];
      const sh = p.value.shots, sel = sh.filter((s) => s.state.selected).length, vid = sh.filter((s) => s.state.video).length;
      return [["script", "① 脚本", sh.length ? sh.length + " 镜" : ""], ["images", "② 场景图", sh.length ? `${sel}/${sh.length}` : ""],
        ["videos", "③ 视频", sh.length ? `${vid}/${sh.length}` : ""], ["final", "④ 成片", p.value.final ? "✓" : ""]];
    });
    const meter = computed(() => {
      const c = p.value.cost;
      return { pct: Math.min(100, (c.spent_usd / c.budget_usd) * 100), warnPct: (store.cfg.budget_warn_usd / c.budget_usd) * 100,
        cls: c.spent_usd >= c.budget_usd ? "over" : c.spent_usd >= store.cfg.budget_warn_usd ? "warn" : "" };
    });
    const update = (np) => (p.value = np);
    return { p, tab, busy, notFound, job, cancelJob, duplicate, remove, banner, logTail, tabs, meter, update, usd, P_STATUS };
  },
  template: `<div class="page">
    <div v-if="notFound" class="panel empty">{{ notFound }}<p><a href="#/">返回列表</a></p></div>
    <div v-else-if="!p" class="muted">加载中…</div>
    <template v-else>
      <div class="page-head">
        <div class="grow">
          <div class="crumb"><a href="#/">全部短片</a> / {{ p.title }}</div>
          <div class="row" style="margin-top:4px"><h1>{{ p.title }}</h1><Badge :map="P_STATUS" :value="p.status" /></div>
          <div class="muted small" style="margin-top:4px">{{ p.ratio }} · 约 {{ p.brief.duration }} 秒<span v-if="p.owner"> · {{ p.owner }}</span>
            <span v-if="p.brief.template"> · 模板「{{ p.brief.template.name }}」</span></div>
        </div>
        <div class="cost-box">
          <div class="row spread small"><span>已花 {{ usd(p.cost.spent_usd) }}</span><span class="muted">上限 {{ usd(p.cost.budget_usd) }}</span></div>
          <div class="meter" :class="meter.cls"><span :style="{ width: meter.pct + '%' }"></span><i :style="{ left: meter.warnPct + '%' }"></i></div>
          <div v-if="p.estimate" class="muted small" style="margin-top:4px">整片预估 {{ usd(p.estimate.total_usd) }}<span v-if="!p.estimate.priced">（未配置单价）</span></div>
          <div class="row" style="margin-top:8px;justify-content:flex-end">
            <button class="ghost small" @click="duplicate">复制</button>
            <button v-if="p.can_edit" class="ghost small danger" :disabled="!!p.job" @click="remove">删除</button></div>
        </div>
      </div>
      <details class="brief"><summary>需求与参考图</summary><p>{{ p.brief.description }}</p>
        <p v-if="p.brief.style" class="muted small">风格：{{ p.brief.style }}</p>
        <div class="refs"><img v-for="u in p.brief.refs" :key="u" :src="u" alt="参考图"></div></details>
      <div v-if="!p.can_edit" class="banner"><div>只读：这是 {{ p.owner }} 的项目，只有本人或管理员可以修改。可以复制、另存为模板或把场景图存入素材库。</div></div>
      <div v-if="banner" class="banner" :class="{ bad: banner.status === 'failed' }">
        <div v-if="banner.status !== 'failed'" class="spin"></div>
        <div class="grow"><div class="row spread">
          <strong>{{ banner.status === 'failed' ? banner.label + '失败：' + (banner.error || '') : banner.label + (banner.status === 'queued' ? '（排队中）' : '中…') }}</strong>
          <button v-if="banner.status !== 'failed' && p.can_edit" class="ghost small danger" @click="cancelJob">取消</button></div>
          <pre>{{ logTail }}</pre></div>
      </div>
      <div class="tabs"><button v-for="t in tabs" :key="t[0]" class="tab" :class="{ on: tab === t[0] }" @click="tab = t[0]">{{ t[1] }}<span v-if="t[2]" class="n">{{ t[2] }}</span></button></div>
      <ScriptTab v-if="tab === 'script'" :p="p" :busy="busy" @job="job" @saved="update" />
      <ImagesTab v-else-if="tab === 'images'" :p="p" :busy="busy" @job="job" @update="update" />
      <VideosTab v-else-if="tab === 'videos'" :p="p" :busy="busy" @job="job" @tab="t => tab = t" />
      <FinalTab v-else :p="p" :busy="busy" @job="job" @update="update" />
    </template></div>`,
};

// ---------- 模板 ----------
const TemplatesPage = {
  components: { PageHead },
  setup() {
    const list = ref(null);
    const load = async () => { try { list.value = await api("/api/templates"); } catch (e) { fail(e); list.value = []; } };
    onMounted(load);
    const mine = (x) => store.cfg.role === "admin" || !x.owner || x.owner === store.cfg.user;
    async function del(x) { if (!confirm(`删除模板「${x.name}」？`)) return;
      try { await api(`/api/templates/${x.id}`, { method: "DELETE" }); load(); } catch (e) { fail(e); } }
    const use = (x) => { store.pendingTemplate = x.id; go("#/new"); };
    return { list, mine, del, use };
  },
  template: `<div class="page"><PageHead title="风格模板" sub="保存全片风格、固定角色外观与音色、画幅时长和参考素材；同系列短片套用同一模板，角色保持一致" />
    <div v-if="list === null" class="muted">加载中…</div>
    <div v-else-if="!list.length" class="panel empty">还没有模板。在短片的「脚本」页点“另存为模板”即可创建。</div>
    <div v-else class="plist"><div v-for="x in list" :key="x.id" class="pcard stack">
      <div class="row spread"><strong>{{ x.name }}</strong><span class="muted small">{{ x.ratio }} · {{ x.duration }} 秒</span></div>
      <div v-if="x.style" class="small">{{ x.style }}</div>
      <div v-if="x.characters.length" class="muted small">{{ x.characters.map(c => c.name + '：' + c.appearance).join('；') }}</div>
      <div v-if="x.assets.length" class="refs"><img v-for="a in x.assets" :key="a.id" :src="a.url" :alt="a.name" :title="a.name"></div>
      <div class="row spread"><span class="muted small">{{ x.owner }}</span><div class="row">
        <button v-if="mine(x)" class="ghost danger" @click="del(x)">删除</button><button class="primary" @click="use(x)">用它新建</button></div></div>
    </div></div></div>`,
};

// ---------- 素材库 ----------
const LibraryPage = {
  components: { Chips, PageHead },
  setup() {
    const list = ref(null), filter = ref("全部"), form = reactive({ name: "", kind: store.cfg.asset_kinds[0] }), file = ref(null);
    const load = async () => { try { list.value = await api("/api/assets"); } catch (e) { fail(e); list.value = []; } };
    onMounted(load);
    const shown = computed(() => (list.value || []).filter((x) => filter.value === "全部" || x.kind === filter.value));
    const mine = (x) => store.cfg.role === "admin" || !x.owner || x.owner === store.cfg.user;
    async function upload() {
      const f = file.value && file.value.files[0];
      if (!f) return toast("请选择图片", true);
      const fd = new FormData(); fd.set("name", form.name.trim()); fd.set("kind", form.kind); fd.set("file", f);
      try { await api("/api/assets", { method: "POST", form: fd }); toast("已上传"); form.name = ""; file.value.value = ""; load(); } catch (e) { fail(e); }
    }
    async function del(x) { if (!confirm(`删除素材「${x.name}」？已用它的短片不受影响。`)) return;
      try { await api(`/api/assets/${x.id}`, { method: "DELETE" }); load(); } catch (e) { fail(e); } }
    return { list, filter, form, file, shown, mine, upload, del, kinds: ["全部", ...store.cfg.asset_kinds], cfg: store.cfg };
  },
  template: `<div class="page"><PageHead title="素材库" sub="团队共享的角色、产品、场景、风格图，新建短片时可直接选作参考图" />
    <div class="panel"><h2>上传素材</h2>
      <div class="grid3"><div><label>名称</label><input type="text" v-model="form.name" placeholder="例如：店主小林正面"></div>
        <div><label>类型</label><select v-model="form.kind"><option v-for="k in cfg.asset_kinds" :key="k">{{ k }}</option></select></div>
        <div><label>图片</label><input type="file" ref="file" accept="image/png,image/jpeg,image/webp"></div></div>
      <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="primary" @click="upload">上传</button></div></div>
    <div class="toolbar"><Chips :options="kinds" v-model="filter" /></div>
    <div v-if="list === null" class="muted">加载中…</div>
    <div v-else-if="!shown.length" class="muted">暂无素材</div>
    <div v-else class="lib-grid"><div v-for="x in shown" :key="x.id" class="lib-item"><img :src="x.url" :alt="x.name" loading="lazy">
      <div class="row spread" style="padding:8px 10px"><div><div class="small">{{ x.name }}</div><div class="muted small">{{ x.kind }} · {{ x.owner }}</div></div>
        <button v-if="mine(x)" class="ghost danger" @click="del(x)">删除</button></div></div></div></div>`,
};

// ---------- 成本看板 ----------
const StatsPage = {
  components: { PageHead, DataTable, Badge },
  setup() {
    const d = ref(null);
    onMounted(async () => { try { d.value = await api("/api/stats"); } catch (e) { fail(e); } });
    const KIND = { script: "脚本 DeepSeek", image: "场景图 Seedream", video: "视频 Seedance", tts: "配音 TTS" };
    const maxMonth = computed(() => Math.max(1, ...(d.value ? d.value.by_month.map((m) => m.spent_usd) : [1])));
    const rate = (r) => (r.total ? Math.round((r.done / r.total) * 100) + "%" : "–");
    const dur = (v) => (v == null ? "–" : v < 90 ? `${v} 秒` : `${(v / 60).toFixed(1)} 分`);
    return { d, KIND, maxMonth, rate, dur, usd, P_STATUS };
  },
  template: `<div class="page"><PageHead title="成本看板" />
    <div v-if="!d" class="muted">加载中…</div>
    <template v-else>
      <div v-if="!d.priced" class="warnline" style="margin-bottom:12px">未配置模型单价，以下花费偏低，仅供参考。</div>
      <div class="tiles">
        <div class="tile"><div class="muted small">累计花费</div><div class="big">{{ usd(d.totals.spent_usd) }}</div><div class="muted small">¥{{ d.totals.spent_cny }}</div></div>
        <div class="tile"><div class="muted small">本月花费</div><div class="big">{{ usd(d.month.spent_usd) }}</div>
          <div class="muted small">{{ d.month.team_budget_usd ? '团队月预算 ' + usd(d.month.team_budget_usd) + (d.month.warn ? ' · 已超 80%' : '') : '未设团队月预算' }}</div></div>
        <div class="tile"><div class="muted small">短片</div><div class="big">{{ d.totals.projects }} 部</div><div class="muted small">已完成 {{ d.totals.done }} 部</div></div>
        <div class="tile"><div class="muted small">每部成片平均</div><div class="big">{{ d.totals.avg_usd_per_done == null ? '–' : usd(d.totals.avg_usd_per_done) }}</div><div class="muted small">上限 {{ usd(d.budget_usd) }}</div></div>
        <div class="tile"><div class="muted small">视频重生成</div><div class="big">{{ d.totals.video_shots ? Math.round(d.totals.video_regens / d.totals.video_shots * 100) + '%' : '–' }}</div>
          <div class="muted small">{{ d.totals.video_regens }} 次 / {{ d.totals.video_shots }} 个镜头</div></div>
      </div>
      <div class="grid2">
        <div class="panel"><h2>按月</h2><div v-if="!d.by_month.length" class="muted">暂无数据</div>
          <div v-for="m in d.by_month" :key="m.month" class="bar-row"><span>{{ m.month }}</span><span class="bar"><i :style="{ width: m.spent_usd / maxMonth * 100 + '%' }"></i></span><span class="bar-val">{{ usd(m.spent_usd) }}</span></div></div>
        <div class="panel"><h2>按模型</h2><DataTable :cols="[{ key: 'kind', label: '类别', fmt: k => KIND[k] || k }, { key: 'spent_usd', label: '花费', cls: 'r', fmt: usd }]" :rows="d.by_kind" /></div>
      </div>
      <div class="grid2">
        <div class="panel"><h2>按成员</h2><DataTable :cols="[{ key: 'user', label: '成员' }, { key: 'projects', label: '短片', cls: 'r' }, { key: 'spent_usd', label: '花费', cls: 'r', fmt: usd }]" :rows="d.by_user" /></div>
        <div class="panel"><h2>任务成功率</h2><DataTable :cols="[{ key: 'label', label: '任务' }, { key: 'total', label: '次数', cls: 'r' }, { key: 'done', label: '成功率', cls: 'r', fmt: (v, r) => rate(r) }, { key: 'avg_s', label: '平均耗时', cls: 'r', fmt: dur }]" :rows="d.jobs" /></div>
      </div>
      <div class="panel"><h2>花费最多的短片</h2>
        <DataTable :cols="[{ key: 'title', label: '短片' }, { key: 'owner', label: '成员' }, { key: 'status', label: '状态' }, { key: 'spent_usd', label: '花费', cls: 'r', fmt: usd }]" :rows="d.top_projects">
          <template #title="{ row }"><a :href="'#/p/' + row.id">{{ row.title }}</a></template>
          <template #status="{ row }"><Badge :map="P_STATUS" :value="row.status" /></template></DataTable></div>
    </template></div>`,
};

// ---------- 成员 ----------
const MembersPage = {
  components: { PageHead },
  setup() {
    const cfg = store.cfg, users = ref(null), sso = ref(null), pw = ref(""), nu = reactive({ name: "", password: "", role: "member" });
    const load = async () => {
      try {
        if (cfg.auth === "oidc" && cfg.role === "admin") sso.value = await api("/api/sso-users");
        else if (cfg.auth === "accounts" && cfg.role === "admin") users.value = await api("/api/users");
      } catch (e) { fail(e); }
    };
    onMounted(load);
    const patch = async (name, body, msg) => { try { await api(`/api/users/${encodeURIComponent(name)}`, { method: "PATCH", body }); toast(msg); load(); } catch (e) { fail(e); } };
    const setRole = async (u, role) => { try { await api(`/api/sso-users/${encodeURIComponent(u.sub)}`, { method: "PATCH", body: { role } }); toast("角色已修改"); load(); } catch (e) { fail(e); } };
    const resetPw = (u) => { const v = prompt(`为 ${u.name} 设置新密码（至少 8 位）`); if (v) patch(u.name, { password: v }, "密码已重置"); };
    async function addUser() { try { await api("/api/users", { method: "POST", body: { ...nu, name: nu.name.trim() } }); toast("已添加"); nu.name = nu.password = ""; load(); } catch (e) { fail(e); } }
    async function myPw() { try { await api(`/api/users/${encodeURIComponent(cfg.user)}`, { method: "PATCH", body: { password: pw.value } }); toast("密码已修改，浏览器会要求重新登录"); pw.value = ""; } catch (e) { fail(e); } }
    const fmt = (t) => new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }).slice(0, 16);
    return { cfg, users, sso, pw, nu, setRole, resetPw, patch, addUser, myPw, fmt };
  },
  template: `<div class="page"><PageHead :title="cfg.role === 'admin' ? '成员' : '我的账号'" />
    <div v-if="cfg.auth === 'oidc'" class="panel stack">
      <div>使用 ainewimage.com 全局账号登录。成员第一次打开本系统后会出现在下面的名单里。</div>
      <div class="muted small">.env 里 <code>OIDC_ADMINS</code> 列出的人固定为管理员；其他人默认是成员，管理员可在这里调整。成员能看所有短片，只能修改自己创建的。</div>
      <table v-if="sso" class="tbl"><thead><tr><th>成员</th><th>邮箱</th><th>最近登录</th><th>角色</th><th class="r">操作</th></tr></thead><tbody>
        <tr v-for="u in sso" :key="u.sub"><td>{{ u.name }}<span v-if="u.name === cfg.user" class="muted small">（我）</span></td><td class="muted small">{{ u.email }}</td>
          <td class="muted small">{{ fmt(u.last_seen) }}</td><td>{{ u.role === 'admin' ? '管理员' : '成员' }}<span v-if="u.fixed_admin" class="muted small">（固定）</span></td>
          <td class="r"><button v-if="!u.fixed_admin" class="ghost" @click="setRole(u, u.role === 'admin' ? 'member' : 'admin')">{{ u.role === 'admin' ? '设为成员' : '设为管理员' }}</button></td></tr>
      </tbody></table>
    </div>
    <div v-else-if="cfg.auth !== 'accounts'" class="panel stack">
      <p>当前{{ cfg.auth === 'password' ? '使用共用口令登录，所有人都是管理员' : '没有启用登录' }}。</p>
      <p class="muted small">正式使用建议改为全局账号登录：.env 设置 AUTH_MODE=oidc 及身份中心参数后重启。</p></div>
    <template v-else>
      <div v-if="cfg.role === 'admin' && users" class="panel"><table class="tbl"><thead><tr><th>成员</th><th>角色</th><th>状态</th><th class="r">操作</th></tr></thead><tbody>
        <tr v-for="u in users" :key="u.name"><td>{{ u.name }}<span v-if="u.name === cfg.user" class="muted small">（我）</span></td>
          <td>{{ u.role === 'admin' ? '管理员' : '成员' }}</td><td><span class="badge" :class="u.disabled ? 'bad' : 'ok'">{{ u.disabled ? '已停用' : '正常' }}</span></td>
          <td class="r"><button class="ghost" @click="resetPw(u)">重置密码</button>
            <button class="ghost" @click="patch(u.name, { role: u.role === 'admin' ? 'member' : 'admin' }, '角色已修改')">{{ u.role === 'admin' ? '设为成员' : '设为管理员' }}</button>
            <button v-if="u.name !== cfg.user" class="ghost danger" @click="patch(u.name, { disabled: !u.disabled }, u.disabled ? '已启用' : '已停用')">{{ u.disabled ? '启用' : '停用' }}</button></td></tr>
      </tbody></table></div>
      <div v-if="cfg.role === 'admin'" class="panel stack"><h2>添加成员</h2>
        <div class="grid3"><input type="text" v-model="nu.name" placeholder="用户名"><input type="password" v-model="nu.password" placeholder="初始密码，至少 8 位">
          <select v-model="nu.role"><option value="member">成员</option><option value="admin">管理员</option></select></div>
        <div class="row" style="justify-content:flex-end"><button class="primary" @click="addUser">添加</button></div></div>
      <div class="panel stack"><h2>修改我的密码</h2><div class="row"><div class="grow"><input type="password" v-model="pw" placeholder="至少 8 位"></div>
        <button class="primary" @click="myPw">保存</button></div></div>
    </template></div>`,
};

// ---------- 系统 ----------
const SystemPage = {
  components: { PageHead },
  setup() {
    const d = ref(null), pinging = ref(false);
    const load = async (ping) => { pinging.value = ping; try { d.value = await api("/api/system" + (ping ? "?ping=1" : "")); } catch (e) { fail(e); } pinging.value = false; };
    onMounted(() => load(false));
    const mark = { ok: ["✓", "ok"], warn: ["!", "todo"], fail: ["✗", "bad"] };
    return { d, pinging, load, mark };
  },
  template: `<div class="page"><PageHead title="系统自检" sub="检查 FFmpeg、Key、单价、模型与磁盘；连通测试只读取列表接口，不产生生成费用">
      <button :disabled="pinging" @click="load(true)">{{ pinging ? '测试中…' : '测试接口连通' }}</button></PageHead>
    <div v-if="!d" class="muted">加载中…</div>
    <div v-else class="panel"><table class="tbl"><tbody><tr v-for="it in d.items" :key="it.name">
      <td style="width:40px"><span class="badge" :class="mark[it.level][1]">{{ mark[it.level][0] }}</span></td>
      <td style="width:130px">{{ it.name }}</td><td class="small">{{ it.detail }}</td></tr></tbody></table>
      <div class="muted small" style="margin-top:10px">回收站里有 {{ d.trash }} 个已删除项目（data/trash）。后台任务线程 {{ d.workers }} 个。</div></div></div>`,
};

// ---------- 外壳：左侧菜单 + 右侧页面 ----------
const PAGES = { list: ListPage, new: NewPage, project: ProjectPage, templates: TemplatesPage, library: LibraryPage,
  stats: StatsPage, members: MembersPage, system: SystemPage };

const App = {
  setup() {
    const menu = computed(() => {
      const c = store.cfg, admin = c.role === "admin";
      return [
        { group: "创作", items: [["new", "#/new", "新建短片", "＋"], ["list", "#/", "全部短片", "▤"]] },
        { group: "资产", items: [["templates", "#/templates", "风格模板", "◇"], ["library", "#/library", "素材库", "▦"]] },
        { group: "管理", items: [["stats", "#/stats", "成本看板", "▥"],
          ...(c.auth !== "open" || admin ? [["members", "#/members", admin ? "成员" : "我的账号", "◎"]] : []),
          ...(admin ? [["system", "#/system", "系统自检", "⚙"]] : [])] },
      ];
    });
    const active = (name) => store.route.name === name || (name === "list" && store.route.name === "project");
    const page = computed(() => PAGES[store.route.name] || ListPage);
    const pageKey = computed(() => store.route.name + (store.route.params.id || ""));
    return { store, menu, active, page, pageKey, go };
  },
  template: `<div class="layout" :class="{ 'menu-open': store.menuOpen }">
    <aside class="sidebar">
      <div class="brand"><span class="logo">▶</span>AI 短片工作台</div><div class="brand-sub">AI 视频创作中心</div>
      <nav class="menu"><div v-for="g in menu" :key="g.group" class="menu-group"><div class="menu-title">{{ g.group }}</div>
        <a v-for="m in g.items" :key="m[0]" :href="m[1]" class="menu-item" :class="{ active: active(m[0]) }" @click="store.menuOpen = false">
          <span class="ico">{{ m[3] }}</span>{{ m[2] }}</a></div></nav>
      <div class="side-foot" v-if="store.cfg.user || store.cfg.logout">
        <div class="who"><span class="avatar">{{ (store.cfg.user || '?').slice(0, 1) }}</span>
          <div><div>{{ store.cfg.user || '未登录' }}</div><div class="muted small">{{ store.cfg.role === 'admin' ? '管理员' : '成员' }}</div></div></div>
        <a v-if="store.cfg.logout" class="logout" :href="store.cfg.logout">退出</a>
      </div>
    </aside>
    <div class="scrim" @click="store.menuOpen = false"></div>
    <main class="content">
      <div class="topbar-m"><button class="ghost burger" @click="store.menuOpen = !store.menuOpen" aria-label="菜单">☰</button><span>AI 短片工作台</span></div>
      <div v-if="store.cfg.demo" class="demobar">演示模式：不调用真实接口、不花钱，图片和视频都是占位素材。正式使用前请关闭 DEMO_MODE。</div>
      <component :is="page" :key="pageKey" v-bind="store.route.params" />
    </main>
    <div v-if="store.toast" class="toast" :class="{ bad: store.toast.bad }">{{ store.toast.msg }}</div>
  </div>`,
};

(async function boot() {
  try {
    store.cfg = await api("/api/config");
    store.route = parseRoute();
    createApp(App).mount("#app");
  } catch (e) {
    document.getElementById("app").textContent = "无法连接服务：" + e.message;
  }
})();
