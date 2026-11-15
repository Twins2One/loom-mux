"use strict";

const $ = (id) => document.getElementById(id);
let DATA = null;            // 当前项目 state
let CUR = null;             // 当前打开的文件 {rel, editable, chapter}
let JOURNEY = null;         // 伙伴面板旅程状态缓存(/api/journey/state)
let _lastLearnChapter = null;  // 最近一次 learn 的章号(供撤销)
let _rewriteSel = null;        // 当前重写的选区 {start, end, span}
let _rewriteText = "";         // 当前重写候选
let _dirty = false;            // 编辑器是否有未保存改动
let _editorOrigin = null; // {root,rel,origin}: only trusted editor input is manual
function setEditorOrigin(origin) {
  _editorOrigin = CUR && DATA ? { root: DATA.root, rel: CUR.rel, origin, content: $("editor").value } : null;
}
function editorSaveOrigin(root, rel) {
  return _editorOrigin && _editorOrigin.root === root && _editorOrigin.rel === rel && _editorOrigin.content === $("editor").value ? _editorOrigin.origin : "agent";
}
let _saveTimer = null;         // 自动保存 debounce
let _seedMode = "sample";      // seed 来源:sample | inherit
let _lastDebugReport = null;   // 写章流里先到的 debug_report 事件,chapter_done 后延时弹报告
let _navYield = false;   // 居中态让位(会话级,换书清零;不落 localStorage)
let _weaving = false;    // 织章中(领航员隐身)
let _navPopOpen = false; // 球浮层开合(重画后按此恢复)
let _navAutoAsked = new Set(); // 居中态自动出题去重:每段至多自动跑一次 LLM,防降级卡无限重拉

// ---------- 书房伙伴对话(P3,与卡片机并存)----------
let PARTNER = null;            // 伙伴对话事件缓存({events:[...]}),对应当前书;null=尚未加载
let _partnerRoot = null;       // PARTNER 所属书 root(换书判据,同 JOURNEY 缓存套路,防串书)
let _partnerBusy = false;      // 一轮 say() 请求进行中:输入框/发送键置灰,阻止并发发送
let _partnerGen = 0;           // 对话世代号:每轮 partnerSay 自增;归档「另起一段对话」/ 换书时同步前移,
                                // 旧世代流里迟到的事件一律作废(比 DATA.root 更细——同一本书内新旧会话也能区分)
let _partnerDelta = "";        // 当前轮 assistant_delta 累积预览(未落盘,权威事件一到就清空)
// 书房角色对话的服务端代际口径(2026-11-08 复审核对):agentCenterSay 与快照恢复共用——
// 「另起一段对话」是前端归档(jsonl),服务端任务始终落「默认」目录=服务端当前代际(§13.2)。
// 将来若后端引入真实多代际,两处必须同步改这一个常量。
const _AGENT_CONV = "默认";
let _partnerReasoning = "";    // 当前轮 reasoning_delta 累积(v2 思考层,纯 UI 灰字,transient;权威事件一到清空)
let _partnerAbort = null;      // 本轮 AbortController(P1.5 停止键用);轮结束置 null
let _partnerStopped = false;   // 上一轮被用户「停」→ 渲染尾部出「· 已停止 ·」;换书/另起/再发清零
let _partnerAutoOpened = new Set();   // 本次会话已自动开场过的书 root(前端侧去重;服务端空 text 双保险)
let _pcInputEl = null;         // 当前渲染的对话输入框引用(供「改一改」把候选内容填进去)

// 输入区跨重绘保留草稿和光标；异步回复结束只在用户未转移焦点时接回输入。
const _chatDrafts = new Map();
let _chatFocusPending = null;
function chatInputSnapshot(card, key) {
  const old = card.querySelector("textarea.pc-input");
  const active = document.activeElement;
  if (old && old.dataset.draftKey) {
    _chatDrafts.set(old.dataset.draftKey, { value: old.value, start: old.selectionStart, end: old.selectionEnd });
    if (active === old && old.dataset.draftKey === key) _chatFocusPending = { key, anchor: null };
  }
  return active;
}
function fitChatInput(ta) {
  ta.style.height = "auto";
  const cap = Math.min(220, window.innerHeight * 0.3);
  ta.style.height = Math.min(ta.scrollHeight, cap) + "px";
  ta.style.overflowY = ta.scrollHeight > cap ? "auto" : "hidden";
}
function mountChatInput(ta, key, previousActive) {
  ta.dataset.draftKey = key;
  const draft = _chatDrafts.get(key);
  if (draft && !ta.value) ta.value = draft.value;
  ta.addEventListener("input", () => {
    _chatDrafts.set(key, { value: ta.value, start: ta.selectionStart, end: ta.selectionEnd });
    fitChatInput(ta);
  });
  fitChatInput(ta);
  const active = document.activeElement;
  const pending = _chatFocusPending;
  const neutral = !active || active === document.body || active === document.documentElement;
  const eligible = pending && pending.key === key && (neutral || active === pending.anchor || active === previousActive && previousActive?.classList?.contains("pc-input"));
  if (!ta.disabled && eligible && ta.getClientRects().length && !document.querySelector(".overlay:not(.hidden)")) {
    ta.focus({ preventScroll: true });
    if (draft) ta.setSelectionRange(draft.start, draft.end);
    _chatFocusPending = null;
  } else if (pending && !neutral && active !== pending.anchor && active !== previousActive) {
    _chatFocusPending = null;
  }
}
document.addEventListener("focusin", (ev) => {
  if (_chatFocusPending && ev.target !== _chatFocusPending.anchor && !ev.target.matches?.("textarea.pc-input")) _chatFocusPending = null;
});
document.addEventListener("pointerdown", (ev) => {
  if (_chatFocusPending && !ev.target.closest?.(".pc-input-row")) _chatFocusPending = null;
});
window.addEventListener("resize", () => {
  document.querySelectorAll("textarea.pc-input").forEach((ta) => fitChatInput(ta));
});
function chatKey(role) { return `${DATA && DATA.root}|${role}`; }
const ROLE_WAIT_LINES = {
  "领航员": ["正在梳理这本书的方向…", "正在核对地基里还缺什么…", "正在想下一步怎么聊更清楚…"],
  "设定师": ["正在核对世界规则…", "正在梳理人物与设定的关系…", "正在检查设定是否自洽…"],
  "大纲师": ["正在梳理故事主线…", "正在对照卷纲与章纲…", "正在推敲冲突和转折…"],
  "战斗设计师": ["正在对照战力与规则…", "正在分析招式间的克制关系…", "正在推敲战斗的节奏与地形…"],
  "情绪节奏设计师": ["正在梳理情绪起伏…", "正在分析铺垫与释放的节拍…", "正在对照人物的情感走向…"],
  "编辑总监": ["正在审视全书结构…", "正在对照前后情节的一致性…", "正在梳理伏笔与回收…"],
  "批评家": ["正在检查因果链…", "正在对照人物言行…", "正在寻找设定与情节的疑点…"],
  "市场分析师": ["正在分析题材与读者期待…", "正在梳理作品卖点…", "正在对照市场判断的依据…"],
  "人物侧写师": ["正在梳理人物动机与关系…", "正在对照人物卡与世界规则…", "正在细化人物之间的互动…"],
};
const _roleWaitStarted = new Map();
function roleWaitText(role) {
  if (!_roleWaitStarted.has(role)) _roleWaitStarted.set(role, Date.now());
  const lines = ROLE_WAIT_LINES[role] || [`${role}正在梳理思路…`, `${role}正在核对已有信息…`];
  return lines[Math.floor((Date.now() - _roleWaitStarted.get(role)) / 4500) % lines.length];
}
function roleThinking(role) {
  const el = document.createElement("div");
  el.className = "pc-thinking";
  el.dataset.waitRole = role;
  el.innerHTML = zhaoIconHtml() + `<span>${escHtml(roleWaitText(role))}</span>`;
  return el;
}
setInterval(() => {
  document.querySelectorAll(".pc-thinking[data-wait-role]").forEach((el) => {
    const span = el.querySelector("span");
    if (span) span.textContent = roleWaitText(el.dataset.waitRole);
  });
}, 1500);

// ---------- 领航员在场形态:navMode 三态推导(hidden/center/float)----------
// ── 角色对话的居中界面（作者 2026-10-20 需求 B）────────────────────────────
// 为什么改：原先点角色弹的是浮窗，作者要的是「原版 loom 刚开书时书房伙伴那个界面」
// （主视图居中那块）。于是把「当前正在跟谁聊」提成一个状态，navMode 据此把主视图让出来。
// 领航员 = 走原来的书房伙伴通道；作者智库 = 走 agent chat 通道（人数以 AUTHOR_AGENTS 为准）。两者共用同一套 DOM 样式。
let _navRole = "领航员";        // 当前居中对话的是谁
let _navLockedSlot = "";        // 丁方案：这次对话锁定的落点
let _navForceCenter = false;    // 用户主动点开角色对话 → 强制占主视图（含领航员本身）
let _agentCenter = null;        // { role, events:[], busy:false, loadError:"" }

function navMode() {
  if (!DATA) return "hidden";
  if (_weaving) return "hidden";
  if (_navForceCenter) return "center";                      // 主动打开的角色对话
  if (_navRole && _navRole !== "领航员") return "center";     // 作者智库对话
  if (DATA.writing_unlocked === false) return _navYield ? "float" : "center";
  return "float";
}
function journeyHost() {
  return navMode() === "center" ? $("nav-center-card") : $("nav-pop-card");
}

// ---------- 领航员:可选中可复制 + 正文阶段可展成右侧栏 ----------
// 为什么需要「选区守卫」:面板每来一个流式增量就重绘一次(innerHTML=""),选区会被一起清掉
// ——用户一选中,手一松字就没了。所以:有选区在面板里时,这一帧不重绘。
function _selInCard(card) {
  try {
    const s = window.getSelection && window.getSelection();
    if (!card || !s || !s.rangeCount || s.isCollapsed) return false;
    const node = s.getRangeAt(0).commonAncestorContainer;
    return card.contains(node.nodeType === 1 ? node : node.parentNode);
  } catch (e) { return false; }
}
// 因为「有选区就不重绘」而欠下的那一帧,等选区消失后补上——否则流式已结束的面板会一直停在旧内容。
let _navSkipPaint = false, _navSelBound = false;
function _bindNavSelWatch() {
  if (_navSelBound) return;
  _navSelBound = true;
  document.addEventListener("selectionchange", () => {
    if (!_navSkipPaint || _selInCard(journeyHost())) return;
    _navSkipPaint = false;
    paintJourney();
  });
}
function _navSideWanted() { return localStorage.getItem("loom.navSide") === "1"; }
let _navRO = null, _navWt = null;
function applyNavSide() {
  const pop = $("nav-popover");
  if (!pop) return;
  const on = _navSideWanted();
  pop.classList.toggle("nav-side", on);
  // body 上的类只在「侧栏真的在屏上」时加:中心模式下浮层是隐藏的,
  // 那时候不该顺手把领航员球也藏了(会变成找不到入口)。
  document.body.classList.toggle("nav-side-on", on && !pop.classList.contains("hidden"));
  const b = $("nav-side-toggle");
  if (b) b.textContent = on ? "⇤ 收回" : "⇥ 展开";
  if (!on) { pop.style.width = ""; if (_navRO) _navRO.disconnect(); return; }
  // 拖宽的宽度记在这儿:不记的话每次刷新都弹回 440px,看着像「自己乱跳」。
  // 同时写进 --nav-side-w:底部织章状态条要靠它重新居中(见 style.css .run-strip)。
  const w = parseInt(localStorage.getItem("loom.navSideW") || "", 10);
  const useW = w >= 320 ? Math.min(w, Math.round(window.innerWidth * 0.8)) : 440;
  pop.style.width = useW + "px";
  document.body.style.setProperty("--nav-side-w", useW + "px");
  if (window.ResizeObserver) {
    if (!_navRO) {
      _navRO = new ResizeObserver(() => {
        if (!_navSideWanted()) return;
        clearTimeout(_navWt);
        _navWt = setTimeout(() => {
          const cur = Math.round(pop.getBoundingClientRect().width);
          if (cur >= 320) {
            localStorage.setItem("loom.navSideW", String(cur));
            document.body.style.setProperty("--nav-side-w", cur + "px");
          }
        }, 250);
      });
    }
    _navRO.disconnect();
    _navRO.observe(pop);
  }
}
function toggleNavSide() {
  localStorage.setItem("loom.navSide", _navSideWanted() ? "0" : "1");
  applyNavSide();
  renderJourney();
  if (_navPopOpen) paintNavPopList();
}
async function copyNavText() {
  const card = journeyHost();
  const text = (card ? card.innerText : "").trim();
  if (!text) { toast("这一段还没有可复制的文字"); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制这段文字");
  } catch (e) {
    // 剪贴板权限被拒(浏览器/WebView 都可能) → 退回「全选」,让用户自己 Ctrl+C
    try {
      const r = document.createRange();
      r.selectNodeContents(card);
      const s = window.getSelection();
      s.removeAllRanges(); s.addRange(r);
      toast("已全选,按 Ctrl+C 复制");
    } catch (e2) { toast("复制失败:" + (e && e.message ? e.message : e), true); }
  }
}
// 对话式是伙伴面板的唯一形态(老版问答卡已下线,不再提供切回入口)——恒返回 chat,
// 哪怕旧 localStorage 残留 "card" 也忽略,免把用户困在没有出口的卡片机里。
function partnerUiMode() { return "chat"; }

// ---------- 图标(iconfont Symbol)----------
// 单一改名处:HTML 里写 <span class="ico" data-ico="export">,JS 里用 icon("export")。
// 若你在 iconfont 项目里给图标起的 id 与右值不同,只改这张表即可(无需动各处调用)。
const IC = {
  sample: "icon-book",        key: "icon-key",
  theme: "icon-sun",          themeDark: "icon-moon",
  focus: "icon-focus",        focusExit: "icon-fullscreen-exit",
  chapters: "icon-doc",       writeNext: "icon-arrow-right",
  export: "icon-export",      backup: "icon-save",
  brain: "icon-brain",        fingerprint: "icon-fingerprint",  seed: "icon-magic",
  skills: "icon-tool",        agents: "icon-robot",
  search: "icon-search",      rewrite: "icon-scissors",         learn: "icon-trend-up",
  arrowUp: "icon-arrow-up",   arrowDown: "icon-arrow-down",      close: "icon-close",
  pin: "icon-pin",            edit: "icon-edit",
  outline: "icon-doc",        // 本章细纲(分镜)
  check: "icon-check",        cross: "icon-cross",
  redo: "icon-refresh",       warn: "icon-warning",
  play: "icon-play",          skip: "icon-skip",
  chevron: "icon-chevron-right",  // 折叠区展开/收起(展开时 CSS 转 90°)
  back: "icon-arrow-left",        // 回首页(退出当前书 / 换一本)
  history: "icon-history",        // 本章版本历史
  plus: "icon-plus",              // 插入空章
  trash: "icon-trash",            // 删除章节
};
function icon(name, cls) {
  const id = IC[name] || name;
  return `<svg class="icon${cls ? " " + cls : ""}" aria-hidden="true"><use xlink:href="#${id}"></use></svg>`;
}
// 给页面里所有 [data-ico] 占位符填上 svg(只填一次)
function hydrateIcons(root) {
  (root || document).querySelectorAll("[data-ico]").forEach((el) => {
    if (el._ico) return;
    el.insertAdjacentHTML("afterbegin", icon(el.dataset.ico));
    el._ico = true;
  });
}
function escHtml(s) {
  // 引号也要转:这个函数被用在 data-rel="…" 这类属性里(交叉审查 P2-7)
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
    "\"": "&quot;", "'": "&#39;" }[c]));
}

// ---------- 小工具 ----------
// 错误 code(loom/errors.py 目录键)→ 可操作提示后缀。最小版只盖 余额/鉴权 两族,其余留空。
function codeHint(code) {
  if (!code) return "";
  if (code === "deepseek_insufficient_balance") return "(下一步:去 platform.deepseek.com 充值后重试)";
  if (code === "deepseek_auth_failed" || code === "deepseek_key_missing" || code === "openai_compat_key_missing")
    return "(下一步:检查设置里的 API Key)";
  return "";
}

/* ── 通用「后台在干活」提示条(2026-10-21 需求确认) ─────────────────────────
   原来只有「写章」有底部状态条,而且得先点「收起,后台织」才看得见。
   重排章纲 / 大纲复核 / 蒸馏文风 / 导出 / 通读全书这些长任务**完全没有反馈**,
   点了像没反应,几十秒后突然弹个错。
   做法:不逐个改调用点,而是挂在 jreq 上——任何请求超过 1.2 秒就亮条,回来就收。
   只读展示,不拦人、不禁用任何按钮;点「展开」只有织章时才有意义。 */
// [路径片段, 谁在干(角色), 干什么]
// 2026-10-21 需求确认:底部条要写清"XXX 正在思考…XXs",光写任务名不够。
const _SLOW_LABELS = [
  ["/api/write", "", "织章"], ["/api/pipeline", "", "织章"],
  ["/api/chains/reroll", "章节细纲师", "重排章纲"],
  ["/api/chains", "领航员", "检查联动"],
  ["/api/align", "大纲师", "大纲复核"],
  ["/api/style/distill", "量文风", "蒸馏文风"],
  ["/api/fullreview", "编辑总监", "通读全书"],
  ["/api/card/expand", "大纲师", "铺章纲"],
  ["/api/volplan", "大纲师", "铺卷纲"],
  ["/api/export", "", "导出"],
  ["/api/snapshot", "", "拍快照"],
  ["/api/diagnose", "查资料", "提炼设定"],
  ["/api/knowledge", "查资料", "查知识库"],
  ["/api/learn", "量文风", "学这章的手改"],
  ["/api/evolve", "领航员", "自进化"],
  // 2026-10-21 补:上面漏了几个真会跑很久的(作者点「重新生成细纲」时底部只显示"处理中")
  ["/api/outline/regen", "章节细纲师", "重新生成细纲"],
  ["/api/card/expand", "大纲师", "重排章纲"],
  ["/api/volplan", "大纲师", "铺卷纲"],
  ["/api/arbitrate", "领航员", "冲突裁定"],
  ["/api/merge/preview", "领航员", "核对合并"],
  ["/api/repair", "领航员", "修复"],
  ["/api/partner/say", "领航员", "对话"],
  ["/api/agent/chat", "", "对话"],
  ["/api/seed", "量文风", "学你的嗓音"],
  ["/api/draft", "领航员", "铺底稿"],
  ["/api/journey", "领航员", "建地基"],
  ["/api/navigator", "领航员", "复核地基"],
  ["/api/brain", "", "改设定"],
  ["/api/chapter", "", "处理本章"],
];
function _slowRunningRole() {
  // 织章时真正在跑的那一棒(从六步浮窗的 pill 上读),比查表准
  const el = document.querySelector("#agent-pills .pill.running .pi-name");
  return el ? el.textContent : "";
}
function _slowWho(url) {
  const hit = _SLOW_LABELS.find(([k]) => String(url).includes(k));
  if (!hit) {          // 命中不到至少要能看出是哪个接口,别写"处理中"糊过去
    const tail = String(url).split("?")[0].replace(/\/$/, "").split("/").pop() || "处理中";
    return { who: "", what: tail };
  }
  const running = _slowRunningRole();
  return { who: running || hit[1], what: hit[2] };
}
let _slowN = 0, _slowT0 = 0, _slowTimer = null, _slowTick = null;
function _slowLabelOf(url) {
  const { who, what } = _slowWho(url);
  return who ? `${who}（${what}）` : what;
}
function _slowElapsed() {
  const s = Math.floor((Date.now() - _slowT0) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}
function _slowBegin(url) {
  _slowN += 1;
  if (_slowN > 1 || _runMinimized) return;      // 并发时只留一个;织章自己有更细的条
  _slowTimer = setTimeout(() => {
    const strip = $("run-strip");
    const nt = $("nav-ball");
    if (!strip || strip.classList.contains("hidden") && _weaving) return;
    _slowT0 = Date.now();
    strip.classList.remove("hidden", "err");
    strip.classList.add("slow");
    $("rs-label").textContent = _slowLabelOf(url) + " 正在思考…";
    $("rs-pills").innerHTML = "";
    $("rs-stop").classList.add("hidden");
    $("rs-expand").classList.add("hidden");
    const tick = () => {
      const lab = $("rs-label");
      if (lab && !strip.classList.contains("hidden")) lab.textContent = `${_slowLabelOf(url)} 正在思考… ${_slowElapsed()}`;
    };
    _slowTick = setInterval(tick, 1000);
    tick();
  }, 1200);
}
function _slowEnd() {
  _slowN = Math.max(0, _slowN - 1);
  if (_slowN > 0) return;
  clearTimeout(_slowTimer); _slowTimer = null;
  clearInterval(_slowTick); _slowTick = null;
  const strip = $("run-strip");
  if (!strip || !strip.classList.contains("slow")) return;   // 织章那条不归这里管
  strip.classList.add("hidden");
  strip.classList.remove("slow");
}

function formatApiError(data, status) {
  const direct = data && (data.error || data.reason || data["说明"]);
  if (typeof direct === "string" && direct.trim()) return direct.trim();
  const detail = data && data.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const lines = detail.map((item) => {
      if (!item || typeof item !== "object") return String(item || "").trim();
      const loc = Array.isArray(item.loc) ? item.loc.map(String).join(".") : "";
      const msg = typeof item.msg === "string" ? item.msg.trim() : "";
      return [loc, msg].filter(Boolean).join("：");
    }).filter(Boolean);
    if (lines.length) return lines.join("；");
  }
  if (detail && typeof detail === "object") {
    try { return JSON.stringify(detail); } catch (_) { /* fall through */ }
  }
  if (data && typeof data.code === "string" && data.code) return data.code;
  return `请求失败 (${status})`;
}

async function jreq(method, url, body) {
  _slowBegin(url);
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  // 没有超时的话,后端一旦挂住,这个 await 永远不回来:面板的「正在花钱」护栏再也不会复位,
  // 连 Esc 都关不掉(交叉审查第 97 轮)。15 分钟对单次模型调用绰绰有余。
  const ctl = new AbortController();
  const _timer = setTimeout(() => ctl.abort(), 15 * 60 * 1000);
  opt.signal = ctl.signal;
  let r;
  try {
    r = await fetch(url, opt);
  } catch (err) {
    clearTimeout(_timer);
    _slowEnd();
    if (err && err.name === "AbortError") throw new Error("等太久没有回应(超过 15 分钟),已放弃这次请求。");
    throw err;
  }
  clearTimeout(_timer);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(formatApiError(data, r.status) + codeHint(data.code));
    e.code = data.code;   // code 原样带上,调用方可细分处理
    e.status = r.status;  // 409 分两种:busy(任务在跑)/ needs_merge(磁盘被改过)
    e.body = data;
    _slowEnd();
    throw e;
  }
  _slowEnd();
  return data;
}
let _toastTimer = 0;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  // 定时器要留句柄:3 秒内连弹两条时,上一条的定时器会把新的一条一起藏掉(看不见)
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => (t.className = "toast hidden"), isErr ? 5200 : 3200);
}
function countChars(s) { return (s.match(/\S/g) || []).length; }  // 非空白字符(含中文/标点)

function parseContextLenInput(value) {
  const raw = String(value == null ? "" : value).trim();
  if (!raw) return 0;
  if (!/^[1-9]\d*$/.test(raw)) {
    const error = new Error("单会话上下文上限必须是正整数；留空使用默认 128000。");
    error.code = "invalid_context_len";
    throw error;
  }
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed)) {
    const error = new Error("单会话上下文上限太大，必须填写安全范围内的正整数。");
    error.code = "invalid_context_len";
    throw error;
  }
  return parsed;
}

function parseContextLenElement(input) {
  if (input && input.validity && input.validity.badInput) {
    const error = new Error("单会话上下文上限必须是正整数；留空使用默认 128000。");
    error.code = "invalid_context_len";
    throw error;
  }
  return parseContextLenInput(input ? input.value : "");
}

function contextLenSummary(limit, source) {
  return `当前 ${limit || 128000} token · ${source || "默认"}`;
}

function isContextLenInputError(error) {
  // 2026-11-16：文案已改名「单会话上下文上限」，这条兜底正则还钉着旧名的开头，
  // 于是永远匹配不上（现在靠 error.code 兜着，但兜底已经死了）。两种写法都认。
  return !!(error && (error.code === "invalid_context_len"
    || /^(?:单会话)?上下文上限(?:必须|太大)/.test(String(error.message || ""))));
}

// ---------- 主题(明暗) ----------
// ---------- 面板可访问性:role / aria-modal / 焦点进去、出来回原处 ----------
// 原来全站没有 role="dialog"、没有焦点陷阱、关闭不回焦,Tab 会跑到遮罩背后的按钮上(P2-3)。
// 这里不手写 id 清单:扫 DOM 里所有 .overlay —— 新加面板自动被覆盖。
let _a11yOpener = null;

function a11yOverlays() {
  const overlays = document.querySelectorAll(".overlay");
  overlays.forEach((o) => {
    if (o.getAttribute("role") !== "dialog") {
      o.setAttribute("role", "dialog");
      o.setAttribute("aria-modal", "true");
      const h = o.querySelector("h3");
      if (h && h.textContent.trim()) o.setAttribute("aria-label", h.textContent.trim());
    }
  });
  return overlays;
}

function _a11yOnChange(o) {
  const open = !o.classList.contains("hidden");
  if (open) {
    if (!o.contains(document.activeElement)) {
      if (!o._a11yOpener) o._a11yOpener = document.activeElement;
      const first = o.querySelector(
        "input:not([type=hidden]), select, textarea, button.primary, button");
      if (first) { try { first.focus({ preventScroll: true }); } catch (e) { first.focus(); } }
    }
  } else if (o._a11yOpener) {
    const back = o._a11yOpener;
    o._a11yOpener = null;
    if (back && back.focus && document.contains(back)) {
      try { back.focus({ preventScroll: true }); } catch (e) { /* 元素没了就算了 */ }
    }
  }
}

// Tab 在面板里循环:不这么做,焦点会溜到遮罩背后的工具栏上,
// 而那个按钮此刻是看不见的(视觉上像是焦点凭空消失了)。
function _a11yFocusable(o) {
  const sel = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]),'
    + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  return [...o.querySelectorAll(sel)].filter((x) => x.getClientRects().length > 0);
}

function _a11yTrap(o, ev) {
  if (ev.key !== "Tab" || o.classList.contains("hidden")) return;
  const f = _a11yFocusable(o);
  if (!f.length) return;
  const first = f[0];
  const last = f[f.length - 1];
  const inside = o.contains(document.activeElement);
  if (ev.shiftKey && (!inside || document.activeElement === first)) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && (!inside || document.activeElement === last)) {
    ev.preventDefault();
    first.focus();
  }
}

function a11yWatch() {
  // 面板如果是运行时 createElement 出来的(回收站就是),启动时那一扫根本看不见它。
  // 所以在 body 上挂一个「新节点就再扫一遍」的观察器(只挂一次,_a11yWatched 保证幂等)。
  if (!a11yWatch._watchingBody) {
    a11yWatch._watchingBody = true;
    new MutationObserver(() => a11yWatch())
      .observe(document.body, { childList: true, subtree: false });
  }
  a11yOverlays().forEach((o) => {
    if (o._a11yWatched) return;
    o._a11yWatched = true;
    new MutationObserver(() => _a11yOnChange(o))
      .observe(o, { attributes: true, attributeFilter: ["class"] });
    o.addEventListener("keydown", (ev) => _a11yTrap(o, ev));
  });
}

function applyKbdHints() {
  // index.html 里写死 ␘K / ␘F / ␘. 在 Windows 上是错的教学
  const set = (id, text, title) => {
    const el = $(id);
    if (!el) return;
    if (text != null) el.textContent = text;
    if (title != null) el.title = title;
  };
  set("kbd-hint", kbd("K"), `命令面板:一切操作都在这(${kbd("K")})`);
  set("btn-focus", null, `专注模式 ${kbd(".")}`);
  set("btn-search", null, `章内搜索 ${kbd("F")}`);
  set("btn-save-file", null, `保存 ${kbd("S")}`);
}

function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("loom_theme", t);
  const b = $("btn-theme"); if (b) b.innerHTML = icon(t === "dark" ? "themeDark" : "theme");
}
function initTheme() {
  const saved = localStorage.getItem("loom_theme");
  const t = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(t);
  applyKbdHints();      // 快捷键提示按平台说人话(Windows 上不许写 ⌘)
  a11yWatch();          // 面板补 role/aria 并管焦点(新加的面板自动覆盖)
}
function toggleTheme() {
  applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
}

// ---------- 专注模式 ----------
function toggleFocus() { document.body.classList.toggle("focus-mode"); }
function exitFocus() { document.body.classList.remove("focus-mode"); }

// ---------- 启动 ----------
window.addEventListener("DOMContentLoaded", () => {
  hydrateIcons();
  initTheme();
  bind();
  fillAgents();        // 14 角色面板渲染(作者 2026-10-20)
  loadGenres();
  renderRecent();       // 欢迎页的书架(用户级注册表)
  loadConnectStatus();  // 欢迎页的接入状态
  loadVersion();        // 设置里显示当前版本号
  autoReopen();         // 自动回到上一本(localStorage 没有就用书架最近一本)
});

async function loadVersion() {
  try {
    const d = await jreq("GET", "/api/version");
    // 本项目是 Loom 的改造分支,产品名与版本号都跟原版分家(2026-10-20 需求确认)
    // 2026-10-21:版本号这一格里现在还挂着「环境自检」小链接,所以只写它自己的 span,
    // 不能对容器 textContent 赋值(那会把链接节点一起冲掉)。
    if (d.version) $("app-version-text").textContent = `Loom-mux v${d.version}`;
  } catch (e) { /* 版本号是装饰,拉不到就留空 */ }
}

async function loadGenres() {
  try {
    const d = await jreq("GET", "/api/genres");
    const sel = $("new-genre");
    (d.genres || []).forEach((g) => {
      const o = document.createElement("option");
      o.value = g; o.textContent = g;
      sel.appendChild(o);
    });
  } catch (e) { /* 题材是可选增益,拉不到就只留"不选题材" */ }
}

function bind() {
  // 开一本书:表单在弹层里(一屏一事);「浏览…」走桌面端原生文件夹对话框(浏览器兜底才手输)
  $("btn-new-book").onclick = () => {
  $("create-error").textContent = "";
  _togglePickButtons();
  loadFavDirs();        // 路径下拉候选(桌面/文档/下载/家目录的绝对路径)
  $("create-overlay").classList.remove("hidden");
  $("new-name").focus();
};
  $("btn-import-book").onclick = () => { $("import-error").textContent = ""; _togglePickButtons(); $("import-overlay").classList.remove("hidden"); };
  $("btn-import-folder").onclick = () => {
    $("folder-confirm").classList.add("hidden");
    _importRouted = null;
    $("folder-name").value = "";
    $("folder-error").textContent = "";
    $("folder-pick").classList.toggle("hidden", !_hasNativePicker());
    $("folder-overlay").classList.remove("hidden");
  };
  //2026-11-03（已有资料夹入口）：书房新建书浮窗「＋已有资料夹」落 / 带标记,这里自动弹开。
  try {
    if (localStorage.getItem("loom_open_folder_import") === "1") {
      localStorage.removeItem("loom_open_folder_import");
      $("btn-import-folder").click();
    }
  } catch (e) { /*隐私模式忽略*/ }
  $("folder-cancel").onclick = () => $("folder-overlay").classList.add("hidden");
  $("folder-pick").onclick = async () => { const p = await window.pywebview.api.pick_folder(); if (p) $("folder-path").value = p; };
  $("folder-path").addEventListener("input", () => { _importRouted = null; $("folder-confirm").classList.add("hidden"); });
  $("folder-scan").onclick = scanFolder;
  $("folder-commit").onclick = commitFolder;
  $("create-cancel").onclick = () => $("create-overlay").classList.add("hidden");
  $("import-cancel").onclick = () => $("import-overlay").classList.add("hidden");
  $("new-parent-pick").onclick = () => pickFolderInto("new-parent");
  $("open-path-pick").onclick = () => pickFolderInto("open-path");
  //2026-11-03（五点①）：目录浏览浮窗按钮
  $("browse-cancel").onclick = () => $("browse-overlay").classList.add("hidden");
  $("browse-up").onclick = async () => {
    // parent 由 browseLoad 记录在按钮显隐;再取一次拿 parent 路径
    try {
      const d = await jreq("POST", "/api/fs/browse", { path: _browsePath });
      if (d.parent != null) await browseLoad(d.parent);
    } catch (e) { $("browse-error").textContent = e.message; }
  };
  $("browse-pick").onclick = () => {
    if (_browseTarget) {
      _browseTarget.value = _browsePath;
      _browseTarget.dispatchEvent(new Event("input"));
    }
    $("browse-overlay").classList.add("hidden");
  };
  $("btn-create").onclick = createProject;
    $("btn-open").onclick = importBook;
  /*2026-11-03（作者裁定⑤）：写作页右上角「回首页」→「回书房」+「书房」按钮删除。
    一个入口就够：btn-close-proj 现在直接跳书房（原 btn-study 的 openStudy 带 health 探活逻辑），
    不再走 showWelcome()（首页=织布机欢迎页，书房才是作者的主界面）。 */
  $("btn-close-proj").onclick = openStudy;
  // 书房入口（2026-10-27，0.8.0）：先用 /api/study/health 探一枪，**通了才跳**。
  // 为什么不用 <a href>：将来启动器要决定"先开哪一页"时只改这一处；
  // 为什么先探：study 路由没挂载时直接跳会白屏，宁可留在原地给一句人话。
  $("btn-welcome-study").onclick = openStudy;
  $("btn-theme").onclick = toggleTheme;
  $("btn-focus").onclick = toggleFocus;
  $("focus-exit").onclick = exitFocus;
  $("btn-settings").onclick = openSettings;
  //2026-11-03（模式终身绑定）：bindWritingModeControls 已删（设置页三档下拉组 #wm-fold 删除）。
  $("settings-close").onclick = () => $("settings-overlay").classList.add("hidden");
$("btn-route-refresh").onclick = loadRoutes;
  $("kbd-hint").onclick = openCmdk;
  // 接入模型(用户级默认后端)
  $("btn-connect").onclick = openConnect;
  $("cn-cancel").onclick = () => $("connect-overlay").classList.add("hidden");
  $("cn-save").onclick = saveConnect;
  $("cn-provider").onchange = cnApplyProvider;
  $("cn-model").onchange = () => cnOnModelSelect();
  $("cn-fetch-models").onclick = cnFetchModels;
  $("cn-probe").onclick = cnProbe;
  $("btn-doctor").onclick = runDoctor;
  $("doctor-close").onclick = () => $("doctor-overlay").classList.add("hidden");
  $("nav-threads").onclick = () => openStudio("timeline");   // 脉络单入口:弹层自带三 tab,侧栏不占三行
  $("nav-mirror").onclick = openMirror;                      // 镜台:与脉络平级,脉络投影故事、它投影你
  $("mirror-close").onclick = () => $("mirror-overlay").classList.add("hidden");
  $("studio-tab-timeline").onclick = () => renderStudio("timeline");
  $("studio-tab-foreshadow").onclick = () => renderStudio("foreshadow");
  $("studio-tab-names").onclick = () => renderStudio("names");
  $("studio-tab-statebook").onclick = () => renderStudio("statebook");
  $("studio-close").onclick = () => $("studio-overlay").classList.add("hidden");
  $("flow-close").onclick = () => $("flow-overlay").classList.add("hidden");
  // 2026-10-21:「查看这一棒的提示词」入口已删(作者:作者不需要知道 agent 提示词这种细节)
  $("btn-save-backend").onclick = saveBackend;
  $("btn-probe").onclick = probeBackend;
  $("btn-fetch-models").onclick = fetchModels;
  // 目标字数小浮窗(2026-10-20):点字数条上的「目标 X」打开
  $("target-save").onclick = saveTargetChars;
  $("target-cancel").onclick = closeTargetPop;
  $("chapter-chars").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveTargetChars(); }
    if (e.key === "Escape") { e.preventDefault(); closeTargetPop(); }
  });
  document.addEventListener("click", (e) => {
    const pop = $("target-pop");
    if (!pop || pop.classList.contains("hidden")) return;
    if (pop.contains(e.target) || (e.target.closest && e.target.closest(".target-btn"))) return;
    closeTargetPop();
  });
  $("model").onchange = onModelSelect;
  $("provider").onchange = () => {
    // 切供应商:从服务端下发的供应商表(DATA.backend.providers)派生默认模型 + 下拉预设 + base_url
    const p = $("provider").value;
    const spec = providerSpec(p);
    rebuildModelPresets(p);
    setModelValue(spec ? spec.default_model : "");
    if (spec && !spec.base_url_locked) $("base-url").value = $("base-url").value || spec.base_url || "";
    applyProviderUI(p);
  };
  $("btn-write-next").onclick = () => writeChapter(DATA.next_chapter, false);
  $("btn-export").onclick = openExport;
  $("export-close").onclick = () => $("export-overlay").classList.add("hidden");
$("export-run").onclick = exportRun;
$("export-plan").onclick = exportPreview;
["export-fmt", "export-split"].forEach((id) => { $(id).onchange = exportPreview; });
$("btn-backup").onclick = backupBook;
  $("btn-trash").onclick = openTrash;
$("btn-facts").onclick = openFacts;
$("facts-close").onclick = () => $("facts-overlay").classList.add("hidden");
$("btn-arb").onclick = openArb;
$("btn-ents").onclick = openEnts;
$("ents-close").onclick = () => $("ents-overlay").classList.add("hidden");
$("ents-reload").onclick = entsLoad;
$("ents-scan").onclick = entsScan;
$("ents-preview").onclick = entsPreview;
$("ents-rename").onclick = entsRename;
$("btn-chdiag").onclick = openChDiag;
$("chdiag-close").onclick = () => $("chdiag-overlay").classList.add("hidden");
$("chdiag-load").onclick = chdiagLoad;
$("chdiag-sync").onclick = chdiagSync;
$("chdiag-lease").onclick = () => chdiagLease("acquire");
$("chdiag-release").onclick = () => chdiagLease("release");
$("arb-close").onclick = () => $("arb-overlay").classList.add("hidden");
$("arb-scan").onclick = arbScan;
$("arb-ai").onclick = arbAi;
$("facts-reload").onclick = factsReload;
$("facts-ingest").onclick = factsIngest;
$("facts-extract").onclick = factsExtract;
$("facts-add").onclick = factsAdd;
$("knows-ask").onclick = knowsAsk;
  $("btn-seed").onclick = openSeed;
// 同一件事两个界面、两套口径(P2-4):侧栏入口统一走完整面板。
// 2026-10-20:文风档案改成"别人的文章 → 文档库 → 逐篇蒸馏",提示语跟着改(原来的"加多份稿"已过时)。
  $("btn-stylepack").onclick = () => openStylePanel();
  $("seed-tab-sample").onclick = () => switchSeed("sample");
  $("seed-tab-reference").onclick = () => switchSeed("reference");
  $("seed-tab-inherit").onclick = () => switchSeed("inherit");
  $("seed-cancel").onclick = () => $("seed-overlay").classList.add("hidden");
  $("seed-go").onclick = doSeed;
  $("btn-save-file").onclick = saveFile;
  $("btn-learn").onclick = () => CUR && learn(CUR.chapter);
  $("btn-history").onclick = openHistory;
  $("history-close").onclick = () => $("history-overlay").classList.add("hidden");
  $("btn-sensitive").onclick = scanSensitive;
  $("btn-agent-rewrite").onclick = openSlotPicker;
  $("btn-migrate-legacy").onclick = migrateLegacyBrain;
  $("btn-preview").onclick = () => CUR && setPreview(!CUR.preview);
  $("btn-outline").onclick = () => CUR && CUR.chapter != null && openOutline(CUR.chapter);
  $("btn-outline-regen").onclick = regenOutline;
  $("btn-debug").onclick = debugChapter;
  $("debug-close").onclick = () => $("debug-overlay").classList.add("hidden");
  $("btn-rewrite").onclick = openRewrite;
  $("btn-brain-rewrite").onclick = openBrainRewrite;
  $("btn-brain-continue").onclick = openBrainContinue;
$("btn-brain-copy").onclick = openBrainCopy;
$("btn-card-expand").onclick = expandCardOutline;
$("btn-import-half").onclick = importHalfDraft;
$("btn-snapshot").onclick = openSnapPanel;
$("btn-style").onclick = openStylePanel;
$("btn-knowledge").onclick = openKnowledge;
$("know-close").onclick = () => {
  if (KNOW_BUSY) { toast("顾问正在想(在花钱):等它回完再关。", true); return; }
  $("know-overlay").classList.add("hidden");
};
$("btn-align").onclick = openAlign;
$("web-search").onclick = webSearch;
$("web-fetch").onclick = webFetch;
$("align-close").onclick = () => {
  if (ALIGN_BUSY) { toast("正在逐章对照(在花钱):等它回完再关。", true); return; }
  $("align-overlay").classList.add("hidden");
};
$("align-go").onclick = runAlign;
$("align-accept").onclick = acceptAlign;
$("align-refresh").onclick = refreshAlign;
$("know-go").onclick = askKnowledge;
$("know-accept").onclick = acceptKnowledge;
$("know-refresh").onclick = refreshKnowledge;
// R10 花钱闸:报了价之后**改了抽法/上限/路径,报价就作废**——不然报 30 次、
// 改全量后一点按全量花,数字还是旧的(交叉审查 P0)。
["half-path", "half-mode", "half-recent", "half-max"].forEach((_id) => {
  const _el = $(_id);
  if (_el) { _el.addEventListener("change", invalidateHalfQuote); _el.addEventListener("input", invalidateHalfQuote); }
});
$("merge-close").onclick = closeMergePanel;
$("merge-save").onclick = mergeSave;
$("merge-take-disk").onclick = () => { $("merge-out").value = $("merge-disk").value; mergeMeta(); };
$("merge-take-mine").onclick = () => { $("merge-out").value = $("merge-mine").value; mergeMeta(); };
$("merge-take-disk-tail").onclick = () => {
  const disk = $("merge-disk").value;
  const mine = $("merge-mine").value;
  $("merge-out").value = disk.replace(/\s*$/, "") + "\n\n" + mine.replace(/^\s*/, "");
  mergeMeta();
};
$("merge-out").addEventListener("input", mergeMeta);
$("style-close").onclick = closeStylePanel;
$("style-refresh").onclick = styleRefresh;
$("style-add").onclick = styleAdd;
// 2026-10-20:文风档案改成「文档库」(粘贴 / 浏览导入 / 勾选逐篇蒸馏 / 删除)
$("style-import").onclick = styleImport;
$("style-browse").onclick = styleBrowse;
$("style-del").onclick = () => styleDeleteTitles(_styleSelArr());
$("style-distill").onclick = styleDistillSelected;
$("style-use").onclick = styleUse;
$("style-sel-all").onchange = () => {
  const on = $("style-sel-all").checked;
  _styleSel = new Set(on ? _styleDocs.map((d) => d["标题"]) : []);
  paintStyleList();
};
// 「浏览…」两个模式都显示:
// 桌面版(pywebview)弹系统多选文件框;浏览器拿不到路径 ⇒ 点了给提示并聚焦输入框(见 styleBrowse)。
$("style-browse").classList.remove("hidden");
// 多档案(2026-10-20 需求确认):下拉切换 / 新建
$("style-lib-add").onclick = styleLibAdd;
$("style-lib").onchange = () => {
  _styleLib = $("style-lib").value || _styleLib;
  _styleSel = new Set();
  styleRefresh();
};
$("snap-close").onclick = closeSnapPanel;
$("snap-make").onclick = snapMake;
  $("btn-vol-summary").onclick = volSummary;
  $("btn-book-summary").onclick = bookSummary;
$("half-close").onclick = () => $("half-overlay").classList.add("hidden");
$("half-do-preview").onclick = halfPreview;
$("half-run").onclick = halfRun;
$("half-stop").onclick = halfStop;
  $("rewrite-cancel").onclick = hideInlinePanel;
  $("rewrite-go").onclick = doRewrite;
  $("rewrite-again").onclick = doRewrite;
  $("rewrite-apply").onclick = applyRewrite;
  $("rewrite-instruction").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const go = $("rewrite-go"), again = $("rewrite-again");
    (!go.classList.contains("hidden") ? go : again).click();   // Enter=跑这次改写(候选出来后=再来一个)
  });
  $("learn-keep").onclick = () => { $("learn-overlay").classList.add("hidden"); openFile("外置大脑/写作指纹.md", true, null); };
  $("learn-revert").onclick = revertLearn;
  $("run-close").onclick = closeRun;
  $("run-minimize").onclick = minimizeRun;
  $("run-stop").onclick = requestStop;
  $("rs-stop").onclick = requestStop;
  $("rs-expand").onclick = () => { if (_stripDone != null) { hideStrip(); closeRun(); } else expandRun(); };
  $("diag-commit").onclick = commitDiagnose;
  $("diag-cancel").onclick = () => $("diagnose-overlay").classList.add("hidden");
  $("nav-ball").onclick = () => { _navPopOpen = !_navPopOpen; renderJourney(); if (_navPopOpen) paintNavPopList(); };
  $("nav-copy").onclick = copyNavText;
  $("nav-side-toggle").onclick = toggleNavSide;
  applyNavSide();   // 启动即按上次的选择摆好(否则要等第一次重绘才生效)
  // 窗口变窄时重算侧栏宽度上限(否则 80vw 的钳制只在切换那一刻生效,缩窗后可能盖满整屏)
  window.addEventListener("resize", () => { if (_navSideWanted()) applyNavSide(); });
  $("nav-panel").onclick = () => openPanel();   // 议事厅:一次问多位同事(2026-10-25)
  // 侧栏指示灯:4 秒读一次「谁在思考 / 谁还在等你」(只读文件尾巴,不调模型)
  setInterval(refreshAgentLamps, 4000);
  // 角色转介由领航员在对话中提示，不再提供独立的「该找谁」入口。
  $("nav-browse").onclick = () => {
    // 「先自己逛逛」：如果是在角色对话里，先退出对话再让位给编辑器
    if (_navForceCenter) {
      _navForceCenter = false;
      _navRole = "领航员";
      _navLockedSlot = "";
      _agentCenter = null;
      markRoleSelected(null);   // 退出角色对话 → 侧边栏不留标记
    }
    _navYield = true;
    renderJourney();
  };

  // 编辑器:实时字数 + 自动保存 + 搜索联动 + 输入退场
  $("editor").addEventListener("input", (ev) => {
    setEditorOrigin(ev.isTrusted ? "manual" : "agent");
    if (!$("rewrite-inline").classList.contains("hidden")) hideInlinePanel();   // 手改了正文,候选的选区偏移已失效
    updateWordCount(); markDirty(); if (_searchOn) runSearch(); noteTyping();
  });
  $("editor").addEventListener("blur", () => document.body.classList.remove("typing"));

  // 朱批浮条:选中正文就地浮出「重写这段/复制」
  const ed = $("editor");
  ed.addEventListener("mouseup", maybeSealBar);
  ed.addEventListener("keyup", maybeSealBar);
  ed.addEventListener("scroll", hideSealBar);
  $("seal-bar").addEventListener("mousedown", (e) => e.preventDefault());  // 别抢走 textarea 的选区
  document.addEventListener("mousedown", (e) => {
    const b = $("seal-bar");
    if (b.classList.contains("on") && !b.contains(e.target) && e.target !== ed) hideSealBar();
  });
  $("seal-rewrite").onclick = () => { hideSealBar(); openRewrite(); };
  $("seal-copy").onclick = async () => {
    const t = ed.value.substring(ed.selectionStart, ed.selectionEnd);
    try { await navigator.clipboard.writeText(t); toast("已复制"); }
    catch (e) { toast("复制失败:" + e.message, true); }
    hideSealBar();
  };

  // 悬停行级「改」:rAF 节流;移到按钮本身不算离开
  ed.addEventListener("mousemove", (e) => {
    if (_lineBtnRaf) return;
    _lineBtnRaf = requestAnimationFrame(() => { _lineBtnRaf = 0; maybeLineEditBtn(e); });
  });
  ed.addEventListener("mouseleave", (e) => { if (e.relatedTarget !== $("line-edit-btn")) hideLineEditBtn(); });
  ed.addEventListener("scroll", hideLineEditBtn);
  $("line-edit-btn").addEventListener("mouseleave", hideLineEditBtn);
  $("line-edit-btn").onclick = lineEditClick;

  // 章内搜索
  $("btn-search").onclick = openSearch;
  $("search-close").onclick = closeSearch;
  $("search-next").onclick = () => gotoMatch(_mi + 1);
  $("search-prev").onclick = () => gotoMatch(_mi - 1);
  $("search-input").addEventListener("input", runSearch);
  $("search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); gotoMatch(e.shiftKey ? _mi - 1 : _mi + 1); }
    if (e.key === "Escape") { e.preventDefault(); closeSearch(); }
  });

  // 命令面板
  $("cmdk-input").addEventListener("input", () => renderCmds($("cmdk-input").value));
  $("cmdk-input").addEventListener("keydown", cmdkKey);

  // 全局快捷键
  document.addEventListener("keydown", globalKeys);

  // 防丢稿:关窗/切走前保住未保存手改
  window.addEventListener("beforeunload", (e) => { if (_dirty) { e.preventDefault(); e.returnValue = ""; } });
  window.addEventListener("blur", () => { if (_dirty) autosave(); });
}

// ---------- 项目 ----------
// 原生文件夹对话框:桌面端(pywebview)有桥就走系统选择器;浏览器兜底保持手输
function _hasNativePicker() { return !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder); }
function _togglePickButtons() {
  const on = _hasNativePicker();
  // 2026-10-20 需求确认:「放在哪个文件夹」后面要给个选文件夹的入口。
  // 桌面版(pywebview)弹系统框;浏览器拿不到绝对路径 ⇒ 按钮留着,点了给候选目录(见 pickFolderInto)。
  $("new-parent-pick").classList.remove("hidden");
  $("open-path-pick").classList.toggle("hidden", !on);
}
async function pickFolderInto(inputId) {
  const inp = $(inputId);
  if (_hasNativePicker()) {
    try {
      const p = await window.pywebview.api.pick_folder();
      if (p) inp.value = p;   // 取消选择 → 保持原值
    } catch (e) { toast("打开文件夹选择器失败:" + e.message, true); }
    return;
  }
  /*2026-11-03（作者五点反馈①）：浏览器模式也弹真目录浏览浮窗
    （POST /api/fs/browse 导航,选完回填）。旧"填候选+toast"口径废除。 */
  openBrowseOverlay(inp);
}

async function createProject() {
  $("create-error").textContent = "";
  const name = $("new-name").value.trim();
  if (!name) { $("create-error").textContent = "先给这本书起个名字。"; $("new-name").focus(); return; }
  const idea = $("new-idea").value.trim();
  try {
    const d = await jreq("POST", "/api/project/create",
      { name, parent: $("new-parent").value.trim(), genre: $("new-genre").value || null,
        idea });   // FB-平台:建书不再选平台(平台降级为立项里可选可跳的最后一格);server 端 platform 默认空
    // 2026-11-02（作者裁定②）：新书写作模式——创建成功后写进这本书的 loom.toml
    // （PUT /api/writing_mode 书级覆盖，复用现有校验；空值不写=跟随全局默认）。
    const wm = $("new-writing-mode") ? $("new-writing-mode").value : "";
    if (wm && d && d.root) {
      try { await jreq("PUT", "/api/writing_mode", { root: d.root, mode: wm }); }
      catch (e2) { toast("书写模式没写进配置(可在设置里再改):" + e2.message, true); }
    }
    $("create-overlay").classList.add("hidden");
    enterProject(d);
    // 填了设定=明确意图:模型就绪就直接铺底稿(仅建书这一次;draftBrain 自带就绪检查与 toast)
    if (idea && providerKeyed((d.backend || {}).provider)) draftBrain("");
  } catch (e) { $("create-error").textContent = e.message; }
}

async function importBook() {
  $("import-error").textContent = "";
  const p = $("open-path").value.trim();
  if (!p) {
    $("import-error").textContent = _hasNativePicker()
      ? "先点「浏览…」选中那本书的文件夹。" : "先填那本书的项目文件夹路径。";
    return;
  }
  try {
    const d = await jreq("POST", "/api/project/open", { root: p });
    $("import-overlay").classList.add("hidden");
    enterProject(d);
  } catch (e) { $("import-error").textContent = e.message; }
}
let _importRouted = null;   // scan 结果:{世界观:[...],...}
async function scanFolder() {
  const folder = $("folder-path").value.trim();
  if (!folder) { $("folder-error").textContent = "先选一个文件夹。"; return; }
  $("folder-error").textContent = "";
  $("folder-confirm").classList.add("hidden");
  _importRouted = null;
  try {
    const d = await jreq("POST", "/api/project/import/scan", { folder });
    _importRouted = d.routed;
    $("folder-name").value = d.name_suggest || "";
    renderRouted(d.routed, d.unknown || []);
    $("folder-confirm").classList.remove("hidden");
  } catch (e) { $("folder-error").textContent = e.message; }
}
const _BUCKETS = ["正文", "世界观", "人物", "卡章纲", "立项卡", "违禁词", "文风参考"];
function renderRouted(routed, unknown) {
  const box = $("folder-routed"); box.innerHTML = "";
  _BUCKETS.forEach((b) => {
    const files = routed[b] || [];
    if (!files.length) return;
    const h = document.createElement("div"); h.className = "fr-bucket"; h.textContent = `${b}(${files.length})`;
    box.appendChild(h);
    files.forEach((f) => { const d = document.createElement("div"); d.className = "fr-file"; d.textContent = f; box.appendChild(d); });
  });
  if (unknown.length) {
    const h = document.createElement("div"); h.className = "fr-bucket"; h.textContent = `没猜出来,你来指认(${unknown.length}):`;
    box.appendChild(h);
    unknown.forEach((f) => {
      const row = document.createElement("div"); row.className = "fr-file fr-unknown"; row.textContent = f;
      const sel = document.createElement("select"); sel.dataset.file = f;
      ["跳过", ..._BUCKETS].forEach((opt) => { const o = document.createElement("option"); o.value = opt; o.textContent = opt; sel.appendChild(o); });
      row.appendChild(sel); box.appendChild(row);
    });
  }
}
async function commitFolder() {
  if (!_importRouted) return;
  const folder = $("folder-path").value.trim();
  const name = $("folder-name").value.trim();
  if (!name) { $("folder-error").textContent = "先给这本书起个名字。"; return; }
  const routing = {}; _BUCKETS.forEach((b) => { routing[b] = [...(_importRouted[b] || [])]; });
  document.querySelectorAll("#folder-routed .fr-unknown select").forEach((sel) => {
    if (sel.value !== "跳过") routing[sel.value].push(sel.dataset.file);
  });
  try {
    const d = await jreq("POST", "/api/project/import/commit",
      { folder, name, parent: $("new-parent") ? ($("new-parent").value.trim() || "~/Desktop") : "~/Desktop", routing });
    $("folder-overlay").classList.add("hidden");
    enterProject(d.state || d);
    const s = d.summary || { placed: {}, notes: [] };
    const placed = _BUCKETS.filter((b) => s.placed[b]).map((b) => `${b} ${s.placed[b]} 份`).join("、");
    showGuide({
      title: "导入完成",
      bodyHtml: `<p class="guide-lead">已接进来:${placed || "(空)"}。</p>` +
        (s.notes.length ? `<p class="hint">${s.notes.map((n) => "· " + n).join("<br>")}</p>` : "") +
        `<p class="hint">你的设定原样进来了。缺立项/世界观/主角/章纲的话,写第一章前伙伴会带你补齐。</p>`,
      primary: { label: "知道了" },
    });
  } catch (e) { $("folder-error").textContent = e.message; }
}
async function openProject(root, silent) {
  try {
    const d = await jreq("POST", "/api/project/open", { root });
    enterProject(d);
  } catch (e) {
    if (silent) {
      if (new URLSearchParams(location.search).has("root")) history.replaceState(null, "", "/");
      showWelcome();
      document.documentElement.removeAttribute("data-direct-open");
      toast("这本书没打开，请从书架另选一本。", true);
    } else { $("welcome-error").textContent = e.message; }
  }
}
function closeHalfPanel() {          // 换书/回首页/重开时用:面板不能跨书活着
  HALF_QUOTE = null;
  const el = $("half-overlay");
  if (el) el.classList.add("hidden");
}

// 换书清账:两份「归纳」报价不能跨书活着 —— 在 A 上拿的价,切到 B 再点一下就真花钱了(UI 审查 P1-2)。
function clearVolQuotes() {
  VOL_QUOTE = null;
  BOOK_QUOTE = null;
  const vb = $("btn-vol-summary"), bb = $("btn-book-summary");
  if (vb) { vb.textContent = "归纳卷纲"; vb.disabled = false; }
  if (bb) { bb.textContent = "归纳全书大纲"; bb.disabled = false; }
}

// 换书清编辑器:否则书 B 里还显示书 A 的正文,手改会拿 A 的 base_hash 去写 B(UI 审查 P1-3)。
function resetEditor() {
  CUR = null;
  const ed = $("editor");
  if (ed) { ed.value = ""; ed.readOnly = true; }
  const path = $("editor-path");
  if (path) path.textContent = "选左边一个文件来看/改";
  const empty = $("editor-empty");
  if (empty) empty.classList.remove("hidden");   // 空态提示回来（视觉集中检查 2026-11-02）
  const _edBox = $("editor");
  if (_edBox) _edBox.classList.add("hidden");    // 空 textarea 的深底块不再出现
  ["btn-save-file", "btn-history", "btn-sensitive", "btn-learn", "btn-rewrite",
   "btn-brain-rewrite", "btn-brain-continue", "btn-brain-copy"].forEach((id) => {
    const b = $(id);
    if (b) b.classList.add("hidden");
  });
}

// ── 书房入口（2026-10-27，0.8.0）──────────────────────────────────────────
// 书房是**独立页面** `study.html`（外面的壳：自习室 / 织布机），不在本页里嵌。
// 跳之前先打 `GET /api/study/health`：通了才跳；没通就留在原地给一句人话——
// 「直接跳过去白屏」是最难解释的失败。
async function openStudy() {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 800);
  try {
    const r = await fetch("/api/study/health", { cache: "no-store", signal: ctl.signal });
    if (!r.ok) throw new Error("health " + r.status);
    location.href = "/study.html";
  } catch (e) {
    toast("书房还没就绪（后端 study 接口没挂上），先留在织布机这边。", true);
  } finally {
    clearTimeout(timer);
  }
}

function showWelcome() {
  closeHalfPanel();
  clearVolQuotes();
  resetEditor();
  $("app").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("nav-ball").classList.add("hidden");
  $("nav-popover").classList.add("hidden");
  _navPopOpen = false;
  renderRecent();
  loadConnectStatus();
  pollGuides("welcome");   // 批C:欢迎页引导(wizard 未完成时后端回 first-shelf 等)
}
function enterProject(d) {
  DATA = d;
  closeHalfPanel();      // 换书时半截稿面板也不能留着
  clearVolQuotes();      // 报价是「这本书的价」,不能带到下一本
  // 提案/复核也不能跨书活着 —— 否则「写进知识库」能把 A 书的结论写进 B 书
  KNOW_PROP = null;
  ALIGN_REPORT = null;
  if ($("know-accept")) $("know-accept").classList.add("hidden");
  if ($("align-accept")) $("align-accept").classList.add("hidden");
  if ($("know-out")) $("know-out").textContent = "";
  if ($("align-out")) $("align-out").textContent = "";
  resetEditor();         // 编辑器里不许还挂着上一本书的内容
  hideInlinePanel();   // 换书收面板:稳态残留的候选绝不能落进另一本书
  JOURNEY = null;               // 换书清旅程态:过期卡绝不能渲染在另一本书上
  _chainHints = [];             // 换书清联动提示:上一本书的「设定变了」不许挂在下一本上
  paintChainBar();
  _navYield = false; _weaving = false; _navPopOpen = false;   // 换书清领航员在场态:让位/织章/浮层开合都是会话级,不跨书带
  _navAutoAsked = new Set();
  // 2026-10-20 需求确认:进主视图默认打开「领航员」。
  // 2026-10-21 修正:只对**新书**(一行正文都还没有)这样;老书保留作者上次用的角色——
  // 否则"点设定集看文件"会被硬拽回领航员,很烦。
  const _isNewBook = !(d.chapters && d.chapters.length);
  if (_isNewBook) {
    _navRole = "领航员";
    _chatFocusPending = { key: chatKey("领航员"), anchor: document.body };
    _navLockedSlot = "";
    _navForceCenter = true;
    _agentCenter = null;
    if (typeof markRoleSelected === "function") markRoleSelected("领航员");
  } else if (_navForceCenter) {
    _navForceCenter = false;      // 老书:别停在"起书居中"那种全屏引导态
  }
  PARTNER = null; _partnerRoot = null; _partnerBusy = false; _partnerDelta = ""; _partnerReasoning = ""; _partnerStopped = false; _partnerGen++;   // 换书清伙伴对话缓存:过期对话绝不能渲染在另一本书上;世代号前移作废旧书迟到事件
  // 2026-10-25 修「两本书切换后对话记录交叉」:
  // 原来这两样只在「新书」分支里清 —— 老书切过去时 `_agentCenter` 还留着**上一本书**同一个
  // 角色的对话(event 里没有书的名字,只有角色名),于是设定师的面板把 A 书的话画在 B 书上,
  // 而且接着发话会追加进那串旧记录后面。议事厅同理(轮次号是全局的,不认书)。
  _agentCenter = null;
  _panel = null;
  _navLockedSlot = "";    // 锁定落点也是书内路径,换书必须清
  localStorage.setItem("loom_root", d.root);
  recordRecent(d.root, d.title);
  $("welcome").classList.add("hidden");
  $("app").classList.remove("hidden");
  render();
  // 批C:进书后引导(no-volume/style-archive-empty 等按书状态触发);
  // first-shelf 的完成由开书动作本身证明,这里顺带回执(后端 done_when 也会自判,双保险)。
  guideSeen("first-shelf");
  setTimeout(() => pollGuides("book"), 1200);   // 等 render/树初载完再问,别抢首屏
}

// ---------- 我的书架(用户级注册表 ~/.loom/projects.json,server 维护) ----------
// 新建/导入/样例统一自动入架:路径只在「导入」时输一次,以后永远点选。
// 比 localStorage 稳:换浏览器/清缓存/桌面端都还在。
function recordRecent(_root, _title) { /* 入架已由 server 在开书端点完成,前端无事可做 */ }
// 书架移除的在途集合（防双击重复提交；见 window.onShelfRemove）
const _shelfRemoving = new Set();

async function renderRecent() {
  const ul = $("recent-list"), block = $("recent-block");
  let list = [];
  try { list = (await jreq("GET", "/api/projects")).projects || []; } catch (e) { /* 书架读不到不拦欢迎页 */ }
  ul.innerHTML = "";
  if (!list.length) { block.classList.add("hidden"); return; }
  /*2026-11-02（返工补完）：书卡构建走 StudyCore.buildBookCard 同源函数——
  与书房书架（workbench.openShelf）同一份实现，样式/行为不会再漂。
  本地 bookHue 已删；移除回执经 window.onShelfRemove（下方赋值）。 */
  // 审查反例③：× 双击会在 forget 返回前再发一次。同一本书在途时吞掉重复点击，并把 × 禁用。
  const _removing = _shelfRemoving;
  window.onShelfRemove = async (book, btn) => {
    const key = String((book && book.root) || "");
    if (_removing.has(key)) return;               // 在途:禁重复提交
    _removing.add(key);
    if (btn) btn.disabled = true;                 // 在途禁用按钮（下面 finally 一定恢复,失败还能再点）
    let ok = false;
    try {
      const confirmed = await StudyCore.confirmAction("移出书架", `将「${book.title || book.root || "未命名"}」移出书架？\n只移除书架记录，书稿仍保留在原文件夹。`, "确认移出");
      if (!confirmed) return;
      const r = await jreq("POST", "/api/projects/forget", { root: book.root });
      // 与书房同一判据（验收 A1）：只有"服务端明确成功"才算成功。jreq 非 2xx 会抛，
      // 所以这里 r 一定是对象；写成 `!!r &&` 是为了两处判据逐字一致、不靠"它不会返回 null"这个前提。
      ok = !!r && r.ok !== false;
    } catch (err) { ok = false; } finally {
      _removing.delete(key);
      if (btn) btn.disabled = false;
    }
    // H-01:被移除的正是残留那本 → 键一起清,否则下次打开首页又自动进它。
    // 审查 P2(补):**只在服务端真移除成功时清**。失败时书还在注册表里,清了键也一样会被
    // "最近一本"打开——那是白丢信息;如实说一声,让作者知道移除没生效。
    if (ok) {
      try {
        if (StudyCore.sameRoot(localStorage.getItem("loom_root"), book.root)) {
          localStorage.removeItem("loom_root");
        }
      } catch (err) { /* localStorage 不可用(隐私模式)就当没有残留键 */ }
    } else {
      toast("这一步没成,书还在书架上。", true);
    }
    renderRecent();
  };
  list.forEach((r) => {
    ul.appendChild(StudyCore.buildBookCard(r, (book) => openProject(book.root, false)));
  });
  block.classList.remove("hidden");
}

// 启动自动回书:localStorage 有上一本就开它;没有(换浏览器/清过缓存)→ 书架最近一本
// H-01(2026-11-10 需求方授权最小修复):残留根与书架首项**都必须命中本书架成员**才自动开。
// 判据来源就是 /api/projects(书架本身就长在它上面);命不中 → 清残留键、留在欢迎页。
// ?root= 直开保持原样——那是显式意图,外部工具/快捷方式依赖它,不做成员限制。
// 书架读不到时不猜成员:既不清键(下次再试)也不自动开,宁可停在欢迎页。
async function autoReopen() {
  const requestedRoot = new URLSearchParams(location.search).get("root");
  if (requestedRoot) { openProject(requestedRoot, true); return; }
  let list = null;
  try { list = (await jreq("GET", "/api/projects")).projects || []; } catch (e) { list = null; }
  const saved = localStorage.getItem("loom_root");
  if (saved) {
    if (!list) return;                                  // 判不了成员:不清键、不自动开
    if (!onShelf(list, saved)) {
      localStorage.removeItem("loom_root");             // 不在本书架:残留不能续命
      return;
    }
    openProject(saved, true); return;
  }
  const first = list ? list.find((r) => r && r.exists) : null;
  if (first) openProject(first.root, true);
}

// 书架成员判据(同源比较见 StudyCore.sameRoot:注册表存 resolve 后的路径,残留键是回显原值)
function onShelf(list, root) {
  const same = (window.StudyCore && StudyCore.sameRoot) || ((a, b) => a === b);
  return (list || []).some((r) => r && same(r.root, root));
}

// ---------- 接入模型(用户级默认后端)----------
let _conn = null;   // GET /api/backend/default 的响应(providers/keys_set/当前默认)
async function loadConnectStatus() {
  const dot = $("cb-dot"), text = $("cb-text"), btn = $("btn-connect");
  try { _conn = await jreq("GET", "/api/backend/default"); }
  catch (e) { text.textContent = "接入状态读取失败"; return; }
  const spec = (_conn.providers || []).find((p) => p.id === _conn.provider);
  const label = spec ? spec.label : _conn.provider;
  const keyed = spec && (spec.kind === "cli" || (_conn.keys_set || {})[_conn.provider]);
  if (_conn.has_default && keyed) {
    dot.className = "cb-dot ok";
    text.innerHTML = `已接入 <b>${escHtml(label)}</b>${_conn.model ? " · " + escHtml(_conn.model) : ""} —— 新书会自动用它`;
    btn.textContent = "换一个";
    btn.classList.remove("primary");
    pingBackend();          // 每次打开欢迎页自动探一次活(只读 GET /models,不花 token)
  } else {
    dot.className = "cb-dot";
    text.textContent = "还没接入模型 —— 连一次,以后每本书都用得上";
    btn.textContent = "接入模型";
    btn.classList.add("primary");
  }
}

// 模型探活(2026-10-20 需求确认):连不上就让接入卡片红闪两下,别让作者带着坏配置去写章。
// 只用 GET /models —— 不发生时请求、不花 token;失败也不弹窗,不打断。
async function pingBackend() {
  const box = $("connect-block");
  if (!box) return;
  let r = null;
  try { r = await jreq("GET", "/api/backend/ping"); }
  catch (e) { r = { ok: false, error: e.message }; }
  if (r && r.ok) return;
  box.classList.remove("cb-bad");
  void box.offsetWidth;               // 强制 reflow,否则连续两次失败不会重播动画
  box.classList.add("cb-bad");
  setTimeout(() => box.classList.remove("cb-bad"), 1700);   // 两下闪完就撤,别长期红着吓人
  const t = $("cb-text");
  if (t && r && r.error) t.title = `探活失败:${r.error}`;
}
function cnSpec(pid) { return (_conn && _conn.providers || []).find((p) => p.id === pid) || null; }
function cnKeyed(pid) {
  const s = cnSpec(pid);
  if (s && s.kind === "cli") return true;
  return !!(_conn && _conn.keys_set && _conn.keys_set[pid]);
}
async function openConnect() {
  $("cn-error").textContent = ""; $("cn-status").textContent = ""; $("cn-status").className = "backend-status";
  if (!_conn) { try { _conn = await jreq("GET", "/api/backend/default"); } catch (e) { toast(e.message, true); return; } }
  const sel = $("cn-provider"); sel.innerHTML = "";
  (_conn.providers || []).forEach((p) => { const o = document.createElement("option"); o.value = p.id; o.textContent = p.label; sel.appendChild(o); });
  sel.value = _conn.provider || "deepseek";
  $("cn-api-key").value = "";
  cnApplyProvider();
  cnFillModels(cnSpec(sel.value) ? cnSpec(sel.value).models : []);
  cnSetModel(_conn.model || (cnSpec(sel.value) ? cnSpec(sel.value).default_model : ""));
  $("cn-context-len").value = _conn.context_len || "";
  $("cn-context-len-hint").textContent = contextLenSummary(_conn.context_limit, _conn.context_len_source);
  $("connect-overlay").classList.remove("hidden");
}
function cnApplyProvider() {
  const p = $("cn-provider").value, spec = cnSpec(p);
  const needsKey = spec ? spec.needs_key : true;
  const isCustom = p === "openai_compat";
  const isCli = spec && spec.kind === "cli";
  $("cn-api-key").classList.toggle("hidden", !needsKey);
  $("cn-key-label").classList.toggle("hidden", !needsKey);
  $("cn-base-url").classList.toggle("hidden", !isCustom);
  $("cn-fetch-models").classList.toggle("hidden", !(spec && spec.can_list_models));
  $("cn-probe").classList.toggle("hidden", !isCli);
  $("cn-key-hint").textContent = (spec && spec.hint) || "";
  if (needsKey) $("cn-api-key").placeholder = cnKeyed(p) ? "已设置(留空=不改)" : `填 ${spec ? spec.label : p} 的 API Key`;
  if (isCustom && spec && !$("cn-base-url").value) $("cn-base-url").value = spec.base_url || "";
  cnFillModels(spec ? spec.models : []);
  cnSetModel(spec ? spec.default_model : "");
}
function cnFillModels(models) {
  const sel = $("cn-model"); sel.innerHTML = "";
  (models || []).forEach((m) => { const o = document.createElement("option"); o.value = m.id; o.textContent = m.label || m.id || "(默认)"; sel.appendChild(o); });
  const c = document.createElement("option"); c.value = "__custom__"; c.textContent = "✎ 手动输入…"; sel.appendChild(c);
}
function cnSetModel(model) {
  const sel = $("cn-model"), custom = $("cn-model-custom"); model = model || "";
  if (model && ![...sel.options].some((o) => o.value === model)) {
    const o = document.createElement("option"); o.value = model; o.textContent = model;
    sel.insertBefore(o, sel.querySelector('option[value="__custom__"]'));
  }
  sel.value = model;
  if (sel.selectedIndex < 0) sel.selectedIndex = 0;
  if (sel.value === "__custom__") { custom.classList.remove("hidden"); custom.value = model !== "__custom__" ? model : ""; }
  else { custom.classList.add("hidden"); custom.value = ""; }
}
function cnOnModelSelect() {
  const custom = $("cn-model-custom");
  if ($("cn-model").value === "__custom__") { custom.classList.remove("hidden"); custom.focus(); }
  else custom.classList.add("hidden");
}
function cnCurrentModel() {
  const sel = $("cn-model");
  return sel.value === "__custom__" ? $("cn-model-custom").value.trim() : sel.value;
}
async function cnFetchModels() {
  const p = $("cn-provider").value, btn = $("cn-fetch-models"), old = btn.innerHTML;
  btn.disabled = true; btn.textContent = "拉取中…";
  try {
    const d = await jreq("POST", "/api/backend/models", { provider: p, base_url: $("cn-base-url").value.trim() || null, api_key: $("cn-api-key").value.trim() || null });
    if (d.ok && (d.models || []).length) { cnFillModels(d.models); $("cn-model").selectedIndex = 0; $("cn-model-custom").classList.add("hidden"); toast(d.message || `拉到 ${d.models.length} 个模型`); }
    else toast(d.message || "没拉到模型(检查 base_url / key)", true);
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.innerHTML = old; }
}
async function cnProbe() {
  const s = $("cn-status"); s.className = "backend-status"; s.textContent = "检测中…";
  try {
    const d = await jreq("GET", `/api/backend/probe?provider=${encodeURIComponent($("cn-provider").value)}`);
    s.className = "backend-status " + (d.ok ? "ok" : "bad");
    s.innerHTML = icon(d.ok ? "check" : "cross", "stat-ico") + " " + escHtml(d.message || "");
    if (!d.ok && d.hint) toast(d.hint, true);
  } catch (e) { s.className = "backend-status bad"; s.textContent = e.message; }
}
async function saveConnect() {
  $("cn-error").textContent = "";
  const p = $("cn-provider").value;
  // 护栏把「改模型与供应商」列为「需输入确认」级,但这一屏以前点一下就改、连个 confirm 都没有。
  // 它会改变**之后所有新书**的出稿质量与花费,所以这里必须让人停一下。
  const _m = cnCurrentModel();
  if (!window.confirm(`要把默认模型改成「${p} / ${_m}」吗?\n` +
    `这会改变之后新建的书的出稿质量与花费(已有的书用各自 loom.toml 里的设置)。`)) return;
  try {
    const d = await jreq("PUT", "/api/backend/default", {
      provider: p, model: cnCurrentModel(),
      base_url: $("cn-base-url").value.trim() || null,
      api_key: $("cn-api-key").value.trim() || null,
      context_len: parseContextLenElement($("cn-context-len")),
    });
    if (d.model_warning) toast(d.model_warning, true);
    _conn = null;   // 强制重拉状态
    $("connect-overlay").classList.add("hidden");
    await loadConnectStatus();
    toast("已接入,记住了 —— 新建的书会自动用它");
  } catch (e) { $("cn-error").textContent = e.message; }
}

async function refresh() {
  DATA = await jreq("GET", `/api/project/state?root=${encodeURIComponent(DATA.root)}`);
  render();
  refreshPending();          // AI 写过的章纲有没有人过目,顺手更新横幅
}

// ── 联动提示条(丙 · 固定工作流;作者 2026-10-20)────────────────────────────
// 三条硬边界:①提示本身零模型调用、零写盘;②只有点「知道了」/点动作才 ack(只写 .loom_state.json);
// ③绝不自动发模型请求——动作最多是「打开对谈并预填一句」,发送由人按。
// 判据全在后端 chains.py(本机文件指纹);前端只负责「怎么说话」和「把人送去哪」。
let _chainHints = [];      // 后端只回事实,文案在前端拼
let _chainTimer = 0;       // 去抖:render() 会被连着调
let _chainPrefill = null;  // 联动预填:后点覆盖先点
let _chainPrefillRole = null;  // 这条预填是给哪个角色的
// 2026-10-21 真机:点「去问大纲师」后输入框是空的。原因不是没设,而是 paintAgentChatCenter
// 会画两次(先空壳、载入历史后再画一次),第一次就把 _chainPrefill 取走了,
// 用户看到的是第二次画出来的空框。改成:留着不清,按角色匹配地重新贴上,
// 只有用户自己动了输入框、或发送出去,才清掉。
const CHAIN_MAX = 4;       // 上限 = 链数;超出按 ③b > ③ > ② > ① 截断(设计文档 §五)
const CHAIN_COST = { settings: "", final: "点了才花钱", card: "3 次调用", outline: "1 次调用" };

function scheduleChainCheck() {
  if (!DATA || !DATA.root) return;
  clearTimeout(_chainTimer);
  _chainTimer = setTimeout(checkChains, 1500);
}

async function checkChains() {
  if (!DATA || !DATA.root || document.hidden) return;
  const root = DATA.root;
  let d;
  try { d = await jreq("GET", `/api/chains?root=${encodeURIComponent(root)}`); }
  catch (e) { return; }                       // 提示层坏了绝不影响开书/写章
  if (!DATA || DATA.root !== root) return;    // 迟到的响应不许贴到另一本书上
  if (d && d.need_baseline) {
    try { await jreq("POST", "/api/chains/baseline", { root }); } catch (e) { /* 下次再建 */ }
    return;                                   // 建基线这一轮不提示(老书不许一开就刷一脸)
  }
  _chainHints = (d && d.hints) || [];
  paintChainBar();
}

function paintChainBar() {
  const el = $("chain-bar");
  if (!el) return;
  el.innerHTML = "";
  if (!_chainHints.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const shown = _chainHints.slice(0, CHAIN_MAX);
  shown.forEach((h) => el.appendChild(chainChip(h)));
  if (_chainHints.length > shown.length) {
    const more = document.createElement("span");
    more.className = "chain-more";
    more.textContent = `还有 ${_chainHints.length - shown.length} 条`;
    el.appendChild(more);
  }
}

// 外置大脑/设定集/10-世界观/金手指.md → 10-世界观/金手指
function _shortRel(rel) {
  return String(rel || "").replace(/^外置大脑\/设定集\//, "").replace(/\.md$/, "");
}

function chainChip(h) {
  const chip = document.createElement("span");
  chip.className = "chain-chip";
  const txt = document.createElement("span"); txt.className = "cc-text";
  const cost = document.createElement("span"); cost.className = "cc-cost";
  const go = document.createElement("button"); go.className = "cc-go";

  if (h.key === "settings") {
    const files = (h.files || []).map(_shortRel);
    txt.textContent = `设定集变了 ${h.count} 处${files.length ? "：" + files.join("、") : ""}`;
    go.textContent = "去问大纲师";
    go.onclick = () => {
      _chainPrefill = `设定集刚改了：${files.join("、")}。看一下全书大纲 / 卷纲 / 卡章纲要不要跟着调；`
        + "先给判断和具体改法，别动文件。";
      _chainPrefillRole = "大纲师";
      openAgentChat("大纲师");
      ackChain("settings");    // 一点就走:ack 只代表「我看见了」,不代表处理完
    };
  } else if (h.key === "final") {
    txt.textContent = `第 ${h.chapter} 章写完了 · 让批评家挑挑毛病？`;
    go.textContent = "去挑毛病";
    go.onclick = () => {
      _chainPrefill = `读第 ${h.chapter} 章，挑硬伤：OOC / 设定漂移 / 因果断裂 / 章末钩没接住。`
        + "只出问题清单和确切位置，一个字都别改。";
      _chainPrefillRole = "批评家";
      openAgentChat("批评家");
      ackChain("final");
    };
  } else if (h.key === "card") {
    const n = h.next_chapter;
    txt.textContent = `卡章纲变了 · 第 ${n} 章细纲建议重算`;
    go.textContent = "重新生成细纲";
    go.onclick = async () => {
      if (!confirm(`重新生成第 ${n} 章细纲？会花 3 次模型调用，当前细纲（含你的手改）会被覆盖。`)) return;
      go.disabled = true;
      try {
        const text = await callRegen(n);
        if (text == null) return;          // callRegen 自己已经 toast 过了
        toast(`第 ${n} 章细纲已重新生成`);
        await ackChain("card");
      } finally { go.disabled = false; }
    };
  } else if (h.key === "outline") {
    const b = h.block || {}, canGo = !!(h.refill || {}).可用;
    txt.textContent = `卷纲变了 · 第 ${b.起}-${b.止} 章这一块（你正在写第 ${b.正在写} 章）建议重排章纲`;
    go.textContent = "重排这一块";
    go.disabled = !canGo;
    go.title = canGo ? "只补空行/缺行，你写过的行一个字不动"
      : ((h.refill || {}).原因 || "这块章纲已写满");
    go.onclick = async () => {
      if (!confirm(`重排第 ${b.起}-${b.止} 章的章纲？会花 1 次模型调用。\n\n`
        + "只补空行/缺行，你写过的行不会被覆盖。")) return;
      go.disabled = true;
      try {
        const r = await jreq("POST", "/api/card/expand", { root: DATA.root, lo: b.起, hi: b.止 });
        if (r && r.ok === false) { toast(r.reason || r.error || "没铺成", true); return; }
        const n = ((r || {}).落盘 || {}).落了几行;
        toast(`这块章纲补了 ${n == null ? "?" : n} 行`);
        await refresh();
        await ackChain("outline");
      } catch (e) { toast("没铺成：" + e.message, true); }
      finally { go.disabled = !canGo; }
    };
  } else { return chip; }

  if (CHAIN_COST[h.key]) cost.textContent = CHAIN_COST[h.key];
  const x = document.createElement("button");
  x.className = "cc-x"; x.textContent = "×"; x.title = "知道了（不再提示这条）";
  x.onclick = () => ackChain(h.key);
  chip.appendChild(txt);
  if (cost.textContent) chip.appendChild(cost);
  chip.appendChild(go);
  chip.appendChild(x);
  return chip;
}

async function ackChain(key) {
  const root = DATA && DATA.root;
  if (!root) return;
  _chainHints = _chainHints.filter((h) => h.key !== key);
  paintChainBar();              // 先上屏:网络失败也不该让作者卡在「点不掉」
  try { await jreq("POST", "/api/chains/ack", { root, key }); }
  catch (e) { /* 记不上账只会「下次再提示一次」,不惊动作者 */ }
}

// 切回窗口补查一次(页面隐藏期间不查,见 checkChains)
window.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleChainCheck(); });

// ---------- 备份 / 导出(纯本地) ----------
// R18:格式 × 切分 × 落点目录,另加「打印成 PDF」(作者裁定走浏览器打印)。
// 以前这里只发 {root},于是永远是「整本 txt 进 导出/」,第二次导出还必撞同名整批拒绝,
// 而提示语让人「换个导出目录」—— 界面上根本没有这个输入(UI 审查 P1-2)。
// 「这本书还在吗」统一守卫(2026-10-21)。
// 真机:作者把书目录挪走后界面还开着旧书,文风档案/导出这些要写盘的动作一个个回后端原始报错
// (「不是 loom 项目(没有 loom.toml)」),糊到脸上没人看得懂。写盘前先问一句,给一句人话。
async function _ensureBook(quiet) {
  if (!DATA || !DATA.root) { if (!quiet) toast("先打开一本书。", true); return false; }
  let d = null;
  try { d = await jreq("GET", `/api/project/check?root=${encodeURIComponent(DATA.root)}`); }
  catch (e) { return true; }        // 检查端点本身抽风时不拦人,让真实调用去报错
  if (d && d.is_book) return true;
  if (!quiet) {
    toast(d && d.exists
      ? "这本书的文件夹还在,但里面没有 loom.toml —— 可能不是 Loom 的书。请回首页重新导入。"
      : "这本书的文件夹不在了(被移动或删除)。请回首页重新导入,或打开别的书。", true);
  }
  return false;
}

function openExport() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  $("export-overlay").classList.remove("hidden");
  exportPreview();
}
function _exportArgs() {
  return {
    fmt: $("export-fmt").value,
    split: $("export-split").value,
    out_dir: ($("export-dir").value || "").trim(),
  };
}
async function exportPreview() {
  const box = $("export-report");
  if (!DATA || !DATA.root) return;
  const g = _exportArgs();
  box.textContent = "算一下会生成几份…";
  try {
    const q = new URLSearchParams({ root: DATA.root, fmt: g.fmt, split: g.split });
    const d = await jreq("GET", `/api/export/plan?${q.toString()}`);
    if (d.ok === false) { box.textContent = d.reason || "算不出方案。"; return; }
    const fs = d.文件 || [];
    const names = fs.slice(0, 4).map((f) => f.文件名).join("、") + (fs.length > 4 ? " …" : "");
    let t = `会写 ${fs.length} 份,共 ${d.章数} 章:${names}`;
    if (d.说明) t += `\n${d.说明}`;
    if (d.格式 === "pdf") t += "\n生成的是打印稿(HTML):浏览器打开后 Ctrl+P → 另存为 PDF。";
    box.textContent = t;
  } catch (e) {
    box.textContent = "算方案失败:" + (e.message || e);
  }
}
async function exportRun() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  if (!(await _ensureBook())) { $("export-report").textContent = "这本书的文件夹不在了,导出没意义。请回首页重新导入。"; return; }
  const btn = $("export-run");
  btn.disabled = true;
  try {
    const d = await jreq("POST", "/api/export", { root: DATA.root, ..._exportArgs() });
    if (!d || d.ok === false) {
      // 后端会说清是「同名已存在」还是「跑到书外/受保护位置」,照它的原话给人
      toast(d && (d.reason || d.error) ? (d.reason || d.error) : "导出没成。", true);
      $("export-report").textContent = (d && (d.reason || d.error)) || "导出没成。";
      return;
    }
    const n = d.合计章数 != null ? d.合计章数 : d.chapters;
    const way = d.方式 === "打印" ? "(打印稿,浏览器 Ctrl+P 存成 PDF)" : "";
    toast(`已导出 ${n} 章 → ${d.目录 || ""}${way}`);
    $("export-report").textContent = `已导出 ${n} 章 → ${d.目录 || "导出/"}` +
      (d.写出 ? `\n${d.写出.map((w) => w.文件 + "(" + w.章数 + " 章)").join("、")}` : "") +
      (d.方式 === "打印" ? "\n浏览器打开这些 HTML → Ctrl+P → 另存为 PDF。" : "");
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}
async function backupBook() {
  try {
    const d = await jreq("POST", "/api/backup", { root: DATA.root });
    toast(`已备份整本 → ${d.path}(拷到云盘/U盘才算真备份)`);
  } catch (e) { toast(e.message, true); }
}

// ---------- 回收站(软删的出口) ----------
// 删章是**软删**:文件挪进 正文/.回收站/ 并留一份清单。之前界面上没有任何地方能看见它、
// 也点不回去,护栏却写着「可撤销」(交叉审查 UI-P1-7)。这里补一个最小的出口:
// 列表 + 还原(还原前后端会**先全查再全挪**,目标已有文件就整批拒绝,绝不覆盖)。
function _trashOverlay() {
  let ov = document.getElementById("trash-overlay");
  if (ov) return ov;
  ov = document.createElement("div");
  ov.id = "trash-overlay";
  // 走统一的遮罩(Esc 的栈只认 .overlay;以前自造 z-index:9999,既关不掉又永远盖住错误 toast)
  ov.className = "overlay hidden";
  const card = document.createElement("div");
  card.className = "run-card settings-card";
  ov.appendChild(card);
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.classList.add("hidden"); });
  document.body.appendChild(ov);
  a11yWatch();      // body 观察器要等微任务;首次打开先同步补一次(否则第一次不聚焦、关掉不回焦)
  return ov;
}

// ---------- 实体表(正名 / 别名 / 改名) ----------
// /api/entities 一族此前零入口:词典只能拿记事本改,而改名要「全库替换 + 照着旧名打一遍」,
// 手改几乎必然漏文件(漏掉的正文会留下半新半旧的名字)。
function openEnts() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  $("ents-overlay").classList.remove("hidden");
  $("ents-report").textContent = "";
  entsLoad();
}
async function entsLoad() {
  const head = $("ents-head"), box = $("ents-body");
  head.textContent = "读表…";
  box.innerHTML = "";
  try {
    const d = await jreq("GET", `/api/entities?root=${encodeURIComponent(DATA.root)}`);
    if (d.ok === false || d.error) { head.textContent = d.reason || d.error || "读不出来。"; return; }
    const rows = d.实体 || [];
    head.textContent = `词典里 ${rows.length} 条` + (rows.length ? "" : "(空表:可以先「扫材料」)");
    rows.forEach((e) => {
      const row = document.createElement("div");
      row.className = "facts-row";
      row.textContent = `${e.id} · ${e.类型 || "?"} · ${e.正名}` +
        ((e.别名 || []).length ? `(别名:${e.别名.join("、")})` : "");
      box.appendChild(row);
    });
  } catch (e) { head.textContent = "读表失败:" + (e.message || e); }
}
async function entsScan() {
  const rep = $("ents-report");
  rep.className = "hint";
  rep.textContent = "扫材料…";
  try {
    const d = await jreq("POST", "/api/entities/build", { root: DATA.root, write: false });
    if (d.ok === false || d.error) { rep.className = "error"; rep.textContent = d.reason || d.error || "扫不动。"; return; }
    const will = d.会新增 || d.新增 || [];
    rep.textContent = `材料里扫到 ${will.length ?? d.新增条数 ?? "?"} 个该进表的人物(还没写)。`;
    if (will.length) {
      const btn = document.createElement("button");
      btn.className = "mini";
      btn.textContent = "把这些补进表";
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const w = await jreq("POST", "/api/entities/build", { root: DATA.root, write: true });
          if (w.ok === false || w.error) { rep.className = "error"; rep.textContent = w.reason || w.error || "没补成。"; return; }
          rep.textContent = `补进去了 ${(w.新增 || []).length ?? w.新增条数 ?? "?"} 条。`;
          entsLoad();
        } catch (e) { rep.className = "error"; rep.textContent = "没补成:" + (e.message || e); }
        finally { btn.disabled = false; }
      };
      rep.appendChild(document.createElement("br"));
      rep.appendChild(btn);
    }
  } catch (e) { rep.className = "error"; rep.textContent = "扫不动:" + (e.message || e); }
}
async function entsPreview() {
  const box = $("ents-preview-box");
  const oldName = $("ents-old").value.trim(), newName = $("ents-new").value.trim();
  box.innerHTML = "";
  if (!oldName || !newName) { toast("旧名和新名都要填", true); return; }
  try {
    const d = await jreq("POST", "/api/entities/rename",
      { root: DATA.root, old: oldName, new: newName });
    if (d.ok === false) {
      const bad = document.createElement("div");
      bad.className = "error";
      bad.textContent = d.reason || "改不了。";
      box.appendChild(bad);
      return;
    }
    const tip = document.createElement("div");
    tip.className = "hint";
    tip.textContent = `来源:${d.来源} · 要替换的词:${(d.要替换的词 || []).join("、")} · ` +
      `合计 ${d.合计处数} 处。范围:${d.范围}`;
    box.appendChild(tip);
    (d.文件 || []).forEach((f) => {
      const row = document.createElement("div");
      row.className = "facts-row";
      row.textContent = `${f.文件} · ${f.处} 处　… ${f.示例}`;
      box.appendChild(row);
    });
  } catch (e) {
    const err = document.createElement("div");
    err.className = "error";
    err.textContent = "预览失败:" + (e.message || e);
    box.appendChild(err);
  }
}
async function entsRename() {
  const rep = $("ents-report");
  const oldName = $("ents-old").value.trim(), newName = $("ents-new").value.trim();
  const typed = $("ents-typed").value.trim();
  if (!oldName || !newName || !typed) { toast("旧名 / 新名 / 确认都要填", true); return; }
  try {
    const d = await jreq("POST", "/api/entities/rename",
      { root: DATA.root, old: oldName, new: newName, typed });
    // 注意:确认词不对时后端给的是 **预览 + ok:true + 已执行:false**(HTTP 400)。
    // 只看 ok 会当场撒谎说「改完了」——这里必须认「已执行」。
    if (d.ok === false || d.error || d.已执行 === false) {
      rep.className = "error";
      rep.textContent = d.code === "need_typed"
        ? `这次一个字都没动:要照着旧名「${d.需要输入 || oldName}」一字不差地打一遍。`
        : (d.reason || d.error || "没改成。");
      return;
    }
    rep.className = "hint";
    rep.textContent = `改完了:动了 ${d.改了文件 ?? "?"} 个文件、实际 ${d.实际处数 ?? "?"} 处` +
      (d.没读动 && d.没读动.length ? `。没读动 ${d.没读动.length} 个(编码不对,要手工改):` +
        d.没读动.map((x) => x.文件).join("、") : "。");
    $("ents-preview-box").innerHTML = "";
    entsLoad();
  } catch (e) { rep.className = "error"; rep.textContent = "没改成:" + (e.message || e); }
}

// ---------- 本章诊断(R12 运行卡 / R8 影响面 / 修复方案 / R23 模板 / 写租约) ----------
// 这五个端点此前主线界面零入口(诊断台 diag.html 里只有 report/repair apply 那几样)。
function openChDiag() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  $("chdiag-overlay").classList.remove("hidden");
  $("chdiag-report").textContent = "";
  const ch = (CUR && CUR.chapter) || ((DATA.chapters || []).slice(-1)[0] || {}).n || 1;
  $("chdiag-ch").value = ch;
  chdiagLoad();
}
function _diagSec(title, lines) {
  const wrap = document.createElement("div");
  wrap.className = "facts-sec";
  const h = document.createElement("div");
  h.className = "facts-sec-title";
  h.textContent = title;
  wrap.appendChild(h);
  if (!lines.length) {
    const e = document.createElement("div");
    e.className = "hint";
    e.textContent = "没有。";
    wrap.appendChild(e);
    return wrap;
  }
  lines.forEach((t) => {
    const row = document.createElement("div");
    row.className = "facts-row";
    row.textContent = t;
    wrap.appendChild(row);
  });
  return wrap;
}
async function chdiagLoad() {
  const head = $("chdiag-head"), box = $("chdiag-body");
  const ch = Number($("chdiag-ch").value) || 0;
  if (!ch) { head.textContent = "先填第几章。"; return; }
  head.textContent = `读第 ${ch} 章…`;
  box.innerHTML = "";
  const q = (p, extra = "") => `${p}?root=${encodeURIComponent(DATA.root)}${extra}`;
  try {
    const [card, imp, plan, tpl, st] = await Promise.all([
      jreq("GET", q("/api/run/card", `&chapter=${ch}`)),
      jreq("GET", q("/api/chapter/impact")),
      jreq("GET", q("/api/repair/plan", `&chapter=${ch}`)),
      jreq("GET", q("/api/review/template")),
      jreq("GET", q("/api/chapter/state", `&chapter=${ch}`)),
    ]);
    const cardLines = [];
    cardLines.push(`调用 ${card.调用次数 ?? 0} 次 · 总耗时 ${card.总耗时ms ?? 0} ms · 失败 ${card.失败次数 ?? 0} 次`);
    (card.逐棒 || []).forEach((r) => cardLines.push(
      `${r.角色 || r.棒 || "?"} · ${r.耗时ms ?? "?"} ms${r.失败 ? " · 失败:" + (r.原因 || "") : ""}`));
    cardLines.push("tokens:" + (card.tokens == null ? card.tokens说明 || "无" : card.tokens));
    box.appendChild(_diagSec(`运行卡(R12)· 状态 ${card.状态 || (st.状态 || "?")}`, cardLines));

    const impLines = (imp.要重算的章 || []).map((x) =>
      `第${x.章 || x}章` + (x.原因 ? " · " + x.原因 : ""));
    impLines.push(`预计调用 ${imp.预计调用次数 ?? 0} 次(${imp.说明 || "这是下限"})`);
    box.appendChild(_diagSec("影响面(R8):改上游后哪些章要重算", impLines));

    const planLines = (plan.步骤 || []).map((x) =>
      `${x.序号 ?? "-"}. ${x.旧句 || ""} → ${x.新句 || ""}` +
      (x.理由 ? `(${x.理由})` : ""));
    (plan.需人工 || []).forEach((x) => planLines.push("需人工:" + (x.说明 || x)));
    box.appendChild(_diagSec(`修复方案(还没改一个字)`, planLines));

    box.appendChild(_diagSec("审稿模板(R23)",
      [tpl.在 ? `有:${tpl.标题 || ""}(规则 ${(tpl.规则 || []).length} 条)` : (tpl.说明 || "没有,这一项不参与复审")]));

    // 后端返回的是 {状态, 脏, 脏因, 写者, 变过的材料}(写者=租约持有者,没有则空)
    const holder = st.写者 || st.holder || "";
    box.appendChild(_diagSec("这一章的状态与写者",
      [`状态 ${st.状态 || "?"}${st.脏 ? " · 脏(" + (st.脏因 || "上游变过") + ")" : ""}`,
       holder ? `正在写:${holder}` : "没有租约(别的窗口也能开这一章)"]));
  } catch (e) {
    head.textContent = "读不动:" + (e.message || e);
  }
}
async function chdiagSync() {
  const rep = $("chdiag-report");
  rep.className = "hint";
  rep.textContent = "打脏标…";
  try {
    const d = await jreq("POST", "/api/chapter/sync", { root: DATA.root });
    if (d.ok === false) { rep.className = "error"; rep.textContent = d.reason || "没打成。"; return; }
    rep.textContent = `打好了 ${(d.脏章 || d.标记 || []).length ?? d.条数 ?? 0} 个(只打标,没重跑)。`;
    chdiagLoad();
  } catch (e) { rep.className = "error"; rep.textContent = "没打成:" + (e.message || e); }
}
async function chdiagLease(action) {
  const rep = $("chdiag-report");
  const ch = Number($("chdiag-ch").value) || 0;
  if (!ch) { rep.className = "error"; rep.textContent = "先填第几章。"; return; }
  try {
    const d = await jreq("POST", "/api/chapter/lease",
      { root: DATA.root, chapter: ch, action, owner: "本窗口" });
    if (d.ok === false) { rep.className = "error"; rep.textContent = d.reason || "没借到。"; return; }
    rep.className = "hint";
    rep.textContent = action === "release" ? "标记放掉了。" : "标上了:别的窗口会看到这一章你在写。";
    chdiagLoad();
  } catch (e) { rep.className = "error"; rep.textContent = "没借到:" + (e.message || e); }
}

// ---------- 冲突与裁定(R5) ----------
// /api/arbitrate 此前收的是「调用方给的冲突清单」,而全仓没有产出清单的地方 —— 等于永远跑不起来。
// 现在后端不传清单时自己扫一遍(确定性部分),这里负责把「以谁为准 / 要改哪层 / 需不需要请示」摆给人。
function openArb() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  $("arb-overlay").classList.remove("hidden");
  $("arb-report").textContent = "";
  arbScan();
}
function arbPaint(d) {
  const head = $("arb-head"), box = $("arb-body");
  box.innerHTML = "";
  const items = d.条目 || [];
  head.textContent = `找到 ${items.length} 条可仲裁的冲突` +
    ((d.其它 || []).length ? ` · 另有 ${d.其它.length} 条只报不裁的结构问题` : "") +
    (d.有例外裁定 ? " · 这本书已有例外裁定,所以每条都需你确认" : "");
  if (!items.length) {
    const none = document.createElement("div");
    none.className = "hint";
    none.textContent = "没发现跨层写不一样的地方。";
    box.appendChild(none);
  }
  items.forEach((it) => box.appendChild(arbItem(it)));
  if ((d.其它 || []).length) {
    const t = document.createElement("div");
    t.className = "facts-sec-title";
    t.textContent = "只报不裁(没有「以谁为准」可言)";
    box.appendChild(t);
    d.其它.forEach((o) => {
      const row = document.createElement("div");
      row.className = "facts-row";
      row.textContent = `${o.项}:${o.值}${o.说明 ? " · " + o.说明 : ""}`;
      box.appendChild(row);
    });
  }
}
async function arbScan() {
  const head = $("arb-head");
  head.textContent = "扫一遍…";
  $("arb-body").innerHTML = "";
  try {
    const d = await jreq("POST", "/api/arbitrate", { root: DATA.root, conflicts: [] });
    if (d.ok === false) { head.textContent = d.reason || "扫不动。"; return; }
    arbPaint(d);
  } catch (e) {
    head.textContent = "扫不动:" + (e.message || e);
  }
  arbLoadOverrides();
}
// AI 读一遍:确定性检测只比「同一个键写了两个值」;正文是散文,「左手持剑 / 右手持剑」这种
// 矛盾要读懂句子才看得见。所以这一步花一次模型调用,而且**只出提案**——原句查不到的标红,
// 人自己勾哪些送进裁定。提案一个字都不落盘。
async function arbAi() {
  const rep = $("arb-report"), box = $("arb-body"), head = $("arb-head");
  const ch = (CUR && CUR.chapter) || 0;
  if (!ch) { toast("先打开一章正文", true); return; }
  if (!window.confirm(`让模型读一遍第 ${ch} 章的正文与设定/章纲,找事实层面的矛盾。\n\n` +
    "这会花一次模型调用;它只出提案,一个字都不会改,也不会自己记进裁定记录。要继续吗?")) return;
  head.textContent = `读第 ${ch} 章…`;
  box.innerHTML = "";
  rep.className = "hint";
  rep.textContent = "";
  try {
    const d = await jreq("POST", "/api/conflicts/extract", { root: DATA.root, chapter: ch });
    if (d.ok === false) { head.textContent = d.reason || "没读成。"; return; }
    const rows = d.冲突 || [];
    head.textContent = `模型给了 ${rows.length} 条冲突提案(还没写进任何地方)`;
    if (!rows.length) {
      const none = document.createElement("div");
      none.className = "hint";
      none.textContent = d.说明 || "模型没抽到事实层面的矛盾。";
      box.appendChild(none);
      return;
    }
    const picks = [];
    rows.forEach((it) => {
      const sec = document.createElement("div");
      sec.className = "facts-sec";
      const t = document.createElement("div");
      t.className = "facts-sec-title";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!it.原句在正文里;      // 原句查得到的才默认勾;查不到的多半是编的
      cb.dataset.no = String(it.序号);
      sec.appendChild(cb);
      sec.appendChild(document.createTextNode(" " + it.项));
      box.appendChild(sec);
      ["设定", "章纲", "正文"].forEach((L) => {
        if (!it[L]) return;
        const row = document.createElement("div");
        row.className = "facts-row";
        row.textContent = `${L}:${it[L].值}` + (it[L].位置 ? `　[${it[L].位置}]` : "");
        sec.appendChild(row);
      });
      const q = document.createElement("div");
      q.className = it.原句在正文里 ? "hint" : "error";
      q.textContent = it.原句
        ? (it.原句在正文里 ? `正文原句:${it.原句}` : `原句在正文里查不到(很可能编的):${it.原句}`)
        : "模型没给正文原句";
      sec.appendChild(q);
      picks.push({ cb, it });
    });
    const bar = document.createElement("div");
    bar.className = "field-row";
    const go = document.createElement("button");
    go.className = "mini";
    go.textContent = "把勾选的交给裁定";
    go.onclick = async () => {
      const chosen = picks.filter((p) => p.cb.checked).map((p) => p.it);
      if (!chosen.length) { rep.className = "error"; rep.textContent = "一条都没勾。"; return; }
      go.disabled = true;
      try {
        const r = await jreq("POST", "/api/arbitrate",
          { root: DATA.root, conflicts: chosen.map((it) => {
            const o = { 项: it.项 };
            ["设定", "章纲", "正文"].forEach((L) => { if (it[L]) o[L] = it[L]; });
            return o;
          }) });
        if (r.ok === false) { rep.className = "error"; rep.textContent = r.reason || "裁定没算出来。"; return; }
        rep.className = "hint";
        rep.textContent = "下面这些是按「设定 > 章纲 > 正文」算出来的;有一处例外裁定后,其余会逐处问你。";
        arbPaint(r);
      } catch (e) { rep.className = "error"; rep.textContent = "没算成:" + (e.message || e); }
      finally { go.disabled = false; }
    };
    bar.appendChild(go);
    box.appendChild(bar);
  } catch (e) {
    head.textContent = "没读成:" + (e.message || e);
  }
}
function arbItem(it) {
  const wrap = document.createElement("div");
  wrap.className = "facts-sec";
  const title = document.createElement("div");
  title.className = "facts-sec-title";
  title.textContent = it.项 + (it.一致 ? "(各层一致)" : "");
  wrap.appendChild(title);
  (it.各处 || []).forEach((c) => {
    const row = document.createElement("div");
    row.className = "facts-row";
    row.textContent = `${c.层}:${c.值}　[${c.位置}]`;
    wrap.appendChild(row);
  });
  const line = document.createElement("div");
  line.className = "hint";
  line.textContent = (it.需请示
    ? `「${it.项}」:按默认应改成以${it.以谁为准}为准,要改吗?(这处不会被自动改)`
    : (it.依据 || it.reason || "")) + (it.要改的层 && it.要改的层.length ? `　要改:${it.要改的层.join("、")}` : "");
  wrap.appendChild(line);
  // 只有两层都有值时才谈得上「以哪层为准」;单层的就是没冲突
  const layers = (it.各处 || []).map((c) => c.层);
  if (layers.length >= 2) {
    const bar = document.createElement("div");
    bar.className = "field-row";
    layers.forEach((L) => {
      const b = document.createElement("button");
      b.className = "mini";
      b.textContent = `记下:这一处以${L}为准`;
      b.onclick = () => arbOverride(it, L);
      bar.appendChild(b);
    });
    wrap.appendChild(bar);
  }
  return wrap;
}
async function arbOverride(it, layer) {
  const rep = $("arb-report");
  const reason = window.prompt(`把「${it.项}」记成「以${layer}为准」。\n这是例外裁定,只对这一项生效,别的冲突仍会逐处问你。\n\n写一句理由(可留空):`, "");
  if (reason == null) return;
  try {
    const d = await jreq("POST", "/api/arbitrate/override",
      { root: DATA.root, item: it.项, layer, reason: reason.trim() });
    if (d.ok === false) { rep.className = "error"; rep.textContent = d.reason || "没记上。"; return; }
    rep.className = "hint";
    rep.textContent = `记下了:「${it.项}」以${layer}为准(只对这一项生效)。`;
    arbScan();
  } catch (e) { rep.className = "error"; rep.textContent = "没记上:" + (e.message || e); }
}
async function arbLoadOverrides() {
  const box = $("arb-overrides");
  box.innerHTML = "";
  try {
    const d = await jreq("GET", `/api/arbitrate/overrides?root=${encodeURIComponent(DATA.root)}`);
    const rows = d.例外裁定 || [];
    if (!rows.length) {
      const none = document.createElement("div");
      none.className = "hint";
      none.textContent = "还没有。";
      box.appendChild(none);
      return;
    }
    rows.forEach((r) => {
      const row = document.createElement("div");
      row.className = "facts-row";
      row.textContent = `${r.项} → 以${r.以谁为准}为准${r.理由 ? " · " + r.理由 : ""}${r.记于 ? " · " + r.记于 : ""}`;
      box.appendChild(row);
    });
  } catch (e) {
    const err = document.createElement("div");
    err.className = "error";
    err.textContent = "读不到裁定记录:" + (e.message || e);
    box.appendChild(err);
  }
}

// ---------- 资料账本(R3/R4:伏笔 / 剧情线 / 时间线 / 知情矩阵) ----------
// 后端 /api/facts 一族早就写好了(四视图、灌伏笔、AI 抽提案、确认入账、谁知道),
// 前端此前**一个都没接**:账本只能拿记事本打开看。
let _factsProposal = [];
let _factsCur = 0;
function openFacts() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  $("facts-overlay").classList.remove("hidden");
  $("facts-proposal").classList.add("hidden");
  $("facts-proposal").innerHTML = "";
  $("facts-report").textContent = "";
  factsReload();
}
function _factsSec(title, items, fmt) {
  const wrap = document.createElement("div");
  wrap.className = "facts-sec";
  const h = document.createElement("div");
  h.className = "facts-sec-title";
  h.textContent = `${title}(${items.length})`;
  wrap.appendChild(h);
  if (!items.length) {
    const e = document.createElement("div");
    e.className = "hint";
    e.textContent = "还没有。";
    wrap.appendChild(e);
    return wrap;
  }
  items.slice(0, 300).forEach((it) => {
    const row = document.createElement("div");
    row.className = "facts-row";
    row.textContent = fmt(it);
    wrap.appendChild(row);
  });
  return wrap;
}
async function factsReload() {
  const head = $("facts-head"), box = $("facts-body");
  if (!DATA || !DATA.root) return;
  head.textContent = "读账本…";
  box.innerHTML = "";
  try {
    const d = await jreq("GET", `/api/facts?root=${encodeURIComponent(DATA.root)}`);
    if (d.ok === false) { head.textContent = d.reason || "读不出来。"; return; }
    _factsCur = d.当前章 || 0;
    head.textContent = `当前章 ${_factsCur || "?"} · 账本 ${d.条数 ?? 0} 条 · ${d.账本 || ""}`;
    box.appendChild(_factsSec("伏笔", d.伏笔 || [], (x) =>
      `${x.伏笔} · 埋设 第${x.埋设章 ?? "?"}章 · 推进到 第${x.最后推进章 ?? "?"}章` +
      (x.状态 ? ` · ${x.状态}` : "") + (x.该收 ? ` · 该在第${x.该收}章收` : "")));
    box.appendChild(_factsSec("剧情线", d.剧情线 || [], (x) =>
      `${x.剧情线} · 推进 ${x.推进次数} 次 · 最后 第${x.最后推进章 ?? "?"}章` +
      (x.多久没推进 != null ? ` · 已 ${x.多久没推进} 章没动` : "")));
    box.appendChild(_factsSec("时间线", d.时间线 || [], (x) =>
      `第${x.章}章 · ${(x.条目 || []).map((e) => `${e.时间点}:${e.事件 || ""}`).join("; ")}`));
    box.appendChild(_factsSec("知情", d.知情 || [], (x) =>
      `${x.人物}:${(x.知道 || []).map((k) => `第${k.章}章 ${k.事}=${k.值 || ""}`).join("、")}`));
  } catch (e) {
    head.textContent = "读账本失败:" + (e.message || e);
  }
}
async function factsIngest() {
  const rep = $("facts-report");
  rep.className = "hint";
  rep.textContent = "灌伏笔…";
  try {
    const d = await jreq("POST", "/api/facts/ingest", { root: DATA.root });
    if (d.ok === false) { rep.className = "error"; rep.textContent = d.reason || "没灌成。"; return; }
    rep.textContent = `灌好了:${d.条数 ?? d.灌入 ?? "?"} 条(只替换来源=卡章纲的行,手写行没动)。`;
    factsReload();
  } catch (e) { rep.className = "error"; rep.textContent = "没灌成:" + (e.message || e); }
}
async function factsExtract() {
  const ch = (CUR && CUR.chapter) || _factsCur;
  if (!ch) { toast("先选中一章(或让账本先读到当前章)", true); return; }
  const rep = $("facts-report"), box = $("facts-proposal");
  rep.className = "hint";
  rep.textContent = `让 AI 读第 ${ch} 章抽提案…(会花钱一次,不写盘)`;
  try {
    const d = await jreq("POST", "/api/facts/extract", { root: DATA.root, chapter: ch });
    if (d.ok === false) { rep.className = "error"; rep.textContent = d.reason || "没抽成。"; return; }
    _factsProposal = d.提案 || d.proposal || [];
    rep.textContent = `抽到 ${_factsProposal.length} 条提案。勾选要入账的,再点「确认入账」。`;
    box.classList.remove("hidden");
    box.innerHTML = "";
    const tip = document.createElement("p");
    tip.className = "hint";
    tip.textContent = "证据句必须在正文里查得到,否则入账会被退回(那是模型编的)。";
    box.appendChild(tip);
    if (!_factsProposal.length) {
      const none = document.createElement("div");
      none.className = "hint";
      none.textContent = "这一章没抽出可入账的条目。";
      box.appendChild(none);
      return;
    }
    _factsProposal.forEach((r) => {
      const row = document.createElement("label");
      row.className = "facts-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = r.序号;
      cb.checked = true;
      row.appendChild(cb);
      const txt = document.createElement("span");
      txt.textContent = `第${r.章 || ch}章 · ${r.类型} · ${r.主体}` +
        (r.对象 ? ` → ${r.对象}` : "") + (r.值 ? ` = ${r.值}` : "") +
        (r.证据 ? `　「${r.证据}」` : "");
      row.appendChild(txt);
      box.appendChild(row);
    });
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = "确认入账(只写勾中的)";
    btn.onclick = () => factsAccept(ch);
    box.appendChild(btn);
  } catch (e) { rep.className = "error"; rep.textContent = "没抽成:" + (e.message || e); }
}
async function factsAccept(ch) {
  const rep = $("facts-report");
  const picks = [...$("facts-proposal").querySelectorAll('input[type="checkbox"]')]
    .filter((c) => c.checked).map((c) => Number(c.value));
  if (!picks.length) { rep.className = "error"; rep.textContent = "一条都没勾。"; return; }
  try {
    const d = await jreq("POST", "/api/facts/accept",
      { root: DATA.root, chapter: ch, picks, proposal: _factsProposal });
    if (d.ok === false) {
      rep.className = "error";
      rep.textContent = (d.reason || "没入成。") +
        ((d.退回 || []).length ? "\n退回:" + d.退回.map((r) => `${r.主体}:${r.原因}`).join("; ") : "");
      return;
    }
    rep.textContent = `入账 ${d.写入 ?? d.条数 ?? picks.length} 条。`;
    $("facts-proposal").classList.add("hidden");
    factsReload();
  } catch (e) { rep.className = "error"; rep.textContent = "没入成:" + (e.message || e); }
}
async function factsAdd() {
  const rep = $("facts-report");
  const body = {
    root: DATA.root, chapter: Number($("facts-ch").value) || 0,
    kind: $("facts-kind").value, subject: $("facts-subject").value.trim(),
    obj: $("facts-obj").value.trim(), value: $("facts-value").value.trim(),
  };
  if (!body.chapter) { rep.className = "error"; rep.textContent = "得填第几章。"; return; }
  if (!body.subject) { rep.className = "error"; rep.textContent = "得填主体。"; return; }
  try {
    const d = await jreq("POST", "/api/facts", body);
    if (d.ok === false) { rep.className = "error"; rep.textContent = d.reason || "没记上。"; return; }
    rep.className = "hint";
    rep.textContent = "记上了。";
    ["facts-ch", "facts-subject", "facts-obj", "facts-value"].forEach((id) => { $(id).value = ""; });
    factsReload();
  } catch (e) { rep.className = "error"; rep.textContent = "没记上:" + (e.message || e); }
}
async function knowsAsk() {
  const out = $("knows-answer");
  const ch = Number($("knows-ch").value) || 0;
  const who = $("knows-who").value.trim();
  const what = $("knows-what").value.trim();
  if (!ch || !who || !what) { out.className = "error"; out.textContent = "三个都填(第几章 / 谁 / 什么)。"; return; }
  try {
    const q = new URLSearchParams({ root: DATA.root, chapter: String(ch), who, what });
    const d = await jreq("GET", `/api/facts/knows?${q.toString()}`);
    out.className = "hint";
    out.textContent = `${who} 在第 ${ch} 章「${d.判定 || "未知"}」` + (d.说明 ? ` —— ${d.说明}` : "");
  } catch (e) { out.className = "error"; out.textContent = "查不了:" + (e.message || e); }
}

async function openTrash() {
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  const ov = _trashOverlay();
  ov.classList.remove("hidden");
  const card = ov.firstElementChild;
  card.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = "回收站";
  const intro = document.createElement("p");
  intro.className = "hint";
  intro.textContent = "删掉的章节在这里。还原时如果原位置已有同名文件,会整批拒绝(不覆盖)。";
  card.appendChild(h); card.appendChild(intro);
  const report = document.createElement("div");    // 错误写在这里,不再只靠 toast(以前被面板盖住,人看不见)
  report.className = "hint";
  card.appendChild(report);

  let d;
  try {
    d = await jreq("GET", `/api/trash?root=${encodeURIComponent(DATA.root)}`);
  } catch (e) {
    report.className = "error";
    report.textContent = "读不到回收站:" + (e.message || e);
    return;
  }
  const items = (d && d["回收站"]) || [];
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "空的。";
    card.appendChild(empty);
  }
  items.forEach((it) => {
    const row = document.createElement("div");
    row.className = "trash-row";
    const label = document.createElement("div");
    label.className = "trash-name";
    label.textContent = `${it["名字"]}${it["章"] ? " · 第" + it["章"] + "章" : ""} · ${it["产物数"]} 个文件` +
      (it["时间"] ? " · " + it["时间"] : "");
    const btn = document.createElement("button");
    btn.className = "mini";
    btn.textContent = "还原";
    btn.onclick = async () => {
      if (!window.confirm(`把「${it["名字"]}」挪回原来的位置?\n原位置已有文件时会整批拒绝。`)) return;
      btn.disabled = true;
      try {
        const r = await jreq("POST", "/api/trash/restore", { root: DATA.root, name: it["名字"] });
        if (r && r.ok === false) { report.className = "error"; report.textContent = r.reason || "没还原成。"; return; }
        toast(`已挪回 ${r["挪回"] || 0} 个文件`);
        await refresh();
        openTrash();
      } catch (e) {
        report.className = "error";
        report.textContent = "没还原成:" + (e.message || e);
      } finally { btn.disabled = false; }
    };
    row.appendChild(label); row.appendChild(btn);
    card.appendChild(row);
  });

  const foot = document.createElement("div");
  foot.className = "row-end";
  if (items.length) {
    // R19:清空回收站是不可还原的,要照着词打字(后端 guardrail 的确认词)
    const purge = document.createElement("button");
    purge.className = "mini";
    purge.textContent = "清空回收站";
    purge.onclick = async () => {
      const word = window.prompt("清空后不可还原。要清就照着输入:确认清空回收站");
      if (word == null) return;
      purge.disabled = true;
      try {
        const r = await jreq("POST", "/api/trash/purge", { root: DATA.root, token: word.trim() });
        if (r && r.ok === false) { report.className = "error"; report.textContent = r.reason || "没清成。"; return; }
        toast("回收站清空了");
        openTrash();
      } catch (e) {
        report.className = "error";
        report.textContent = "没清成:" + (e.message || e);
      } finally { purge.disabled = false; }
    };
    foot.appendChild(purge);
  }
  const close = document.createElement("button");
  close.className = "ghost";
  close.textContent = "关上";
  close.onclick = () => ov.classList.add("hidden");
  foot.appendChild(close);
  card.appendChild(foot);
}

// ---------- 启动自检(只读,面板呈现) ----------
async function runDoctor() {
  try {
    const d = await jreq("GET", `/api/doctor?root=${encodeURIComponent(DATA.root)}`);
    const list = $("doctor-list"); list.innerHTML = "";
    (d.checks || []).forEach((c) => {
      const el = document.createElement("div");
      el.className = "doctor-item " + (c.ok ? "ok" : "bad");
      const mark = document.createElement("span");
      mark.className = "di-mark"; mark.innerHTML = icon(c.ok ? "check" : "cross");
      const body = document.createElement("div");
      const name = document.createElement("div");
      name.className = "di-name"; name.textContent = c.name;
      body.appendChild(name);
      if (!c.ok) {
        const fix = document.createElement("div");
        fix.className = "di-fix";
        fix.textContent = `缺:${c.missing || "—"}　→　${c.fix || ""}`;
        body.appendChild(fix);
      }
      el.appendChild(mark); el.appendChild(body);
      list.appendChild(el);
    });
    $("doctor-overlay").classList.remove("hidden");
  } catch (e) { toast(e.message, true); }
}

// ---------- 渲染 ----------
let KNOW_BUSY = false;      // 知识顾问正在花钱:别让人一个 Esc 把进度关掉
let ALIGN_BUSY = false;     // 大纲复核同理
let KNOW_SRC = null;        // 提案是「按哪份输入」问出来的 —— 入账要用这一份,不是点按钮那一刻的

function render() {
  // 开书就该看到「AI 写过、还没人过目」的提醒;原来只有保存/写章之后才冒出来(P2-2)
  if (DATA && DATA.root) { try { refreshPending(); } catch (e) { /* 提醒失败不影响开书 */ } }
  scheduleChainCheck();   // 联动提示:开书/换书/刷新/落库后都会经过这里(去抖 1.5s)
  $("proj-title").textContent = DATA.title;
  rebuildProviderOptions();   // 供应商下拉从服务端注册表下发,不在 HTML 里写死
  $("provider").value = DATA.backend.provider;
  rebuildModelPresets(DATA.backend.provider);
  setModelValue(DATA.backend.model);
  $("base-url").value = DATA.backend.base_url || "";
  $("context-len").value = DATA.backend.context_len || "";
  $("context-len-hint").textContent = contextLenSummary(DATA.backend.context_limit, DATA.backend.context_len_source);
  $("chapter-chars").value = DATA.backend.chapter_chars;
  $("api-key").value = "";
  applyProviderUI(DATA.backend.provider);   // 顺带按供应商设 key 框占位/显隐 base_url 与按钮

  const fpMap = {
    default: "中性默认 · 还没懂你",
    sample: icon("check") + " 已学你的样本",
    inherit: icon("check") + " 继承自另一本书",
    // 2026-10-25 修:喂过「别人的范文」(seed 的第二页)时,后端记的是 source=reference,
    // 这张表里没有这个键 → 界面上直接漏出英文单词 "reference"。现在补上,并把兜底也翻成人话
    // (以后再新增来源也不会把英文裸着漏给作者)。
    reference: "参考别人的范文 · 还没脱化成你的",
  };
  const _fpSrc = DATA.fingerprint_source;
  $("fp-source").innerHTML = fpMap[_fpSrc] || (icon("check") + " 已设置(来源:" + escHtml(_fpSrc || "未知") + ")");
  refreshStyleCard();

  // 章节
  /*2026-11-03（作者五点反馈④）：侧栏下方旧正文列表删除——与上方分卷树重复。
    #chapters 元素保留在 DOM（可能被别处引用），内容清空且隐藏。 */
  const ch = $("chapters"); ch.innerHTML = ""; ch.classList.add("hidden");
  if (!DATA.chapters.length) ch.innerHTML = `<li class="hint">还没有章节,点「写下一章」开始</li>`;
  DATA.chapters.forEach((c) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "ch-label";
    label.innerHTML = `<span class="ch-no">第${c.n}章</span>` +
      (c.title ? `<span class="ch-title">${escHtml(c.title)}</span>` : ``) +
      (c.edited ? `<span class="badge on">改过</span>` : ``) +
      (c.learned ? `<span class="badge on">已学</span>` : ``);
    label.title = c.title ? `第${c.n}章 · ${c.title}` : `第${c.n}章`;
    label.onclick = () => openFile(`正文/第${c.n}章.md`, true, c.n, li);
    const actions = document.createElement("span");
    actions.className = "ch-actions";
    const act = (ic, title, fn) => {
      const b = document.createElement("button");
      b.className = "ch-act"; b.title = title; b.innerHTML = icon(ic);
      b.onclick = (e) => { e.stopPropagation(); fn(); };
      return b;
    };
    actions.appendChild(act("arrowUp", "上移一章", () => moveChapter(c.n, "up")));
    actions.appendChild(act("arrowDown", "下移一章", () => moveChapter(c.n, "down")));
    actions.appendChild(act("plus", `在第${c.n}章后插入空章`, () => insertAfter(c.n)));
    actions.appendChild(act("redo", `重写本章正文(按当前细纲覆盖第${c.n}章;想换结构先改/重新生成细纲)`, () => confirmRewriteChapter(c.n)));
    actions.appendChild(act("trash", `删除第${c.n}章`, () => deleteChapter(c.n)));
    // 六棒进度(2026-10-21 需求确认):每一章都能看到六棒做到哪、也能逐棒人工启动
    actions.appendChild(act("check", "看这一章的六棒进度 / 单步启动", () => toggleChapterSteps(c.n, li)));
    li.appendChild(label); li.appendChild(actions);
    ch.appendChild(li);
  });
  $("btn-write-next").innerHTML = `写第${DATA.next_chapter}章 ` + icon("writeNext");

  const VOICE_FILES = ["写作指纹", "文风参考"];   // 「你的声音」栏目:风格类文件与设定分家
  fillList("brain", DATA.brain.filter((b) => !VOICE_FILES.includes(b.name)), true);
  fillList("voice", DATA.brain.filter((b) => VOICE_FILES.includes(b.name)), true);
  fillSkills(DATA.skills);
  fillAgents();
  renderJourney();
  loadVolumeTree();        // A2 分卷分章树（侧栏顶部，2026-11-02 作者手绘图）
  loadWritingMode();       // A1 快捷下拉当前值（GET only，不发 PUT）
}

// ---------- 领航员在场(伙伴面板的书内形态):出题/选项/自答/跳段/回头改 ----------
// navMode 三态编排:未解锁→主区居中(#nav-center);已解锁→球浮层(#nav-ball/#nav-popover);织章中→隐身。
function renderJourney() {
  // S0→S2 一次性交接:地基刚齐、还没写第一章、这本书没交接过 → 球脉冲一下 + 自动弹浮层 + 一句话提示(Task 3)
  const hk = DATA ? "loom_nav_handoff:" + DATA.root : "";
  if (hk && DATA.writing_unlocked && (DATA.chapters || []).length === 0 && !localStorage.getItem(hk)) {
    localStorage.setItem(hk, "1");
    const ball0 = $("nav-ball");
    if (ball0) { ball0.classList.add("nav-handoff"); setTimeout(() => ball0.classList.remove("nav-handoff"), 1400); }
    _navPopOpen = true;   // 交接后自动开一次浮层
    setTimeout(() => { toast("地基齐了——去织第一章。我搬去右下角,随叫随到"); }, 200);
  }
  const mode = navMode();
  const center = $("nav-center"), ball = $("nav-ball"), pop = $("nav-popover");
  const editorScroll = document.querySelector(".editor-scroll");
  if (!center) return;
  center.classList.toggle("hidden", mode !== "center");
  if (editorScroll) editorScroll.classList.toggle("hidden", mode === "center");
  ball.classList.toggle("hidden", mode !== "float");
  if (mode !== "float") { pop.classList.add("hidden"); _navPopOpen = false; }
  else { pop.classList.toggle("hidden", !_navPopOpen); if (_navPopOpen) paintNavPopList(); }   // 恢复显示的顶条随 render 刷新,不吃 stale 未读文案
  if (mode === "hidden") return;
  // 头像位(球与居中头各一次,幂等重建)
  const ava = $("nav-center-ava"), bava = $("nav-ball-ava");
  if (ava) { ava.innerHTML = ""; ava.appendChild(agentAvatar("领航员", "jc-ava", "jc-fallback")); }
  if (bava) { bava.innerHTML = ""; bava.appendChild(agentAvatar("领航员", "jc-ava", "jc-fallback")); }
  if (mode === "center") paintNavCenterChrome();
  if (mode === "float") paintNavDot();
  loadJourney();   // 取 journey/state → paintJourney() 画进 journeyHost()(段进度头两态都要)
  if (partnerUiMode() === "chat") loadPartnerPanel();   // 对话 UI:取 /api/partner/history → paintPartnerChat()
}

async function loadJourney() {
  const root = DATA && DATA.root;
  if (!root) return;
  if (JOURNEY) paintJourney();   // 有缓存且同书:先画一帧治双帧闪烁,后台刷新到了再重画一次
  let out = null;
  try {
    out = await jreq("GET", `/api/journey/state?root=${encodeURIComponent(root)}`);
  } catch (e) {
    out = null;
  }
  if (!DATA || DATA.root !== root) return;   // 已换书:过期响应丢弃,不碰 JOURNEY
  JOURNEY = out;
  if (navMode() === "center") paintNavCenterChrome();   // 段进度条首次加载没画过(chrome 早于 JOURNEY 就绪),这里补一次
  else if (navMode() === "float") { paintNavDot(); if (_navPopOpen) paintNavPopList(); }
  paintJourney();
}

// 居中态镶边(chrome):副标题 + 段进度条(点击=回头改这段)
function paintNavCenterChrome() {
  const role = _navRole || "领航员";
  const panelEntry = $("nav-panel");
  if (panelEntry) {
    panelEntry.disabled = DATA.foundation_ready !== true;
    panelEntry.title = DATA.foundation_ready === true ? "叫几位同事一起分析" : "地基完成后就可以叫大家来议";
  }
  const nameEl = document.querySelector(".nav-center-name");
  const subEl = $("nav-center-sub");
  // 头像按当前角色换（领航员走原样）
  const ava = $("nav-center-ava");
  if (ava) { ava.innerHTML = ""; ava.appendChild(agentAvatar(role, "jc-ava", "jc-fallback")); }

  // 议事厅(2026-10-25):标题与副标题说清「这是多人在答、并发几个」
  if (_panel) {
    if (nameEl) nameEl.textContent = "讨论组 · 一次问,多人答";
    if (subEl) {
      const n = (_panel.roles || []).length;
      subEl.textContent = _panel.busy
        ? `正在讨论 · 并发上限 ${_panel.cap}`
        : (n ? `参会 ${n} 人 · 并发上限 ${_panel.cap} · 点 × 可以把人去掉`
             : "说一句想改什么,领航员会挑几位同事一起看");
    }
    const strip = $("nav-center-strip");
    if (strip) { strip.innerHTML = ""; strip.classList.remove("hidden"); }
    return;
  }

  if (role !== "领航员") {
    const meta = AGENTS_META[role] || {};
    if (nameEl) nameEl.textContent = role;   // 作者 2026-10-20：只显示角色名，不带副标签
    if (subEl) {
      subEl.textContent = _navLockedSlot
        ? `🔒 写入位置已锁定：${humanizeSlot(_navLockedSlot)}`
        : (meta.desc || "");
    }
    const strip = $("nav-center-strip"); if (strip) { strip.innerHTML = ""; strip.classList.add("hidden"); }
    return;
  }

  if (nameEl) nameEl.textContent = "领航员 · 陪你把地基打完";
  const missing = DATA.missing || [];
  const cur = ((JOURNEY && JOURNEY.stages) || []).find((s) => JOURNEY && s.key === JOURNEY.current);
  const gist = missing.length ? `距开写还差 ${missing.length} 项:${missing.join("、")}` : "地基快齐了";
  // 副标题带上「现在这段要定什么」——立项/世界观等的具体指示,别让用户对着空题干瞪眼
  subEl.textContent = cur && cur.hint ? `${gist} · 现在定「${cur.key}」:${cur.hint}` : gist;
  // bug3:撤掉分段进度条(✓立项 ○世界观…)——对话式领航员自会按固定顺序引导,分段 chip 是老卡机遗留、无必要
  const strip = $("nav-center-strip"); strip.innerHTML = ""; strip.classList.add("hidden");
}

// 球未读点:有值就说明球浮层里有条领航员想说的话
function navUnread() {
  const chs = DATA.chapters || [];
  const last = chs[chs.length - 1];
  if (last && last.edited && !last.learned) return "上一章的手改可以 learn 了";
  const voice = ((JOURNEY && JOURNEY.stages) || []).find((s) => s.land === "seed");
  if (voice && !voice.done && !voice.skipped && DATA.fingerprint_source === "default") return "喂几段样本,让指纹像你";
  if (DATA.writing_unlocked && (DATA.missing || []).length) return `地基还差:${DATA.missing.join("、")}`;
  return "";
}
function paintNavDot() { $("nav-dot").classList.toggle("hidden", !navUnread()); }

// 球浮层顶部提示条:未读一句话 + (未解锁时)回居中的路
function paintNavPopList() {
  const list = $("nav-pop-list"); list.innerHTML = "";
  const msg = navUnread();
  if (msg) { const it = document.createElement("div"); it.className = "np-item"; it.textContent = msg; list.appendChild(it); }
  if (DATA.writing_unlocked === false) {
    const back = document.createElement("div"); back.className = "np-item"; back.textContent = "继续答题(回到居中)";
    back.onclick = () => { _navYield = false; _navPopOpen = false; renderJourney(); };
    list.appendChild(back);
  }
}

// 「这个软件怎么用」速查(2026-10-20):作者不用问模型也能看懂地基六步 / 文风参考两个入口 / 作者智库名册。
// 纯静态文本、零模型调用;用原生 <details>,默认收起,不抢版面。
function _guideDetails() {
  const d = document.createElement("details");
  d.className = "jc-guide";
  d.innerHTML = `<summary>这个软件怎么用 · 30 秒速查</summary>
    <div class="jg-body">
      <p><b>地基六步</b>(填在「外置大脑/设定集」里,落盘才算数):
        立项 → 世界观 → 人物 → 卷纲 → 卡章纲 → 声音(可选)。
        卷纲可以先跳、声音可以跳,其余四段不能跳。</p>
      <p><b>文风参考那两个入口别混</b>:<br>
        · <b>喂样本</b> = 放<i>你自己写的</i>字 → 炼《写作指纹》,写手越写越像你;改完稿点「学这章的手改」,它还会自己长。<br>
        · <b>文风档案</b> = 放<i>别人的文章</i> → 每篇各蒸一份规则,写作时按「注入量」塞进提示词。
        「细致度」管蒸多细,「注入量」管用的时候塞多少字。</p>
      <p><b>作者智库</b>:设定师(世界观/规则) · 大纲师(卷纲章纲) ·
        战斗设计师(打斗/战力量级) · 情绪节奏设计师(高潮/情绪曲线) ·
        批评家(挑毛病) · 编辑总监(整本通读) · 市场分析师(市场判断/上架时机) · 人物侧写师(细化角色设计/人物卡与互动细节)。左边栏点开就能一对一聊,也可以让领航员帮你路由。</p>
      <p class="jg-tip">查资料和量文风已经变成<b>工具</b>,不再是两个"人":任何角色(包括写手)卡住时都能自己调
        「查知识」「看数据」「改指纹」这些——<b>具体有哪些以对话里实际调用的为准</b>,这里不列全。</p>
      <p><b>一章怎么织出来</b>:章节细纲师 → 章节设定师 → 章节分镜师 → 写手 → 责任编辑 → 润色师。
        前三棒改同一份细纲,后三棒改同一份正文。</p>
      <p class="jg-tip">稿子都存在本地;右上角「回首页」可以随时换书。</p>
    </div>`;
  return d;
}



// ── 逐章六棒进度 + 单步启动(B,2026-10-21 需求确认)─────────────────────
// 为什么要有:六棒跑一半断了,作者只能从头再来;而本地模型一次跑六棒十几分钟,
// 断了最痛。这里把"每一步"拆成可点的小按钮,做到哪停哪,下次接着点下一步。
const _STEP_HINT = {
  "章节细纲师": "拆场次、定每场字数",
  "章节设定师": "把设定逐条对照进细纲",
  "章节分镜师": "标镜头/视角/慢切",
  "写手": "照细纲写正文",
  "责任编辑": "挑硬伤并当场改稿",
  "润色师": "去 AI 腔,留你的口头禅",
};

async function toggleChapterSteps(n, li) {
  const box = li.querySelector(".ch-steps");
  if (box) { box.remove(); return; }
  const wrap = document.createElement("div");
  wrap.className = "ch-steps";
  wrap.textContent = "读进度…";
  li.appendChild(wrap);
  let d = null;
  try { d = await jreq("GET", `/api/chapter/steps?root=${encodeURIComponent(DATA.root)}&chapter=${n}`); }
  catch (e) { wrap.textContent = "读不到:" + e.message; return; }
  if (!d || d.ok === false) { wrap.textContent = "读不到这一章的进度"; return; }
  wrap.innerHTML = "";
  if (d["上一章没弄完"]) {
    const w = document.createElement("div");
    w.className = "ch-step-warn";
    w.textContent = `上一章(第 ${n - 1} 章)还没弄完——先把上一章写出来,再动这一章。`;
    wrap.appendChild(w);
  } else if (d["上一章偏短"]) {
    // 2026-10-21 裁定(乙):字数低于目标 60% **只提示、不拦**。
    const w = document.createElement("div");
    w.className = "ch-step-hint";
    w.textContent = `提醒:上一章(第 ${n - 1} 章)只有 ${d["上一章字数"]} 字,目标是 ${d["上一章目标"]} 字,`
      + "可能还没写完。想先写这一章也可以,点下面的按钮就行。";
    wrap.appendChild(w);
  }
  d.steps.forEach((st, i) => {
    const row = document.createElement("div");
    row.className = "ch-step" + (st["做过"] ? " done" : "");
    if (d["上一章没弄完"]) row.classList.add("blocked");
    const dot = document.createElement("span"); dot.className = "cs-dot";
    const nm = document.createElement("span"); nm.className = "cs-name"; nm.textContent = st["名字"];
    const meta = document.createElement("span"); meta.className = "cs-meta";
    meta.textContent = st["做过"] ? `${(st["耗时ms"] / 1000).toFixed(1)}s${st["字数"] ? " · " + st["字数"] + " 字" : ""}` : (i === 0 || d.steps[i - 1]["做过"] ? "可以跑" : "等上一棒");
    const go = document.createElement("button");
    go.className = "cs-go";
    go.textContent = st["做过"] ? "重跑" : "跑这一棒";
    go.title = (_STEP_HINT[st["名字"]] || "") + "｜会调用一次模型(花钱)";
    go.disabled = !!(d["上一章没弄完"]);
    go.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`跑「${st["名字"]}」这一棒?\n${_STEP_HINT[st["名字"]] || ""}\n\n会调用一次模型(花钱)。只做这一棒,后面的棒不动。`)) return;
      await runOneStep(n, st["名字"]);
    };
    row.appendChild(dot); row.appendChild(nm); row.appendChild(meta); row.appendChild(go);
    wrap.appendChild(row);
  });
}

// 单步跑:复用六棒浮窗的事件处理,只是把请求换成 /api/chapter/step
// 三入口公用：**确认是 NDJSON 之后**才开织章浮窗（审查反例②：门禁 409/400 期间不许闪"正在生成"）。
function openRunUI(n, title) {
  _wroteChapter = null;
  _lastDebugReport = null;
  _runMinimized = false; _streamChars = 0; _stripDone = null; _stripErr = null;
  _writingChapter = n; showStopBtn();
  hideStrip(); $("run-strip").classList.remove("err");
  $("run-title").textContent = title;
  $("agent-pills").innerHTML = "";
  $("run-log").innerHTML = "";
  $("run-stream").textContent = "";
  $("run-close").classList.add("hidden");
  $("run-force").classList.add("hidden");
  $("run-overlay").classList.remove("hidden");
  _runChapter = n;
  _weaving = true; renderJourney();     // 织章中:领航员隐身(Task 3)
}

async function runOneStep(n, step) {
  if (_weaving) { toast("现在有任务在跑,等它结束", true); return; }
  if (!(await preflightCheck(n, false))) return;
  // F1:单棒入口同样受第一章地基硬门约束(后端 /api/chapter/step 也拦);审稿棒按 T1 豁免
  const _fpre = await foundationPrecheck(n, step);
  if (_fpre) { handleFoundationBlock(_fpre, () => runOneStep(n, step)); return; }
  let resp;
  try {
    resp = await fetch("/api/chapter/step", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: DATA.root, chapter: n, step }),
    });
  } catch (e) { toast("连接失败:" + e.message, true); _weaving = false; return; }
  const ct = resp.headers.get("content-type") || "";
  if (!ct.includes("ndjson")) {
    const d = await resp.json().catch(() => ({}));
    if (handleFoundationBlock(d, () => runOneStep(n, step))) return;   // F1：门禁不闪流式态
    openRunUI(n, `正在跑「${step}」· 第 ${n} 章`);
    logRun(d.error || `失败 (${resp.status})`, "err", "cross");
    showRunClose(true); _weaving = false; renderJourney();
    toast(d.error || "这一棒没跑成", true);
    return;
  }
  openRunUI(n, `正在跑「${step}」· 第 ${n} 章`);   // 确认是 NDJSON：到这里才进流式态
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev = null;
        try { ev = JSON.parse(line); } catch (e) { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    logRun("这一棒中途断了:" + (e.message || e), "err", "cross");
  } finally {
    _stopRunTicker();
    hideStrip(); _runMinimized = false;
    showRunClose(false);
    _weaving = false; renderJourney();
    await refresh();
  }
}

function paintJourney() {
  // 2026-10-21:织章中不要重画中间区——原来这里一路走到 paintPartnerChat,
  // 于是"点写第1章"会把作者正在看的文件/角色界面**硬拽成领航员**。
  // 织章期间中间区保持原样,进度看浮窗/底部条就够了。
  if (_weaving && $("run-overlay") && !$("run-overlay").classList.contains("hidden")) {
    const host = journeyHost && journeyHost();
    if (host) host.classList.toggle("partner-chat", true);
    return;
  }
  // 2026-10-20 作者裁定:卡片式引导整条删掉 —— 用这软件的作者不需要强制傻瓜式引导。
  // 现在只有一条路:对话式(领航员对话 / 作者智库角色对话),台阶提示在顶部副标题里。
  const card = journeyHost();
  if (!card || !DATA) return;
  card.classList.toggle("partner-chat", true);
  // 议事例会(2026-10-25):一对多,独立一套视图(上方横条 + 群聊式时间线)
  if (_panel && navMode() === "center") { paintPanelCenter(); return; }
  // 作者智库角色走自己的渲染，领航员走书房伙伴通道
  if (_navRole && _navRole !== "领航员") { paintAgentChatCenter(); return; }
  paintPartnerChat();
}



async function postJourneyCard() {
  const root = DATA && DATA.root;
  if (!root) return;
  try {
    const out = await jreq("POST", "/api/journey/card", { root });
    if (!DATA || DATA.root !== root) return;   // 已换书:迟到的卡绝不上屏
    JOURNEY = out.state;
    JOURNEY.card = out.card;
    paintJourney();
  } catch (e) {
    if (!DATA || DATA.root !== root) return;
    toast(e.message, true);
  }
}

async function postJourneyAnswer(text) {
  if (!text || !text.trim()) return toast("先写点什么");
  const root = DATA && DATA.root;
  if (!root) return;
  try {
    const out = await jreq("POST", "/api/journey/answer", { root, answer: text.trim() });
    if (!DATA || DATA.root !== root) return;   // 已换书:迟到的响应绝不上屏
    JOURNEY = out.state;
    _navAutoAsked = new Set();   // 答完一题:落到的下一段/下一问在居中态自动出题(接着对话)
    toast("已记下 → " + out.landed);
    paintJourney();
    refresh();   // 外置大脑侧栏跟着长
  } catch (e) {
    if (!DATA || DATA.root !== root) return;
    toast(e.message, true);
  }
}

async function postJourneyGoto(stage, skip) {
  const root = DATA && DATA.root;
  if (!root) return;
  try {
    const out = await jreq("POST", "/api/journey/goto", { root, stage, skip: !!skip });
    if (!DATA || DATA.root !== root) return;   // 已换书:迟到的响应绝不上屏
    JOURNEY = out;
    _navAutoAsked = new Set();   // 回头改/换段:落到的段在居中态自动出题
    paintJourney();
    if (navMode() === "center") paintNavCenterChrome();
  } catch (e) {
    if (!DATA || DATA.root !== root) return;
    toast(e.message, true);
  }
}

// ---------- 书房伙伴对话(P3):say 流式消费 + 候选卡拍板 + 渲染 ----------
// 打开面板先取 history;仅当 history 空且书无章节才自动开场(说明书 §2「开场的触发与幂等」)。
// 换书判据同 loadJourney(_partnerRoot 缓存),避免每次 renderJourney 都重新拉一次。
async function loadPartnerPanel() {
  const root = DATA && DATA.root;
  if (!root) return;
  if (_partnerRoot === root) { paintJourney(); return; }   // 本书已加载过:只重画,不重拉
  _partnerRoot = root;
  PARTNER = null;   // null = 加载中占位(区别于「加载完但确实没有历史」的空数组)
  paintJourney();
  let out = null, failed = null;
  try { out = await jreq("GET", `/api/partner/history?root=${encodeURIComponent(root)}`); }
  catch (e) { failed = e; }
  if (!DATA || DATA.root !== root) return;   // 已换书:迟到的响应绝不上屏
  if (failed) {
    // 读失败绝不等价于「还没聊过」:下面那段自动开场是**一次付费调用**(UI 审查 P1-9)
    _partnerRoot = null;                     // 允许重试
    PARTNER = { events: [], loadError: failed.message || String(failed) };
    paintJourney();
    return;
  }
  PARTNER = (out && Array.isArray(out.events)) ? out : { events: [] };
  paintJourney();
  const noChapters = (DATA.chapters || []).length === 0;
  if (PARTNER.events.length === 0 && noChapters && !_partnerAutoOpened.has(root)) {
    _partnerAutoOpened.add(root);   // 前端去重(防 _navAutoAsked 式重复);服务端对空 text 不追加 user 事件是第二道保险
    partnerSay("");
  }
}

// 一轮 say:ndjson 流式消费,套路照抄 writeChapter() 的 /api/write 消费范式(fetch + reader + 按行 parse)。
async function partnerSay(text) {
  const root = DATA && DATA.root;
  if (!root || _partnerBusy) return;
  beginAgentChainRound(PARTNER, text);
  const gen = ++_partnerGen;   // 本轮世代号:归档新会话/换书会令 _partnerGen 前移,凡 gen 对不上的迟到事件一律作废
  _partnerBusy = true;
  _partnerDelta = "";
  _partnerReasoning = "";              // 新一轮:清上轮思考流
  _partnerStopped = false;             // 新一轮:清上轮的「已停止」标记
  const ctrl = new AbortController();  // P1.5 停止键:点了 abort() → reader/fetch 抛 AbortError
  _partnerAbort = ctrl;
  paintJourney();

  let resp;
  try {
    resp = await fetch("/api/partner/say", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, text }),
      signal: ctrl.signal,
    });
  } catch (e) {
    if (e && e.name === "AbortError") {   // 用户点了停止(fetch 阶段就 abort),不是连接失败——不落 error
      if (gen === _partnerGen) { _partnerBusy = false; _partnerStopped = true; _partnerAbort = null; }
      if (gen === _partnerGen && DATA && DATA.root === root) paintJourney();
      return;
    }
    if (gen === _partnerGen) _partnerBusy = false;   // 世代已过期(旧轮):busy 归属新世代,不许旧轮回头清掉
    if (gen === _partnerGen && DATA && DATA.root === root) {
      if (!PARTNER) PARTNER = { events: [] };
      PARTNER.events.push({ t: "error", text: "连接失败:" + e.message });
      paintJourney();
    }
    return;
  }

  const ct = resp.headers.get("content-type") || "";
  if (!ct.includes("ndjson")) {
    // 非流式响应 = 出错(如 409 partner_busy/织章占锁),同 writeChapter() 对非 ndjson 响应的处理套路
    const d = await resp.json().catch(() => ({}));
    if (gen === _partnerGen) _partnerBusy = false;
    if (gen === _partnerGen && DATA && DATA.root === root) {
      if (!PARTNER) PARTNER = { events: [] };
      PARTNER.events.push({ t: "error", text: (d.error || `失败 (${resp.status})`) + codeHint(d.code) });
      paintJourney();
    }
    return;
  }

  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch (e) { continue; }   // 坏行跳过(同 jsonl「坏行当无」纪律),不许炸整条流
        if (gen === _partnerGen && DATA && DATA.root === root) handlePartnerEvent(ev);
      }
    }
  } catch (e) {
    if (e && e.name === "AbortError") {   // 用户点了停止(流式阶段 abort):不追加 error 气泡,只记已停止
      if (gen === _partnerGen) _partnerStopped = true;
    } else if (gen === _partnerGen && DATA && DATA.root === root) {
      if (!PARTNER) PARTNER = { events: [] };
      PARTNER.events.push({ t: "error", text: "连接中断:" + e.message });
    }
  } finally {
    if (gen === _partnerGen) {   // 本世代已被「另起一段对话」/换书作废:busy/delta 归下一世代管,这里不许再碰
      _partnerBusy = false;
      _partnerDelta = "";
      _partnerReasoning = "";
      _partnerAbort = null;
      settleAgentChainReminder(PARTNER);
      refreshAgentLamps();       // 领航员这一轮结束:灯跟着刷新(2026-10-25)
      if (DATA && DATA.root === root) paintJourney();
    }
  }
}

// 事件信封 {"t":...}(非 /api/write 的 type-keyed):user/assistant/assistant_delta/tool/result/proposal/error。
// 流结束靠 HTTP body 结束,无显式 done 事件——partnerSay() 的 reader 循环 done=true 即收尾。
function handlePartnerEvent(ev) {
  if (!PARTNER) PARTNER = { events: [] };
  const t = ev && ev.t;
  if (t === "reasoning_delta") {
    // v2 思考层:思考流逐块累积(后端已攒几字才发)。纯 UI 灰字,不进 PARTNER.events(transient)。
    _partnerReasoning += (ev.text || "");
  } else if (t === "assistant_delta") {
    // _preview() 每次给的是「刚落定的一整行」,不是累积全文——这里按行拼接重建预览文本
    _partnerDelta += (_partnerDelta ? "\n" : "") + (ev.text || "");
  } else if (t === "assistant") {
    _partnerDelta = "";
    // v2:把本轮思考挂到这条回复上(会话内可回看,收成「已思考 ▸」;不落盘、刷新即失,transient)
    if (_partnerReasoning) ev._reasoning = _partnerReasoning;
    _partnerReasoning = "";
    PARTNER.events.push(ev);
  } else if (t === "user" || t === "tool" || t === "result" || t === "proposal" || t === "error") {
    _partnerDelta = "";   // 权威事件到了(轮内一个新阶段开始/结束),清预览缓冲
    _partnerReasoning = "";   // 非 assistant 的阶段结束:思考已产出动作,清掉(工具轮的思考不单独回看)
    PARTNER.events.push(ev);
  }
  // 其余类型(meta/summary/confirm)不在 say 流里出现(confirm 只来自 partnerConfirm() 本地追加),忽略即可
  paintJourney();
}

function paintPartnerChat() {
  const card = journeyHost();
  if (!card || !DATA) return;
  applyNavSide();
  _bindNavSelWatch();
  // 选区守卫:用户正在面板里选字 → 这一帧不重绘(重绘会把选区清掉,字就选不住了)
  if (_selInCard(card)) { _navSkipPaint = true; return; }
  // sticky-bottom(spec §5):重画前量旧容器是否贴底 + 当前位置,重画后据此决定滚动。
  const inputKey = chatKey(_navRole || "领航员");
  const previousActive = chatInputSnapshot(card, inputKey);
  const oldScroll = card.querySelector(".pc-scroll");
  let stickBottom = true, prevTop = 0;
  if (oldScroll) {
    prevTop = oldScroll.scrollTop;
    stickBottom = (oldScroll.scrollHeight - oldScroll.scrollTop - oldScroll.clientHeight) < 40;
  }
  card.innerHTML = "";

  const scroll = document.createElement("div");
  scroll.className = "pc-scroll";
  card.appendChild(scroll);

  if (PARTNER === null) {
    const wait = document.createElement("div");
    wait.className = "pc-empty";
    wait.textContent = "加载对话记录…";
    scroll.appendChild(wait);
  } else if (PARTNER.loadError) {
    const err = document.createElement("div");
    err.className = "pc-empty";
    err.textContent = "对话记录没读出来:" + PARTNER.loadError +
      "。这是读取出了问题,不是「还没聊过」——所以没有自动开口。";
    const again = document.createElement("button");
    again.className = "mini";
    again.textContent = "重试";
    again.style.marginTop = "8px";
    again.onclick = () => { _partnerRoot = null; PARTNER = null; paintJourney(); loadPartnerPanel(); };
    err.appendChild(again);
    scroll.appendChild(err);
  } else {
    const rendered = renderPartnerEvents(PARTNER.events || []);
    if (!rendered.length && !_partnerBusy) {
      // 空对话 = 新作者第一次进来。给他一句话起点 + 一块可展开的速查(2026-10-20)。
      // 静态文本、零模型调用:没接模型也能看懂这软件怎么用。
      const empty = document.createElement("div");
      empty.className = "pc-empty pc-welcome";
      empty.innerHTML = `<div class="pcw-title">先聊这本书,我一步步来。</div>
        <div class="pcw-sub">你可以先说「我这本大概是什么故事」,也可以直接问「下一步该做什么」。
        底下这块速查看了就懂——不用问我。</div>`;
      empty.appendChild(_guideDetails());
      scroll.appendChild(empty);
    }
    rendered.forEach((el) => scroll.appendChild(el));
  }

  if (_partnerBusy) {
    // 末尾事件是 tool ⟺ dangling(已被扫描渲染成活动行,它本身即 busy 指示器)——
    // 此时不再叠通用「伙伴在想…」,免双指示器(spec §4.4)。
    const evs = (PARTNER && PARTNER.events) || [];
    const danglingTool = evs.length > 0 && evs[evs.length - 1].t === "tool";
    if (_partnerReasoning) {
      // v2 思考层:有思考流。没出正文=思考中(展开、实时流);已出正文=思考收折「已思考▸」+ 正文流。
      scroll.appendChild(pcReasoning(_partnerReasoning, !_partnerDelta));
      if (_partnerDelta) scroll.appendChild(pcBubble("assistant", _partnerDelta, true));
    } else if (_partnerDelta) {
      scroll.appendChild(pcBubble("assistant", _partnerDelta, true));
    } else if (!danglingTool) {
      scroll.appendChild(roleThinking("领航员"));
    }
  }

  if (_partnerStopped && !_partnerBusy) {   // 用户「停」的纯客户端标记(不落盘、不产事件),换书/另起/再发清零
    const stopped = document.createElement("div");
    stopped.className = "pc-stopped";
    stopped.textContent = "· 已停止 ·";
    scroll.appendChild(stopped);
  }

  paintAgentChainReminder(scroll, PARTNER);
  // 加载 history 期间(PARTNER===null)输入框必须禁用:否则这个网络窗口内发的消息会被晚到的 history 响应整体覆盖(见 partnerSay/loadPartnerPanel 注释)
  const loading = PARTNER === null;
  const inputRow = document.createElement("div");
  inputRow.className = "pc-input-row";
  const ta = document.createElement("textarea");
  ta.className = "pc-input";
  ta.rows = 2;
  ta.placeholder = _partnerBusy ? "伙伴在回复,请稍候…" : (loading ? "加载对话记录…" : "跟伙伴说点什么……");
  ta.disabled = _partnerBusy || loading;
  _pcInputEl = ta;
  const doSend = () => {
    const v = ta.value.trim();
    if (!v || _partnerBusy || PARTNER === null) return;
    ta.value = "";
    _chatDrafts.delete(inputKey);
    _chatFocusPending = { key: inputKey, anchor: document.activeElement };
    _roleWaitStarted.set("领航员", Date.now());
    partnerSay(v);
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
  });
  const sendBtn = document.createElement("button");
  sendBtn.className = "jc-btn pc-send";
  if (_partnerBusy) {
    // 回复中:发送键变「停止」——点了 abort() 立刻拿回控制权(P1.5 spec §10.1)
    sendBtn.textContent = "停止";
    sendBtn.disabled = false;
    sendBtn.onclick = () => { if (_partnerAbort) _partnerAbort.abort(); };
  } else {
    sendBtn.textContent = "发送";
    sendBtn.disabled = loading;
    sendBtn.onclick = doSend;
  }
  inputRow.appendChild(ta);
  inputRow.appendChild(sendBtn);
  card.appendChild(inputRow);
  mountChatInput(ta, inputKey, previousActive);

  // 2026-10-25:在领航员这里也能直接转「议事」——把已经打好的那句话原样带过去,不用再打一遍
  const panelBtn = document.createElement("button");
  panelBtn.className = "jc-btn ghost";
  panelBtn.textContent = "叫大家来议";
  panelBtn.title = "把这句话交给几位同事并发分析(费用按人数算,名单会先摆给你看)";
  panelBtn.disabled = _partnerBusy || loading || DATA.foundation_ready !== true;
  if (DATA.foundation_ready !== true) panelBtn.title = "地基完成后就可以叫大家来议";
  panelBtn.onclick = () => {
    _panelPrefill = ta.value.trim();
    ta.value = "";
    openPanel();
  };
  const threadRow = document.createElement("div");
  threadRow.className = "pc-thread-row";
  threadRow.appendChild(panelBtn);

  const newBtn = document.createElement("button");
  newBtn.className = "jc-btn ghost pc-newthread";
  newBtn.textContent = "另起一段对话";
  newBtn.disabled = _partnerBusy;   // 伙伴回复中不许归档,否则旧轮尾巴事件会串进刚重置的新会话(见 partnerNewThread)
  newBtn.title = "归档当前对话,伙伴重新认识这本书(书内容不受影响,旧对话仍留档可查)";
  newBtn.onclick = partnerNewThread;
  threadRow.appendChild(newBtn);
  card.appendChild(threadRow);

  // 贴底则跟读到底,否则维持作者停留位置(不被新事件拽走,spec §5)
  scroll.scrollTop = stickBottom ? scroll.scrollHeight : prevTop;
}

// 单遍配对扫描(spec §4.2):tool 按【下一事件类型】配对——绝不按工具名,
// 否则提设定 result(error) 会被当 proposal 渲染成假候选卡(评审 finding #2)。
// 任何配不到 result/proposal 的 tool 都落 active 或 done,绝不消失(finding #7)。
// ── 作者智库角色的居中对话（作者 2026-10-20 需求 B）────────────────────────
// 与书房伙伴共用同一套 DOM 样式（pc-scroll / pc-input-row / pc-bubble / 候选卡），
// 区别只在数据源与发送通道：这里读写 .伙伴对话/agent-<角色>.jsonl、走 /api/agent/chat。
function agentChatOpts() {
  return {
    role: _navRole || "领航员",
    noAdvance: true,   // 只落库，不替书房伙伴接一轮
    allowRetarget: true,   // 候选卡上给「换到…」（审查跟进项①）
    onEdit: (text) => { if (_pcInputEl) { _pcInputEl.value = text || ""; fitChatInput(_pcInputEl); _pcInputEl.dispatchEvent(new Event("input")); _pcInputEl.focus(); } },
  };
}

// 把一张候选卡换到另一个落点：内容原样保留，不调模型、不花钱。
// 后端会复制原提案、只换 slot，原提案留档（作者还能翻到）。
async function openSlotPickerForRetarget(ev, opts) {
  let slots = [];
  try {
    const d = await jreq("GET", `/api/slots?root=${encodeURIComponent(DATA.root)}&container=`);
    slots = (d.slots || []).filter((s) => String(s.container || "").includes("设定集"));
  } catch (e) { toast("取格子失败：" + e.message, true); return; }
  if (!slots.length) { toast("这本书还没有设定集格子", true); return; }

  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `
    <div class="run-card" style="width:520px;max-height:70vh;display:flex;flex-direction:column">
      <h3>这条内容换到哪一格？</h3>
      <p class="hint">内容原样搬过去，不用重新生成、不花模型调用。原卡会留档。</p>
      <div class="sp-list" style="overflow-y:auto;max-height:44vh;margin:8px 0"></div>
      <div class="row-end"><button class="ghost sp-cancel">取消</button></div>
    </div>`;
  document.body.appendChild(ov);
  const box = ov.querySelector(".sp-list");
  slots.forEach((s) => {
    const it = document.createElement("button");
    it.style.cssText = "display:block;width:100%;text-align:left;margin:4px 0;padding:8px 10px;"
      + "border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);cursor:pointer";
    it.innerHTML = `<b>${escHtml(humanizeSlot(s.id))}</b> `
      + `<span class="agent-tag">${s.filled ? "已填" : "未填"}</span>`;
    it.onclick = async () => {
      ov.remove();
      try {
        const r = await jreq("POST", "/api/proposal/retarget", {
          root: DATA.root, role: _navRole, id: ev.id, new_slot: s.id,
        });
        if (r && r.error) { toast(r.error, true); return; }
        // 就地更新这张卡，不重开对话
        const list = (_agentCenter && _agentCenter.events) || [];
        const tgt = list.find((x) => x && x.t === "proposal" && x.id === ev.id);
        if (tgt) { tgt.id = r.id; tgt.slot = r.slot; tgt.before = r.before || ""; }
        _navLockedSlot = r.slot;   // 后续对话也跟着新落点
        toast("已换到：" + humanizeSlot(r.slot));
        paintJourney();
      } catch (e) { toast("换落点失败：" + e.message, true); }
    };
    box.appendChild(it);
  });
  ov.querySelector(".sp-cancel").onclick = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
}

async function loadAgentCenter(role, lockedSlot) {
  _agentCenter = { role, lockedSlot: lockedSlot || "", events: [], busy: false, loadError: "", _delta: "" };
  paintJourney();
  try {
    const d = await jreq("GET", `/api/agent/chat/history?root=${encodeURIComponent(DATA.root)}`
      + `&role=${encodeURIComponent(role)}&tail=40`);
    if (!_agentCenter || _agentCenter.role !== role) return;
    _agentCenter.events = d.events || [];
  } catch (e) {
    if (_agentCenter && _agentCenter.role === role) _agentCenter.loadError = e.message;
  }
  // 收尾班(补1,§9/§13.1/§13.2):任务事件被 history tail=40 挤出后,1:1 恢复不再有保证
  // → 拉一次 /api/agent/task 快照按当前 role+conversation 筛活动任务,命中就补一行
  // 任务行(零模型;旧任务/completed 不冒充当前任务)。
  // 代际口径(2026-11-08 复审核对):agentCenterSay 不传 conversation_id → 服务端落
  // 「默认」=服务端当前代际(§13.2 老客户端口径);「另起一段对话」是前端归档,服务端
  // 仍落「默认」。故这里与发送同用 _AGENT_CONV 常量,两处口径永远一致。
  try {
    const t = await jreq("GET", `/api/agent/task?root=${encodeURIComponent(DATA.root)}`
      + `&role=${encodeURIComponent(role)}&conversation_id=${encodeURIComponent(_AGENT_CONV)}`
      + `&active_only=true`);
    if (!_agentCenter || _agentCenter.role !== role) return;
    const act = (t.tasks || []).find((x) => (x.scope || {}).role === role) || null;
    if (act && !_agentCenter.events.some((ev) =>
        ev.t === "task_prepared" && ev.task_id === act.task_id)) {
      _agentCenter.events.push({
        t: "task_prepared", task_id: act.task_id, revision: act.revision || 0,
        goal: act.goal || "", scope: act.scope || {},
        text: `〔恢复〕当前任务:${act.goal || act.task_id}`,
      });
    }
  } catch (e) {
    // §9 要求读取失败有可见诊断(2026-11-08 复审):不再静默吞,状态行提示(不挡对话渲染)
    if (_agentCenter && _agentCenter.role === role)
      _agentCenter.loadError = _agentCenter.loadError || `任务快照读取失败:${e.message || e}`;
  }
  paintJourney();
}

async function agentCenterSay(text, opts) {
  const ac = _agentCenter;
  const resume = !!(opts && opts.resume);   // 续轮:不是作者说的话,不 push user 气泡
  if (!ac || ac.busy || (!resume && !String(text || "").trim())) return;
  if (!resume) beginAgentChainRound(ac, text);
  ac.busy = true;
  if (!resume) ac.events.push({ t: "user", text });
  paintJourney();
  try {
    const resp = await fetch("/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        root: DATA.root, role: ac.role, text: text || "", locked_slot: ac.lockedSlot || "",
        resume,
        task_id: (opts && opts.task_id) || "",
        decision_event_id: (opts && opts.decision_event_id) || "",
        conversation_id: (opts && opts.conversation_id) || _AGENT_CONV,
      }),
    });
    if (!resp.ok || !resp.body) throw new Error("连接失败 " + resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) {
        const s = line.trim(); if (!s) continue;
        let ev; try { ev = JSON.parse(s); } catch { continue; }
        if (ev.t === "user") continue;   // 本地已经加过这条，别重复渲染
        if (ev.t === "assistant_delta") {
          ac._delta = (ac._delta || "") + (ev.text || "");
          paintJourney();
        } else {
          ac._delta = "";
          ac.events.push(ev);
          paintJourney();
        }
      }
    }
  } catch (e) {
    ac.events.push({ t: "error", text: String((e && e.message) || e) });
  } finally {
    ac.busy = false;
    ac._delta = "";
    settleAgentChainReminder(ac);
    paintJourney();
    refreshAgentLamps();      // 一轮结束:指示灯跟着刷新
  }
}

function paintAgentChatCenter() {
  const card = journeyHost();
  if (!card || !DATA) return;
  const role = _navRole;
  const ac = _agentCenter;
  if (!ac || ac.role !== role) {
    card.innerHTML = "";
    loadAgentCenter(role, _navLockedSlot);
    return;
  }
  if (_selInCard(card)) { _navSkipPaint = true; return; }

  const inputKey = chatKey(_navRole || "领航员");
  const previousActive = chatInputSnapshot(card, inputKey);
  const oldScroll = card.querySelector(".pc-scroll");
  let stickBottom = true, prevTop = 0;
  if (oldScroll) {
    prevTop = oldScroll.scrollTop;
    stickBottom = (oldScroll.scrollHeight - oldScroll.scrollTop - oldScroll.clientHeight) < 40;
  }
  card.innerHTML = "";
  const scroll = document.createElement("div");
  scroll.className = "pc-scroll";
  card.appendChild(scroll);

  if (ac.loadError) {
    const err = document.createElement("div");
    err.className = "pc-empty";
    err.textContent = "对话记录没读出来：" + ac.loadError;
    scroll.appendChild(err);
  } else {
    const rendered = renderPartnerEvents(ac.events || [], agentChatOpts());
    if (!rendered.length && !ac.busy && !ac._delta) {
      const empty = document.createElement("div");
      empty.className = "pc-empty";
      empty.textContent = `跟「${role}」聊聊这本书吧。`;
      scroll.appendChild(empty);
    }
    rendered.forEach((el) => scroll.appendChild(el));
  }

  if (ac._delta) scroll.appendChild(pcBubble("assistant", ac._delta, true, role));
  else if (ac.busy) {
    // 最后一条是 tool（还没有配对的 result）= 正在查资料，别只显示通用的「在想…」
    const evs = ac.events || [];
    const last = evs[evs.length - 1];
    if (last && last.t === "tool") {
      const think = document.createElement("div");
      think.className = "pc-thinking";
      think.innerHTML = zhaoIconHtml() + `<span>正在${escHtml(last.name || "查资料")}…</span>`;
      scroll.appendChild(think);
    } else {
      scroll.appendChild(roleThinking(role));
    }
  }

  const inputRow = document.createElement("div");
  inputRow.className = "pc-input-row";
  const ta = document.createElement("textarea");
  ta.className = "pc-input";
  ta.rows = 2;
  paintAgentChainReminder(scroll, ac);
  ta.placeholder = ac.busy ? `${role}在回复，请稍候…` : `跟「${role}」说点什么……`;
  ta.disabled = ac.busy;
  _pcInputEl = ta;
  // 联动预填:「去问大纲师 / 去挑毛病」只把话放进输入框,绝不自动发送。
  // 不清空(见上面 _chainPrefill 的注释):这一屏可能被重画,重画时要能再贴上。
  // 用户自己打字或点发送时才清(_chainPrefill=null),免得把人家改过的话再覆盖回去。
  if (_chainPrefill && _chainPrefillRole === role && !ac.busy) ta.value = _chainPrefill;
  ta.addEventListener("input", () => { _chainPrefill = null; _chainPrefillRole = null; });
  const doSend = () => {
    const v = ta.value.trim();
    if (!v || ac.busy) return;
    _chainPrefill = null; _chainPrefillRole = null;
    ta.value = "";
    _chatDrafts.delete(inputKey);
    _chatFocusPending = { key: inputKey, anchor: document.activeElement };
    _roleWaitStarted.set(role, Date.now());
    agentCenterSay(v);
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
  });
  const sendBtn = document.createElement("button");
  sendBtn.className = "jc-btn pc-send";
  sendBtn.textContent = "发送";
  sendBtn.disabled = ac.busy;
  sendBtn.onclick = doSend;
  inputRow.appendChild(ta);
  inputRow.appendChild(sendBtn);
  // 编辑总监专有:全书审(作者 2026-10-20 追加)。先报账再跑;按块可中断;只写 .审稿留痕/
  if (role === "编辑总监") {
    const fr = document.createElement("button");
    fr.className = "jc-btn";
    fr.textContent = "全书审";
    fr.title = "先出一份本地问题地图（不花钱），你看过报账再决定要不要按块通读";
    fr.disabled = ac.busy;
    fr.onclick = () => fullReviewRun(fr);
    inputRow.insertBefore(fr, sendBtn);   // 放在「发送」左边:先看/先审，再说话
  }
  // 2026-10-25:跟一个角色聊到一半改成「议事」——这句话交给大家一起看,名单先摆出来
  const pbtn = document.createElement("button");
  pbtn.className = "jc-btn ghost";
  pbtn.textContent = "叫大家来议";
  pbtn.title = "把这句话交给几位同事并发分析(费用按人数算)";
  pbtn.disabled = ac.busy || DATA.foundation_ready !== true;
  if (DATA.foundation_ready !== true) pbtn.title = "地基完成后就可以叫大家来议";
  pbtn.onclick = () => {
    _panelPrefill = ta.value.trim();
    ta.value = "";
    openPanel();
  };
  // 议事与另起对话并列在底部，输入行只保留发送相关操作。
  card.appendChild(inputRow);
  mountChatInput(ta, inputKey, previousActive);
  // 「另起一段对话」(2026-10-21 需求确认):作者智库每个角色都要有,和领航员那个一样。
  // 为什么必须有:本地模型上下文有限,长对话必然塞爆;归档旧段、另起一段是唯一解。
  // 只挪 .伙伴对话/agent-<角色>.jsonl,不动书里任何内容。
  const threadRow = document.createElement("div");
  threadRow.className = "pc-thread-row";
  const nt = document.createElement("button");
  nt.className = "jc-btn ghost pc-newthread";
  nt.textContent = "另起一段对话";
  nt.title = "归档与这个角色的当前对话、另起一段(书内容不受影响,旧对话仍留档可查)";
  nt.disabled = ac.busy;      // 回复中不许归档,否则旧轮的尾巴事件会串进新会话
  nt.onclick = () => agentNewThread(role);
  threadRow.appendChild(pbtn);
  threadRow.appendChild(nt);
  card.appendChild(threadRow);

  scroll.scrollTop = stickBottom ? scroll.scrollHeight : prevTop;
}

// 归档与某个智库角色的对话、另起一段(镜像 partnerNewThread,只动 .伙伴对话/ 下的 jsonl)
async function agentNewThread(role) {
  const root = DATA && DATA.root;
  if (!root) return;
  const ac = _agentCenter;
  if (ac && ac.busy) { toast(`${role}还在回复,请稍候`, true); return; }
  if (!window.confirm(`归档与「${role}」的当前对话、另起一段?(书内容不受影响,旧对话仍留档可查)`)) return;
  try {
    const out = await jreq("POST", "/api/agent/chat/new", { root, role });
    if (!DATA || DATA.root !== root) return;
    if (out && out.error) { toast(out.error, true); return; }
    await loadAgentCenter(role, (ac && ac.lockedSlot) || "");
    toast(`已与「${role}」另起一段对话`);
  } catch (e) {
    const msg = /busy|在应答|收尾/.test(e.message || "") ? `${role}还在收尾,稍等一下再另起` : e.message;
    toast(msg, true);
  }
}

// ---------- 议事例会(2026-10-25 需求确认)----------
// 一句话需求 → 领航员点名 → 几位同事**真并发**各给一段 → 我来看分歧点。
// 与一对一的区别只有两处:通道是 /api/panel/say,视图是「上方横条 + 群聊式时间线」。
// 钱是乘法的:点名 1 次 + 每位参会角色 1 轮(谁再问同事,每人最多 +2),所以名单和人数一律先摆在界面上。
let _panel = null;   // { events, roles:[{role,why}], cap, state:{role:状态}, buf:{role:流式缓冲}, busy, id, note, repick }
let _panelPrefill = null;   // 从对话界面转「议事」时,把已经打好的那句话原样带过去
const _PANEL_STATE_TEXT = { queued: "排队中", running: "分析中", done: "已答复", error: "出错" };

function openPanel() {
  if (!DATA || DATA.foundation_ready !== true) { toast("先完成地基，再叫大家来议"); return; }
  _navRole = "领航员";
  _navForceCenter = true;
  _navYield = false;
  _navLockedSlot = "";
  _agentCenter = null;
  if (!_panel) {
    _panel = { events: [], roles: [], cap: 2, state: {}, buf: {}, busy: false, id: "", note: "", repick: false };
    loadLastPanel();          // 先把上一场摆出来,别让人对着空白
  }
  markRoleSelected(null);
  renderJourney();
}

// 打开时读回最近一场(只读;读不出来就当没有,不影响开新的一场)
async function loadLastPanel() {
  const root = DATA && DATA.root;
  if (!root) return;
  try {
    const d = await jreq("GET", `/api/panel/history?root=${encodeURIComponent(root)}`);
    const rounds = (d && d.rounds) || [];
    if (!rounds.length) return;
    const id = rounds[0].id;
    const one = await jreq("GET", `/api/panel/history?root=${encodeURIComponent(root)}&panel=${encodeURIComponent(id)}`);
    if (!_panel || (_panel.events || []).length) return;   // 作者已经开口了,别覆盖
    if (!DATA || DATA.root !== root) return;
    const evs = ((one && one.events) || []).filter((e) => e && e.t !== "panel_state");
    const begin = evs.find((e) => e.t === "panel_begin");
    _panel.id = id;
    _panel.cap = (begin && begin.cap) || 2;
    _panel.note = (begin && begin.note) || "";
    _panel.roles = (((begin && begin.roles) || []).map((r) => ({ role: r })));
    // 合并流水里不重复存作者那句(免得刷 N 遍),回看时从 panel_begin 里补一条
    const out = [];
    if (begin && (begin.text || "").trim()) out.push({ t: "user", text: begin.text });
    evs.forEach((e) => { if (e.t !== "panel_begin") out.push(e); });
    _panel.events = out.length ? out : evs;
    paintJourney();
  } catch (e) { /* 上一场读不出来,不影响开新的 */ }
}

function panelChatOpts() {
  return { noAdvance: true, allowRetarget: true,
    onEdit: (text) => { if (_pcInputEl) { _pcInputEl.value = text || ""; fitChatInput(_pcInputEl); _pcInputEl.dispatchEvent(new Event("input")); _pcInputEl.focus(); } } };
}

// 把一条事件并进面板状态。流式增量按角色分桶,否则四个人同时吐字会搅成一锅粥。
function _panelApply(ev) {
  const p = _panel;
  if (!p || !ev) return;
  if (ev.t === "panel_begin") {
    p.id = ev.panel || p.id;
    p.cap = ev.cap || p.cap;
    p.note = ev.note || "";
    p.roles = (ev.roles || []).map((r) => ({ role: r }));
    p.state = {};
    (ev.roles || []).forEach((r) => { p.state[r] = "queued"; });
    p.buf = {};
    p.events.push(ev);
    return;
  }
  if (ev.t === "panel_state") {
    if (ev.role) p.state[ev.role] = ev.state || "";
    return;
  }
  if (ev.t === "assistant_delta") {
    const r = ev.role || "?";
    p.buf[r] = (p.buf[r] || "") + (ev.text || "");
    return;
  }
  const r = ev.role || "";
  if (r) delete p.buf[r];
  p.events.push(ev);
}

async function panelReadStream(resp, onEvent) {
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      const s = line.trim(); if (!s) continue;
      let ev; try { ev = JSON.parse(s); } catch { continue; }
      onEvent(ev);
    }
  }
}

async function panelSay(text) {
  const p = _panel;
  const v = String(text || "").trim();
  if (!p || p.busy || !v || !DATA || !DATA.root) return;
  const root = DATA.root;
  beginAgentChainRound(p, v);
  p.busy = true;
  p.events.push({ t: "user", text: v });     // 我自己的话先上屏(右对齐)
  paintJourney();
  // 名单已在 → 沿用这批人(不再点名,省一次调用);没有任何人 → 让领航员点名
  const body = { root, text: v, roles: (p.roles || []).map((x) => x.role) };
  if (p.repick) { body.repick = true; p.repick = false; }
  try {
    const resp = await fetch("/api/panel/say", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => null);
      const code = detail && typeof detail.code === "string" ? detail.code : "";
      const missing = detail && Array.isArray(detail.missing)
        ? detail.missing.filter((x) => typeof x === "string" && x.trim()).join("、") : "";
      const reason = detail && typeof detail.error === "string" ? detail.error.trim() : "";
      const message = code === "foundation_incomplete"
        ? "地基未补齐" + (missing ? "：缺" + missing : "") + (reason ? "；" + reason : "")
        : (reason || "议事请求未成功") + (missing ? "；缺项：" + missing : "");
      throw new Error(message + `（HTTP ${resp.status}${code ? "；" + code : ""}）`);
    }
    if (!resp.body) throw new Error("连接失败 " + resp.status);
    await panelReadStream(resp, (ev) => {
      if (!DATA || DATA.root !== root) return;
      if (ev.t === "user") return;           // 服务端不再回放我说的那句,这里防御一下
      _panelApply(ev);
      paintJourney();
    });
  } catch (e) {
    if (_panel) { _panel.events.push({ t: "error", text: String((e && e.message) || e) }); }
  } finally {
    if (_panel) { _panel.busy = false; _panel.buf = {}; settleAgentChainReminder(_panel); paintJourney(); }
    refreshAgentLamps();      // 一轮结束:欠账可能刚变成"我说完了,等你"
  }
}

// 领航员收口:我点才跑(多花 1 次调用),不自动
async function panelWrap() {
  const p = _panel;
  if (!p || p.busy || !p.id || !DATA || !DATA.root) return;
  const root = DATA.root;
  p.busy = true;
  paintJourney();
  try {
    const resp = await fetch("/api/panel/wrapup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, panel: p.id }),
    });
    if (!resp.ok || !resp.body) throw new Error("连接失败 " + resp.status);
    await panelReadStream(resp, (ev) => {
      if (!DATA || DATA.root !== root) return;
      _panelApply(ev);
      paintJourney();
    });
  } catch (e) {
    if (_panel) _panel.events.push({ t: "error", text: String((e && e.message) || e) });
  } finally {
    if (_panel) { _panel.busy = false; paintJourney(); }
  }
}

// 上方横条:参会的人 + 各自状态。并发上限低于人数时,「谁在排队」要看得见。
function paintPanelStrip() {
  const strip = $("nav-center-strip");
  if (!strip || !_panel) return;     // 没在议事就别动那条横条(它是领航员/角色对话共用的)
  const p = _panel;
  strip.innerHTML = "";
  strip.classList.remove("hidden");
  strip.classList.add("pn-strip-line");
  const roles = ((p && p.roles) || []).map((x) => x.role);
  if (!roles.length) {
    const h = document.createElement("span");
    h.className = "pn-hint";
    h.textContent = "说一句想改什么 —— 领航员会挑几位同事一起看";
    strip.appendChild(h);
    return;
  }
  const state = (p && p.state) || {};
  roles.forEach((r) => {
    const st = state[r] || "";
    const chip = document.createElement("span");
    chip.className = "pn-chip" + (st === "running" ? " on" : "");
    chip.appendChild(agentAvatar(r, "pn-ava", "pn-fallback"));
    const nm = document.createElement("span");
    nm.className = "pn-chip-name";
    nm.textContent = r;
    chip.appendChild(nm);
    const dot = document.createElement("span");
    dot.className = "pn-dot " + st;
    dot.title = _PANEL_STATE_TEXT[st] || "还没开始";
    chip.appendChild(dot);
    // 「点掉」不要钱:名单点错了,下一轮就不派给他
    const x = document.createElement("button");
    x.className = "pn-x";
    x.textContent = "×";
    x.title = `下一轮不叫「${r}」`;
    x.disabled = !!(p && p.busy);
    x.onclick = () => {
      if (!_panel) return;
      _panel.roles = (_panel.roles || []).filter((y) => y.role !== r);
      delete _panel.state[r];
      paintPanelCenter();
    };
    chip.appendChild(x);
    strip.appendChild(chip);
  });
  const queued = roles.filter((r) => state[r] === "queued").length;
  const running = roles.filter((r) => state[r] === "running").length;
  const meta = document.createElement("span");
  meta.className = "pn-queue";
  meta.textContent = (p && p.busy)
    ? `并发 ${p.cap} · 在跑 ${running}${queued ? ` · 排队 ${queued}` : ""}`
    : `并发上限 ${p.cap}`;
  strip.appendChild(meta);
}

function pnBubble(role, text, live) {
  const wrap = document.createElement("div");
  wrap.className = "pn-row" + (live ? " pn-live" : "") + (role === "领航员" ? " pn-who-host" : "");
  const head = document.createElement("div");
  head.className = "pn-head";
  head.appendChild(agentAvatar(role || "?", "pn-ava", "pn-fallback"));
  const nm = document.createElement("span");
  nm.className = "pn-name";
  nm.textContent = role || "?";
  head.appendChild(nm);
  const b = document.createElement("div");
  b.className = "pn-bubble";
  b.textContent = text || "";
  wrap.appendChild(head);
  wrap.appendChild(b);
  return wrap;
}

// 议事轮次号(20261025-143012) → 2026-10-25 14:30。认不出来就原样返回。
function panelTs(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(String(id || ""));
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : (id || "");
}

function pnDivider(main, sub) {
  const d = document.createElement("div");
  d.className = "pn-divider";
  d.textContent = main || "";
  if (sub) { const s = document.createElement("small"); s.textContent = sub; d.appendChild(s); }
  return d;
}

function pnThinking(role) { return roleThinking(role); }

function pnSys(text) {
  const d = document.createElement("div");
  d.className = "pn-divider";
  d.textContent = text || "";
  return d;
}

function paintPanelCenter() {
  const card = journeyHost();
  if (!card || !DATA) return;
  const p = _panel;
  if (!p) return;
  if (_selInCard(card)) { _navSkipPaint = true; return; }

  paintPanelStrip();     // 横条在卡片外面,不参与下面的重画

  const inputKey = chatKey("议事");
  const previousActive = chatInputSnapshot(card, inputKey);
  const oldScroll = card.querySelector(".pc-scroll");
  let stickBottom = true, prevTop = 0;
  if (oldScroll) {
    prevTop = oldScroll.scrollTop;
    stickBottom = (oldScroll.scrollHeight - oldScroll.scrollTop - oldScroll.clientHeight) < 40;
  }
  card.innerHTML = "";

  const scroll = document.createElement("div");
  scroll.className = "pc-scroll pn-scroll";
  card.appendChild(scroll);

  const events = p.events || [];
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "pc-empty";
    empty.textContent = "说一件事,我让几位同事各自说一遍。他们同时干,但每个人都会先看完这本书的材料。";
    scroll.appendChild(empty);
  }
  events.forEach((ev) => {
    switch (ev && ev.t) {
      case "user": scroll.appendChild(pcBubble("user", ev.text)); break;
      case "assistant": scroll.appendChild(pnBubble(ev.role || "?", ev.text, false)); break;
      case "proposal": scroll.appendChild(pcProposal(ev, panelChatOpts())); break;
      case "result": scroll.appendChild(pcResult(ev)); break;
      case "tool": {
        const verb = partnerToolVerb(ev.name, ev.params);
        scroll.appendChild(pnSys(`${ev.role || ""} · ${verb.done}`));
        break;
      }
      case "error": scroll.appendChild(pcError(ev)); break;
      case "resume": scroll.appendChild(pnDivider(`${ev.role ? ev.role + " · " : ""}${ev.text || "接着上一轮往下走"}`)); break;
      case "panel_begin": {
        const n = (ev.roles || []).length;
        scroll.appendChild(pnDivider(`议事开始 · ${n} 人 · 并发上限 ${ev.cap}`, ev.note || ""));
        break;
      }
      case "panel_end": {
        const errs = (ev.errors || []).length;
        scroll.appendChild(pnDivider(errs ? `议事结束 · ${errs} 人出错` : "议事结束"));
        break;
      }
      default: break;   // panel_state 画在横条上,不占时间线
    }
  });

  // 正在吐字的:每个角色一条,单独淡显(别跟已完成的混在一起)
  Object.keys(p.buf || {}).forEach((r) => {
    const t = p.buf[r];
    if (t) scroll.appendChild(pnBubble(r, t, true));
  });
  // 在跑但还没吐字的:显示"在分析…"
  if (p.busy) {
    ((p.roles || []).map((x) => x.role))
      .filter((r) => (p.state || {})[r] === "running" && !(p.buf || {})[r])
      .forEach((r) => scroll.appendChild(pnThinking(r)));
  }

  const inputRow = document.createElement("div");
  inputRow.className = "pc-input-row";
  const ta = document.createElement("textarea");
  ta.className = "pc-input";
  ta.rows = 2;
  paintAgentChainReminder(scroll, p);
  ta.placeholder = p.busy ? "议事中…" : "说一句想改什么(再说一轮,还是这批人)";
  ta.disabled = p.busy;
  _pcInputEl = ta;
  // 从对话界面点「叫大家来议」带过来的那句话:原样贴在输入框里,由我决定发不发
  if (_panelPrefill) { ta.value = _panelPrefill; _panelPrefill = null; }
  ta.addEventListener("input", () => { _panelPrefill = null; });
  const doSend = () => {
    const v = ta.value.trim();
    if (!v || p.busy) return;
    ta.value = "";
    _chatDrafts.delete(inputKey);
    _chatFocusPending = { key: inputKey, anchor: document.activeElement };
    panelSay(v);
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
  });
  const sendBtn = document.createElement("button");
  sendBtn.className = "jc-btn pc-send";
  sendBtn.textContent = "发送";
  sendBtn.disabled = p.busy;
  sendBtn.onclick = doSend;
  inputRow.appendChild(ta);
  inputRow.appendChild(sendBtn);
  card.appendChild(inputRow);
  mountChatInput(ta, inputKey, previousActive);

  const row = document.createElement("div");
  row.className = "pc-thread-row";
  const wrapBtn = document.createElement("button");
  wrapBtn.className = "jc-btn ghost";
  wrapBtn.textContent = "让领航员收口";
  wrapBtn.title = "把几位的分歧点和可走的路汇成一段(多花 1 次调用)";
  wrapBtn.disabled = p.busy || !p.id || !events.length;
  wrapBtn.onclick = panelWrap;
  row.appendChild(wrapBtn);

  const reBtn = document.createElement("button");
  reBtn.className = "jc-btn ghost";
  reBtn.textContent = "换人重议";
  reBtn.title = "下一句重新让领航员点名(而不是沿用现在这几位)";
  reBtn.disabled = p.busy;
  reBtn.onclick = () => { if (_panel) { _panel.repick = true; toast("下一句会重新点名"); } };
  row.appendChild(reBtn);

  const newBtn = document.createElement("button");
  newBtn.className = "jc-btn ghost pc-newthread";
  newBtn.textContent = "另起一场";
  newBtn.title = "清空这一屏另开一场(旧记录仍留档,不动书里任何内容)";
  newBtn.disabled = p.busy;
  newBtn.onclick = () => {
    if (!_panel || _panel.busy) return;
    _panel = null;
    openPanel();
  };
  row.appendChild(newBtn);
  card.appendChild(row);

  scroll.scrollTop = stickBottom ? scroll.scrollHeight : prevTop;
}

// 编辑总监「一次审全书」:plan(0 调用,报账) → 逐块通读(1 次/块) → 收口(1 次)。
// 只写 .审稿留痕/(块记 + 报告),一个字都不改稿子;块记在盘上就跳过,所以中断后接着跑不白花钱。
async function fullReviewRun(btn) {
  if (!DATA || !DATA.root) return;
  const root = DATA.root;
  let plan;
  try { plan = await jreq("GET", `/api/fullreview/plan?root=${encodeURIComponent(root)}`); }
  catch (e) { return toast("算不出要花多少：" + e.message, true); }
  if (!DATA || DATA.root !== root) return;
  if (!plan || plan.ok === false) return toast((plan && plan.reason) || "算不出方案", true);
  if (!plan.块数) return toast("这本书还没有正文章可审", true);
  const cnt = (plan.体检 || {}).分类计数 || {};
  const hot = Object.entries(cnt).map(([k, v]) => `${k} ${v}`).join(" / ") || "没挑出明显硬问题";
  const msg = [
    `全书审：共 ${plan.章数} 章 / ${plan.块数} 块（已跑 ${plan.已跑块} 块）。`,
    `本次要跑 ${plan.待跑块} 块通读 + 1 次收口 = ${plan.预计调用数} 次调用，`
      + `预计 ${plan.预计分钟[0]}–${plan.预计分钟[1]} 分钟。`,
    "",
    `本地体检（0 调用）先挑出的：${hot}`,
    "",
    "只写 .审稿留痕/ 下的块记与报告，一个字都不会改你的稿子。开始吗？",
  ].join("\n");
  if (!confirm(msg)) return;
  const old = btn.textContent;
  btn.disabled = true;
  let calls = 0;
  try {
    for (const b of plan.块 || []) {
      if (b.已跑) continue;
      btn.textContent = `通读 ${b.起}-${b.止}…`;
      const r = await jreq("POST", "/api/fullreview/block", { root, lo: b.起, hi: b.止 });
      if (r && r.ok === false) throw new Error(r.reason || "这一块没跑成");
      calls += r.调用 || 0;
    }
    btn.textContent = "收口出报告…";
    const fin = await jreq("POST", "/api/fullreview/finish", { root });
    if (fin && fin.ok === false) throw new Error(fin.reason || "收口没成");
    toast(`报告写好了：${fin.报告}（这次 ${calls + 1} 次调用）`);
    await refresh();
    try { await openFile(fin.报告, true, null, null); }
    catch (e) { toast(`报告在 ${fin.报告}（编辑器打不开点开头目录是正常的）`); }
  } catch (e) {
    toast("全书审中断：" + e.message + "（已跑的块不白跑，下次接着跑）", true);
  } finally {
    btn.textContent = old;
    btn.disabled = false;
  }
}

function renderPartnerEvents(events, opts) {
  const out = [];
  let lastPanel = "";
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    // 议事例会留下的记录(2026-10-25 作者口径:要写进一对一历史,但必须标出来)
    // —— 不标的话,作者下次单独跟这个角色聊,会以为那是两人私聊说过的话。
    const panelId = (ev && ev.panel) || "";
    if (panelId && panelId !== lastPanel) out.push(pnDivider(`议事例会 · ${panelTs(panelId)}`));
    lastPanel = panelId;
    if (ev && ev.t === "tool") {
      const next = events[i + 1];
      if (next && next.t === "result") {
        out.push(pcToolResult(ev, next));
        i++;                                  // 连同 result 一起消费
        continue;
      }
      if (next && next.t === "proposal") {
        continue;                             // 压掉 tool 行;proposal 下一轮迭代自渲染
      }
      if (i === events.length - 1 && _partnerBusy) {
        out.push(pcToolActive(ev));           // 末尾 + busy → 活动行
      } else {
        out.push(pcToolDone(ev));             // 轮末残留 / [tool,error] / 非 busy → 静态 done
      }
      continue;
    }
    const el = renderPartnerEvent(ev, opts);
    if (el) out.push(el);
  }
  return out;
}

function renderPartnerEvent(ev, opts) {
  switch (ev && ev.t) {
    // 空 text 的 user 事件(开场触发的 say{text:""})不上屏:防御性处理——服务端理应
    // 不追加这条(spec §2「服务端对空 text:不追加 user 事件」),真机若未来收紧到该口径,
    // 这里的判断天然是 no-op,不会因此显示重复/多余内容。
    case "user": return ev.text && ev.text.trim() ? pcBubble("user", ev.text) : null;
    case "assistant": {
      const bubble = pcBubble("assistant", ev.text, false, (opts && opts.role) || "领航员");
      if (ev._reasoning) {   // v2:本轮思考挂在这条回复上(会话内可回看)→ 上方收折「已思考 ▸」
        const wrap = document.createElement("div");
        wrap.className = "pc-answer";
        wrap.appendChild(pcReasoning(ev._reasoning, false));
        wrap.appendChild(bubble);
        return wrap;
      }
      return bubble;
    }
    case "result": return pcResult(ev);
    // 2026-10-25 续轮标记:这一轮不是作者说的,是"落库之后接着往下走"。
    // 必须画出来 —— 否则作者会以为它自己突然开口了。
    case "resume": return pnDivider(ev.text || "已落库,接着上一轮往下走");
    case "proposal": return pcProposal(ev, opts);
    case "error": return pcError(ev);
    // 夜间补缺最终班(§2e):1:1 任务行上屏——后端事件(task_prepared/plan_updated/
    // task_status)在伙伴对话区渲染轻量一行,复用 _taskEvent 的 revision 校验(§9)。
    case "task_prepared":
    case "plan_updated":
    case "task_status": {
      if (!ev.task_id) return null;
      const rev = ev.revision || 0;
      if (_taskRev[ev.task_id] != null && rev < _taskRev[ev.task_id]) return null;  // 陈旧 revision 忽略
      _taskRev[ev.task_id] = rev;
      const zh = { ready: "待接续", running: "进行中", waiting_author: "等你拍板",
                   paused: "已暂停", blocked: "受阻", completed: "已完成", cancelled: "已取消" };
      let line = ev.t === "task_prepared"
        ? ("任务就绪" + (ev.goal ? "：" + ev.goal : ""))
        : ev.t === "plan_updated" ? (ev.text || ev.summary || "计划已更新")
        : ("任务" + (zh[ev.status] || ev.status || "") + (ev.stop_reason ? "（" + ev.stop_reason + "）" : ""));
      const row = document.createElement("div");
      row.className = "pc-row pc-system";
      const b = document.createElement("div");
      b.className = "pc-bubble";
      b.textContent = line + (rev ? ` 〔任务 v${rev}〕` : "");
      row.appendChild(b);
      return row;
    }
    default: return null;   // confirm/meta/summary:不单独起一行(confirm 的效果画在对应候选卡上)
  }
}

function pcBubble(role, text, live, speakerRole) {
  const wrap = document.createElement("div");
  wrap.className = "pc-row pc-" + role + (live ? " pc-live" : "");
  const av = document.createElement("div");
  av.className = "pc-avatar";
  if (role === "assistant") {
    const who = speakerRole || "领航员";
    av.title = who;
    av.appendChild(agentAvatar(who, "jc-ava", "jc-fallback"));
  } else av.textContent = "你";
  const b = document.createElement("div");
  b.className = "pc-bubble";
  b.textContent = text || "";
  wrap.appendChild(av);
  wrap.appendChild(b);
  return wrap;
}

// 「曌」加载图标(日月绕中心轮转):思考期通用指示,summary/纯 busy 态共用
function zhaoIconHtml() {
  return '<svg class="pc-zhao" viewBox="0 0 24 24" aria-hidden="true"><g class="pc-zhao-spin">'
    + '<circle cx="12" cy="4.6" r="3" fill="currentColor"/>'
    + '<circle cx="12" cy="19.4" r="2.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    + '</g></svg>';
}

// v2 思考层:思考流灰字折叠块。open=true → 思考中(曌+「伙伴在想…」,展开实时流);
// open=false → 已想完正在出正文(「已思考 ▸」收折,可点开回看本轮思考)。transient,轮末即清。
function pcReasoning(text, open) {
  const det = document.createElement("details");
  det.className = "pc-reasoning";
  if (open) det.open = true;
  const sum = document.createElement("summary");
  sum.innerHTML = zhaoIconHtml() + "<span>" + (open ? "伙伴在想…" : "已思考") + "</span>";
  det.appendChild(sum);
  const body = document.createElement("div");
  body.className = "pc-reasoning-body";
  body.textContent = text || "";
  det.appendChild(body);
  return det;
}

function pcResult(ev) {
  const det = document.createElement("details");
  det.className = "pc-result" + (ev.error ? " err" : "");
  const sum = document.createElement("summary");
  sum.textContent = ev.error ? "查询没成功" : "查到的内容";
  det.appendChild(sum);
  const body = document.createElement("div");
  body.className = "pc-result-text";
  body.textContent = ev.error || ev.text || "";
  det.appendChild(body);
  return det;
}

// 工具名 → 三态文案(spec §4.3)。〈basename〉取 params.路径 末段文件名。
// 每个工具每条路径都有定义,无 undefined:提设定成功走 proposal(tool 行被压)、
// 参数错走 result(error) 用 .err;done 仅极端边角兜底。
function partnerToolVerb(name, params) {
  const base = (params && params.路径) ? String(params.路径).split("/").pop() : "";
  switch (name) {
    case "看地基": return { active: "正在看地基…", done: "看了地基", err: "查地基没成功" };
    case "读文件": return { active: `正在读${base}…`, done: `读了${base}`, err: `读${base}没成功` };
    case "提设定": return { active: "正在拟一条设定…", done: "拟了一条设定", err: "拟设定没成功" };
    default: {
      const n = name || "工具";
      return { active: `正在用${n}…`, done: `用了${n}`, err: `用${n}没成功` };
    }
  }
}

// 活动行:旋转指示器 + 「正在看地基…」(spec §4.2 dangling+busy)
function pcToolActive(ev) {
  const row = document.createElement("div");
  row.className = "pc-tool-active";
  const spin = document.createElement("span");
  spin.className = "pc-spinner";
  row.appendChild(spin);
  const label = document.createElement("span");
  label.textContent = partnerToolVerb(ev.name, ev.params).active;
  row.appendChild(label);
  return row;
}

// 静态 done 行:轮末残留 / [tool,error] 兜底 / 非 busy(spec §4.2 后两档)
function pcToolDone(ev) {
  const row = document.createElement("div");
  row.className = "pc-tool-done";
  row.textContent = partnerToolVerb(ev.name, ev.params).done;
  return row;
}

// 合并折叠行:tool + 其 result 配一行(spec §4.2)。summary 用配对 tool 的 done/err 文案,
// 沿用 pcResult 的 .pc-result 折叠骨架 + .err 样式。
function pcToolResult(toolEv, resultEv) {
  const verb = partnerToolVerb(toolEv.name, toolEv.params);
  const det = document.createElement("details");
  det.className = "pc-result" + (resultEv.error ? " err" : "");
  const sum = document.createElement("summary");
  sum.textContent = resultEv.error ? verb.err : verb.done;
  det.appendChild(sum);
  const body = document.createElement("div");
  body.className = "pc-result-text";
  body.textContent = resultEv.error || resultEv.text || "";
  det.appendChild(body);
  return det;
}

function pcError(ev) {
  const row = document.createElement("div");
  row.className = "pc-error";
  row.textContent = (ev.text || "出错了") + codeHint(ev.code);
  return row;
}

// proposal 事件(候选卡,spec §6.1):虚线边+「候选」角标是挡 AI 文字混进书的唯一结构性保证。
// before 非空 → 渲染「现在是:〈旧值〉」+ 确认键文案改〔替换〕(二期「见旧值才换值」的知情前提)。
function pcProposal(ev, opts) {
  const o = opts || {};
  const card = document.createElement("div");
  card.className = "pc-proposal";
  const badge = document.createElement("span");
  badge.className = "pc-proposal-badge";
  badge.textContent = "候选";
  card.appendChild(badge);

  const slotLine = document.createElement("div");
  slotLine.className = "pc-proposal-slot";
  slotLine.textContent = humanizeSlot(ev.slot);
  card.appendChild(slotLine);

  if (ev.before) {
    const beforeLine = document.createElement("div");
    beforeLine.className = "pc-proposal-before";
    beforeLine.textContent = `现在是:${ev.before}`;
    card.appendChild(beforeLine);
  }

  const content = document.createElement("div");
  content.className = "pc-proposal-content";
  content.textContent = ev.content || "";
  card.appendChild(content);

  // 2026-10-23:模型常把「后半截要跟你说的的话」塞进内容里。后端会把它剥出来放在 note
  // (剥掉的那部分**不会**落进设定),这里显示出来,免得作者以为自己的话丢了。
  if (ev.note) {
    const noteLine = document.createElement("div");
    noteLine.className = "pc-proposal-note";
    noteLine.textContent = ev.note;
    card.appendChild(noteLine);
  }

  const landed = findConfirmLanded(ev.id);
  if (landed) {
    const done = document.createElement("div");
    done.className = "pc-proposal-done";
    done.textContent = `✓ 已落库 → ${landed}`;
    card.appendChild(done);
    // 2026-11-12:这行小字 CSS 已隐藏（见 style.css）。路径改挂 title，信息不丢；
    // 更关键的是它不再随"整卡复制"进输入框——后端 strip_ui_marks() 另有一道。
    card.title = "已落库 → " + landed;
    return card;
  }

  // FB-B 模型A:这一格已被同轮兄弟候选落盘 → 本卡变灰、不留死键(点了必撞 stale「这一格刚改过」)
  if (slotTakenBySibling(ev)) {
    card.classList.add("pc-proposal-moot");
    const moot = document.createElement("div");
    moot.className = "pc-proposal-mootnote";
    moot.textContent = "这一格已选了其他方向(想换就跟我说)";
    card.appendChild(moot);
    return card;
  }

  const actions = document.createElement("div");
  actions.className = "pc-proposal-actions";
  const confirmBtn = document.createElement("button");
  confirmBtn.className = "jc-btn";
  confirmBtn.textContent = "落库";
  if (_weaving) {   // 织章进行中:确认键置灰(spec §2,锁裁量红线——confirm 走真书写锁,与织章互斥)
    confirmBtn.disabled = true;
    confirmBtn.title = "织章进行中,跑完即可拍板";
  }
  confirmBtn.onclick = () => partnerConfirm(ev.id, confirmBtn, o);
  const editBtn = document.createElement("button");
  editBtn.className = "jc-btn ghost";
  editBtn.textContent = "改一改";
  editBtn.onclick = () => { if (o.onEdit) o.onEdit(ev.content); else fillPartnerInput(ev.content); };
  actions.appendChild(confirmBtn);
  actions.appendChild(editBtn);
  if (o.allowRetarget) {   // 审查跟进项①：内容不动，换一个落点重挂（不花钱）
    const swapBtn = document.createElement("button");
    swapBtn.className = "jc-btn ghost";
    swapBtn.textContent = "换到…";
    swapBtn.title = "内容原样搬去另一个格子，不重新生成";
    swapBtn.onclick = () => openSlotPickerForRetarget(ev, o);
    actions.appendChild(swapBtn);
  }
  card.appendChild(actions);
  return card;
}

// 落点(容器rel#键)→ 人话,如「外置大脑/世界观/金手指.md#代价」→「世界观 · 金手指 · 代价」。
// 纯字符串启发式(前端没有 slots.py 的槽位扫描器可查),解析不出来就原样兜底显示。
function humanizeSlot(slotId) {
  const s = String(slotId || "");
  const hashIdx = s.lastIndexOf("#");
  const containerRel = hashIdx >= 0 ? s.slice(0, hashIdx) : s;
  const key = hashIdx >= 0 ? s.slice(hashIdx + 1) : "";
  const filename = containerRel.split("/").pop() || "";
  const stem = filename.replace(/\.md$/i, "");
  let stage = "", showStem = false;
  // 设定集分区：外置大脑/设定集/10-世界观/xxx.md、20-人物/xxx.md（旧书可能是 世界观/ 人物/，也认）
  if (/世界观\//.test(containerRel)) { stage = "世界观"; showStem = true; }
  else if (/人物\//.test(containerRel)) { stage = "人物"; showStem = true; }
  else if (/战斗\//.test(containerRel)) { stage = "战斗"; showStem = true; }
  else if (/知识\//.test(containerRel)) { stage = "知识"; showStem = true; }
  else if (/立项卡$|00-立项$/.test(stem)) stage = "立项";
  else if (/卡章纲$/.test(stem)) stage = "卡章纲";
  // 2026-10-25:「改指纹」卡的落点是 写作指纹.md#某小节;分析类产物(语料分析/去AI味口径/情绪曲线)
  // 也走同一条候选卡通道。不给它们一句人话,卡片上就只剩一个「句式偏好」看不出是往哪写。
  else if (/写作指纹$/.test(stem)) stage = "写作指纹";
  else if (/分析\//.test(containerRel)) { stage = "分析"; showStem = true; }
  const segs = [];
  if (stage) segs.push(stage);
  if (showStem) {
    const shortStem = stem.split(/[·・•]/).pop();   // 人物文件「主角·林潜」只取名字
    if (shortStem) segs.push(shortStem);
  }
  if (key === "@name") segs.push("起名字");
  else if (key === "@body") segs.push("正文");
  else if (key) segs.push(key);
  return segs.join(" · ") || s;
}

function findConfirmLanded(id) {
  const events = (PARTNER && PARTNER.events) || [];
  const hit = events.find((e) => e && e.t === "confirm" && e.id === id);
  return hit ? (hit.landed || "已落库") : null;
}

// FB-B 模型A:这一格是否已被【同轮兄弟候选】落盘(同槽备选选了一个,其余变灰,不留点了必撞 stale 的死键)
// 「追加型」候选卡:落盘是往文件里**加**一段,同一落点可以落很多次,不该互斥。
//   · 人格增补 —— evolve.confirm 追加进 agents/<角色>.md 的个人增补区;
//   · 写作指纹 —— journey._land_fingerprint 追加进 写作指纹.md 的某一小节(2026-10-25)。
const APPEND_KINDS = new Set(["人格增补", "写作指纹"]);
function slotTakenBySibling(ev) {
  // 【只适用于替换型落点】外置大脑的一格(如 立项卡#题材)同时只能有一个答案,所以
  // 兄弟卡落过库之后,这一格的其余候选就该灰掉。
  //
  // 「追加型」不是这种落点:本来就允许落多次(学到第二条改法、给同一小节再补一条规则都很正常)。
  // 套用替换型的「一格只能选一个」语义,会让同一小节的第二张候选卡连按钮都不给。
  if (ev && APPEND_KINDS.has(ev.kind)) return false;
  const events = (PARTNER && PARTNER.events) || [];
  for (const e of events) {
    if (e && e.t === "confirm" && e.id !== ev.id) {
      const prop = events.find((x) => x && x.t === "proposal" && x.id === e.id);
      if (prop && !APPEND_KINDS.has(prop.kind) && prop.slot === ev.slot) return true;
    }
  }
  return false;
}

// 还有没有【可点】的候选卡(自己没确认、且这一格没被兄弟占)——落盘后自动接一轮的门(FB-B #3):
// 同槽挑一个 → 其余变灰、无 pending → 前进;异槽逐个决 → 还有卡没决就不前进,等决完。
// 只看【当前轮】——最后一条 user 之后的提案。用户实报 2026-08-23:历史轮里被作者手动打字
// 越过、从没决过的旧提案会永远留在 events 里,这道门于是恒为 true,「落盘后自动接一轮」
// 从此一次都不再触发,领航员卡住不往下引。
function hasActionableProposal() {
  const events = (PARTNER && PARTNER.events) || [];
  let lastUser = -1;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i] && events[i].t === "user") { lastUser = i; break; }
  }
  const round = lastUser >= 0 ? events.slice(lastUser + 1) : events;
  return round.some((ev) => ev && ev.t === "proposal"
    && !findConfirmLanded(ev.id) && !slotTakenBySibling(ev));
}

function fillPartnerInput(text) {
  if (!_pcInputEl) return;
  _pcInputEl.value = text || "";
  _pcInputEl.dispatchEvent(new Event("input"));
  _pcInputEl.focus();
}

// 2026-10-25 需求确认:「落库的动作能否再次触发角色继续思考?让它看看还有没有未完成的计划轮次」
// 做法:落库成功后,如果它上一轮还留了「下一步」或「商讨计划」,就自动接着跑一轮
// (resume=true:不伪造作者的话,只在提示词里交代"那条已经落库了")。
// 三个克制:
//   ① 只在**一对一**里自动续 —— 议事厅是 N 倍钱,不替作者做主;
//   ② 上一轮**既没计划也没下一步**就不续(没有可接着做的事,续了就是白花一次调用);
//   ③ 它还在跑就不插队。
function _hasPendingNext() {
  const evs = (_agentCenter && _agentCenter.events) || [];
  for (let i = evs.length - 1; i >= 0; i--) {
    const ev = evs[i];
    if (ev && ev.t === "assistant" && (ev.text || "").trim()) {
      return /下一步\s*[:：]|商讨计划/.test(ev.text);
    }
  }
  return false;
}

async function agentResumeIfPending(decisionId) {
  const ac = _agentCenter;
  if (!ac || ac.busy || !ac.role) return;
  if (_panel) return;              // 议事厅里不自动续
  if (!_hasPendingNext()) return;
  const proposal = ac.events.find(ev => ev.t === "proposal" && ev.id === decisionId);
  if (!proposal || !proposal.task_id) return;
  await agentCenterSay("", { resume: true, task_id: proposal.task_id,
    conversation_id: proposal.conversation_id || _AGENT_CONV,
    decision_event_id: decisionId });
}

// 拍板落盘 = 新的一轮(非流式,走真书写锁):POST /api/partner/confirm {root, id} → 结果记 confirm(本地追加,
// 供 findConfirmLanded 渲染「已落盘」态)→ 刷新(外置大脑侧栏跟着长,伙伴下一轮看得见,同 postJourneyAnswer 先例)。
function beginAgentChainRound(owner, text) {
  if (owner && String(text || "").trim() && !owner._chainReminder?.pending) owner._chainReminder = null;
}
function collectAgentChainReminder(owner, slot) {
  if (!owner) return;
  const rel = String(slot || "").split("#")[0];
  const role = rel.startsWith("外置大脑/设定集/") ? "大纲师"
    : /^外置大脑\/(?:卡章纲|卷纲|全书大纲)\.md$/.test(rel) ? "设定师" : "";
  if (!role) return;
  if (!owner._chainReminder) owner._chainReminder = { roles: new Set(), pending: true };
  owner._chainReminder.roles.add(role);
  owner._chainReminder.pending = true;
}
function settleAgentChainReminder(owner) {
  if (!owner || !owner._chainReminder) return;
  owner._chainReminder.pending = false;
}
function paintAgentChainReminder(scroll, owner) {
  const reminder = owner && owner._chainReminder;
  if (!reminder || reminder.pending || !reminder.roles.size) return;
  const note = document.createElement("div");
  note.className = "pc-tool";
  note.textContent = "这轮确认的内容已落定，记得找" + [...reminder.roles].join("、") + "检查设定与大纲的一致性。";
  scroll.appendChild(note);
}
async function partnerConfirm(id, btnEl, opts) {
  const noAdvance = !!(opts && opts.noAdvance);   // 作者智库对话里确认：只落库，不替书房伙伴接一轮
  const root = DATA && DATA.root;
  if (!root) return;
  const owner = _panel || (noAdvance ? _agentCenter : PARTNER);
  const proposal = ((owner && owner.events) || []).find((ev) => ev.t === "proposal" && ev.id === id);
  if (btnEl) btnEl.disabled = true;
  try {
    const out = await jreq("POST", "/api/partner/confirm", { root, id });
    if (!DATA || DATA.root !== root) return;   // 已换书:迟到的响应绝不上屏
    if (out && out.error) {
      toast(out.error, true);   // 过期/这一格刚改过(快照失配)/落点冲突——不崩,提案仍可重试或重新问
      if (btnEl) btnEl.disabled = false;
      return;
    }
    if (!PARTNER) PARTNER = { events: [] };
    PARTNER.events.push({ t: "confirm", id, landed: out.landed });
    if (out.state) JOURNEY = out.state;
    collectAgentChainReminder(owner, out.landed || (proposal && proposal.slot));
    if (btnEl && btnEl.parentElement) {
      const done = document.createElement("div");
      done.className = "pc-proposal-done";
      done.textContent = `✓ 已落库 → ${out.landed}`;
      btnEl.parentElement.replaceWith(done);
    }
    paintJourney();
    if (navMode() === "center") paintNavCenterChrome();
    refresh();
    if (noAdvance) {
      await agentResumeIfPending(id);
      if (!owner || !owner.busy) settleAgentChainReminder(owner);
      paintJourney();
      return;
    }
    // bug4下一步:落盘后自动接一轮,领航员按固定顺序引下一格(空 text→后端见末事件=confirm 放行,
    // 不落假 user 气泡)。不 await:确认路径立即返回,引导轮自行流式上屏。
    // FB-B #3:仅当没有还能点的候选卡时才前进——同槽挑完(兄弟变灰)就走,异槽还有卡没决就等决完。
    // 同一实报的另一半:上一轮的流刚收尾时 _partnerBusy 可能还没落回 false,这一下就被吞了。
    // 试一次,不成再退让两拍——只是等状态 settle,拿不到就安静放弃(作者照样能手打)。
    const tryAdvance = () => {
      if (_partnerBusy || hasActionableProposal()) return false;
      partnerSay("");
      return true;
    };
    if (!tryAdvance()) setTimeout(() => {
      if (!DATA || DATA.root !== root) return;
      if (!tryAdvance()) setTimeout(() => {
        if (!DATA || DATA.root !== root) return;
        if (!tryAdvance() && !_partnerBusy) { settleAgentChainReminder(owner); paintJourney(); }
      }, 300);
    }, 150);
  } catch (e) {
    if (!DATA || DATA.root !== root) return;
    toast(e.message, true);
    if (btnEl) btnEl.disabled = false;
  }
}

// 归档当前对话、另起一段(不动书内容,只挪 .伙伴对话/ 下的 jsonl 文件)。
async function partnerNewThread() {
  const root = DATA && DATA.root;
  if (!root) return;
  if (_partnerBusy) { toast("伙伴还在回复,请稍候", true); return; }   // 回复中归档:旧轮 reader 循环没被中止,尾巴事件会串进刚重置的新会话
  if (!window.confirm("归档当前伙伴对话、另起一段?(书内容不受影响,旧对话仍留档可查)")) return;
  try {
    const out = await jreq("POST", "/api/partner/new", { root });
    if (!DATA || DATA.root !== root) return;
    if (out && out.error) { toast(out.error, true); return; }
    _partnerGen++;   // 世代号前移:哪怕 busy 判断被绕过,任何仍在飞的旧世代事件也一律作废(双保险,见 partnerSay)
    _partnerStopped = false;   // 另起清「已停止」标记(镜像 _partnerGen 生命周期,免漏到新会话)
    PARTNER = { events: [] };
    _partnerAutoOpened.delete(root);
    _partnerRoot = null;   // 强制 loadPartnerPanel() 重新走一遍(history 应已归档为空,可能再触发开场)
    loadPartnerPanel();
  } catch (e) {
    if (!DATA || DATA.root !== root) return;
    // 停之后 worker 尚在收尾窗口:归档撞轮锁 409 partner_busy(blocker 门禁)→ 专属文案
    const msg = /partner_busy|还在应答|收尾/.test(e.message || "") ? "伙伴还在收尾,稍等一下再另起" : e.message;
    toast(msg, true);
  }
}

async function startDiagnose() {
  const root = DATA && DATA.root;
  if (!root) return;
  toast("正在读你的正文提炼设定,稍候…");
  let out;
  try {
    out = await jreq("POST", "/api/diagnose/scan", { root });
  } catch (e) { return toast(e.message, true); }
  if (!DATA || DATA.root !== root) return;
  const cand = out.candidates || {};
  if (!cand.世界观 && !cand.人物卡 && !cand.卡章纲) {
    return toast("正文里没提炼出可用设定,直接在伙伴面板答几题吧");
  }
  showDiagnoseConfirm(cand);
}

function showDiagnoseConfirm(cand) {
  const box = $("diag-body"); box.innerHTML = "";
  const SEG = [["世界观", "世界观"], ["人物卡", "人物卡"], ["卡章纲", "卡章纲"]];
  SEG.forEach(([key, label]) => {
    const val = cand[key] || "";
    const wrap = document.createElement("div"); wrap.className = "diag-seg";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!val.trim();
    cb.dataset.seg = key; cb.disabled = !val.trim();
    const h = document.createElement("label"); h.className = "diag-seg-head";
    h.appendChild(cb); h.appendChild(document.createTextNode(" " + label + (val.trim() ? "" : "(正文没提炼到)")));
    const ta = document.createElement("textarea"); ta.className = "diag-seg-text"; ta.dataset.seg = key;
    ta.value = val; ta.rows = Math.min(10, (val.split("\n").length || 1) + 1);
    wrap.appendChild(h); wrap.appendChild(ta); box.appendChild(wrap);
  });
  const pro = $("diag-protagonist"); if (pro) pro.value = "";
  $("diagnose-overlay").classList.remove("hidden");
}

async function commitDiagnose() {
  const root = DATA && DATA.root;
  if (!root) return;
  const picks = { protagonist: ($("diag-protagonist") && $("diag-protagonist").value.trim()) || "" };
  document.querySelectorAll("#diag-body .diag-seg").forEach((seg) => {
    const cb = seg.querySelector("input[type=checkbox]");
    const ta = seg.querySelector("textarea");
    picks[cb.dataset.seg] = cb.checked ? ta.value : "";
  });
  let d;
  try { d = await jreq("POST", "/api/diagnose/commit", { root, picks }); }
  catch (e) { return toast(e.message, true); }
  $("diagnose-overlay").classList.add("hidden");
  if (!DATA || DATA.root !== root) return;
  toast("已预填 → 缺的领航员会接着问");
  enterProject(d);   // 刷新 project_state:签名失配、领航员只问真缺、门禁可能已开
}

// 角色动态渲染(2026-10-31 起):作者智库 + 写作引擎名单以 AUTHOR_AGENTS/ENGINE_AGENTS 为准,不再依赖后端 DATA.agents
const AGENT_ORDER = [
  // 领航员
  "领航员",
  // 作者智库（2026-11-12 新增人物侧写师：8 人。知识顾问与语料分析师仍工具化不计人）
  "设定师", "大纲师", "战斗设计师", "情绪节奏设计师", "人物侧写师", "编辑总监", "批评家", "市场分析师",
  // 写作引擎
  "章节细纲师", "章节设定师", "章节分镜师", "写手", "责任编辑", "润色师",
];
// slug 用于 /agents/ 路径加载;头像实际在 agents/ 目录,同名.png 作者生成
const AGENTS_META = {
  "领航员":       { slug: "navigator",       tag: "带路 · 分派", reads: "全书大纲 · 地基状态",                                              produces: "路由建议",                                                                                          desc: "书房的入口。带你把地基打完，之后按你的意图把活分给对的人；自己不留记忆，每次重新判断。" },
  "设定师":       { slug: "loremaster",      tag: "定世界 · 立规矩", reads: "立项卡 · 世界观 · 人物 · 题材方法论",                                    produces: "设定集 · 世界规则与人物锚点",                                                                                      desc: "全书设定的源头。立世界规则、钉死硬约束、维护设定自洽；写作引擎每一步都拿它当准绳。" },
  "大纲师":       { slug: "outliner",        tag: "定卷 · 定章 · 布线", reads: "设定集 · 目标读者 · 题材方法论",                                  produces: "全书大纲 / 卷纲 / 卡章纲",                                                                            desc: "全书骨架的总设计师。定卷、定章、布爆点、埋长线，产出三级结构；单章怎么填不是它的活。" },
  "知识顾问":     { slug: "knowledge",       tag: "查证 · 供料", reads: "设定集 · 书内全文 · 联网 · 附件",                                          produces: "知识提案（你点头才入设定集）",                                                                                     desc: "给设定集供料的查证者。书内/联网/附件三路取证，出提案，绝不自动写进知识库。" },
  "语料分析师":   { slug: "corpus",          tag: "量文风", reads: "正文 · 写作指纹 · 去AI味口径",                                        produces: "词频/句式/节奏报告 · 去AI味口径",                                                                    desc: "给文风上尺子的人。本地统计词频、句式、节奏（不花 token），产出数据报告；还能按你的话把偏好写进写作指纹、把口径写进去AI味豁免清单，不碰正文。" },
  "战斗设计师":   { slug: "battle",          tag: "战力立法", reads: "设定集 · 战斗相关设定",                                             produces: "设定集 · 战斗体系",                                                                                     desc: "战力的立法者。定等级、定招式、定克制与地形，让每场打斗有账可查，绝不崩战力。" },
  "情绪节奏设计师":{slug: "emotion",         tag: "编排节拍", reads: "卡章纲 · 卷纲 · 各章正文",                                              produces: "情绪曲线（并入细纲注释）",                                                                                     desc: "节奏的编排者。设计章内与卷内的情绪曲线，把爽点、憋屈、爆发排进节拍，读者放不下。" },
  "人物侧写师":   { slug: "character-profiler", tag: "细化人物", reads: "人物卡 · 世界观 · 情绪曲线 · 设定集",                                  produces: "细化的人物卡 · 人物与设定集互动细节",                                                                        desc: "和你一起细化角色设计与人物卡。结合世界观、情绪节奏等素材，补足人物动机、关系与人物和设定集之间的互动细节。" },
  "编辑总监":     { slug: "chief-editor",   tag: "全书守门", reads: "全书大纲 · 卷纲 · 全部章节 · 设定集",                                            produces: "全局质量报告",                                                                                     desc: "全书的质量守门人。跨章审结构、审一致性、审伏笔回收，只看全局；单章硬伤归责任编辑。" },
  "批评家":       { slug: "critic",          tag: "只挑不改", reads: "本章正文 · 设定集 · 卡章纲",                                                 produces: "问题清单",                                                                                         desc: "专挑硬伤的人。查 OOC、设定漂移、因果断裂，只出清单和确切位置，一个字都不改。" },
  "市场分析师":   { slug: "market",          tag: "市场判断", reads: "设定集 · 全书大纲 · 卡章纲 · 违禁词",                                   produces: "市场分析报告",                                                                                     desc: "回答「这本书在市场上怎么样」。卖点四件套逐项过、题材趋势、上架与推荐时机、违禁比对；判断必给依据，决定权在你。" },
  "章节细纲师":   { slug: "ch-outline",     tag: "第1棒 · 填情节", reads: "卡章纲 · 情绪曲线 · 上一章",                                                        produces: "本章细纲",                                                                                       desc: "流水线第一棒。拿卡章纲和情绪曲线，把一章填成有场次、有人物反应、有章末钩的完整细纲。" },
  "章节设定师":   { slug: "ch-setting",     tag: "第2棒 · 校设定", reads: "设定集 · 本章细纲",                                                            produces: "本章细纲(含设定)",                                                                                   desc: "流水线第二棒。拿设定集逐条对照细纲，补细节、纠漂移，保证要写的东西不越界。" },
  "章节分镜师":   { slug: "ch-storyboard",  tag: "第3棒 · 定镜头", reads: "本章细纲 · 设定集",                                                     produces: "本章细纲(含分镜)",                                                                               desc: "流水线第三棒。在细纲里标注镜头、视角、快慢切，用结构化注释写进细纲，给写手指方向。" },
  "写手":         { slug: "writer",         tag: "第4棒 · 落字", reads: "本章细纲 · 写作指纹 · 人物",                                                   produces: "本章初稿",                                                                                       desc: "流水线第四棒，真正落字的人。照细纲写、按你的写作指纹写，越写越像你。" },
  "责任编辑":     { slug: "editor",          tag: "第5棒 · 挑瑕疵", reads: "本章初稿 · 设定集 · 状态账本",                                                        produces: "本章改稿",                                                                                       desc: "流水线第五棒。盘爽点、钩子、OOC、设定漂移，当场改掉；文风不动，那是润色师的事。" },
  "润色师":       { slug: "polisher",        tag: "第6棒 · 去AI味", reads: "本章改稿 · 写作指纹",                                                       produces: "本章终稿",                                                                                       desc: "流水线最后一棒。擦掉 AI 腔和套话，留住你的口头禅和怪句，读起来像真人。" },
};
// 头像:作者生成的同名 .png, /agents/ 路径下
// 换过头像就改这个版本号,浏览器才会重新拉图(否则一直用缓存里的旧图)
const AVATAR_VER = "20261116b";
const AGENT_AVATAR_FALLBACK = { "人物侧写师": "侧" };
function agentAvatar(name, imgCls, fbCls) {
  const img = document.createElement("img");
  img.className = imgCls;
  img.src = `/agents/${encodeURIComponent(name)}.png?v=${AVATAR_VER}`;
  img.alt = name; img.loading = "lazy";
  img.onerror = () => {
    const fb = document.createElement("span");
    fb.className = fbCls;
    fb.textContent = AGENT_AVATAR_FALLBACK[name] || name.slice(0, 1);
    fb.title = `${name}（临时占位，正式头像待本地生成）`;
    img.replaceWith(fb);
  };
  return img;
}
// 书房伙伴(领航员) + 作者智库 8 + 写作引擎 6
// 2026-10-31（J2）：语料分析师与知识顾问已**工具化**，不再进这张名册。
// 2026-11-06（作者批准）：**市场分析师入册**，补市场判断/上架时机职责。
// 2026-11-12（作者批准）：**人物侧写师入册**，智库 7→8，负责细化人物卡及人物与设定集的互动细节。
// 知识顾问/语料分析师仍留在 AGENTS_META 里 —— 历史对话要按名字打开，删了旧会话就断。
const AUTHOR_AGENTS  = ["设定师", "大纲师", "战斗设计师", "情绪节奏设计师", "人物侧写师", "编辑总监", "批评家", "市场分析师"];
const ENGINE_AGENTS = ["章节细纲师", "章节设定师", "章节分镜师", "写手", "责任编辑", "润色师"];
// ── 侧栏角色指示灯(2026-10-25 需求确认)────────────────────────────────────────
// 三档,含义不同就不该长得一样(2026-10-25 二次调整:黄呼吸 / 红慢闪):
//   .busy      → 这个角色正在思考(后端进程内登记;重启即清零,那也是事实)        黄灯慢呼吸
//   .due       → 有一张候选卡等你拍板(最强的一种"欠账")                        红灯慢闪
//   .due-soft  → 它最后一句是它说的,你还没回(普通欠账)                        灰点静止
// 正在屏幕上打开的那个角色不点灯 —— 你看得见,不需要再提醒一次。
let _lampBusy = false;
let _lampData = {};

async function refreshAgentLamps() {
  if (!DATA || !DATA.root || _lampBusy) return;
  _lampBusy = true;
  const root = DATA.root;
  try {
    const d = await jreq("GET", `/api/agents/status?root=${encodeURIComponent(root)}`);
    if (!DATA || DATA.root !== root) return;
    _lampData = (d && d["角色"]) || {};
    paintAgentLamps();
  } catch (e) { /* 读不到就不点灯:指示灯不该拖累别的事 */ }
  finally { _lampBusy = false; }
}

// 「多久以前」的人话。灯现在只报近况(默认 30 分钟窗口),那就得把"多久"写出来。
function _agoText(min) {
  const m = Number(min);
  if (!isFinite(m) || m < 0) return "";
  if (m < 1) return "刚刚";
  if (m < 60) return `${Math.round(m)} 分钟前`;
  const h = m / 60;
  return h < 24 ? `${Math.round(h)} 小时前` : `${Math.round(h / 24)} 天前`;
}

function paintAgentLamps() {
  // 2026-10-25 二次调整:不再排除「正在屏幕上看的那个角色」。
  // 原来那样做的后果很实在 —— 主视图默认开的就是领航员,于是它那颗灯永远不亮,
  // 看起来像"领航员没接上灯"。灯只反映状态,与你在看谁无关。
  document.querySelectorAll(".agent-card[data-role]").forEach((li) => {
    const role = li.dataset.role;
    const lamp = li.querySelector(".agent-lamp");
    if (!lamp) return;
    const st = _lampData[role] || {};
    const busy = !!st["在思考"];
    const card = !!st["待拍板"];                            // 候选卡:最该看的一档
    const soft = !!st["有欠账"] && !card;                    // 普通欠账:它在等你回话
    lamp.classList.toggle("hidden", !busy && !card && !soft);
    lamp.classList.toggle("busy", busy);
    lamp.classList.toggle("due", !busy && card);
    lamp.classList.toggle("due-soft", !busy && !card && soft);
    const ago = _agoText(st["距今分钟"]);
    const tail = ago ? `（${ago}）` : "";
    lamp.title = busy ? `${role}正在思考…`
      : card ? `${role}有一张候选卡等你拍板${tail}`
      : soft ? `${role}还在等你回话${tail}`
             : "";
  });
}

function fillAgents() {
  // 书房伙伴栏：只有领航员（它不属幕后，独立一节，侧栏里也找得到）
  const partnerUl = $("partner-agents");
  if (partnerUl) {
    partnerUl.innerHTML = "";
    partnerUl.appendChild(_agentLi("领航员", "navigator"));
  }
  // 作者智库栏
  const authorUl = $("author-agents");
  authorUl.innerHTML = "";
 const _cnt = $("author-count"); if (_cnt) _cnt.textContent = AUTHOR_AGENTS.length + " 人";
  AUTHOR_AGENTS.forEach((name) => {
    authorUl.appendChild(_agentLi(name, "panel"));
  });
  // 写作引擎栏
  const engineUl = $("engine-agents");
  engineUl.innerHTML = "";
  ENGINE_AGENTS.forEach((name) => {
    engineUl.appendChild(_agentLi(name, "panel"));
  });
  paintAgentLamps();     // 重画完卡片把灯补上(否则每次刷新都会先闪一下没灯)
}
// 通用单个角色卡片 li
function _agentLi(name, context) {
  const meta = AGENTS_META[name] || {};
  const li = document.createElement("li");
  li.className = "agent-card";
  li.dataset.role = name;      // 侧边栏选中态靠它认人(见 markRoleSelected)
  li.appendChild(agentAvatar(name, "agent-ava", "agent-fallback"));
  const info = document.createElement("div"); info.className = "agent-info";
  const nm = document.createElement("div"); nm.className = "agent-name"; nm.textContent = name;
  if (meta.tag) { const t = document.createElement("span"); t.className = "agent-tag"; t.textContent = meta.tag; nm.appendChild(t); }
  const ds = document.createElement("div"); ds.className = "agent-desc"; ds.textContent = meta.desc || "";
  info.appendChild(nm); info.appendChild(ds);
  li.appendChild(info);
  // 右上角指示灯(2026-10-25 需求确认):在思考 = 闪;有欠账 = 常亮。
  // 两盏灯的意思见后端 agent_status.py 的模块头 —— 都是事实,不是猜。
  const lamp = document.createElement("span");
  lamp.className = "agent-lamp hidden";
  li.appendChild(lamp);
  if (context === "panel") {
    // 写作引擎角色:单击打开全流程浮窗,高亮当前角色
    if (ENGINE_AGENTS.includes(name)) {
      li.onclick = () => openFlow(name);
    } else {
      // 作者智库角色:点开主视图对话
      li.onclick = () => openAgentChat(name);
    }
  } else {
    // 领航员：打开居中的书房伙伴界面（作者 2026-10-20 需求 B：不再用浮窗）
    li.onclick = () => openAgentChat("领航员");
  }
  return li;
}
// 领航员的「该找谁」面板（作者 2026-10-20 裁定 2：只提示角色，不自动开会话）
let _navSuggestEl = null;
function closeNavigatorSuggest() {
  if (_navSuggestEl) _navSuggestEl.remove();
  _navSuggestEl = null;
}

function openNavigatorSuggest() {
  closeNavigatorSuggest();
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.id = "nav-suggest-overlay";
  ov.innerHTML = `
    <div class="run-card" style="width:560px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <span class="ns-ava" style="width:36px;height:36px;flex:none"></span>
        <div>
          <div style="font-weight:600">领航员 · 该找谁</div>
          <div class="hint" style="margin:2px 0 0;font-size:var(--fs-xs)">
            说一句你想干什么，我告诉你该找哪个角色（可能要几个配合）。我不会替你打开它们的会话。
          </div>
        </div>
        <button class="ghost ns-close" style="margin-left:auto">关闭</button>
      </div>
      <div style="display:flex;gap:8px">
        <input class="ns-input" type="text" placeholder="例：这章有没有 OOC / 帮我设计战力体系 / 写第三章"
               style="flex:1;padding:8px;border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface);color:var(--text)" />
        <button class="primary ns-go">问</button>
      </div>
      <div class="ns-result" style="margin-top:12px;min-height:60px"></div>
    </div>`;
  document.body.appendChild(ov);
  _navSuggestEl = ov;

  const ava = ov.querySelector(".ns-ava");
  if (ava) ava.replaceWith(agentAvatar("领航员", "agent-ava", "agent-fallback"));

  const input = ov.querySelector(".ns-input");
  const result = ov.querySelector(".ns-result");
  const close = () => closeNavigatorSuggest();
  ov.querySelector(".ns-close").onclick = close;
  ov.onclick = (e) => { if (e.target === ov) close(); };

  const ask = async () => {
    const text = (input.value || "").trim();
    if (!text) return;
    result.innerHTML = `<div class="hint">正在判断…</div>`;
    try {
      const r = await fetch("/api/navigator/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, root: PROJECT_ROOT }),
      });
      const d = await r.json();
      const list = d.candidates || ["领航员"];
      // 只给「找谁」的建议；点角色名才开它的会话
      const chips = list.map((n) => {
        const meta = AGENTS_META[n] || {};
        return `<button class="ns-chip" data-role="${escHtml(n)}"
          style="display:inline-flex;align-items:center;gap:6px;margin:4px 6px 4px 0;padding:6px 10px;
                 border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--text);cursor:pointer">
          <b style="font-weight:600">${escHtml(n)}</b>
          <span class="agent-tag">${escHtml(meta.tag || "")}</span>
        </button>`;
      }).join("");
      result.innerHTML = `
        <div class="hint" style="margin-bottom:6px">建议找：</div>
        <div>${chips}</div>
        <div class="hint" style="margin-top:8px;font-size:var(--fs-xs)">点名字可以直接开它的对话（不想开就忽略）。</div>`;
      result.querySelectorAll(".ns-chip").forEach((btn) => {
        btn.onclick = () => { const n = btn.getAttribute("data-role"); close(); openAgentChat(n); };
      });
    } catch (e) {
      result.innerHTML = `<div class="hint">判断失败：${escHtml(e.message)}</div>`;
    }
  };
  ov.querySelector(".ns-go").onclick = ask;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ask(); } });
  input.focus();
}
// ── 丁方案入口（2026-10-20）：作者点选要改哪一格，模型只写内容 ──────────────
// 为什么要这样：真机实测证明模型能写对整数参数，但「从上下文精确复制长标识符」老出错
// （8 次调用全漏落点）。落点是簿记、不是创作，交给作者点比让模型猜可靠得多。
// ⚠️ 排除 90-速记本：那是 14 个角色各自的私有工作记录，不是「设定」，
// 让角色改写它没有意义（而且会造成跨角色污染）。审查 2026-10-20 指出这一条要显式写明。
const SETTABLE_RE = /^外置大脑\/设定集\/(?!90-速记本\/).+\.md$/i;
function isSettableFile(rel) { return SETTABLE_RE.test(String(rel || "")); }

// ── 旧结构 → 设定集（作者 2026-10-20 裁定「乙」：作者点了才搬，不自动迁移）──────
function isLegacyBrainFile(rel) {
  const r = String(rel || "");
  return /^外置大脑\/(世界观|人物)\//.test(r) || r === "外置大脑/立项卡.md";
}

async function migrateLegacyBrain() {
  if (!DATA || !DATA.root) return;
  const ok = window.confirm(
    "把旧结构的设定复制进「外置大脑/设定集/」？\n\n"
    + "· 只复制，不删原文件\n"
    + "· 已存在的同名文件跳过（不覆盖你后来改的）\n"
    + "· 搬完侧栏会显示新结构，AI 角色才读得到这些设定"
  );
  if (!ok) return;
  try {
    const r = await jreq("POST", "/api/brain/migrate-legacy", { root: DATA.root });
    if (r && r.error) { toast(r.error, true); return; }
    const copied = (r.copied || []).length;
    const skipped = (r.skipped || []).length;
    toast(`搬完：复制 ${copied} 份，跳过 ${skipped} 份（已存在）`);
    await refresh();
  } catch (e) { toast("搬移失败：" + e.message, true); }
}

// 哪个角色负责哪个分区（决定这次对话用谁的提示词）
function roleForContainer(rel) {
  const r = String(rel || "");
  if (r.includes("/30-战斗")) return "战斗设计师";
  if (r.includes("/40-知识")) return "知识顾问";
  return "设定师";   // 00-立项 / 10-世界观 / 20-人物 都归设定师
}

// 2026-10-31（db749946 裁定 #2）：**只换显示、键不动**。
// roleForContainer() 的返回值同时是 agent 键（openAgentChat(role) 要去找
// agents/知识顾问.md），所以「知识顾问 → 查资料」只能在这里做纯展示映射，
// 绝不能改函数返回值。名册里去掉的是「这个人」，不是「查资料」这个动作。
const ROLE_DISPLAY = { "知识顾问": "查资料" };
function roleLabel(role) { return ROLE_DISPLAY[role] || role; }

async function openSlotPicker() {
  const rel = CUR && CUR.rel;
  if (!rel) return;
  let slots = [];
  try {
    const d = await jreq("GET", `/api/slots?root=${encodeURIComponent(DATA.root)}&container=${encodeURIComponent(rel)}`);
    slots = d.slots || [];
  } catch (e) { toast("取格子失败：" + e.message, true); return; }
  if (!slots.length) { toast("这个文件没有可写的格子", true); return; }

  const role = roleForContainer(rel);
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `
    <div class="run-card" style="width:520px;max-height:70vh;display:flex;flex-direction:column">
      <h3>让「${escHtml(roleLabel(role))}」改写哪一格？</h3>
      <p class="hint">你选的这一格会锁定在这次对话里——角色只负责写内容，不会（也不能）自己挑文件。</p>
      <div class="sp-list" style="overflow-y:auto;max-height:44vh;margin:8px 0"></div>
      <div class="row-end"><button class="ghost sp-cancel">取消</button></div>
    </div>`;
  document.body.appendChild(ov);
  const box = ov.querySelector(".sp-list");
  slots.forEach((s) => {
    const it = document.createElement("button");
    it.style.cssText = "display:block;width:100%;text-align:left;margin:4px 0;padding:8px 10px;"
      + "border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);cursor:pointer";
    it.innerHTML = `<b>${escHtml(s.key || s.label || "")}</b> `
      + `<span class="agent-tag">${s.filled ? "已填" : "未填"}</span>`
      + (s.preview ? `<div class="hint" style="margin-top:2px;font-size:var(--fs-xs)">${escHtml(s.preview)}</div>` : "");
    it.onclick = () => { ov.remove(); openAgentChat(role, s.id); };
    box.appendChild(it);
  });
  ov.querySelector(".sp-cancel").onclick = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
}

let _agentChat = null;   // { role, el, historyEl, inputEl, lockedSlot }

// 打开作者智库角色的对话（居中界面）。
// 作者 2026-10-20 需求 B：不再用浮窗，改成原版 loom「书房伙伴」那个居中界面。
// 领航员仍走它原来的 partner 通道；其余角色走 /api/agent/chat。
// 侧边栏选中态(2026-10-20 需求确认):点开哪个角色,就在那张卡片上留个标记。
// 只加一个 class(细竖条 + 淡底),不加徽标、不改字号,免得跟 14 角色已有的层级配色打架。
function markRoleSelected(name) {
  ["partner-agents", "author-agents", "engine-agents"].forEach((id) => {
    const ul = $(id);
    if (!ul) return;
    ul.querySelectorAll(".agent-card").forEach((li) => {
      li.classList.toggle("on", !!name && li.dataset.role === name);
    });
  });
}

function openAgentChat(name, lockedSlot) {
  const oldCard = journeyHost();
  if (oldCard) chatInputSnapshot(oldCard, chatKey(_navRole || "领航员"));
  _navRole = name || "领航员";
  _chatFocusPending = { key: chatKey(_navRole), anchor: document.activeElement };
  _navLockedSlot = String(lockedSlot || "").trim();
  _navForceCenter = true;   // 主动点开 → 占主视图（哪怕领航员本来只该在右下角球）
  _navYield = false;
  _agentCenter = null;      // 换角色 / 换落点都要重新拉历史
  markRoleSelected(_navRole);
  if (typeof closeAgentChat === "function") closeAgentChat();   // 关掉可能残留的旧浮窗
  renderJourney();
}

// ── 以下三个是旧「浮窗版角色对话」的残留辅助函数 ──────────────────────────
// 界面已改居中（见 openAgentChat），它们暂时保留，避免别处调用点直接炸；
// 后续清理时可一并删除。
function appendAgentMsg(kind, text) {
  if (!_agentChat) return null;
  const row = document.createElement("div");
  row.style.cssText = "margin:6px 0;display:flex;gap:8px;" + (kind === "user" ? "justify-content:flex-end" : "");
  const b = document.createElement("div");
  b.style.cssText = "max-width:78%;padding:7px 10px;border-radius:10px;white-space:pre-wrap;line-height:1.55;font-size:var(--fs-sm);" +
    (kind === "user" ? "background:var(--accent-tint);color:var(--accent-ink)" : "background:var(--surface);border:1px solid var(--line)");
  b.textContent = text;
  row.appendChild(b);
  _agentChat.historyEl.appendChild(row);
  _agentChat.historyEl.scrollTop = _agentChat.historyEl.scrollHeight;
  return b;
}

function closeAgentChat() {
  if (_agentChat && _agentChat.el) _agentChat.el.remove();
  _agentChat = null;
}

// ---------- agent 全流程面板:只讲写作引擎 6 道接力(作者 2026-10-20) ----------
// 作者智库是准备/审视角色,不进「写一章」的流水线,别塞进这个浮层。
let _flowAgent = null;
function openFlow(focusName) {
  markRoleSelected(focusName || null);   // 侧边栏同步高亮这个角色
  const box = $("flow-stages"); box.innerHTML = "";
  ENGINE_AGENTS.forEach((name, i) => {
    if (i > 0) { const a = document.createElement("div"); a.className = "flow-arrow"; a.innerHTML = icon("chevron"); box.appendChild(a); }
    const meta = AGENTS_META[name] || {};
    const st = document.createElement("div");
    st.className = "flow-stage" + (name === focusName ? " on" : "");
    st.appendChild(agentAvatar(name, "flow-ava", "flow-fallback"));
    const body = document.createElement("div"); body.className = "flow-body";
    const nm = document.createElement("div"); nm.className = "flow-name";
    const b = document.createElement("b"); b.textContent = name; nm.appendChild(b);
    if (meta.tag) { const t = document.createElement("span"); t.className = "flow-tag"; t.textContent = meta.tag; nm.appendChild(t); }
    const ds = document.createElement("div"); ds.className = "flow-desc"; ds.textContent = meta.desc || "";
    const io = document.createElement("div"); io.className = "flow-io";
    io.innerHTML = `<span class="flow-reads">读 ${escHtml(meta.reads || "")}</span><span class="flow-prod">产出 ${escHtml(meta.produces || "")}</span>`;
    body.appendChild(nm); body.appendChild(ds); body.appendChild(io);
    st.appendChild(body);
    st.onclick = () => {
      box.querySelectorAll(".flow-stage").forEach((x) => x.classList.remove("on"));
      st.classList.add("on");
      _flowAgent = name;
      _syncFlowPrompt();
    };
    box.appendChild(st);
  });
  _flowAgent = focusName;
  _syncFlowPrompt();
  $("flow-overlay").classList.remove("hidden");
  const on = box.querySelector(".flow-stage.on"); if (on) on.scrollIntoView({ block: "nearest" });
}
// 写作引擎角色:轻量说明浮窗
function showAgentTip(name, meta) {
  const tip = document.createElement("div");
  tip.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-md);padding:20px;z-index:9999;min-width:300px;max-width:420px;box-shadow:var(--shadow-2)";
  tip.innerHTML = `<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
    ${agentAvatar(name, "flow-ava", "flow-fallback").outerHTML || ""}
    <div><b style="font-size:var(--fs-md)">${escHtml(name)}</b><br><span style="color:var(--accent);font-size:var(--fs-sm)">${escHtml(meta.tag || "")}</span></div>
  </div>
  <div style="font-size:var(--fs-sm);color:var(--text-soft);line-height:1.6;margin-bottom:12px">${escHtml(meta.desc || "")}</div>
  <div style="font-size:var(--fs-xs);color:var(--text-mute);border-top:1px solid var(--line);padding-top:10px">
    <div><b>读:</b> ${escHtml(meta.reads || "—")}</div>
    <div style="margin-top:4px"><b>产出:</b> ${escHtml(meta.produces || "—")}</div>
  </div>
  <button onclick="this.closest('[style*=&quot;position:fixed&quot;]').remove()" style="margin-top:16px;width:100%;padding:8px;background:var(--accent);color:white;border:none;border-radius:var(--radius-sm);cursor:pointer">知道了</button>`;
  document.body.appendChild(tip);
}
function _syncFlowPrompt() {
}

// ---------- 通用引导浮层(skills 意图介绍 / 外置大脑空状态)----------
function showGuide({ title, bodyHtml, primary, ghost }) {
  $("guide-title").textContent = title;
  $("guide-body").innerHTML = bodyHtml;
  const pb = $("guide-primary"), gb = $("guide-ghost");
  const wire = (btn, spec) => {
    if (spec) { btn.classList.remove("hidden"); btn.textContent = spec.label;
      btn.onclick = () => { $("guide-overlay").classList.add("hidden"); spec.fn && spec.fn(); }; }
    else btn.classList.add("hidden");
  };
  wire(pb, primary); wire(gb, ghost);
  $("guide-overlay").classList.remove("hidden");
}

// ── 批C 引导注册表消费端（2026-11-03，作者"全面引导"三问之三）─────────────
// 后端 done_when 已判定，GET /api/guides/pending 只回当前该触发的；
// 前端按 context（welcome/study/book/style 等界面键）取对应引导，
// 复用 showGuide 引导卡（who 署名 + say 正文 + 「知道了」回执 POST seen）。
// 每个界面会话只弹一次（内存去重），后端 last_seen 兜底永不重复轰炸。
const _guidesShown = new Set();   // 本会话已展示过的引导 id
async function pollGuides(context) {
  try {
    const d = await jreq("GET", `/api/guides/pending?context=${encodeURIComponent(context)}` +
      (DATA && DATA.root ? `&root=${encodeURIComponent(DATA.root)}` : ""));
    const items = (d && (d.guides || d.pending)) || [];   // 兼容两种键名(后端实回 pending)
    for (const g of items) {
      if (_guidesShown.has(g.id)) continue;
      _guidesShown.add(g.id);
      showGuide({
        title: g.title || (g.who ? g.who + "想对你说" : "小提示"),
        bodyHtml: `<p class="guide-lead">${mdToHtml(g.say || "")}</p>` +
          (g.who ? `<div class="guide-who">${icon("robot", "guide-ico")} ${escHtml(g.who)}</div>` : ""),
        ghost: { label: "知道了", fn: () => guideSeen(g.id) },
      });
      break;   // 一次只弹一条,关掉再问下一条(不轰炸)
    }
  } catch (e) { /* 引导接口没挂上/离线:静默,引导永远不挡正事 */ }
}
async function guideSeen(id) {
  try { await jreq("POST", "/api/guides/seen", { id }); } catch (e) { /* seen 失败静默 */ }
}

// 起书完整性硬门禁:missing 没齐就拦,转发去居中态领航员(Task 3:弹层退役)
function enterNavCenter(missing) {
  _navYield = false; _navPopOpen = false;
  markRoleSelected("领航员");   // 回到居中的领航员 → 侧边栏也跟着标它
  renderJourney();
  const sub = $("nav-center-sub");
  if (sub) { sub.classList.add("hot"); setTimeout(() => sub.classList.remove("hot"), 1000); }
  const first = (missing || [])[0];
  if (first) postJourneyGoto(first, false);
}
function openGateGuide(missing) { enterNavCenter(missing); }   // 弹层退役,签名不动(软拦与409两调用点无感)

/* ── F1 第一章地基硬门·三入口统一消费（2026-11-11 派单 审查记录）──────────
   后端三条入口（/api/write、/api/chapter/step、/api/chapter/run-step）在**章 1**缺地基时
   都在开流前返回非流式 JSON；三种 code 必须能分辨：
     foundation_incomplete(409)  地基还差字段，带 missing / missing_details({id,问})
     not_a_book(400)             目录里没有 loom.toml（书被挪走/删了）
     config_unreadable(400)      loom.toml 读不出来（配置损坏）
   三条硬要求：①展示**具体缺项**、可就地补齐、补齐后能重试；
             ②这两种状态码都**不得进入流式态**（不许先显示"正在生成"）；
             ③foundation 字段 id 是**配置字段**，不是旧四类资料门（立项/世界观/主角/章纲）
               的 stage key —— 所以绝不走 openGateGuide，两套导航语义不同，混用会把人带错地方。 */
const _FOUNDATION_CODES = ["foundation_incomplete", "not_a_book", "config_unreadable"];

function isFoundationBlock(d) { return !!(d && _FOUNDATION_CODES.indexOf(d.code) >= 0); }

// 章 1 开流前预检。**只服务整章生成（/api/write，step 为空）**——那条入口后端本来就**没有**
// 审稿豁免（writing.py 无条件拦章 1 缺地基），所以这里的判据与后端一致，不存在"第二套判据"。
// 单棒（step 非空）**一律不预拦**：T1 豁免的唯一实现在后端 agents.review_pass_ok；
// 前端若复制一份（"章1 + 责任编辑/润色师 + 有现稿"）就是审查点名的"各写一套"，
// 且一旦前端对现稿的判断过期就会**误拦合法审稿**（反例①：请求根本没发出，后端放行也没用）。
// 单棒的"不许先显示正在生成"由 openRunUI 保证——等确认 NDJSON 才进运行态。
async function foundationPrecheck(n, step) {
  if (Number(n) !== 1 || !DATA || !DATA.root) return null;
  if (String(step || "").trim() !== "") return null;      // 单棒：交后端判，前端不预拦
  try {
    const d = await jreq("GET", `/api/navigator/foundation?root=${encodeURIComponent(DATA.root)}`);
    if (d && d.done === false) {
      return { code: "foundation_incomplete", missing: d["缺失项"] || [],
               missing_details: d["缺失项详情"] || [] };
    }
  } catch (e) { /* 读不到就不预拦：入口会再拦一次，别把网络抖动当缺项 */ }
  return null;
}

// 退出流式态：标题/底条/浮窗都不许留着"正在生成"的样子。
function _leaveStreamUI() {
  try { _stopRunTicker(); } catch (e) { /* 空转保护：没起过 ticker 也无所谓 */ }
  hideStrip(); _runMinimized = false;
  _weaving = false;
  const f = $("run-force"); if (f) f.classList.add("hidden");
  const ov = $("run-overlay"); if (ov) ov.classList.add("hidden");
  renderJourney();
}

// 三入口统一消费点。返回 true = 已按门禁处理，调用方**必须直接 return**（不得继续进流式态）。
function handleFoundationBlock(d, retry) {
  if (!isFoundationBlock(d)) return false;
  _leaveStreamUI();
  openFoundationGate(d, retry);
  return true;
}

// 门禁卡：三种 code 三种文案，缺项可就地填，补齐后重试。
function openFoundationGate(d, retry) {
  const code = (d && d.code) || "";
  const old = $("foundation-gate");
  if (old) old.remove();
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.id = "foundation-gate";
  const card = document.createElement("div");
  card.className = "run-card settings-card";
  card.style.maxHeight = "78vh"; card.style.overflowY = "auto";
  const h = document.createElement("h3");
  const brief = document.createElement("p"); brief.className = "hint";
  const body = document.createElement("div");
  const row = document.createElement("div"); row.className = "row-end";
  const msg = document.createElement("span"); msg.className = "hint";
  card.append(h, brief, body, row);
  row.appendChild(msg);
  overlay.appendChild(card);

  const doRetry = () => { overlay.remove(); if (typeof retry === "function") retry(); };
  const mkBtn = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = cls; b.textContent = label; b.onclick = fn;
    return b;
  };
  const para = (text) => { const p = document.createElement("p"); p.className = "hint"; p.textContent = text; return p; };

  if (code === "foundation_incomplete") {
    const details = Array.isArray(d.missing_details) ? d.missing_details : [];
    const n = details.length || (d.missing || []).length;
    h.textContent = `第一章还开不了工：地基还差 ${n} 项`;
    brief.textContent = "把这几个填上就能开始第一章。同一份地基在领航员里也能补。";
    const inputs = {};
    details.forEach((it) => {
      const lab = document.createElement("label");
      lab.className = "field-label";
      lab.textContent = it["问"] || it.id;
      const inp = document.createElement("input");
      inp.type = "text"; inp.setAttribute("data-fid", it.id);
      inputs[it.id] = inp;
      body.append(lab, inp);
    });
    if (!details.length) {
      body.appendChild(para("还差：" + ((d.missing || []).join("、") || "（服务端没给字段名）")
        + "。去领航员里补齐也一样。"));
    }
    let busy = false;
    const save = mkBtn("补齐并重试", "primary", async () => {
      if (busy) return;                                   // 禁重复点
      busy = true; save.disabled = true; save.textContent = "保存中…"; msg.textContent = "";
      const answers = {};
      Object.keys(inputs).forEach((k) => { answers[k] = inputs[k].value.trim(); });
      try {
        const r = await jreq("POST", "/api/navigator/foundation/save", { root: DATA.root, answers });
        if (!r || r.ok === false) throw new Error((r && r.error) || "没存上");
        doRetry();
      } catch (e) {
        busy = false; save.disabled = false; save.textContent = "补齐并重试";
        msg.textContent = "没存上：" + ((e && e.message) || e) + "（改完可以再试）";
      }
    });
    row.appendChild(save);
    row.appendChild(mkBtn("先不写", "ghost", () => overlay.remove()));
  } else if (code === "not_a_book") {
    h.textContent = "这本书打不开";
    brief.textContent = d.error || "这个位置没有 loom.toml。";
    body.appendChild(para("书可能被挪走或删了。找回文件夹后重试，或者回首页另选一本。"));
    row.appendChild(mkBtn("重试", "primary", doRetry));
    row.appendChild(mkBtn("回首页", "ghost", () => {
      overlay.remove(); localStorage.removeItem("loom_root"); showWelcome();
    }));
  } else {
    h.textContent = "这本书的配置读不出来";
    brief.textContent = d.error || "loom.toml 读不出来（配置可能损坏）。";
    body.appendChild(para("先修好这本书的 loom.toml（对照备份，或照另一本书的格式），再点重试。"));
    row.appendChild(mkBtn("重试", "primary", doRetry));
    row.appendChild(mkBtn("知道了", "ghost", () => overlay.remove()));
  }

  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

// skills 是给 Agent 看的「方法论手册」:点开先讲清是什么 / 哪一棒用,而不是糊一脸 Markdown
const SKILLS_META = {
  "世界观引擎": { who: "设定师", what: "怎么搭一个自洽、能长期写下去的世界观与力量体系。" },
  "故事引擎":   { who: "大纲师", what: "把『这章要完成什么』拆成有爆点、有钩子的分镜结构。" },
  "网文大神":   { who: "写手",   what: "网文落字手艺:画面代替形容词、对白带潜台词、短段落、章末留钩。" },
  "黄金开篇":   { who: "写手",   what: "只在第 1 章用:开篇怎么几句话就抓住读者。" },
  "评估自检":   { who: "编辑",   what: "章级自检清单:爽点够不够、钩子立没立、有没有 OOC / 设定漂移 / 拖节奏。" },
  "去AI味":     { who: "润色师", what: "怎么擦掉机器腔:套话头尾、空洞排比、黑名单词、被焊死的连接词。" },
  "金手指":     { who: "设定师", what: "金手指设计法:七字段、代价库、反制库,别让主角无敌到没冲突。" },
};
function fillSkills(items) {
  //2026-10-30 修真缺陷：这个列表容器在某一轮 UI 改动里被拿掉了，
  // 但这里还在硬拿 `$("skills").innerHTML`：拿到 null 就抛异常，
  // 而它是在 enterProject 中间调的 —— 于是「点一本书」整个流程就断在半路，回到欢迎页。
  // skills 列表不是关键路径：元素不在就安静跳过（不能因为一个辅助列表堵死开书）。
  const ul = $("skills");
  if (!ul) return;
  ul.innerHTML = "";
  items.forEach((it) => {
    const li = document.createElement("li");
    li.textContent = it.name;
    li.onclick = () => openSkillGuide(it.name, it.rel);
    ul.appendChild(li);
  });
}
function openSkillGuide(name, rel) {
  const m = SKILLS_META[name] || {};
  showGuide({
    title: `${name} · 方法论`,
    bodyHtml:
      `<p class="guide-lead">${escHtml(m.what || "这一棒 Agent 写作时参照的方法论。")}</p>` +
      (m.who ? `<div class="guide-who">${icon("robot", "guide-ico")} ${escHtml(m.who)}写作时会读它</div>` : "") +
      `<p class="hint">方法论是给 Agent 看的「手艺说明书」,平时不用管。想了解可查看全文。</p>`,
    primary: { label: "知道了" },
    ghost: { label: "查看方法论全文", fn: () => openFile(rel, false, null) },
  });
}

// 外置大脑:空 / 还没懂你 时给 onboarding,而不是甩一个空编辑器或中性默认文本
const BRAIN_GUIDE = {
  "世界观": "这本书的世界设定、力量体系、规则。设定师每章会读它,抽出本章能用的设定与绝不能违反的硬约束。",
  "人物卡": "主要人物的设定、关系、底线。防止写着写着人物 OOC、性格漂移。",
  "卡章纲": "整本书的分章规划,每章一句话任务。大纲师照它搭本章骨架;每章 learn 后这里还会自动补一条「AI 回顾」。",
  "写作指纹": "你的文风档案——句式、口头禅、节奏、禁用词。写手照它写,于是越写越像你。",
  "违禁词": "国内平台审核的敏感雷区清单 + 改写指引。写手/润色师参照它绕开红线;编辑器里「违禁词」按钮按它做本地粗筛。",
  "状态账本": "跨章状态流水账(物品/状态/规则/时钟),每章除虫后自动记一节;你可改可删,AI 只追加。",
};
function maybeBrainGuide(rel, content) {
  if (!rel.startsWith("外置大脑/")) return;
  if (rel.endsWith("状态账本.md")) return;  // 账本由除虫自动填,模板头永远带占位括注,「没填」引导不适用
  const empty = !content || !content.trim();
  const isFp = rel.endsWith("写作指纹.md");
  const neutralFp = isFp && /中性默认|还没.{0,4}懂你|还没学到你/.test(content);
  const isTemplate = !isFp && /占位示例|换成你自己的/.test(content);  // 还是出厂占位模板,也当「没填」引导
  if (!empty && !neutralFp && !isTemplate) return;
  const name = rel.split("/").pop().replace(/\.md$/, "");
  const desc = BRAIN_GUIDE[name] || "外置大脑的一部分,Agent 写作时会读它。";
  if (neutralFp) {
    showGuide({
      title: "写作指纹 · 还没懂你",
      bodyHtml: `<p class="guide-lead">${escHtml(desc)}</p>` +
        `<p class="hint">现在是中性默认。喂一段你写的字,或写几章手改后点「学这章的手改 learn」,它会慢慢长成你的样子——越写越像你。</p>`,
      primary: { label: "喂样本,让它懂我", fn: openSeed },
      ghost: { label: "先不管" },
    });
  } else {
    const draftable = ["世界观", "人物卡", "卡章纲"].includes(name);
    showGuide({
      title: `${name} · ${empty ? "还是空的" : "还是占位模板"}`,
      bodyHtml: `<p class="guide-lead">${escHtml(desc)}</p>` +
        (draftable
          ? `<p class="hint">不想从空白开始?写一句你的故事设定,让 AI 起草<b>世界观 / 人物卡 / 卡章纲</b>三件套,你再改成自己的。</p>` +
            `<textarea id="draft-idea" class="draft-idea" placeholder="一句话设定(可留空,按书名+题材发挥)。例:灵气复苏的都市,外卖小哥觉醒了能看见别人头顶任务的金手指。"></textarea>`
          : `<p class="hint">这是你来填的「外置大脑」。直接在编辑器里写就行,写得越具体,Agent 越不跑偏。</p>`),
      primary: draftable
        ? { label: "✨ 生成初稿(AI 起草,你再改)", fn: () => draftBrain(($("draft-idea") || {}).value || "") }
        : { label: "开始填写", fn: _startBrainEdit },
      ghost: { label: draftable ? "我自己填" : "知道了", fn: _startBrainEdit },
    });
  }
}
/* 占位模板弹窗「开始填写/知道了」的去处(2026-11-07 手测 #1 P2 根因修复):
   外置大脑文件打开时默认**预览态**(editor 是 hidden 的)——原来只做 editor.focus(),
   hidden 元素 focus 无效,人看着渲染页打不了字、点保存存的是占位旧值还显示「已保存」。
   这里先切到编辑态再聚焦,人真正落得了笔。 */
function _startBrainEdit() {
  if (CUR && CUR.preview) setPreview(false);
  $("editor").focus();
}
async function draftBrain(idea) {
  // 起草也要后端就绪:顺手保存后端(同写章),deepseek 仍缺 key 才拦
  try { await persistBackend(true); } catch (e) { toast("先配置好后端:" + e.message, true); return; }
  const dspec = providerSpec(DATA.backend.provider) || {};
  if (dspec.needs_key && !providerKeyed(DATA.backend.provider)) {
    toast(`先在设置里填 ${dspec.label || ""} 的 key(或把供应商切到 Claude / Codex 免 key)再生成初稿`, true);
    flashSetting("api-key");
    return;
  }
  toast("AI 正在起草 世界观 / 人物卡 / 卡章纲…十几秒,稍候");
  try {
    const d = await jreq("POST", "/api/brain/draft", { root: DATA.root, idea: idea || "" });
    if (d.state) DATA = d.state;
    render();
    const w = d.written || [], s = d.skipped || [];
    toast(`已起草:${w.join(" / ") || "（无）"}${s.length ? `;${s.join("/")}你已填,没动` : ""} —— 打开看看,改成你的`);
    if (w.length) showGuide({
      title: "设定底稿已就位",
      bodyHtml:
        `<p class="guide-lead">世界观 / 人物 / 卡章纲已按书名起草好初稿——它们是你的,三种方式随时维护:</p>` +
        `<p class="hint">① <b>随时手改</b>:在左栏「设定」里打开直接改,自动保存;<br>` +
        `② <b>让 AI 改写/续写</b>:打开世界观或人物文件,选中一段点「改写选中」,或点「AI 续写」接着铺;<br>` +
        `③ <b>越写越准</b>:每章 learn 后 AI 回顾/补充自动追加,你手写的部分永远不被覆盖。</p>`,
      primary: { label: "知道了" },
    });
  } catch (e) { toast(e.message, true); }
}

// ---------- 违禁词自检(本地粗筛,只提示不阻断) ----------
async function scanSensitive() {
  if (!CUR || CUR.chapter == null) return;
  try {
    const d = await jreq("POST", "/api/sensitive/scan", { root: DATA.root, text: $("editor").value });
    const hits = d.hits || [];
    if (!hits.length) {
      showGuide({
        title: "违禁词自检 · 没命中",
        bodyHtml: `<p class="guide-lead">本地粗筛没命中触发词。</p>` +
          `<p class="hint">这只兜低级漏网——平台审核偏严且不公开清单,终审仍建议人工把关。触发词可在「外置大脑 / 违禁词」里按题材增删。</p>`,
        primary: { label: "知道了" },
      });
      return;
    }
    const rows = hits.map((h) =>
      `<div class="sens-row"><span class="sens-word">${escHtml(h.word)}</span>` +
      `<span class="sens-cat">${escHtml(h.category)}</span><span class="sens-cnt">×${h.count}</span></div>`).join("");
    showGuide({
      title: `违禁词自检 · 命中 ${hits.length} 类`,
      bodyHtml: `<div class="sens-list">${rows}</div>` +
        `<p class="hint">只提示、不阻断。改写心法见「外置大脑 / 违禁词」:能架空就架空、能侧写就别直写、不给可复现的违法细节。</p>`,
      primary: { label: "知道了" },
      ghost: { label: "看改写指引", fn: () => openFile("外置大脑/违禁词.md", true, null) },
    });
  } catch (e) { toast(e.message, true); }
}
function fillList(id, items, editable) {
  const ul = $(id); ul.innerHTML = "";
  items.forEach((it) => {
    if (it.children) {   // 目录形态(世界观/、人物/):可折叠分组,一节(人)一行
      const li = document.createElement("li"); li.className = "brain-group";
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.innerHTML = `${icon("chevron", "disclosure")} ${escHtml(it.name)} <span class="side-sub">${it.children.length} 篇</span>`;
      det.appendChild(sum);
      const sub = document.createElement("ul"); sub.className = "list brain-sub";
      it.children.forEach((c) => {
        const cli = document.createElement("li");
        cli.textContent = c.name;
        cli.onclick = (e) => { e.stopPropagation(); openFile(c.rel, editable, null, cli); };
        sub.appendChild(cli);
      });
      det.appendChild(sub); li.appendChild(det); ul.appendChild(li);
      return;
    }
    const li = document.createElement("li");
    const isFp = it.rel.endsWith("写作指纹.md");
    if (isFp) li.className = "fp";
    li.innerHTML = (isFp ? icon("fingerprint") : "") + escHtml(it.name);
    li.onclick = () => openFile(it.rel, editable, null, li);
    ul.appendChild(li);
  });
}

// ---------- Markdown 渲染(给普通用户看渲染版,不是一脸 # 和 -) ----------
function mdEsc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function mdInline(s) {
  return mdEsc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function mdToHtml(md) {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  let html = "", i = 0, inUl = false, inOl = false;
  const close = () => { if (inUl) { html += "</ul>"; inUl = false; } if (inOl) { html += "</ol>"; inOl = false; } };
  while (i < lines.length) {
    const raw = lines[i], l = raw.trim();
    let m;
    if (!l) { close(); i++; continue; }
    if (m = l.match(/^(#{1,6})\s+(.*)$/)) { close(); const lv = m[1].length; html += `<h${lv}>${mdInline(m[2])}</h${lv}>`; i++; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(l)) { close(); html += "<hr>"; i++; continue; }
    if (m = l.match(/^>\s?(.*)$/)) { close(); const q = []; while (i < lines.length && (m = lines[i].trim().match(/^>\s?(.*)$/))) { q.push(mdInline(m[1])); i++; } html += `<blockquote>${q.join("<br>")}</blockquote>`; continue; }
    if (m = l.match(/^[-*+]\s+(.*)$/)) { if (!inUl) { close(); html += "<ul>"; inUl = true; } const ind = raw.length - raw.trimStart().length; html += `<li${ind >= 2 ? ` style="margin-left:${Math.min(ind, 12) * 7}px"` : ""}>${mdInline(m[1])}</li>`; i++; continue; }
    if (m = l.match(/^\d+[.)]\s+(.*)$/)) { if (!inOl) { close(); html += "<ol>"; inOl = true; } html += `<li>${mdInline(m[1])}</li>`; i++; continue; }
    close(); const p = []; while (i < lines.length) { const t = lines[i].trim(); if (!t || /^(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|-{3,}$|\*{3,}$|_{3,}$)/.test(t)) break; p.push(mdInline(t)); i++; } html += `<p>${p.join("<br>")}</p>`;
  }
  close();
  return html;
}
function setPreview(on) {
  if (!CUR) return;
  CUR.preview = on;
  const ed = $("editor"), pv = $("preview");
  if (on) { pv.innerHTML = mdToHtml(ed.value); pv.classList.remove("hidden"); ed.classList.add("hidden"); $("btn-preview").innerHTML = icon("edit") + " 编辑"; }
  else { pv.classList.add("hidden"); ed.classList.remove("hidden"); $("btn-preview").innerHTML = icon("edit") + " 预览"; }
}

// ---------- 文件编辑 ----------
function isRenamableSetting(rel) {
  const m = /^外置大脑\/设定集\/(20-人物|50-自定义)\/([^/]+)\.md$/i.exec(String(rel || ""));
  return !!m && !/^(?:[._]|README$|索引$|成长档案$)/i.test(m[2]);
}
function preservesCharacterType(rel, name) {
  if (!String(rel).startsWith("外置大脑/设定集/20-人物/")) return true;
  const oldStem = String(rel).split("/").pop().replace(/\.md$/i, "");
  const prefix = oldStem.split(/[·・•]/, 1)[0];
  const renamed = /^([^·・•]+)[·・•](.+)\.md$/i.exec(name);
  return !!renamed && renamed[1] === prefix && !!renamed[2].trim() && !renamed[2].includes("未命名");
}
async function renameCurrentFile() {
  if (!CUR || !CUR.editable || !isRenamableSetting(CUR.rel)) return;
  const current = CUR, root = DATA.root;
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.id = "file-rename-overlay";
  ov.innerHTML = `<form class="run-card file-rename-card" role="dialog" aria-modal="true" aria-labelledby="rename-heading">
    <h3 id="rename-heading">修改文件名</h3>
    <p class="hint">只修改当前设定文档的文件名，文档内容保留。新文件名不能与同目录已有文件重复。</p>
    <p class="rename-original hint"></p>
    <label class="field-label" for="file-rename-name">新文件名（.md）</label>
    <input id="file-rename-name" type="text" autocomplete="off" required />
    <p class="error" role="alert"></p>
    <div class="row-end"><button type="button" class="ghost rename-cancel">取消</button><button type="submit" class="primary">确认改名</button></div>
  </form>`;
  const form = ov.querySelector("form"), input = ov.querySelector("input");
  const error = ov.querySelector(".error"), submit = ov.querySelector('[type="submit"]');
  const originalName = current.rel.split("/").pop();
  ov.querySelector(".rename-original").textContent = "原文件名：" + originalName;
  input.maxLength = 160;
  input.value = originalName;
  let busy = false;
  const close = () => { if (!busy) { ov.remove(); $("btn-rename-file").focus({ preventScroll: true }); } };
  ov.querySelector(".rename-cancel").onclick = close;
  ov.onclick = (ev) => { if (ev.target === ov) close(); };
  ov.onkeydown = (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
    if (ev.key === "Tab") {
      const controls = [...form.querySelectorAll("input,button")].filter((el) => !el.disabled);
      if (ev.shiftKey && document.activeElement === controls[0]) { ev.preventDefault(); controls.at(-1).focus(); }
      else if (!ev.shiftKey && document.activeElement === controls.at(-1)) { ev.preventDefault(); controls[0].focus(); }
    }
  };
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    if (busy) return;
    const name = input.value.trim();
    if (!name || !/\.md$/i.test(name) || /[\\/:*?"<>|\x00-\x1f]/.test(name) || /^[._]/.test(name) || /^(?:README|索引)\.md$/i.test(name) || /^成长档案\.md$/i.test(name) || name.length > 160 || /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name)) {
      error.textContent = "请填写普通 .md 文件名，不要包含路径、特殊字符或固定文档名称。"; return;
    }
    if (!preservesCharacterType(current.rel, name)) {
      error.textContent = "人物文件须保留原有主角／配角／反派类型前缀和「·」分隔，后面填写具体名字，不能使用「未命名」。"; return;
    }
    if (name === originalName) { error.textContent = "文件名没有变化。"; return; }
    busy = true; submit.disabled = true; input.disabled = true; error.textContent = "";
    try {
      clearTimeout(_saveTimer);
      await autosave();
      if (_dirty) throw new Error("当前改动还没有保存，请先保存成功再改名。");
      if (CUR !== current || DATA.root !== root) throw new Error("当前文件已经切换，请重新打开改名窗口。");
      const out = await jreq("POST", "/api/file/rename", { root, rel: current.rel, name, base_hash: current.hash, confirm: true });
      current.rel = out.rel;
      current.hash = out.hash;
      if (CUR === current && DATA.root === root) $("editor-path").textContent = out.rel;
      ov.remove();
      toast("文件已改名");
      if (DATA.root === root) {
        try { await refresh(); }
        catch (e) { toast("文件已改名，侧栏刷新暂未成功：" + (e.message || e), true); }
      }
    } catch (e) { error.textContent = e.message || String(e); }
    finally { busy = false; submit.disabled = false; input.disabled = false; }
  };
  document.body.appendChild(ov);
  input.focus(); input.setSelectionRange(0, originalName.length - 3);
}

// 与后端 guardrail.PROTECTED_REL 对齐:这些位置手写 PUT 必 403,别当成可编辑文件打开。
const PROTECTED_PREFIX = ["正文/.原稿", "正文/.回收站", "正文/.历史", "外置大脑/写作指纹.md"];
function isProtectedRel(rel) {
  const r = String(rel || "").replace(/\\/g, "/");
  return PROTECTED_PREFIX.some((p) => r === p || r.startsWith(p + "/"));
}

async function openFile(rel, editable, chapter, li) {
  if (isProtectedRel(rel)) editable = false;   // 打开就说清「这份不能手改」,别让人白打一通再 403
  if (navMode() === "center") { _navYield = true; renderJourney(); }   // 点文件=用户主动逛正文,居中态让位给编辑器
  if (_dirty && CUR && CUR.editable) {
    try { await autosave(); } catch (e) { /* autosave 自己会 toast;下面按 _dirty 决定拦不拦 */ }
  }
  // 还没存上就切走 = 手改直接被抹掉(交叉审查 P0-1)。除非人明确说不要了。
  if (_dirty) {
    const _rel0 = (CUR && CUR.rel) || "当前文件";
    if (!window.confirm(`「${_rel0}」还有没存上的改动。\n\n切走会丢掉这些字。\n确定切走吗?(取消 = 留在这份里)`)) return;
  }
  clearTimeout(_saveTimer); clearDirty(); closeSearch();
  hideInlinePanel();   // 切文件收面板:稳态残留的候选绝不能落进另一个文件
  document.querySelectorAll(".list li.active").forEach((x) => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const d = await jreq("GET", `/api/file?root=${encodeURIComponent(DATA.root)}&rel=${encodeURIComponent(rel)}`);
  // 记住这一版在磁盘上的 hash:下次保存时带上,别处改过就能被发现(R10)。
  CUR = { rel, editable, chapter, hash: d.hash || "" };
  _editorOrigin = null;
  const _ee = $("editor-empty");
  if (_ee) _ee.classList.add("hidden");   // 有文件了，空态提示退场
  $("editor").classList.remove("hidden");   // textarea 回来
  $("editor").value = d.content;
  $("editor").readOnly = !editable;
  $("editor-path").textContent = rel + (editable ? "" : (isProtectedRel(rel) ? "(只读:这份受保护,只能走专门的动作改)" : "(只读)"));
  $("btn-save-file").classList.toggle("hidden", !editable);
  $("btn-rename-file").classList.toggle("hidden", !editable || !isRenamableSetting(rel));
  $("btn-rename-file").onclick = renameCurrentFile;
  $("btn-search").classList.remove("hidden");
  // 历史从「只认正文」推广到任何 .md:卡章纲/全书大纲合过一次也有旧版可退
  $("btn-history").classList.toggle("hidden", !/\.md$/i.test(rel));
  $("btn-sensitive").classList.toggle("hidden", chapter == null);
  // 丁方案入口：设定集里的文件能「让角色改写这一格」（落点由作者点选、模型只写内容）
  $("btn-agent-rewrite").classList.toggle("hidden", !isSettableFile(rel));
  // 旧结构文件：给一个「搬进设定集」的入口（只复制不删，作者点了才跑）
  $("btn-migrate-legacy").classList.toggle("hidden", !isLegacyBrainFile(rel));
  $("btn-learn").classList.toggle("hidden", chapter == null);
  $("btn-rewrite").classList.toggle("hidden", chapter == null);
  const brainRW = editable && chapter == null && isBrainRWFile(rel);
  $("btn-brain-rewrite").classList.toggle("hidden", !brainRW);
  $("btn-brain-continue").classList.toggle("hidden", !brainRW);
  $("btn-brain-copy").classList.toggle("hidden", !brainCopyKind(rel));
  $("btn-card-expand").classList.toggle("hidden", rel !== "外置大脑/卡章纲.md");
  const _ih = $("btn-import-half");
  if (_ih) _ih.classList.toggle("hidden", rel !== "外置大脑/卡章纲.md");
  // 逆推半截稿的第二步/第三步:只在对应的那一份材料上出现
  $("btn-vol-summary").classList.toggle("hidden", rel !== "外置大脑/卷纲.md");
  $("btn-book-summary").classList.toggle("hidden", rel !== "外置大脑/全书大纲.md");
  $("btn-outline").classList.toggle("hidden", chapter == null);  // 正文章节才给「本章细纲」入口
  $("btn-outline-regen").classList.add("hidden");                 // 默认藏,只在看细纲时由 openOutline 亮出
  $("btn-debug").classList.toggle("hidden", chapter == null);
  $("btn-preview").classList.remove("hidden");
  $("btn-snapshot").classList.toggle("hidden", !DATA.root);
  $("btn-style").classList.toggle("hidden", !DATA.root);
  /*2026-11-05（作者裁定「开始下线后端的知识顾问」）：查资料按钮永久隐藏——
    后端 /api/knowledge/ask 已加下线闸（410 knowledge_retired）；只读列表保留。 */
  $("btn-knowledge").classList.add("hidden");
  $("btn-align").classList.toggle("hidden", !DATA.root);
  setPreview(!rel.startsWith("正文/"));   // 外置大脑/skills/agents 默认渲染预览;正文/细纲 默认纯文本编辑
  $("status-note").textContent = chapter != null
    ? (_vtMode === "教学" && /^正文\/\.细纲\//.test(rel)
        ? "教学模式：先写细纲,保存后可跑审查（责任编辑/编辑总监）。"
        : "改完会自动保存;再点【学这章的手改】把你的风格喂给指纹。")
    : "";
  updateWordCount();
  maybeBrainGuide(rel, d.content);   // 外置大脑空/未懂你 → 给 onboarding,不甩空编辑器
}
async function saveFile() {
  if (!CUR || !CUR.editable) return;
  /* 预览态防「假成功」(2026-11-07 手测 #1 P2):预览态下编辑器是隐藏的,人根本没改过字——
     这时点保存会把占位旧值原样存回去还报「已保存」,让人以为刚才写的内容存上了。
     改为:静默切到编辑态并提示,不落盘。 */
  if (CUR.preview) { setPreview(false); toast("现在是渲染预览,先切到编辑态再改字;没有新改动可存。"); return; }
  clearTimeout(_saveTimer);
  const savingFile = CUR, savingRoot = DATA.root, savingContent = $("editor").value;
  const savingOrigin = editorSaveOrigin(savingRoot, savingFile.rel);
  /*③混合流（2026-11-03）：辅助/教学模式下，保存**章产物位**（正文/细纲）走
    /api/chapter/manual——后端落产物位+更新指纹基线+字数软诊断（warning toast，
    不硬拦）。失败降级走普通 /api/file（不挡保存）。FullAuto 与其它文件照旧。 */
  const _m = /^正文\/第(\d+)章\.md$/.exec(CUR.rel || "");
  const _o = /^正文\/\.细纲\/第(\d+)章\.md$/.exec(CUR.rel || "");
  if (_vtMode !== "FullAuto" && (_m || _o) && DATA && DATA.root) {
    const n = _m ? +_m[1] : +_o[1];
    try {
      const r = await jreq("POST", "/api/chapter/manual", {
        root: DATA.root, chapter: n,
        kind: _m ? "正文" : "细纲",
        content: savingContent,
        origin: savingOrigin,
      });
      if (!DATA || DATA.root !== savingRoot || CUR !== savingFile) return;
      if (r && r.hash) CUR.hash = r.hash;
      if (r && r.warning) toast(r.warning, true);
      if ($("editor").value === savingContent) clearDirty();
      toast("已保存" + (r && r["字数"] != null ? `（${r["字数"]} 字）` : ""));
      $("status-note").innerHTML = "已保存 " + icon("check");
      scheduleChainCheck();
      if (CUR.chapter != null) refresh();
      return;
    } catch (e) {
      if (e.status === 409 && e.body && e.body.code === "busy") {
        toast("这本书正在跑任务,等它跑完再保存", true); return;
      }
      toast("手写落位没成功,改走普通保存:" + (e.message || e), true);
      // 落到下面的普通保存
    }
  }
  try {
    // R10:带上「我这一版基于哪个 hash」。磁盘上被别处改过就 409,问人再决定覆盖。
    const _body = { root: savingRoot, rel: savingFile.rel, content: savingContent, origin: savingOrigin };
    if (CUR.hash) _body.base_hash = CUR.hash;
    let _r;
    try {
      _r = await jreq("PUT", "/api/file", _body);
    } catch (e) {
      if (e.status === 409 && e.body && e.body.code === "needs_merge") {
        // R10:不再二选一(盖掉 / 丢掉),开三栏让人自己合;盘上那版绝不自动丢
        openMergePanel(CUR.rel, e.body.current || "", e.body.current_hash || "",
                       $("editor").value);
        toast("磁盘上被别处改过:三栏里自己合一下(盘上那版不会自动丢)。", true);
        return;
      } else if (e.status === 409 && e.body && e.body.code === "busy") {
        toast("这本书正在跑任务(导入/铺章纲),等它跑完再保存", true);
        return;
      } else { throw e; }
    }
    if (!DATA || DATA.root !== savingRoot || CUR !== savingFile) return;
    if (_r && _r.hash) CUR.hash = _r.hash;
  } catch (e) {
    // 护栏级位置(原稿/回收站/写作指纹)本来就**不接收手写覆盖**。以前这里没有 catch,
    // 点「保存」会静默失败、正文还在编辑器里、人以为存上了(交叉审查 UI-P1-1)。
    toast(`没保存:${e.message || e}`, true);
    $("status-note").innerHTML = "没保存 " + icon("warn");
    return;
  }
  if ($("editor").value === savingContent) clearDirty();
  toast("已保存");
  $("status-note").innerHTML = "已保存 " + icon("check");
  // 联动:手改设定集/卡章纲/卷纲都是「文件变了」的主要来源,保存成功就查一次(去抖)
  scheduleChainCheck();
  if (CUR.chapter != null) refresh();
}

// ---------- 字数 / 自动保存 / 脏标记 ----------
function updateWordCount() {
  const wc = $("wordcount"), prog = $("progress"), bar = $("progress-bar");
  if (!CUR) { wc.textContent = ""; prog.classList.add("hidden"); return; }
  const n = countChars($("editor").value);
  if (CUR.chapter != null && DATA && DATA.backend.chapter_chars) {
    const target = DATA.backend.chapter_chars;
    // 「目标」是个按钮:点它就地改终稿目标字数(2026-10-20 需求确认,从设置页搬来)。
    wc.innerHTML = `字数 <b>${n.toLocaleString()}</b> · <button id="target-btn" `
      + `class="target-btn" title="点这里改终稿目标字数(它同时是进度条的上限)">`
      + `目标 ${target.toLocaleString()}</button>`;
    const tb = $("target-btn");
    if (tb) tb.onclick = openTargetPop;
    prog.classList.remove("hidden");
    bar.style.width = Math.min(100, (n / target) * 100) + "%";
    bar.classList.toggle("done", n >= target);
  } else {
    wc.innerHTML = `字数 <b>${n.toLocaleString()}</b>`;
    prog.classList.add("hidden");
  }
}

// ---- 终稿目标字数小浮窗(2026-10-20 从设置页搬到正文字数条) ----
function openTargetPop() {
  if (!CUR || !CUR.chapter) { toast("先选一章正文再改目标字数。", true); return; }
  const pop = $("target-pop");
  if (!pop) return;
  const inp = $("chapter-chars");
  if (inp) inp.value = (DATA && DATA.backend && DATA.backend.chapter_chars) || 800;
  pop.classList.remove("hidden");
  if (inp) { inp.focus(); inp.select(); }
}
function closeTargetPop() {
  const pop = $("target-pop");
  if (pop) pop.classList.add("hidden");
}
async function saveTargetChars() {
  const inp = $("chapter-chars");
  const v = parseInt((inp && inp.value) || "", 10);
  if (!v || v < 200) { toast("目标字数至少 200。", true); return; }
  inp.value = v;
  await persistBackend(true);   // 与「保存」同一条落盘路径,不新开第二种口径
  const now = (DATA && DATA.backend && DATA.backend.chapter_chars) || 0;
  closeTargetPop();
  // 不谎报:persistBackend 若因模型名为空提前返回,字数根本没落盘
  if (now === v) toast(`终稿目标字数已改为 ${v.toLocaleString()}。`);
  else toast("字数没存上:先把模型名补全(设置里的「模型」),再回来改字数。", true);
}
function markDirty() {
  if (!CUR || !CUR.editable) return;
  _dirty = true;
  document.querySelector(".path").classList.add("dirty");
  $("status-note").textContent = "未保存…";
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(autosave, 900);
}
function clearDirty() {
  _dirty = false;
  document.querySelector(".path").classList.remove("dirty");
}
let _saving = null;   // 串行化自动保存:debounce 与 learn 前的显式落盘可能撞上,后到的等前一个写完
async function autosave() {
  if (!CUR || !CUR.editable || !_dirty) return;
  if (_saving) { await _saving; if (!_dirty) return; }   // 等在飞的那次;它已把最新内容存了就不重复写
  const rel = CUR.rel, content = $("editor").value, root = DATA.root;
  const origin = editorSaveOrigin(root, rel);
  _saving = (async () => {
    try {
      const _b2 = { root, rel, content, origin };
    if (CUR && CUR.rel === rel && CUR.hash) _b2.base_hash = CUR.hash;
    let _r2;
    try {
      _r2 = await jreq("PUT", "/api/file", _b2);
    } catch (e) {
      // 自动保存撞上冲突/任务在跑:停下来告诉人,绝不静默覆盖磁盘上的新内容。
      if (e.status === 409) {
        toast("自动保存暂停:磁盘上这份被改过了(导入/铺章纲?),先手动保存一次并选择要哪一版", true);
        return;
      }
      throw e;
    }
    if (DATA && DATA.root === root && CUR && CUR.rel === rel) {
      if (_r2 && _r2.hash) CUR.hash = _r2.hash;
      if ($("editor").value === content) { clearDirty(); $("status-note").innerHTML = "已自动保存 " + icon("check"); }
      scheduleChainCheck();
    }
    } catch (e) {
      // 只在状态栏写一行 12px 灰字 = 没提示(交叉审查 P0-1):人以为存上了,一切走
      $("status-note").textContent = "自动保存没成功(这一份还没存上):" + e.message;
      toast("自动保存没成功,改动还在编辑器里:" + (e.message || e), true);
    }
    finally { _saving = null; }
  })();
  await _saving;
}

// ---------- 本章版本历史(覆盖前自动留快照,可回滚) ----------
async function openHistory() {
  // 后端 fsutil 对任何 .md 都留历史(键名「文件-<name>-<sha1>」),这里以前只放行章节,
  // 于是「历史」按钮对卡章纲/全书大纲/细纲是死键,index.html 却写着「正文以外也能退」(交叉审查 P1-8)。
  if (!CUR) return;
  const box = $("history-list");
  box.innerHTML = `<div class="hint">读取中…</div>`;
  // 标题按文件说人话:正文是「本章」,卡章纲/卷纲就是它自己的名字(P2-6)
  const _hTitle = $("history-title");
  if (_hTitle) _hTitle.textContent = (CUR.chapter != null) ? "本章版本历史" : "版本历史";
  $("history-overlay").classList.remove("hidden");
  try {
    if (_dirty) await autosave();   // 先把当前手改落盘,免得回滚后丢
    const d = await jreq("GET", `/api/history?root=${encodeURIComponent(DATA.root)}&rel=${encodeURIComponent(CUR.rel)}`);
    renderHistory(d.versions || []);
  } catch (e) { box.innerHTML = `<div class="error">${e.message}</div>`; }
}
function _fmtStamp(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id);   // 尾部微秒忽略不显示
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : id;
}
function renderHistory(versions) {
  const box = $("history-list"); box.innerHTML = "";
  if (!versions.length) {
    box.innerHTML = `<div class="hint">还没有历史版本。保存或 AI 重写覆盖一次后,旧版会自动留在这里(正文以外的 .md 也一样)。</div>`;
    return;
  }
  versions.forEach((v) => {
    const el = document.createElement("div"); el.className = "hist-item";
    const head = document.createElement("div"); head.className = "hist-head";
    const meta = document.createElement("div"); meta.className = "hist-meta";
    meta.innerHTML = `<span class="hist-time">${_fmtStamp(v.id)}</span><span class="hist-chars">${v.chars} 字</span>`;
    const act = document.createElement("button"); act.className = "mini"; act.textContent = "回滚到这版";
    act.onclick = () => restoreHistory(v.id);
    head.appendChild(meta); head.appendChild(act);
    const prev = document.createElement("div"); prev.className = "hist-preview"; prev.textContent = v.preview || "";
    el.appendChild(head); el.appendChild(prev);
    box.appendChild(el);
  });
}
async function restoreHistory(id) {
  if (!CUR) return;
  if (!window.confirm(`回滚会用这个历史版本覆盖「${CUR.rel}」(当前版本会先自动存一份,可再回滚回来)。确定?`)) return;
  try {
    const d = await jreq("POST", "/api/history/restore", { root: DATA.root, rel: CUR.rel, id });
    $("editor").value = d.content;
    clearDirty(); updateWordCount();
    $("history-overlay").classList.add("hidden");
    toast("已回滚到该版本");
    if (CUR.chapter != null) refresh();
  } catch (e) { toast(e.message, true); }
}

// ---------- seed ----------
function openSeed() {
  $("seed-error").textContent = "";
  switchSeed("sample");
  $("seed-overlay").classList.remove("hidden");
}
function switchSeed(m) {
  _seedMode = m;
  $("seed-tab-sample").classList.toggle("on", m === "sample");
  $("seed-tab-reference").classList.toggle("on", m === "reference");
  $("seed-tab-inherit").classList.toggle("on", m === "inherit");
  $("seed-pane-sample").classList.toggle("hidden", m !== "sample");
  $("seed-pane-reference").classList.toggle("hidden", m !== "reference");
  $("seed-pane-inherit").classList.toggle("hidden", m !== "inherit");
  // 2026-10-20:两档都是「你自己的字」——单段 vs 多篇;别人的文章归「文风档案」。
  $("seed-go").textContent = m === "reference" ? "提炼指纹(多篇)" : "提炼指纹";
}
async function doSeed() {
  $("seed-error").textContent = "";
  const body = { root: DATA.root };
  if (_seedMode === "inherit") {
    const p = $("seed-inherit-path").value.trim();
    if (!p) { $("seed-error").textContent = "填那本书的写作指纹文件路径。"; return; }
    body.inherit = p;
  } else if (_seedMode === "reference") {
    const ref = $("seed-ref-text").value.trim();
    if (!ref) { $("seed-error").textContent = "贴几篇范文再蒸馏。"; return; }
    body.reference = ref;
  } else {
    const text = $("seed-text").value.trim();
    if (!text) { $("seed-error").textContent = "贴点你写的字再提炼。"; return; }
    body.text = text;
  }
  // 交叉审查 UI-P0-4:这一档会**整份重写**《写作指纹》(fingerprint.seed_from_reference 走
  // guard_write + set_fingerprint_source("reference")),还要花一次调用。原来既无确认,
  // 提示语还写着「不会顶替你的指纹」,与真实行为相反。先说清,再动手。
  if (_seedMode === "reference") {
    if (!window.confirm("这一步会:①调用一次模型(花钱);②整份重写《写作指纹》,原来的指纹会被替换,不可撤销。\n\n"
        + "这里只该贴**你自己写的**多篇;别人的文章请走侧边栏「文风档案」。\n\n继续吗?")) return;
  }
  $("seed-go").textContent = "提炼中…"; $("seed-go").disabled = true;
  try {
    DATA = await jreq("POST", "/api/seed", body);
    render();
    $("seed-overlay").classList.add("hidden");
    $("seed-text").value = "";
    $("seed-ref-text").value = "";
    toast(_seedMode === "reference" ? "起点指纹已就位(来源=范文)。之后的手改 learn 仍只认你的改稿"
                                     : "写作指纹已就位,它开始懂你了");
  } catch (e) { $("seed-error").textContent = e.message; }
  finally {
    // 范文模式蒸馏失败后若写回「提炼指纹」,人看到的是另一件事的按钮(P2-5)
    $("seed-go").textContent = (m === "reference") ? "蒸馏文风" : "提炼指纹";
    $("seed-go").disabled = false;
  }
}

// ---------- learn ----------
async function learn(n) {
  try {
    if (CUR && CUR.chapter === n && _dirty) await autosave();  // 先把手改落盘,别让 learn 学到旧稿
    const d = await jreq("POST", "/api/learn", { root: DATA.root, chapter: n });
    _lastLearnChapter = n;
    toast(`第${n}章手改已学,写后摘要已补进卡章纲`);
    if (d.warn) toast(d.warn, true);   // 软提示:这次 learn 疑似把指纹磨短,可在弹窗里一键撤销
    await refresh();
    showLearnChanges(d.changes || { added: [], removed: [] }, extractRecap(d["卡章纲"] || "", n),
      { world: d["世界观补充"] || "", chars: d["人物卡补充"] || "" });
  } catch (e) { toast(e.message, true); }
}
function extractRecap(kataGang, n) {
  const lines = kataGang.split("\n");
  const head = new RegExp(`^\\s*-\\s*第\\s*${n}\\s*章`);
  const nextCh = /^\s*-\s*第\s*\d+\s*章/;
  let i = lines.findIndex((l) => head.test(l));
  if (i < 0) return "";
  const block = [];
  for (let j = i + 1; j < lines.length; j++) {
    if (nextCh.test(lines[j])) break;          // 到下一章为止
    if (lines[j].trim() === "" && !block.length) continue;
    block.push(lines[j]);
  }
  const joined = block.join("\n");
  const k = joined.indexOf("[AI回顾]");
  return k >= 0 ? joined.slice(k).replace(/^\s+/, "").replace(/\n\s+/g, "\n").trim() : "";
}
function showLearnChanges(changes, recap, supp) {
  const box = $("learn-changes"); box.innerHTML = "";
  const add = changes.added || [], rem = changes.removed || [];
  // 盖印仪式:摘要一行 + 朱印落下(一次性动效,reduced-motion 下静止)
  const anchors = add.filter((l) => l.trim().startsWith(">")).length;
  $("learn-summary").innerHTML = (add.length || rem.length)
    ? `学进 <b>${add.length}</b> 条` + (anchors ? `(含你的 anchor 例句 ${anchors} 条)` : "") +
      (rem.length ? ` · 拆掉 ${rem.length} 条` : "")
    : "指纹这次没有明显变化。";
  const st = $("learn-stamp");
  st.style.animation = "none"; void st.offsetWidth; st.style.animation = "";
  if (!add.length && !rem.length) {
    box.innerHTML = '<div class="hint">这次没有明显的规则变化。</div>';
  } else {
    if (rem.length) {
      const w = document.createElement("div");
      w.className = "learn-warn";
      w.innerHTML = `⚠️ 这次从你的写作指纹里 <b>删掉了 ${rem.length} 条</b>(下面红底划线的)。` +
        `若那是你之前 learn 攒下来的嗓音、不该被抹掉,点下面 <b>「撤销这次 learn」</b> 一键还原。`;
      box.appendChild(w);
      const h = document.createElement("div"); h.className = "sub-head"; h.textContent = "删掉的(原有规则被抹掉)"; box.appendChild(h);
      rem.forEach((l) => { const e = document.createElement("div"); e.className = "chg rem"; e.textContent = "− " + l; box.appendChild(e); });
    }
    if (add.length) {
      const h = document.createElement("div"); h.className = "sub-head"; h.textContent = "新增的(更像你)"; box.appendChild(h);
      add.forEach((l) => { const e = document.createElement("div"); e.className = "chg add"; e.textContent = "+ " + l; box.appendChild(e); });
    }
  }
  // 有删除时,撤掉「保留」的墨色强调——让「撤销」成为唯一显眼按钮,
  // 别让作者顺手点「保留」把攒下来的嗓音丢了。
  $("learn-keep").classList.toggle("primary", rem.length === 0);
  const wrap = $("learn-recap-wrap");
  if (recap) { $("learn-recap").textContent = recap; wrap.classList.remove("hidden"); }
  else wrap.classList.add("hidden");
  // 外置大脑随章补充(世界观/人物卡):有就展示,让作者看见 loom 动了哪些设定(只追加、可改可删)
  const sw = $("learn-supp-wrap"); const sup = supp || {};
  const segs = [];
  if (sup.world) segs.push("【世界观】\n" + sup.world);
  if (sup.chars) segs.push("【人物卡】\n" + sup.chars);
  if (segs.length) { $("learn-supp").textContent = segs.join("\n\n"); sw.classList.remove("hidden"); }
  else sw.classList.add("hidden");
  $("learn-overlay").classList.remove("hidden");
}
async function revertLearn() {
  if (_lastLearnChapter == null) return;
  try {
    await jreq("POST", "/api/learn/revert", { root: DATA.root, chapter: _lastLearnChapter });
    toast("已撤销这次 learn,指纹还原");
    $("learn-overlay").classList.add("hidden");
    await refresh();
    openFile("外置大脑/写作指纹.md", true, null);
  } catch (e) { toast(e.message, true); }
}

// ---------- 本章细纲(看 / 改 / 重新生成) ----------
async function openOutline(n) {
  const rel = `正文/.细纲/第${n}章.md`;
  // 旧章 / 还没生成过 → 细纲文件不存在;问一下要不要现在生成
  try {
    await jreq("GET", `/api/file?root=${encodeURIComponent(DATA.root)}&rel=${encodeURIComponent(rel)}`);
  } catch (e) {
    if (!confirm(`第${n}章还没有细纲(可能是旧章)。现在用「设定师→大纲师」生成一份?`)) return;
    if (await callRegen(n) == null) return;
  }
  await openFile(rel, true, null, null);   // 当普通可编辑文件打开(非正文章节,不出 learn/重写选中等)
  CUR.outline = n;
  $("editor-path").textContent = rel;
  $("status-note").textContent = `这是第${n}章的细纲(分镜)。改完它,重写本章(覆盖)就按你的细纲来;想要新方案点「重新生成细纲」。`;
  $("btn-outline-regen").classList.remove("hidden");
}
async function regenOutline() {
  if (!CUR || CUR.outline == null) return;
  if (!confirm(`重新生成第${CUR.outline}章细纲?当前细纲(含你的手改)会被覆盖。`)) return;
  const text = await callRegen(CUR.outline);
  if (text == null) return;
  $("editor").value = text; clearDirty(); toast("细纲已重新生成");
}
async function callRegen(n) {
  const btn = $("btn-outline-regen"); const old = btn.textContent;
  btn.disabled = true; btn.textContent = "生成中…(设定师→大纲师)";
  try {
    const d = await jreq("POST", "/api/outline/regen", { root: DATA.root, chapter: n });
    return d.outline;
  } catch (e) { toast(e.message, true); return null; }
  finally { btn.disabled = false; btn.textContent = old; }
}

// ---------- 局部重写 ----------
function openRewrite() {
  if (!CUR || CUR.chapter == null) return;
  const ed = $("editor");
  const start = ed.selectionStart, end = ed.selectionEnd;
  const span = ed.value.substring(start, end);
  if (!span.trim()) { toast("先在正文里选中要重写的一段", true); return; }
  _rwMode = "chapter";
  _rewriteSel = { start, end, span };
  showInlinePanel({ src: span, goLabel: "重写",
    placeholder: "可选指令:太铺张压一压 / 换个更冷的章末钩 / 加一句冲突…(留空就按你的嗓音改顺)" });
}
// 世界观/人物 的设定文件(单文件老书 + 目录新书)才有 AI 改写/续写;
// 成长档案是 learn 的 AI 自留地、写作指纹是 voice——都不掺和(与 server 端 rel 守卫同口径)
function isBrainRWFile(rel) {
  if (rel === "外置大脑/世界观.md" || rel === "外置大脑/人物卡.md") return true;
  return /^外置大脑\/(世界观|人物)\//.test(rel) && rel.endsWith(".md") && !rel.endsWith("成长档案.md");
}
function openBrainRewrite() {
  if (!CUR || !CUR.editable || !isBrainRWFile(CUR.rel)) return;
  setPreview(false);                       // 预览态没有选区,切回纯文本编辑
  const ed = $("editor");
  const start = ed.selectionStart, end = ed.selectionEnd;
  const span = ed.value.substring(start, end);
  if (span.trim().length < 2) { toast("先在设定里选中要改写的一段", true); return; }
  _rwMode = "brain";
  _rewriteSel = { start, end, span };
  showInlinePanel({ src: span, goLabel: "改写",
    placeholder: "怎么改?例:代价再狠一点 / 等级压到 7 级(留空=改得更具体、能落地)" });
}
// 「从另一本书复制」——只对这两个文件亮出按钮(作者 2026-10-18 裁定:
// 不做全局违禁词库/全局范文库,只做单本 + 复制)。
// R22 文风档案:列档案 / 加稿 / 蒸馏 / 选档案+强度。蒸馏**会花一次模型调用**,按之前要说清。
async function refreshStyleCard() {
  if (!DATA.root) return;
  try {
    const d = await jreq("GET", `/api/style/packs?root=${encodeURIComponent(DATA.root)}`);
    const p = (d && d.在用) || {};
    const n = (d && d.档案) ? d.档案.length : 0;
    $("style-source").innerHTML = p.档案
      ? `文风档案·已建立（${escHtml(p.档案)}）`
      : (n ? `文风档案·已建立` : "文风档案·空空如也");
  } catch (e) { /* 侧栏说明拿不到就算了,不打扰 */ }
}


// 铺下一块章纲:一次 50 章(作者 2026-10-18 定的粒度)。**会花钱**,点之前说清。
// R26 半截稿导入:一块面板走完「切章 → 报账 → 开始 → 停」。
// 为什么不用 prompt/confirm:浏览器弹窗收不了「上限/抽法」,而且回车即花钱太险。
let HALF_QUOTE = null;     // 上次报账的结果;null = 还没报账,「确实开始」不可点

// ---------------------------------------------------------------- R10 三栏合并
let MERGE = null;      // {rel, diskHash}

function openMergePanel(rel, diskText, diskHash, mineText) {
  MERGE = { rel, diskHash };
  $("merge-disk").value = diskText;
  $("merge-mine").value = mineText;
  $("merge-out").value = mineText;      // 默认用我这一版做底,但从左栏能拿到盘上那版
  $("merge-overlay").classList.remove("hidden");
  mergeMeta();
}
function closeMergePanel() { $("merge-overlay").classList.add("hidden"); MERGE = null; }

function mergeMeta() {
  const n = (t) => (t || "").length;
  $("merge-disk-meta").textContent = `${n($("merge-disk").value)} 字`;
  $("merge-mine-meta").textContent = `${n($("merge-mine").value)} 字`;
  const out = $("merge-out").value;
  const same = out === $("merge-disk").value ? "(和磁盘一致)" :
    (out === $("merge-mine").value ? "(和我这一版一致)" : "");
  $("merge-out-meta").textContent = `${n(out)} 字 ${same}`;
}

async function mergeSave() {
  if (!MERGE) return;
  const out = $("merge-out").value;
  if (!out.trim()) { toast("右栏是空的,没保存。", true); return; }
  $("merge-save").disabled = true;
  try {
    // base_hash 用「磁盘上那一版的 hash」:合完之后如果又有人改,还会再拦一次
    const d = await jreq("PUT", "/api/file",
      { root: DATA.root, rel: MERGE.rel, content: out, base_hash: MERGE.diskHash });
    if (d && d.hash && CUR && CUR.rel === MERGE.rel) { CUR.hash = d.hash; $("editor").value = out; }
    clearDirty();
    toast("合完了,存的是右栏那一版。");
    closeMergePanel();
    if (CUR && CUR.chapter != null) refresh();
  } catch (e) {
    if (e.status === 409 && e.body && e.body.code === "needs_merge") {
      toast("刚又有人改了这份文件:左栏已换成最新那版,再合一次。", true);
      $("merge-disk").value = e.body.current || "";
      MERGE.diskHash = e.body.current_hash || "";
      mergeMeta();
    } else { toast(`没合上:${e.message || e}`, true); }
  } finally { $("merge-save").disabled = false; }
}

// ---------------------------------------------------------------- R15 文风档案
// ---------- R27 知识顾问:提案 → 人勾选 → 入账 ----------
let KNOW_PROP = null;      // 当前这份提案(入账发的是它,不是模型原文)

function openKnowledge() {
  if (!DATA.root) { toast("先打开一本书。", true); return; }
  $("know-overlay").classList.remove("hidden");
  $("know-out").textContent = "";        // 上一轮的结果不许挂在新一轮前面
  $("know-accept").classList.add("hidden");
  KNOW_PROP = null;
  refreshKnowledge();
  refreshWebStatus();      // 别让状态行永远停在「还没查」
}

async function askKnowledge() {
  const q = ($("know-q").value || "").trim();
  if (!q) { toast("先写下要问什么。", true); $("know-q").focus(); return; }
  const inside = ($("know-inside").checked && CUR && CUR.rel)
    ? `【${CUR.rel}】\n${($("editor").value || "").slice(0, 6000)}` : "";
  const btn = $("know-go");
  btn.disabled = true;
  btn.textContent = "顾问在想…";
  KNOW_BUSY = true;
  $("know-accept").classList.add("hidden");    // 这一问还没出结果,别让人把上一份入账了
  const _root = DATA.root;      // 这一问是问哪本书 —— await 回来要核,不然换书后结果会贴错
  KNOW_SRC = { root: _root, question: q,
               attached: $("know-attached").value || "", web: $("know-web").value || "" };
  $("know-out").textContent = "调用模型,可能要十几秒。这会在「深度逻辑推理」槽上计一次调用。";
  try {
    const d = await jreq("POST", "/api/knowledge/ask", {
      root: DATA.root, question: q, inside,
      attached: $("know-attached").value || "", web: $("know-web").value || "",
      evidence: !!($("know-evidence") && $("know-evidence").checked),
      browse: !!($("know-browse") && $("know-browse").checked),
    });
    if (DATA.root !== _root) {      // 回来的已经不是那本书了:宁可不显示,也不许贴错
      KNOW_PROP = null;
      toast("这中间换了书,上一问的结果没贴过来(免得贴到别的书上)。", true);
      return;
    }
    KNOW_PROP = d;
    renderKnowProposal(d);
  } catch (e) {
    KNOW_PROP = null;
    $("know-out").textContent = "没问成:" + e.message;
    toast(e.message, true);
  } finally {
    KNOW_BUSY = false;
    btn.disabled = false;
    btn.textContent = "问一下(会花钱)";
  }
}

function renderKnowProposal(d) {
  const box = $("know-out");
  if (!d || d.ok === false) {
    box.textContent = "这次没给出能用的提案:" + ((d && (d.说明 || d.reason)) || "模型没按约定输出");
    $("know-accept").classList.add("hidden");
    return;
  }
  const sec = (t, rows, empty) => {
    const body = (rows && rows.length)
      ? `<ul>${rows.map((x) => `<li>${escHtml(x)}</li>`).join("")}</ul>`
      : `<p class="hint">${escHtml(empty)}</p>`;
    return `<h4>${escHtml(t)}</h4>${body}`;
  };
  box.innerHTML =
    `<div class="know-topic">${escHtml(d.主题 || "")}</div>` +
    `<h4>结论(初步判断)</h4><p class="know-concl">${escHtml(d.结论 || "")}</p>` +
    sec("依据", d.依据, "(模型没给依据 —— 这条要格外小心)") +
    sec("不确定", d.不确定, "(模型说没有不确定 —— 但这是 AI 判断,仍需你看)") +
    sec("关联", d.关联, "(没挂到任何材料上)") +
    (((d.关联没对上 || []).length)
      ? sec("关联没对上(书里找不到这些材料)", d.关联没对上, "")
      : "");
  $("know-accept").classList.toggle("hidden", !d.结论);
  // 取证 / 浏览到底做没做,照实写出来(裁定 3/2:做了要说做了,没做也要说没做)
  const _ev = d.取证 || {};
  const _bw = d.浏览 || {};
  const _bits = [];
  const _skip = (_ev.跳过的章 || []);
  _bits.push(_ev.做没做
    ? `跨章取证:查了第 ${(_ev.章 || []).join("、")} 章`
      + (_skip.length ? `(跳过第 ${_skip.join("、")} 章:那几章没读到或太长)` : "")
    : `跨章取证:没做(${_ev.为什么没做 || "没勾选或没配那一槽"})`);
  _bits.push(_bw.做没做
    ? `联网抓页:抓了 ${(_bw.页 || []).filter((x) => x.抓没抓到).length} 页`
    : `联网抓页:没做(${_bw.为什么没做 || "没勾选或顾问判断不用上网"})`);
  const _meta = document.createElement("p");
  _meta.className = "hint know-meta";
  _meta.textContent = _bits.join(" · ");
  box.appendChild(_meta);
}

async function acceptKnowledge() {
  if (!KNOW_PROP || !KNOW_PROP.结论) { toast("还没有可入账的提案。", true); return; }
  if (KNOW_SRC && KNOW_SRC.root && DATA.root !== KNOW_SRC.root) {
    toast("这份提案是另一本书问出来的,不能入到这本书里。", true);   // 换过书就别让它跨书落盘
    return;
  }
  if (!window.confirm(`把这条件写成「外置大脑/知识/${(KNOW_PROP.主题 || "").slice(0, 24)}.md」?\n\n`
    + "写进去的是「初步判断」,不是设定;要进设定请自己挪进对应文件。")) return;
  try {
    const d = await jreq("POST", "/api/knowledge/accept", {
      root: (KNOW_SRC && KNOW_SRC.root) || DATA.root,
      question: (KNOW_SRC && KNOW_SRC.question) || ($("know-q").value || "").trim(),
      proposal: KNOW_PROP,
      attached: (KNOW_SRC && KNOW_SRC.attached) || "",
      web: (KNOW_SRC && KNOW_SRC.web) || "",
    });
    if (d.ok === false) { toast(d.reason || "没写进去", true); return; }
    toast(`已写入 ${d.文件}(索引 ${d.索引条目} 行)`);
    KNOW_PROP = null;
    $("know-accept").classList.add("hidden");
    try {
      await refreshKnowledge();
    } catch (e) {
      toast("写进去了,但列表没刷新成:" + e.message, true);
    }
    refresh();                 // 侧栏/文件树跟着刷新(原来调的 refreshSidebar 根本不存在)
  } catch (e) { toast(e.message, true); }
}

async function refreshKnowledge() {
  const box = $("know-list");
  if (!DATA.root) { box.textContent = "先打开一本书。"; return; }
  box.textContent = "读列表…";
  try {
    const d = await jreq("GET", `/api/knowledge?root=${encodeURIComponent(DATA.root)}`);
    const docs = (d && d.知识) || [];
    if (!docs.length) {
      box.innerHTML = `<div class="hint">还没有知识条目。问出第一条,点「写进知识库」并确认就会出现在 `
        + `<code>外置大脑/知识/</code>。</div>`;
      return;
    }
    box.innerHTML = `<div class="hint">已入账 ${docs.length} 条;反向索引 ${d.索引行数 || 0} 行`
      + `(大纲师/设定师每章都会读到它)。</div>` + docs.map((x) =>
        `<button class="mini know-item" data-rel="${escHtml(x.文件)}">${escHtml(x.主题)}</button>`).join(" ");
    box.querySelectorAll(".know-item").forEach((b) => {
      b.onclick = () => openFile(b.dataset.rel, false, null, null);
    });
  } catch (e) { box.textContent = "列不出来:" + e.message; }
}
// 点遮罩空白处关闭(两个新面板);正在花钱时不关
["know-overlay", "align-overlay"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("mousedown", (ev) => {
    if (ev.target !== el) return;
    if (id === "know-overlay" && KNOW_BUSY) return;
    if (id === "align-overlay" && ALIGN_BUSY) return;
    el.classList.add("hidden");
  });
});

// ---------------- 裁定 2:搜索服务 + 抓网页(没配就明说没配) ----------------
async function refreshWebStatus() {
  const box = $("web-status");
  if (!box) return;
  if (!DATA.root) { box.textContent = "先打开一本书(搜索服务是每本书的 loom.toml 里配的)。"; return; }
  try {
    const r = await jreq("GET", `/api/websrc?root=${encodeURIComponent(DATA.root)}`);
    if (r.配置了) {
      box.textContent = `搜索服务已配:${r.engine}${r.key有值 ? "" : "(key 环境变量没值,可能被拒)"}。`
        + "搜来的内容未经核实,引用时记得带网址。";
      box.classList.remove("warn");
    } else {
      box.textContent = (r.为什么 || "搜索服务没配。") + " 也可以直接把搜索结果贴进上面的框。";
      box.classList.add("warn");
    }
  } catch (e) {
    box.textContent = "读不到搜索配置:" + e.message;
  }
}

function _appendWeb(text) {
  const box = $("know-web");
  box.value = (box.value ? box.value.trim() + "\n\n" : "") + text;
}

async function webSearch() {
  const q = ($("web-q").value || "").trim();
  if (!q) return toast("先写要搜什么。", true);
  const btn = $("web-search");
  btn.disabled = true;
  btn.textContent = "搜着…";
  try {
    const r = await jreq("POST", "/api/websrc/search", { root: DATA.root, query: q });
    // HTTP 层的失败(400 not_configured / 502)会在 jreq 里抛,走下面的 catch;
    // 这条只防「回 200 但业务上没成」的那种形状。
    if (r.ok === false) { toast(r.reason || "没搜成。", true); refreshWebStatus(); return; }
    if (!r.条数) { toast("没搜到结果。"); return; }
    const lines = r.结果.map((it) => `- ${it.标题 || "(无标题)"}\n  ${it.网址}\n  ${it.摘要 || ""}`);
    _appendWeb(`【联网 · 搜「${q}」】\n${lines.join("\n")}`);
    toast(`搜到 ${r.条数} 条,已贴进上面的框。`);
  } catch (e) {
    // 没配搜索服务时后端会把「怎么配」一起回过来(e.body.怎么配);以前这里只显示 e.message。
    const _how = (e.body && (e.body["怎么配"] || e.body.hint)) || "";
    toast("没搜成:" + e.message + (_how ? "\n\n" + _how : ""), true);
  } finally {
    btn.disabled = false;
    btn.textContent = "搜一下(联网)";
  }
}

async function webFetch() {
  const url = ($("web-url").value || "").trim();
  if (!url) return toast("先贴一个网址。", true);
  const btn = $("web-fetch");
  btn.disabled = true;
  btn.textContent = "抓着…";
  try {
    const r = await jreq("POST", "/api/websrc/fetch", { root: DATA.root, url: url });
    if (r.ok === false) { toast(r.reason || "没抓成。", true); return; }   // 同上:HTTP 失败走 catch
    _appendWeb(`【联网 · 抓网页】${r.标题 || ""}\n${r.网址}\n${r.正文}${r.截断 ? "\n…(后略)" : ""}`);
    toast(`抓到 ${r.字符数} 字,已贴进上面的框。`);
  } catch (e) {
    toast("没抓成:" + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "抓这一页";
  }
}

// ---------------- 裁定 3:领航员·大纲符合度复核(只出意见) ----------------
let ALIGN_REPORT = null;
let ALIGN_ROOT = null;      // 这份复核属于哪本书(换书后不许存)

function openAlign() {
  $("align-overlay").classList.remove("hidden");
  // 上一份复核不许跟着进来:旧表还在、旧 ALIGN_REPORT 还在,能重复存档,甚至存到别的书上。
  ALIGN_REPORT = null;
  ALIGN_ROOT = null;
  $("align-accept").classList.add("hidden");
  $("align-out").innerHTML = "";
  const box = $("align-chapter");
  if (box && !box.value) box.value = String((CUR && CUR.chapter) || 1);
  if ($("align-list").textContent === "还没打开。") refreshAlign();
}

async function runAlign() {
  if (!DATA.root) return toast("先打开一本书。", true);
  const ch = parseInt($("align-chapter").value, 10) || 1;
  $("align-go").disabled = true;
  ALIGN_BUSY = true;
  const _root = DATA.root;      // 同理:await 回来要核是不是还是这本书
  $("align-accept").classList.add("hidden");
  $("align-out").innerHTML = '<p class="hint">正在逐章对照…(这一步会花钱,别急着点第二次)</p>';
  try {
    const r = await jreq("POST", "/api/align/review", { root: _root, chapter: ch });
    if (DATA.root !== _root) {
      $("align-out").innerHTML = "";
      toast("这中间换了书,上一份复核没贴过来。", true);
      return;
    }
    if (r.ok === false) { $("align-out").innerHTML = ""; return toast(r.reason || "没得出结果。", true); }
    ALIGN_REPORT = r;
    ALIGN_ROOT = _root;      // 这份复核是哪本书算的 —— 存档只许存回这本书
    renderAlign(r);
    if ((r.偏离章 || []).length) toast(`有 ${r.偏离章.length} 章偏离,逐条看。`);
    else toast("这一块都符合。");
  } catch (e) {
    $("align-out").innerHTML = "";
    toast("没复核成:" + e.message, true);
  } finally {
    ALIGN_BUSY = false;
    $("align-go").disabled = false;
  }
}

function renderAlign(r) {
  const box = $("align-out");
  box.innerHTML = "";
  const head = document.createElement("p");
  head.className = "hint";
  head.textContent = `第 ${r.起}-${r.止} 章,对照 ${r.对照章数} 章,偏离 ${(r.偏离章 || []).length} 章。`
    + "这些只是意见:改章纲还是改正文由你定。";
  box.appendChild(head);
  const tb = document.createElement("table");
  tb.className = "align-table";
  tb.innerHTML = "<thead><tr><th>章</th><th>判断</th><th>理由</th><th>建议</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const v of (r.逐章 || [])) {
    const tr = document.createElement("tr");
    for (const cell of [v.章, v.判断, v.理由 || "", v.建议 || ""]) {
      const td = document.createElement("td");
      td.textContent = String(cell);
      tr.appendChild(td);
    }
    if (v.判断 === "偏离") tr.className = "align-off";
    tbody.appendChild(tr);
  }
  tb.appendChild(tbody);
  box.appendChild(tb);
  if ((r.没读懂的行 || []).length) {
    const bad = document.createElement("p");
    bad.className = "hint";
    bad.textContent = "模型有几句我没读懂(没算进上表):" + r.没读懂的行.join(";");
    box.appendChild(bad);
  }
  $("align-accept").classList.remove("hidden");
}

async function acceptAlign() {
  if (!ALIGN_REPORT) return;
  if (ALIGN_ROOT && DATA.root !== ALIGN_ROOT) {
    toast("这份复核是另一本书算出来的,不能存到这本书里。", true);   // 换过书就拒
    return;
  }
  if (!window.confirm("把这份复核意见存成文档?只写「外置大脑/复核/」,不动章纲也不动正文。")) return;
  try {
    const r = await jreq("POST", "/api/align/accept",
      { root: ALIGN_ROOT || DATA.root, report: ALIGN_REPORT });
    if (!r.ok) { toast(r.reason || "没存成。", true); return; }
    toast("已存:" + r.文件);
    $("align-accept").classList.add("hidden");   // 存过就收起来,免得同一份重复存
    ALIGN_REPORT = null;
    refreshAlign();
  } catch (e) {
    toast("没存成:" + e.message, true);
  }
}

async function refreshAlign() {
  if (!DATA.root) return;
  const box = $("align-list");
  box.textContent = "读列表…";
  try {
    const r = await jreq("GET", `/api/align?root=${encodeURIComponent(DATA.root)}`);
    box.innerHTML = "";
    if (!r.条数) { box.textContent = "还没有存过复核文档。"; return; }
    const ul = document.createElement("ul");
    for (const f of r.文件) {
      const li = document.createElement("li");
      li.textContent = f;
      ul.appendChild(li);
    }
    box.appendChild(ul);
  } catch (e) {
    box.textContent = "读不到已有复核:" + e.message;
  }
}

// ---------------------------------------------------------------- 文风档案(文档库)
// 2026-10-20 作者裁定:这里放的是**别人的文章**;作者自己的字走「喂样本」(《写作指纹》)。
// 每篇各自蒸一份(库里累积多份)→ 后端把它合并成派生的《档案.md》→ 注入链照旧只读那一份。
let _styleLib = "主档案";   // 当前在看的档案(可切换,2026-10-20 需求确认支持多档案)
let _styleDocs = [];         // [{标题,字数,已蒸馏,蒸馏于,细致度}]
let _styleSel = new Set();   // 勾选的标题

async function openStylePanel() {
  if (!DATA.root) { toast("先打开一本书。", true); return; }
  $("style-error").textContent = "";
  $("style-overlay").classList.remove("hidden");
  if (!(await _ensureBook())) {
    _styleBlock("这本书的<b>文件夹不在了</b>(被移动或删除),文档库没法读写。"
      + "请回首页重新导入,或打开别的书。");
    return;
  }
  styleRefresh();
  pollGuides("style");   // 批C:文风档案空时的语料分析师引导(后端按档案状态判)
}
function closeStylePanel() { $("style-overlay").classList.add("hidden"); }

// 书不可用时的整块提示:把列表区换成一句人话,并把会写盘的键全禁掉。msg 是 HTML。
function _styleBlock(msg) {
  const box = $("style-list");
  if (box) box.innerHTML = `<div class="style-blocked">${msg}</div>`;
  _styleDocs = []; _styleSel = new Set();
  _styleSetEnabled(false);
  if ($("style-lib-sum")) $("style-lib-sum").textContent = "";
  if ($("style-using")) $("style-using").textContent = "";
}

// 逐个开关那些会写盘的键(禁用时保留「刷新」可用,好让书恢复后能自己重试)
function _styleSetEnabled(on) {
  ["style-add", "style-import", "style-distill", "style-del", "style-use", "style-lib-add"]
    .forEach((id) => { if ($(id)) $(id).disabled = !on; });
  if (on) updateStyleBtn();      // 恢复后按当前勾选数重算「蒸馏选中的 N 篇」
}

function _styleSelArr() { return Array.from(_styleSel); }

function updateStyleBtn() {
  const n = _styleSel.size;
  const btn = $("style-distill");
  if (btn) { btn.textContent = `蒸馏选中的 ${n} 篇(${n} 次调用)`; btn.disabled = n === 0; }
  const del = $("style-del");
  if (del) del.disabled = n === 0;
  const all = $("style-sel-all");
  if (all) all.checked = _styleDocs.length > 0 && n === _styleDocs.length;
}

function paintStyleList() {
  const box = $("style-list");
  box.textContent = "";
  if (!_styleDocs.length) {
    box.textContent = "库里还没有文档。①里粘贴一段加进来,或在②里填文件路径点「导入」(桌面版可直接「浏览…」)。";
    updateStyleBtn();
    return;
  }
  _styleDocs.forEach((doc) => {
    const row = document.createElement("div");
    row.className = "style-doc" + (doc["已蒸馏"] ? " done" : "");
    /*2026-11-03（作者五点反馈⑤）：文档行两行化——标题行(勾+标题+删除)+元信息行
      (字数·蒸馏状态全宽,不再截断"没…3359字·已蒸…中")。 */
    const top = document.createElement("div");
    top.className = "sd-top";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = _styleSel.has(doc["标题"]);
    cb.onchange = () => {
      if (cb.checked) _styleSel.add(doc["标题"]); else _styleSel.delete(doc["标题"]);
      updateStyleBtn();
    };
    const t = document.createElement("span");
    t.className = "sd-title";
    t.textContent = doc["标题"];
    t.title = doc["标题"];
    const del = document.createElement("button");
    del.className = "sd-del";
    del.textContent = "×";
    del.title = "从库里删掉这一篇(不花钱)";
    del.onclick = () => styleDeleteTitles([doc["标题"]]);
    top.append(cb, t, del);
    const meta = document.createElement("div");
    meta.className = "sd-meta-full";
    meta.textContent = `${doc["字数"]} 字 · ` + (doc["已蒸馏"]
      ? `已蒸馏(${doc["蒸馏于"] || "?"}${doc["细致度"] ? " · 细致度:" + doc["细致度"] : ""})` : "未蒸馏");
    row.append(top, meta);
    box.append(row);
  });
  updateStyleBtn();
}

// 档案下拉:列出这本书已有的几套 + 当前这套(哪怕还没落盘也要能选)
function paintStyleLibs(packs) {
  const sel = $("style-lib");
  if (!sel) return;
  const names = (packs || []).map((p) => p["名字"]);
  if (!names.includes(_styleLib)) names.unshift(_styleLib);
  sel.innerHTML = "";
  names.forEach((n) => {
    const o = document.createElement("option");
    o.value = n;
    o.textContent = n;
    sel.append(o);
  });
  sel.value = _styleLib;
}

async function styleLibAdd() {
  const n = ($("style-lib-new").value || "").trim();
  if (!n) { toast("先给新档案起个名字。", true); return; }
  if (/[\\/:*?"<>|]/.test(n)) { toast("名字里不能有 \\ / : * ? \" < > | 这些字符。", true); return; }
  _styleLib = n;                       // 不用先落盘:加第一篇文档时后端会自动建目录
  $("style-lib-new").value = "";
  _styleSel = new Set();
  toast(`已切到「${n}」。往里加文档、蒸它,就是这套的规则。`);
  await styleRefresh();
}

async function styleRefresh() {
  const box = $("style-list");
  box.textContent = "读列表…";
  try {
    const d = await jreq("GET",
      `/api/style/packs?root=${encodeURIComponent(DATA.root)}&name=${encodeURIComponent(_styleLib)}`);
    paintStyleLibs(d && d["档案"]);
    const lib = (d && d["库"]) || {};
    _styleDocs = lib["文档"] || [];
    const alive = new Set(_styleDocs.map((x) => x["标题"]));
    _styleSel = new Set(_styleSelArr().filter((t) => alive.has(t)));   // 删掉的自动出列
    const using = (d && d["在用"]) || {};
    if (using["强度"]) $("style-strength").value = using["强度"];
    $("style-using").textContent = using["档案"]
      ? `正在用「${using["档案"]}」· 注入量 ${using["强度"]}`
      : "还没选定用哪份——蒸好之后点「用这份」才会进提示词。";
    $("style-lib-sum").textContent = `本档案 ${lib["篇数"] || 0} 篇 · 已蒸 ${lib["已蒸馏"] || 0} 篇`;
    _styleSetEnabled(true);      // 书回来了就恢复可点(上一次可能被 _styleBlock 禁过)
    paintStyleList();
  } catch (e) {
    // 「不是 loom 项目」是最常见的一种:书目录被挪走了。别把后端原话糊在列表里。
    const msg = String(e.message || e);
    if (msg.includes("不是 loom 项目") || msg.includes("not_a_book")) {
      _styleBlock("这本书的<b>文件夹不在了</b>(被移动或删除),文档库没法读写。"
        + "请回首页重新导入,或打开别的书。");
    } else {
      box.textContent = "读列表失败:" + msg;
    }
  }
}

async function styleAdd() {
  const title = $("style-title").value.trim();
  const text = $("style-text").value;
  if (!text.trim()) { toast("先把文字贴进去。", true); return; }
  try {
    const d = await jreq("POST", "/api/style/raw", {
      root: DATA.root, name: _styleLib,
      filename: title || `粘贴-${new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "")}`,
      text,
    });
    if (d && d.ok === false) { toast(d.reason || d.error || "没加进去。", true); return; }
    $("style-text").value = ""; $("style-title").value = "";
    toast("已加进文档库(原样留档,没改一个字)。");
    await styleRefresh();
  } catch (e) { toast(e.message, true); }
}

async function styleImport() {
  const raw = $("style-paths").value.trim();
  if (!raw) { toast("先填文件路径,或点「浏览…」。", true); return; }
  await styleImportPaths(raw.split(";").map((s) => s.trim()).filter(Boolean));
}

async function styleImportPaths(paths) {
  if (!paths || !paths.length) return;
  try {
    const d = await jreq("POST", "/api/style/import", { root: DATA.root, name: _styleLib, paths });
    if (d && d.ok === false) { toast(d.reason || d.error || "没导进来。", true); return; }
    const okN = (d["导入"] || []).length, skip = d["跳过"] || [];
    $("style-import-note").textContent = `导入 ${okN} 篇` + (skip.length
      ? `;跳过 ${skip.length} 个:` + skip.map((x) => `${x["文件"]}(${x["原因"]})`).join("、") : "");
    $("style-paths").value = "";
    toast(`导进文档库 ${okN} 篇(原件一个字没动)。`);
    await styleRefresh();
  } catch (e) { toast(e.message, true); }
}

async function styleBrowse() {
  if (!_hasNativePicker()) {
    // 浏览器:拿不到绝对路径 ⇒ 聚焦输入框 + 说清怎么填,别让人对着一个"点了没反应"的按钮
    $("style-paths").focus();
    toast("浏览器里选不了文件(拿不到完整路径)——把文件路径粘到旁边输入框,多个用 ; 隔开,再点「导入」。");
    return;
  }
  try {
    const res = await window.pywebview.api.pick_files();
    const list = Array.isArray(res) ? res : (res ? [res] : []);
    if (!list.length) return;
    await styleImportPaths(list);
  } catch (e) { toast("调系统文件框失败:" + e.message, true); }
}

async function styleDeleteTitles(titles) {
  if (!titles || !titles.length) return;
  if (!window.confirm(`从库里删掉 ${titles.length} 篇?\n原文与这几篇的蒸馏产物会一起删;合并档案会自动重算。\n(不花钱)`)) return;
  try {
    for (const t of titles) {
      const d = await jreq("POST", "/api/style/delete", { root: DATA.root, name: _styleLib, title: t });
      if (d && d.ok === false) { toast(d.reason || d.error || `删「${t}」没成功。`, true); return; }
    }
    titles.forEach((t) => _styleSel.delete(t));
    toast(`删掉 ${titles.length} 篇。`);
    await styleRefresh();
  } catch (e) { toast(e.message, true); }
}

async function styleDistillSelected() {
  const titles = _styleSelArr();
  if (!titles.length) { toast("先勾选要蒸的文档。", true); return; }
  const again = _styleDocs.filter((d) => _styleSel.has(d["标题"]) && d["已蒸馏"]).map((d) => d["标题"]);
  const detail = $("style-detail").value;
  let msg = `把选中的 ${titles.length} 篇各蒸一份规则?\n会调 ${titles.length} 次模型(花钱)。细致度:${detail}。`;
  if (again.length) {
    msg += `\n\n其中 ${again.length} 篇以前蒸过,这次会覆盖它们旧的蒸馏产物:\n· ${again.join("\n· ")}`;
  }
  if (!window.confirm(msg + "\n\n继续吗?")) return;
  const btn = $("style-distill");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "蒸馏中…";
  $("style-error").textContent = "";
  try {
    const d = await jreq("POST", "/api/style/distill",
      { root: DATA.root, name: _styleLib, docs: titles, detail });
    const okN = d["成功数"] || 0, fail = d["失败"] || [];
    if (fail.length) {
      $("style-error").textContent = `有 ${fail.length} 篇没蒸成:`
        + fail.map((x) => `${x["标题"]}(${x["原因"]})`).join(";");
    }
    toast(`蒸好 ${okN} 篇${fail.length ? `,${fail.length} 篇失败` : ""}。`);
    await styleRefresh();
  } catch (e) {
    $("style-error").textContent = e.message;
    toast(e.message, true);
  } finally { btn.textContent = old; updateStyleBtn(); }
}

async function styleUse() {
  const strength = $("style-strength").value;
  try {
    const d = await jreq("POST", "/api/style/pointer", { root: DATA.root, name: _styleLib, strength });
    if (d && d.ok === false) { toast(d.reason || d.error || "没设上。", true); return; }
    toast(`以后出稿用这份文风,注入量 ${strength}。`);
    await styleRefresh();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- R18 快照
function openSnapPanel() {
  if (!DATA.root) { toast("先打开一本书。", true); return; }
  $("snap-overlay").classList.remove("hidden");
  snapRefresh();
}
function closeSnapPanel() { $("snap-overlay").classList.add("hidden"); }

async function snapRefresh() {
  const box = $("snap-list");
  box.textContent = "在读…";
  try {
    const d = await jreq("GET", `/api/snapshot/list?root=${encodeURIComponent(DATA.root)}`);
    const rows = (d && d["快照"]) || [];
    box.textContent = "";
    if (!rows.length) { box.textContent = "还没拍过快照。先点「拍一张(不花钱)」留个底。"; return; }
    rows.forEach((s) => {
      const row = document.createElement("div");
      row.className = "snap-row";
      const name = document.createElement("span");
      name.className = "snap-name";
      name.textContent = s["名称"];
      const meta = document.createElement("span");
      meta.className = "snap-meta";
      meta.textContent = `${s["时间"]} · ${s["文件数"]} 个文件 · ${Math.round((s["字节"] || 0) / 1024)} KB`;
      const verify = document.createElement("button");
      verify.className = "ghost";
      verify.textContent = "校验";
      verify.onclick = () => snapVerify(s["名称"]);
      const back = document.createElement("button");
      back.className = "ghost";
      back.textContent = "恢复";
      back.onclick = () => snapRestore(s["名称"]);
      row.append(name, meta, verify, back);
      box.append(row);
    });
  } catch (e) { box.textContent = e.message; }
}

async function snapMake() {
  $("snap-make").disabled = true;
  try {
    const d = await jreq("POST", "/api/snapshot/make", { root: DATA.root });
    if (d && d.ok) toast(d["说明"] || "拍好了。");
    else toast((d && (d["说明"] || d.error)) || "没拍成。", true);
  } catch (e) { toast(e.message, true); }
  finally { $("snap-make").disabled = false; snapRefresh(); }
}

async function snapVerify(name) {
  try {
    const d = await jreq("GET", `/api/snapshot/verify?root=${encodeURIComponent(DATA.root)}&name=${encodeURIComponent(name)}`);
    if (d.ok) toast(`和这一版一致(${d["一致"]} 个文件)。`);
    else toast(`不一致:缺 ${(d["缺"] || []).length} 个 / 改了 ${(d["不同"] || []).length} 个。`, true);
  } catch (e) { toast(e.message, true); }
}

async function snapRestore(name) {
  // R19「需输入确认」:全库回退是破坏性操作,照着名字输才作数
  const typed = window.prompt(`要把整本书恢复成「${name}」吗?\n这么做的后果:盘上这些文件会被快照里的版本盖回去(快照里没有的文件不动)。\n确认请照着输入快照名:`, "");
  if (typed == null) return;
  try {
    const d = await jreq("POST", "/api/snapshot/restore", { root: DATA.root, name, typed });
    if (d && d.ok) {
      toast(d["说明"] || "恢复好了。");
      closeSnapPanel();
      await refresh();        // 盘上东西变了,左边树和当前文件都得重读
    } else {
      toast((d && (d["说明"] || d.error)) || "没恢复。", true);
    }
  } catch (e) { toast(e.message, true); }
}

function openHalfPanel() {
  HALF_QUOTE = null;
  // 手输一整条路径很烦:记住上次用的那份,下次直接在这儿
  try {
    const last = localStorage.getItem("loom_half_path");
    if (last && !$("half-path").value.trim()) $("half-path").value = last;
  } catch (e) { /* 存不了就算了,不影响导稿 */ }
  $("half-preview").textContent = "还没切章。先点「先看看(不花钱)」。";
  $("half-run").disabled = true;
  $("half-run").textContent = "确实开始(会花钱)";
  $("half-stop").classList.add("hidden");
  $("half-close").disabled = false;
  $("half-close").textContent = "关闭";
  $("half-overlay").classList.remove("hidden");
  // 若刚才被 Esc 关掉时任务还在跑:重开后必须把「停」还回来,否则没有停止入口
  if (HALF_BUSY) {
    $("half-stop").classList.remove("hidden");
    $("half-close").disabled = true;
    $("half-close").textContent = "正在抽(先停再关)";
    $("half-preview").textContent = "这个任务还在跑(在花钱)。点「停」可以停下,已抽完的不会重抽。";
  }
}

async function halfPreview() {
  const path = $("half-path").value.trim();
  if (!path) { $("half-preview").textContent = "先填稿子的路径。"; return; }
  $("half-preview").textContent = "正在切章(不花钱)…";
  let d;
  try {
    d = await jreq("POST", "/api/import/preview", { path });
    try { localStorage.setItem("loom_half_path", path); } catch (e) { /* 同上 */ }
  } catch (e) {
    $("half-preview").textContent = "切章失败:" + (e.message || e);
    return;
  }
  try {
    const 缺 = (d.缺号 && d.缺号.length) ? `缺号:${d.缺号.join(",")}` : "章号连续";
    $("half-preview").textContent =
      `切法:${d.切法} · 认出 ${d.章数} 章(共 ${d.总字数} 字) · ${缺}`;
    // 接着报账:让「会花几次」在按下去之前就摆在眼前
    const mode = $("half-mode").value;
    const recent = Math.max(1, parseInt($("half-recent").value || "30", 10));
    const maxCh0 = Math.max(1, parseInt($("half-max").value || "200", 10));
    const q = await jreq("POST", "/api/import/run",
      { root: DATA.root, path, mode, recent, step: 10, max_chapters: maxCh0 });
    HALF_QUOTE = q;
    // 报账按上限截一次:报 500 章、真跑只抽 200 章,人会以为被吞了钱。
    const 预计 = Math.min(q.预计调用次数, maxCh0);
    const 目录 = (q.疑似目录 && q.疑似目录.length) ? ` · 疑似目录(不抽):${q.疑似目录.join(",")}` : "";
    // 裁定 2026-10-19:楔子/序章单独成第 0 章,这里如实报出来,别让人以为被吞了
    const 楔子 = q.楔子 ? ` · 另有${q.楔子.标题}(${q.楔子.字数} 字,单独成第 0 章)` : "";
    $("half-preview").textContent =
      `切法:${q.切法} · 认出 ${q.总章数} 章 · ${缺}${目录}${楔子}\n` +
      `按「${q.抽法}」会抽 ${Math.min(q.要抽的章数, maxCh0)} 章 = ${预计} 次调用(现在还没花钱)` +
      (q.要抽的章数 > maxCh0 ? `\n注意:还差 ${q.要抽的章数 - maxCh0} 章超过单次上限,这次先抽前 ${maxCh0} 章,再点一次接着抽` : "");
    $("half-run").disabled = q.要抽的章数 === 0;
    $("half-run").textContent = q.要抽的章数
      ? `确实开始:抽 ${Math.min(q.要抽的章数, maxCh0)} 章(${预计} 次调用)`
      : "没有要抽的章";
  } catch (e) {
    $("half-preview").textContent = "报账失败(还没花钱):" + (e.message || e);
  }
}

let HALF_BUSY = false;

function invalidateHalfQuote() {
  if (HALF_BUSY) return;                 // 跑着的时候不动它:按钮正被占用
  if (!HALF_QUOTE) return;
  HALF_QUOTE = null;
  $("half-run").disabled = true;
  $("half-run").textContent = "确实开始(会花钱)";
  $("half-preview").textContent =
    "参数改过了:上面那个价是按旧参数报的,不作数。再点「先看看(不花钱)」重新报一次。";
}

async function halfRun() {
  if (!HALF_QUOTE) { toast("先点「先看看(不花钱)」,看过切章结果再花钱", true); return; }
  const path = $("half-path").value.trim();
  const mode = $("half-mode").value;
  const recent = Math.max(1, parseInt($("half-recent").value || "30", 10));
  const maxCh = Math.max(1, parseInt($("half-max").value || "200", 10));
  const want = Math.min(HALF_QUOTE.要抽的章数, maxCh);
  HALF_BUSY = true;
  HALF_QUOTE = null;                     // 这一次已经把报价用掉了:别再让同一份价重复花
  $("half-run").disabled = true;
  $("half-stop").classList.remove("hidden");
  $("half-close").disabled = true;      // 跑着的时候不让关:关了「停」就找不到了
  $("half-close").textContent = "正在抽(先停再关)";
  $("half-preview").textContent =
    `正在抽:最多 ${want} 章…(可以点「停」,已抽完的不会重抽)`;
  try {
    const d = await jreq("POST", "/api/import/run", {
      root: DATA.root, path, mode, recent, step: 10, confirm: true, max_chapters: maxCh });
    const 正文 = (d.正文 && d.正文.说明) ? ` · ${d.正文.说明}` : "";
    const 没读 = (d.未抽出 && d.未抽出.length) ? ` · ${d.未抽出.length} 章没读出来` : "";
    // 「抽完」在停/截断/漏章时都是假话,而这句是用户决定要不要再花一次钱的唯一依据。
    let 开头 = "抽完了:";
    if (d.停下原因 && d.停下原因.indexOf("停") >= 0) 开头 = "按你说的停了:";
    else if (d.停下原因 && d.停下原因.indexOf("上限") >= 0) 开头 = "到单次上限,先停在这:";
    else if (没读) 开头 = "抽了一部分,有章没读出来:";
    $("half-preview").textContent =
      `${开头}${d.抽了几章}/${d.要抽的章数} 章(${d.调用次数} 次调用)${正文}${没读}\n` +
      (d.停下原因 ? `${d.停下原因}\n` : "") +
      `没抽出来的章下次再点会接着抽(已抽完的不会重抽)。` +
      // 裁定 2026-10-19:本卷剩余章纲不自动铺,只报数字 + 入口
      (d.提示 ? `\n${d.提示}` : "");
    toast(开头.replace(":", "") + ` ${d.抽了几章} 章`, !!没读 || !!d.停下原因);
  } catch (e) {
    // 502 = 一章都没抽出来(常见:没配 key);正文可能已经落好了,别让人以为白导一次
    const b = e.body || {};
    const 正文2 = (b.正文 && b.正文.说明) ? ` · ${b.正文.说明}` : "";
    $("half-preview").textContent = "抽取失败:" + (e.message || e) + 正文2 +
      "\n已抽完的章不会重抽,处理完再点一次就能接着来。";
    toast("抽取失败:" + (e.message || e), true);
  } finally {
    HALF_BUSY = false;                     // 跑完放开:不然以后改参数都不作废报价了
    $("half-run").disabled = false;
  // 报价开跑时已清掉,按钮上的「抽 N 章(M 次调用)」必须一起回位(P2-7)
  $("half-run").textContent = "确实开始(会花钱)";
    $("half-stop").classList.add("hidden");
    $("half-close").disabled = false;
    $("half-close").textContent = "关闭";
    HALF_QUOTE = null;
    refresh();
    // 章纲已经改到盘上了:正开着卡章纲的话要重载,否则人看到的是旧内容("什么都没变")
    await reloadIfOpen("外置大脑/卡章纲.md");
  }
}

// 当前正在看的文件正好被后台改过 → 重载它(外置大脑默认是预览态,重载后要重渲)
async function reloadIfOpen(rel) {
  if (!CUR || CUR.rel !== rel || !DATA.root) return;
  try {
    const fresh = await jreq("GET", "/api/file?root=" + encodeURIComponent(DATA.root) +
                         "&rel=" + encodeURIComponent(rel));
    $("editor").value = fresh.content;
    if (CUR.hash !== undefined) CUR.hash = fresh.hash || "";
    if (CUR.preview) setPreview(true);
    clearDirty();
  } catch (e) {
    toast("改完了,但重载这个文件失败,请手动点一下它:" + (e.message || e), true);
  }
}

// 「归纳卷纲 / 归纳全书大纲」:点一下先报账(不花钱),再点一下才真跑。
let VOL_QUOTE = null, BOOK_QUOTE = null;

async function volSummary() {
  const btn = $("btn-vol-summary");
  // 报价那一步必须先 return 出去,**不经过下面那个 finally** ——
  // 以前 finally 无条件清 VOL_QUOTE 并把按钮写回原文,于是永远停在「再点一次」(交叉审查 P1-1)。
  if (!VOL_QUOTE) {
    try {
      const q = await jreq("POST", "/api/volplan/volumes", { root: DATA.root });
      VOL_QUOTE = q;
      btn.textContent = "确认:归纳卷纲(会花钱)";
      toast(q.说明 || "报账好了,再点一次才真跑", false);
    } catch (e) {
      toast("归纳卷纲失败:" + (e.message || e), true);
    }
    return;
  }
  try {
    btn.disabled = true;
    const d = await jreq("POST", "/api/volplan/volumes", { root: DATA.root, confirm: true });
    const 跳过 = d.跳过卷数 ? `,跳过 ${d.跳过卷数} 卷(没改动的不算)` : "";
    toast(`归纳了 ${d.归纳了几卷} 卷(${d.调用次数} 次调用)${跳过}`, !d.归纳了几卷);
    await reloadIfOpen("外置大脑/卷纲.md");
  } catch (e) {
    toast("归纳卷纲失败:" + (e.message || e), true);
  } finally {
    btn.disabled = false;
    btn.textContent = "归纳卷纲";
    VOL_QUOTE = null;
  }
}

async function bookSummary() {
  const btn = $("btn-book-summary");
  if (!BOOK_QUOTE) {          // 同 volSummary:报价这一步不经过 finally(交叉审查 P1-1)
    try {
      const q = await jreq("POST", "/api/volplan/overview", { root: DATA.root });
      BOOK_QUOTE = q;
      btn.textContent = "确认:归纳全书大纲(1 次调用)";
      toast(q.说明 || "报账好了,再点一次才真跑", false);
    } catch (e) {
      toast("归纳全书大纲失败:" + (e.message || e), true);
    }
    return;
  }
  try {
    btn.disabled = true;
    const d = await jreq("POST", "/api/volplan/overview", { root: DATA.root, confirm: true });
    if (d.ok) {
      toast("全书主线草稿写好了(在全书大纲里,标题带「待你确认」)", false);
      await reloadIfOpen("外置大脑/全书大纲.md");
    } else {
      toast(d.说明 || "这一轮没写成", true);
    }
  } catch (e) {
    toast("归纳全书大纲失败:" + (e.message || e), true);
  } finally {
    btn.disabled = false;
    btn.textContent = "归纳全书大纲";
    BOOK_QUOTE = null;
  }
}

async function halfStop() {
  try {
    const d = await jreq("POST", "/api/import/cancel", { root: DATA.root });
    $("half-preview").textContent = (d.说明 || "已叫停");
  } catch (e) {
    toast("叫停失败:" + (e.message || e), true);
  }
}

// 「这些章纲是 AI 写的、还没人确认」:横幅 + 一键确认(只清标记,不动内容)
async function refreshPending() {
  const el = $("pending-banner");
  if (!el || !DATA.root) return;
  try {
    const d = await jreq("GET", "/api/pending?root=" + encodeURIComponent(DATA.root));
    const keys = Object.keys(d.待确认 || {});
    if (!keys.length) { el.classList.add("hidden"); el.textContent = ""; return; }
    const parts = keys.map((k) => {
      const n = (d.待确认[k].章 || []).length;
      return k + (n ? ` 第${d.待确认[k].章[0]}-${d.待确认[k].章[n - 1]}章` : "");
    });
    el.classList.remove("hidden");
    el.textContent = `AI 写的 ${parts.join(" · ")},还没人过目 —— 点这里确认已看过`;
    el.title = "确认后只清这条提醒,不修改任何章纲内容";
    el.onclick = async () => {
      await jreq("POST", "/api/pending/confirm", { root: DATA.root });
      refreshPending();
    };
  } catch (e) {
    el.classList.add("hidden");
  }
}

async function importHalfDraft() {
  openHalfPanel();
}

async function expandCardOutline() {
  let cov;
  try { cov = await jreq("GET", `/api/card/coverage?root=${encodeURIComponent(DATA.root)}`); }
  catch (e) { toast(`查不到铺到哪了:${e.message}`, true); return; }
  const rows = (cov && cov.各卷) || [];
  if (!rows.length) { toast("卷纲里还没有「- 起止:第a-b章」,先写卷纲", true); return; }
  const brief = rows.map((r) => r.问题 ? `${r.卷}:${r.问题}` : `${r.卷} 已铺 ${r.已铺}/${r.章数}`).join(";");
  // 折卷警告:卷纲只填了起止、没填卷名 → 引擎认为没分卷,千章的卡章纲会整份进模型。必须先说。
  const fold = (cov && cov.折卷) || {};
  const warn = (fold.警告 && fold.警告.length) ? `\n\n⚠ ${fold.警告.join(" ")}` : "";
  if (!window.confirm(`当前进度:${brief}${warn}\n\n下一步铺一块 50 章,会调用一次模型(花钱)。开始了就等它跑完。\n\n开始吗?`)) return;
  toast("正在铺这一块(一次模型调用)…");
  try {
    const r = await jreq("POST", "/api/card/expand", { root: DATA.root, size: 50 });
    if (r && r.ok === false) { toast(r.reason || r.error || "没铺成", true); return; }
    const n = (r && r.下一块) || {};
    toast(`铺完第 ${r.这块.起}–${r.这块.止} 章` + (n.起 ? `;下一块从第 ${n.起} 章开始` : ";卷内已铺满"));
    if (CUR && CUR.rel === "外置大脑/卡章纲.md") {
      const fresh = await jreq("GET", `/api/file?root=${encodeURIComponent(DATA.root)}&rel=${encodeURIComponent(CUR.rel)}`);
      $("editor").value = fresh.content;
      if (CUR.preview) setPreview(true);   // UI-P0-3:外置大脑默认是预览模式,不重渲就等于没刷新
    }
  } catch (e) { toast(`没铺成:${e.message}`, true); }
}

function brainCopyKind(rel) {
  if (rel === "外置大脑/违禁词.md") return "违禁词";
  if (rel === "外置大脑/文风参考.md") return "文风参考";
  return null;
}

let _brainCopyOpen = false;   // 2026-11-05（⑧顺手做掉）：防重入——await 间隙连点会叠两个浮窗
async function openBrainCopy() {
  if (_brainCopyOpen) return;
  const kind = brainCopyKind(CUR && CUR.rel);
  if (!kind) { toast("这个文件没有「复制」入口", true); return; }
  let d;
  try {
    d = await jreq("GET", `/api/brain/copyable?root=${encodeURIComponent(DATA.root)}&kind=${encodeURIComponent(kind)}`);
  } catch (e) { toast(`列不出来:${e.message}`, true); return; }
  if (d && d.ok === false) { toast(d.error || d.reason || "列不出来", true); return; }
  const cands = (d && d.候选) || [];

  const host = el("div");
  host.style.cssText = "position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center";
  const box = el("div");
  box.style.cssText = "background:var(--surface-raised);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:16px 18px;max-width:560px;width:92vw;max-height:80vh;overflow:auto;box-shadow:0 12px 40px rgba(0,0,0,.35)";
  const h = el("div");
  h.textContent = `从另一本书复制「${kind}」`;
  h.style.cssText = "font-weight:600;margin-bottom:6px";
  box.appendChild(h);
  const tip = el("div");
  tip.textContent = "只抄这一个文件,正文和其它设定一概不碰。本书已有的内容不会被悄悄换掉 —— 覆盖要你点头。";
  tip.style.cssText = "font-size:12px;color:var(--text-mute);margin-bottom:10px;line-height:1.6";
  box.appendChild(tip);

  if (!cands.length) {
    const empty = el("div");
    empty.textContent = `书架上的其它书里都没有「${kind}」——先去那本书里填一份,再回来复制。`;
    empty.style.cssText = "padding:8px 0;color:var(--text-mute)";
    box.appendChild(empty);
  }

  cands.forEach((c) => {
    const row = el("div");
    row.style.cssText = "display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)";
    const name = el("div");
    name.textContent = c.名字 + (c[kind + "字节"] ? `  ·  ${c[kind + "字节"]} 字节` : "");
    name.style.cssText = "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    name.title = c.root;
    const go = el("button", "mini");
    go.textContent = "复制过来";
    go.onclick = () => doBrainCopy(kind, c, host);
    row.appendChild(name);
    row.appendChild(go);
    box.appendChild(row);
  });

  const foot = el("div");
  foot.style.cssText = "display:flex;justify-content:flex-end;margin-top:12px";
  const close = el("button", "ghost");
  close.textContent = "关闭";
  close.onclick = () => { host.remove(); _brainCopyOpen = false; };
  foot.appendChild(close);
  box.appendChild(foot);
  host.appendChild(box);
  document.body.appendChild(host);
}

async function doBrainCopy(kind, c, host) {
  const send = (over) => jreq("POST", "/api/brain/copy", {
    root: DATA.root, from_root: c.root, kind, overwrite: !!over,
  });
  let d;
  try { d = await send(false); } catch (e) { toast(`没抄成:${e.message}`, true); return; }
  if (d && d.ok === false) {
    if (d.code === "target_exists") {
      // 本书已经有内容:把两边的字节数摆出来,人点头才覆盖。
      // 接口自己就写着「可撤销:False」—— 那就得在确认框里说出来,别让人点完才知道(UI 审查 P1)。
      if (!window.confirm(`${d.reason}\n\n覆盖后**不可撤销**(接口未提供还原),旧内容不会先备份。\n\n要覆盖吗?`)) return;
      try { d = await send(true); } catch (e) { toast(`没抄成:${e.message}`, true); return; }
      if (d && d.ok === false) { toast(d.reason || "没抄成", true); return; }
    } else {
      toast(d.reason || "没抄成", true);
      return;
    }
  }
  host.remove();
  _brainCopyOpen = false;
  toast(`已从「${c.名字}」抄来「${kind}」(${d.字节} 字节${d.覆盖了原有的 ? ",覆盖了原来的" : ""})`);
  const rel = CUR && CUR.rel;
  if (rel) {
    try {
      const fresh = await jreq("GET", `/api/file?root=${encodeURIComponent(DATA.root)}&rel=${encodeURIComponent(rel)}`);
      $("editor").value = fresh.content;
      if (CUR.preview) setPreview(true);   // UI-P0-3:外置大脑默认是预览模式,不重渲就等于没刷新
    } catch (e) { /* 读不回就算了,下次点开就是新的 */ }
  }
}

function openBrainContinue() {
  if (!CUR || !CUR.editable || !isBrainRWFile(CUR.rel)) return;
  setPreview(false);
  const ed = $("editor");
  const n = ed.value.length;
  _rwMode = "brain-continue";
  _rewriteSel = { start: n, end: n, span: "" };
  ed.setSelectionRange(n, n);
  showInlinePanel({ src: "", goLabel: "续写",
    placeholder: "想续写什么?例:补一个反派势力 / 给主角加一段少年往事(留空=顺着现有设定补最缺的)" });
}
async function doRewrite() {
  if (!_rewriteSel) return;
  $("rewrite-error").textContent = "";
  $("rewrite-go").disabled = $("rewrite-again").disabled = true;
  const instruction = $("rewrite-instruction").value.trim();
  try {
    let d;
    if (_rwMode === "chapter") {
      d = await jreq("POST", "/api/rewrite", { root: DATA.root, chapter: CUR.chapter,
        full_text: $("editor").value, span: _rewriteSel.span, instruction });
      _rewriteText = d.rewritten;
    } else if (_rwMode === "brain") {
      d = await jreq("POST", "/api/brain/rewrite", { root: DATA.root, rel: CUR.rel,
        full_text: $("editor").value, span: _rewriteSel.span, instruction });
      _rewriteText = d.rewritten;
    } else {
      d = await jreq("POST", "/api/brain/continue", { root: DATA.root, rel: CUR.rel,
        full_text: $("editor").value, instruction });
      _rewriteText = d.continued;
    }
    $("rewrite-result").textContent = _rewriteText;
    $("rewrite-result").classList.remove("hidden");
    $("rewrite-go").classList.add("hidden");
    $("rewrite-again").classList.remove("hidden");
    $("rewrite-apply").classList.remove("hidden");
    positionInlinePanel();   // 结果撑高面板后重夹一次,别顶出编辑器
  } catch (e) { $("rewrite-error").textContent = e.message; }
  finally { $("rewrite-go").disabled = $("rewrite-again").disabled = false; }
}
async function applyRewrite() {
  if (!_rewriteSel || !_rewriteText) return;
  const ed = $("editor");
  if (_rwMode === "chapter") {
    // 正文铁律:必须走 /api/rewrite/apply,外科式同步 .原稿 快照,learn 只学手改
    const content = ed.value.substring(0, _rewriteSel.start) + _rewriteText + ed.value.substring(_rewriteSel.end);
    try {
      await jreq("POST", "/api/rewrite/apply", { root: DATA.root, chapter: CUR.chapter, content,
        old_span: _rewriteSel.span, new_span: _rewriteText });
      ed.value = content;
      setEditorOrigin("agent");
      clearDirty(); updateWordCount();
      hideInlinePanel(); toast("已替换选中段");
      await refresh();
    } catch (e) { $("rewrite-error").textContent = e.message; }
    return;
  }
  // 设定文件:客户端替换/追加 → 走编辑器既有保存链路(无快照概念,learn 红线无涉;确认才落盘)
  const insert = _rwMode === "brain-continue"
    ? (ed.value.endsWith("\n") ? "\n" : "\n\n") + _rewriteText + "\n" : _rewriteText;
  ed.value = ed.value.substring(0, _rewriteSel.start) + insert + ed.value.substring(_rewriteSel.end);
  setEditorOrigin("agent");
  markDirty(); updateWordCount();
  hideInlinePanel();
  try { await saveFile(); } catch (e) { toast("落盘失败:" + e.message, true); return; }
  toast(_rwMode === "brain-continue" ? "已续写进文末" : "已替换选中段");
}

// ---------- 重写已存在章节(覆盖逃生门) ----------
function confirmRewriteChapter(n) {
  const ok = window.confirm(`重写会用 AI 重新生成并【覆盖】第 ${n} 章的现有正文。\n建议先「备份整本」。确定继续?`);
  if (ok) writeChapter(n, true);
}

// ---------- 章节管理(删 / 插 / 上下移)----------
async function chapterOp(url, body) {
  try {
    const d = await jreq("POST", url, body);
    // 这些端点失败时给的是 **200 + ok:false**(不是 HTTP 错),以前一律当成功:
    // 编辑器被清空、列表刷新,人以为删成了/插成了(交叉审查 UI-P1-2)。
    if (d && d.ok === false) {
      toast(d.error || d.reason || "这一步没成,盘上没动。", true);
      return;
    }
    // 章号要变了,下面会清空编辑器 —— 先把当前手改落盘,否则「改两句 → 划过章节行点到 ↑」= 白改(UI 审查 P1-11)
    if (_dirty && CUR && CUR.editable) {
      try { await autosave(); } catch (e) { /* autosave 自己会 toast */ }
    }
    if (_dirty) {
      if (!window.confirm("当前文件还有没存上的改动。\n\n这一步会重排章号并清空编辑器,继续就等于丢掉这些字。\n确定继续吗?")) return;
    }
    // 章号变了:收掉当前打开的章节视图,避免指向错号
    hideInlinePanel();
    CUR = null; $("editor").value = ""; $("editor-path").textContent = "选左边一个文件来看/改";
    ["btn-rename-file", "btn-save-file", "btn-search", "btn-history", "btn-sensitive", "btn-learn", "btn-rewrite", "btn-brain-rewrite", "btn-brain-continue", "btn-brain-copy", "btn-card-expand", "btn-import-half", "btn-vol-summary", "btn-book-summary", "btn-outline", "btn-outline-regen", "btn-debug", "btn-preview", "btn-agent-rewrite", "btn-migrate-legacy"].forEach((id) => $(id).classList.add("hidden"));
    document.querySelectorAll(".list li.active").forEach((x) => x.classList.remove("active"));
    updateWordCount();
    if (d.state) { DATA = d.state; render(); } else { await refresh(); }
    if (d.note) toast(d.note);
  } catch (e) { toast(e.message, true); }
}
function deleteChapter(n) {
  if (!window.confirm(`删除第 ${n} 章?\n它会移进回收站(右上「回收站」按钮里可以点还原),后面的章节自动往前补号。`)) return;
  chapterOp("/api/chapter/delete", { root: DATA.root, n });
}
function insertAfter(n) { chapterOp("/api/chapter/insert", { root: DATA.root, n }); }
function moveChapter(n, direction) { chapterOp("/api/chapter/move", { root: DATA.root, n, direction }); }

// ---------- write(流式) ----------
let _wroteChapter = null;

// 开跑前体检(2026-10-21 需求确认:崩溃风险出现前预警,并说清具体风险)。
// 高危(如模型名不在服务端清单里、缺角色文件)→ 让作者先决定要不要改;中低危只提醒。
async function preflightCheck(n, silent) {
  let d = null;
  try { d = await jreq("GET", `/api/preflight?root=${encodeURIComponent(DATA.root)}&chapter=${n || 0}`); }
  catch (e) { return true; }               // 体检本身失败不拦人
  const risks = (d && d["风险"]) || [];
  if (!risks.length) return true;
  const lines = risks.map((r) => `· [${r["级别"]}] ${r["风险"]}\n   → ${r["怎么办"]}`).join("\n");
  risks.forEach((r) => logRun(`[风险·${r["级别"]}] ${r["风险"]}`, r["级别"] === "高" ? "err" : "warn", "warn"));
  if (!silent) {
    if ((d["高危数"] || 0) > 0) {
      return window.confirm(`开跑前体检发现 ${d["高危数"]} 条高危风险:\n\n${lines}\n\n不去修、直接开跑吗?`);
    }
    toast(`体检提醒 ${risks.length} 条(不影响开跑):${risks[0]["风险"]}`);
  }
  return true;
}

async function writeChapter(n, force) {
  // 正在写就别再开一次:第二次请求会被后端 409 拒,却会把在跑那次的进度/状态条一起抹掉(UI 审查 P1-4)
  if (_weaving) { toast("这一章还在写,等它跑完,或先点「停」", true); return; }
  // 书目录被挪走/删掉时别再往下跑:后面每一步都会以"不是 loom 项目"结尾(2026-10-21)
  if (!(await _ensureBook())) return;
  // 写章 = 顺手保存后端:不必先点「保存后端」,点写章就把当前后端表单(provider/model/字数/key)落盘。
  // 2026-10-21 真机:这一步内部抛了一次(Cannot set properties of null),结果**整条写章路被挡住**——
  // 可它只是个"顺手",DATA 里已经有后端配置了。所以改成失败只提醒、不阻断。
  try { await persistBackend(true); }
  catch (e) {
    if (isContextLenInputError(e)) { toast(e.message, true); return; }
    console.warn("顺手保存后端失败(不阻断写章):", e);
    toast("后端表单这次没存上,但写章照常开始。若报模型错,请点「保存后端」后再试。");
  }
  // 开跑前体检:高危风险先摆给作者看(模型名不在清单里这种,一跑就 502)
  if (!(await preflightCheck(n, false))) return;
  // 首跑防空转:没填 key/base_url 就别进面板转半天再报错——自动打开设置并高亮对应输入框
  const be = (DATA && DATA.backend) || {};
  const bspec = providerSpec(be.provider) || {};
  if (be.provider === "openai_compat" && !be.base_url) {
    toast("自定义供应商要先填 base_url + API Key 再开写", true);
    flashSetting("base-url"); return;
  }
  if (bspec.needs_key && !providerKeyed(be.provider)) {
    toast(`先在设置里填 ${bspec.label || be.provider} 的 API Key(或把供应商切到 Claude / Codex 免 key)再开写`, true);
    flashSetting("api-key"); return;
  }
  // 起书完整性硬门禁:四项没齐 → 引导去补,不再给「就这样写」逃生门(硬门禁下它必撞 409)
  if (DATA.writing_unlocked === false) {
    openGateGuide(DATA.missing || []);
    return;
  }
  // F1:第一章地基硬门——缺项就地补齐,别先把"正在生成"摆出来
  const _fpre = await foundationPrecheck(n, "");
  if (_fpre) { handleFoundationBlock(_fpre, () => writeChapter(n, force)); return; }

  // 审查反例②(前端 F1)：浮窗**必须等确认是 NDJSON 之后再开**。
  // 原来在 fetch 之前就 `run-overlay` 显示 + `_weaving=true`，于是"预检通过、开流前才发现
  // 配置坏了"(400 config_unreadable) 的那一瞬，界面已经闪过"正在生成"——那正是派单禁止的。
  let resp;
  try {
    resp = await fetch("/api/write", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: DATA.root, chapter: n, force }),
    });
  } catch (e) {
    logRun("连接失败:" + e.message, "err"); showRunClose();
    _weaving = false; renderJourney();
    return;
  }

  const ct = resp.headers.get("content-type") || "";
  if (!ct.includes("ndjson")) {
    const d = await resp.json().catch(() => ({}));
    // F1:三种门禁码(409 foundation_incomplete / 400 not_a_book / 400 config_unreadable)
    // 都在这里收口——都不得进流式态,也不用给"覆盖重写"这种按钮。
    if (handleFoundationBlock(d, () => writeChapter(n, force))) return;
    openRunUI(n, `正在写第 ${n} 章…`);   // 非门禁的失败:照旧给浮窗 + 覆盖入口
    logRun(d.error || `失败 (${resp.status})`, "err", "cross");
    _weaving = false;   // 门禁引导要在织章隐身解除后才显得出居中态(Task 3)
    if (resp.status === 409) {
      if (d.code === "brain_incomplete") {
        DATA.writing_unlocked = false;                 // 服务端 409=权威:同步本地,navMode 才会算 center
        if (d.missing) DATA.missing = d.missing;
        openGateGuide(d.missing || []);          // 门禁:引导去补齐,绝不给覆盖按钮
      } else if (d.code === "chapter_exists" || d.code === "chapter_drifted") {
        $("run-title").textContent = d.code === "chapter_exists"
          ? `第 ${n} 章已存在` : `第 ${n} 章正文被改过`;
        showRunForce(n);                          // 仅 chapter_exists/drifted 才给覆盖
      } else if (d.code === "project_busy" || d.code === "partner_busy") {
        // 锁被占 = 这本书正有任务在跑,和「本章已存在」是两回事;给覆盖重写会把人带错
        $("run-title").textContent = "这本书正有任务在跑";
        $("run-force").classList.add("hidden");
      } else {
        $("run-title").textContent = `没能开始写第 ${n} 章`;
        $("run-force").classList.add("hidden");
      }
    }
    showRunClose();
    renderJourney();
    return;
  }

  openRunUI(n, `正在写第 ${n} 章…`);   // 确认是 NDJSON：到这里才进流式态
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let broken = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev = null;
        try { ev = JSON.parse(line); } catch (e) { continue; }   // 半行/坏行当没有,别炸整条流
        handleEvent(ev);
      }
    }
  } catch (e) {
    // 网络断 / 后端中途崩:不能悄悄当成功 —— 这章可能根本没落盘(UI 审查 P1-3)
    broken = true;
    $("run-strip").classList.add("err");
    logRun("写到一半断了:" + (e.message || e) + "(这一章的正文可能没落好,去正文里看一眼)", "err", "cross");
  } finally {
    _stopRunTicker();
    hideStrip(); _runMinimized = false;   // 2026-10-21 需求确认:跑完就收条,别一直挂着"停在…"
    showRunClose(broken);   // 流中断也得关窗,但按钮要写清「中断」
    _weaving = false; renderJourney();   // 织章结束(正常/中断都到这):领航员复位(Task 3)
  }
}

/* ── 六步写文浮窗(2026-10-21 按需求确认重做) ────────────────────────────
   原来每棒只有一个名字 + 一行日志,看不出"现在跑到第几步、这一步多久了、产了多少字"。
   现在:①每棒一行 = 角色名 + 状态点 + 耗时 + 输出字数;②标题带总进度 N/6 与总计时;
   ③正在跑的那一棒实时显示"思考… 12s",写完回落成"12.3s · 1180 字"。 */
let _runT0 = 0, _runTicker = null, _runRoles = [], _runStepT0 = {}, _runStepChars = {}, _runDone = 0;
let _runChapter = null;   // 这一轮写第几章(浮窗标题用)
function _mmss(ms) {
  const t = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}
function _paintRunTitle() {
  if (!_runT0) return;
  const t = $("run-title"); if (!t) return;
  const running = document.querySelector("#agent-pills .pill.running .pi-name");
  const step = running ? `${_runDone + 1}/${_runRoles.length} ${running.textContent}` : `${_runDone}/${_runRoles.length}`;
  const chap = _runChapter != null ? `第 ${_runChapter} 章` : "本章";
  t.textContent = `正在写${chap} · ${step} · 已用 ${_mmss(Date.now() - _runT0)}`;
}
function _startRunTicker() {
  clearInterval(_runTicker);
  _runTicker = setInterval(() => {
    _paintRunTitle();
    const el = document.querySelector("#agent-pills .pill.running");
    if (el) {
      const r = el.dataset.role;
      const meta = el.querySelector(".pi-meta");
      if (meta && _runStepT0[r]) meta.textContent = `思考… ${_mmss(Date.now() - _runStepT0[r])}`;
    }
  }, 1000);
}
function _stopRunTicker() { clearInterval(_runTicker); _runTicker = null; }

// 任务准备架构（2026-11-08 §9）:任务事件展示——轻量一行进时间线,
// 带任务标识;陈旧 revision 忽略（同任务只显示更新的 revision,§9 前端去重）。
const _taskRev = {};   // task_id -> 已显示的最大 revision
function _taskEvent(ev, text, icon) {
  if (!ev || !ev.task_id) return;
  const rev = ev.revision || 0;
  if (_taskRev[ev.task_id] != null && rev < _taskRev[ev.task_id]) return;  // 陈旧 revision 忽略
  _taskRev[ev.task_id] = rev;
  _ev(text + (rev ? ` 〔任务 v${rev}〕` : ""), null, icon || "play");
}

function handleEvent(ev) {
  if (ev.type === "pipeline_start") {
    const box = $("agent-pills"); box.innerHTML = "";
    _runRoles = ev.roles.slice(); _runStepT0 = {}; _runStepChars = {}; _runDone = 0;
    _runT0 = Date.now(); _runChapter = ev.chapter != null ? ev.chapter : _runChapter;
    _activeStep = null;
    ev.roles.forEach((r) => {
      const step = document.createElement("div");
      step.className = "tl-step"; step.id = "pill-" + r; step.dataset.role = r;
      const head = document.createElement("div"); head.className = "tl-head";
      const dot = document.createElement("span"); dot.className = "tl-dot";
      const nm = document.createElement("span"); nm.className = "pi-name"; nm.textContent = r;
      const meta = document.createElement("span"); meta.className = "pi-meta"; meta.textContent = "待";
      head.appendChild(dot); head.appendChild(nm); head.appendChild(meta);
      head.onclick = () => step.classList.toggle("open");   // 点标题可手动展开/收起
      const body = document.createElement("div"); body.className = "tl-body";
      step.appendChild(head); step.appendChild(body);
      box.appendChild(step);
    });
    _paintRunTitle(); _startRunTicker();
  } else if (ev.type === "agent_start") {
    const p = $("pill-" + ev.role);
    if (p) {
      p.classList.add("running", "open");     // 跑起来就展开它自己那一段
      _runStepT0[ev.role] = Date.now();
      const meta = p.querySelector(".pi-meta"); if (meta) meta.textContent = "思考… 00:00";
      _activeStep = { el: p, body: p.querySelector(".tl-body") };
    }
    _paintRunTitle();
    _tlLine(`${ev.role} 开始…`, "play");
    $("run-stream").textContent = "";  // 新一棒,清空实时稿区
  } else if (ev.type === "agent_chunk") {
    const s = $("run-stream");
    s.textContent += ev.delta;
    s.scrollTop = s.scrollHeight;  // 跟着写,自动滚到底
    const r = document.querySelector("#agent-pills .pill.running");
    if (r) {
      const role = r.dataset.role;
      _runStepChars[role] = (_runStepChars[role] || 0) + (ev.delta || "").length;
      const meta = r.querySelector(".pi-meta");
      if (meta && _runStepT0[role]) {
        meta.textContent = `思考… ${_mmss(Date.now() - _runStepT0[role])} · ${_runStepChars[role]} 字`;
      }
    }
  } else if (ev.type === "agent_done") {
    const p = $("pill-" + ev.role);
    if (p) {
      p.classList.remove("running"); p.classList.add("done");
      p.classList.remove("open");            // 跑完收起,只留一行结论(需求确认)
      const meta = p.querySelector(".pi-meta");
      const secs = _runStepT0[ev.role] ? ((Date.now() - _runStepT0[ev.role]) / 1000).toFixed(1) : "?";
      const chars = _runStepChars[ev.role] || 0;
      if (meta) meta.textContent = `${secs}s` + (chars ? ` · ${chars} 字` : "");
    }
    _runDone += 1; _paintRunTitle();
    _tlLine(`${ev.role} —— ${ev.produces}`, "check", true);
    _activeStep = null;                      // 后面的事件不再挂在这一棒下面
  } else if (ev.type === "agent_skip") {
    const p = $("pill-" + ev.role);
    if (p) {
      p.classList.add("done");
      const meta = p.querySelector(".pi-meta"); if (meta) meta.textContent = "跳过";
    }
    _runDone += 1; _paintRunTitle();
    _ev(`${ev.role} —— 跳过(已完成、上游未变)`, null, "skip");
  } else if (ev.type === "gate_start") {
    const p = $("pill-" + ev.role); if (p) p.classList.add("running");
    _ev(`${ev.label}复审 · 第${ev.round}轮 …`, null, "search");
  } else if (ev.type === "gate_pass") {
    _ev(`${ev.label}通过(无硬伤)`, "ok", "check");
  } else if (ev.type === "gate_issues") {
    _ev(`发现 ${ev.issues.length} 处硬伤:`);
    ev.issues.forEach(it => _ev(
      `  · ${it["类别"]}:${it["问题"]}` + (it["证据"] ? `(证据:「${it["证据"]}」)` : "")));
  } else if (ev.type === "gate_revise") {
    _ev(`回炉重写中 …`, null, "redo");
    $("run-stream").textContent = "";  // 回炉稿重新流式
  } else if (ev.type === "gate_exhausted") {
    _ev(`${ev.label}跑满 ${ev.rounds} 轮仍有 ${ev.issues.length} 处残留 → 记入留痕,不阻断,继续`, null, "warn");
  } else if (ev.type === "edit_note") {
    _ev(`本章改动留痕已存(.审稿留痕/)`, null, "edit");
  } else if (ev.type === "debug_report") {
    _lastDebugReport = { chapter: ev.chapter, issues: ev.issues || [] };
    _ev(`除虫:${(ev.issues || []).length ? ev.issues.length + " 条跨章矛盾(见报告)" : "未发现跨章矛盾"}`,
           (ev.issues || []).length ? "warn" : "");
    if (CUR && CUR.rel === "外置大脑/状态账本.md") {
      if (_dirty) toast("除虫刚给状态账本记了一笔,但你正在改它——保存前先复制你的改动,或稍后手动合并", true);
      else openFile("外置大脑/状态账本.md", true, null);   // 无脏稿:静默重拉,防 autosave 拿旧全文覆盖新节
    }
  } else if (ev.type === "sensitive") {
    const ws = (ev.hits || []).slice(0, 6).map((h) => h.word).join("、");
    _ev(`违禁词粗筛:命中 ${ev.count} 处(${ws}${(ev.hits || []).length > 6 ? "…" : ""})——只提示,不阻断,可在「违禁词」里改写`, null, "warn");
  } else if (ev.type === "task_prepared") {
    // 任务准备架构（2026-11-08 §9）:轻量"当前任务/正在做什么/下一步"展示,不替代计划卡
    _taskEvent(ev, "任务就绪" + (ev.goal ? "：" + ev.goal : ""), "play");
  } else if (ev.type === "plan_updated") {
    _taskEvent(ev, ev.summary || "计划已更新", "edit");
  } else if (ev.type === "task_status") {
    const zh = { ready: "待接续", running: "进行中", waiting_author: "等你拍板",
                 paused: "已暂停", blocked: "受阻", completed: "已完成", cancelled: "已取消" };
    _taskEvent(ev, "任务" + (zh[ev.status] || ev.status)
      + (ev.stop_reason ? "（" + ev.stop_reason + "）" : ""), ev.status === "completed" ? "check" : null);
  } else if (ev.type === "warn") {
    _ev("· " + ev.message);
  } else if (ev.type === "info") {
    _ev(ev.message, null, "play");
  } else if (ev.type === "cancelled") {
    // 作者叫停:流水线停在某一棒之后。正式正文没动,半稿在 正文/.半稿。
    _wroteChapter = null; _lastDebugReport = null;
    hideStopBtn();
    const at = ev.role === "起笔前" ? "还没起笔" : `「${ev.role}」之后`;
    _ev(`已停在${at}:后面的工序没跑。` + (ev.half ? `半稿在 ${ev.half}` : ""), null, "warn");
    $("run-title").textContent = `第 ${ev.chapter} 章已停在${at}`;
    showRunClose();
  } else if (ev.type === "chapter_done") {
    _wroteChapter = ev.chapter;
    hideStopBtn();
    _ev(`第${ev.chapter}章终稿 ${ev.chars} 字`, "ok", "check");
    $("run-title").textContent = `第 ${ev.chapter} 章写完`;
    scheduleChainCheck();   // 联动链②:写完一章 → 提示让批评家挑毛病(事件已经在,不新增 emit)
    if (_lastDebugReport && _lastDebugReport.issues.length) {
      const r = _lastDebugReport; _lastDebugReport = null;
      setTimeout(() => renderDebugReport(r.chapter, r.issues), 400);   // 写完稍候弹报告,不抢完成态
    } else { _lastDebugReport = null; }
  } else if (ev.type === "error") {
    _ev(ev.message + codeHint(ev.code), "err", "cross");
    hideStopBtn();
    _lastDebugReport = null;
  }
  updateStrip(ev);
}

// ---------- 织章状态条:弹窗收起后,织机在底部继续响 ----------
let _runMinimized = false, _streamChars = 0, _stripDone = null, _stripErr = null;
let _writingChapter = 0, _stopRequested = false;   // 叫停:记住正在写第几章,防止重复点

// 叫停的语义要说清(见 loom/cancel.py):只停后续步骤——手上这一棒会写完,下一棒不再开始。
async function requestStop() {
  if (_stopRequested) return;
  _stopRequested = true;
  $("run-stop").disabled = true; $("rs-stop").disabled = true;
  try {
    const r = await fetch("/api/write/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: DATA.root, chapter: _writingChapter }),
    });
    const d = await r.json().catch(() => ({}));
    _ev(d.note || "已请停:这一棒写完就停。半稿会落在 正文/.半稿。", null, "warn");
    toast("已请停:这一棒写完就停");
    if (_runMinimized) renderStrip();
  } catch (e) {
    _stopRequested = false;
    $("run-stop").disabled = false; $("rs-stop").disabled = false;
    toast("请停失败:" + e.message, true);
  }
}
function showStopBtn() {
  _stopRequested = false;
  $("run-stop").classList.remove("hidden");
  $("run-stop").disabled = false; $("rs-stop").disabled = false;
  $("rs-stop").classList.remove("hidden");
}
function hideStopBtn() {
  $("run-stop").classList.add("hidden"); $("run-stop").disabled = false;
  $("rs-stop").classList.add("hidden"); $("rs-stop").disabled = false;
  _stopRequested = false;
}
function minimizeRun() { _runMinimized = true; $("run-overlay").classList.add("hidden"); renderStrip(); }
function expandRun() { _runMinimized = false; hideStrip(); $("run-overlay").classList.remove("hidden"); }
function hideStrip() { $("run-strip").classList.add("hidden"); }

// 点状态条空白处 = 重新展开六棒窗口(2026-10-21 作者:收起后点不开了)
document.addEventListener("click", (e) => {
  const strip = $("run-strip");
  if (!strip || strip.classList.contains("hidden")) return;
  if (!strip.contains(e.target)) return;
  if (e.target.closest("button")) return;      // 点「停/展开」按按钮自己的逻辑走
  if ($("run-overlay")) { _runMinimized = false; $("run-overlay").classList.remove("hidden"); hideStrip(); }
});
function updateStrip(ev) {
  if (ev.type === "agent_start" || ev.type === "gate_revise") _streamChars = 0;
  else if (ev.type === "agent_chunk") _streamChars += (ev.delta || "").length;
  else if (ev.type === "chapter_done") _stripDone = ev.chapter;
  else if (ev.type === "error") _stripErr = (ev.message || "出错") + codeHint(ev.code);
  if (!_runMinimized) return;
  renderStrip();
  if (ev.type === "chapter_done") toast(`第${ev.chapter}章写完 · 墨落定`);
  else if (ev.type === "error") toast(ev.message + codeHint(ev.code), true);
}
function renderStrip() {
  const strip = $("run-strip");
  strip.classList.remove("hidden");
  strip.classList.toggle("err", !!_stripErr);
  $("rs-label").textContent = _stripErr ? "织章出错" :
    (_stopRequested && _stripDone == null ? "正在停…(这一棒写完就停)" :
    (_stripDone != null ? `第${_stripDone}章写完` : $("run-title").textContent || "织章中"));
  const pills = [...document.querySelectorAll("#agent-pills .pill")];
  const box = $("rs-pills"); box.innerHTML = "";
  pills.forEach((p, i) => {
    if (i > 0) {
      const c = document.createElement("span");
      c.className = "rs-conn" + (pills[i - 1].classList.contains("done") ? " done" : "");
      box.appendChild(c);
    }
    const s = document.createElement("span");
    s.className = "rs-role" + (p.classList.contains("done") ? " done"
      : p.classList.contains("running") ? " running" : "");
    s.textContent = p.textContent;
    box.appendChild(s);
  });
  $("rs-count").textContent =
    (_stripDone == null && !_stripErr && _streamChars > 0) ? `已织 ${_streamChars.toLocaleString()} 字` : "";
  $("rs-expand").textContent = _stripDone != null ? "去看正文" : "展开";
  // 2026-10-21 修:通用慢请求条会把这两个键 hide 掉,而且不会恢复 ⇒
  // 六棒窗口收起后「展开」看不见了,点不开。这里每次画都显式恢复。
  $("rs-expand").classList.remove("hidden");
  $("rs-stop").classList.toggle("hidden", !_weaving);
}
// 事件行:签名同 logRun,但优先挂到"当前这一棒"下面(作者要的竖向时间线)
function _ev(text, cls, ic) {
  if (_activeStep && _activeStep.body) {
    const line = document.createElement("div");
    line.className = "tl-line " + (cls || "");
    line.innerHTML = (ic ? icon(ic, "log-ico") + " " : "") + escHtml(text);
    _activeStep.body.appendChild(line);
    _activeStep.body.scrollTop = _activeStep.body.scrollHeight;
  } else {
    logRun(text, cls, ic);
  }
}

// 竖向时间线里的一行:有"当前这一棒"就挂在它下面,没有就落到总日志区
let _activeStep = null;
function _tlLine(text, ic, isLast) {
  const line = document.createElement("div");
  line.className = "tl-line" + (isLast ? " tl-last" : "");
  line.innerHTML = (ic ? icon(ic, "log-ico") + " " : "") + escHtml(text);
  if (_activeStep && _activeStep.body) {
    _activeStep.body.appendChild(line);
    _activeStep.body.scrollTop = _activeStep.body.scrollHeight;
  } else {
    logRun(text, isLast ? "ok" : null, ic);
  }
}

function logRun(text, cls, ic) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  if (ic) div.innerHTML = icon(ic, "log-ico") + " " + escHtml(text);
  else div.textContent = text;
  $("run-log").appendChild(div);
  $("run-log").scrollTop = $("run-log").scrollHeight;
}
function showRunClose(broken) {
  const b = $("run-close");
  // 断流/解析炸时不能再写「完成」——那是把中断当成功(UI 审查 P1-3)
  b.textContent = broken ? "中断了,先去看看正文" : "完成,去看正文";
  b.classList.remove("hidden");
}
function showRunForce(n) {
  const b = $("run-force");
  b.onclick = () => writeChapter(n, true);
  b.classList.remove("hidden");
}
async function closeRun() {
  $("run-overlay").classList.add("hidden");
  hideStrip(); _runMinimized = false;
  _weaving = false; renderJourney();   // 兜底:织章后领航员必须复位,漏一处=球永久消失(Task 3)
  await refresh();
  if (_wroteChapter != null) {
    await openFile(`正文/第${_wroteChapter}章.md`, true, _wroteChapter);
    maybeCoachLearnLoop();  // T0:章稿落进编辑器那一刻,教 learn 闭环(只第一次)
  }
}

// ---------- 后端配置 ----------
function providerSpec(pid) {
  return ((DATA.backend && DATA.backend.providers) || []).find((p) => p.id === pid) || null;
}

// 供应商下拉 = 服务端 PROVIDERS 注册表(provider_catalog)的投影
function rebuildProviderOptions() {
  const sel = $("provider");
  const cur = sel.value;
  sel.innerHTML = "";
  ((DATA.backend && DATA.backend.providers) || []).forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id; o.textContent = p.label;
    sel.appendChild(o);
  });
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

// 这个供应商「接入就绪」了吗:cli 免 key 恒 true;其余看各自 key 是否已存(keys_set 由服务端按 key_env 逐家算)
function providerKeyed(pid) {
  const spec = providerSpec(pid);
  if (spec && spec.kind === "cli") return true;
  const ks = (DATA.backend && DATA.backend.keys_set) || {};
  if (pid in ks) return !!ks[pid];
  // 老服务端兜底(没有 keys_set 字段时)
  return pid === "openai_compat" ? !!(DATA.backend && DATA.backend.openai_compat_key_set)
                                 : !!(DATA.backend && DATA.backend.key_set);
}

// 用一组 {id,label} 重填模型【下拉】(末尾永远留一个「手动输入…」兜底,但默认是选、不是填)
function fillModelOptions(models) {
  const sel = $("model");
  sel.innerHTML = "";
  (models || []).forEach((m) => {
    const o = document.createElement("option");
    o.value = m.id; o.textContent = m.label || m.id || "（默认)";
    sel.appendChild(o);
  });
  const custom = document.createElement("option");
  custom.value = "__custom__"; custom.textContent = "✎ 手动输入…";
  sel.appendChild(custom);
}

// 切供应商时,用该供应商的预设建议重建下拉
function rebuildModelPresets(pid) {
  const spec = providerSpec(pid);
  fillModelOptions(spec ? spec.models : []);
}

// 选中某个模型:不在下拉里就先插一项再选(老配置/拉取结果都能正确回显);切到「手动输入」才露文本框
function setModelValue(model) {
  const sel = $("model"), custom = $("model-custom");
  model = model || "";
  if (model && ![...sel.options].some((o) => o.value === model)) {
    const o = document.createElement("option");
    o.value = model; o.textContent = model;
    sel.insertBefore(o, sel.querySelector('option[value="__custom__"]'));
  }
  sel.value = model;
  if (sel.selectedIndex < 0) sel.selectedIndex = 0;   // 空/没匹配上 → 落到第一项
  // 若落到了「手动输入…」(例:自定义供应商还没拉取预设)→ 露出文本框给用户填,别让他对着空下拉发愣
  if (sel.value === "__custom__") { custom.classList.remove("hidden"); custom.value = model && model !== "__custom__" ? model : ""; }
  else { custom.classList.add("hidden"); custom.value = ""; }
}

// 读当前选的模型(选了「手动输入」就读文本框)
function currentModelValue() {
  const sel = $("model");
  return sel.value === "__custom__" ? $("model-custom").value.trim() : sel.value;
}

// 下拉切到「手动输入…」→ 露出文本框聚焦;否则收起
function onModelSelect() {
  const custom = $("model-custom");
  if ($("model").value === "__custom__") { custom.classList.remove("hidden"); custom.focus(); }
  else custom.classList.add("hidden");
}

async function persistBackend(silent) {
  // 把顶栏后端表单(provider/model/base_url/字数/key)落盘;写章前会静默调一次,免得用户没点「保存后端」就开写。
  const key = $("api-key").value.trim();
  const model = currentModelValue();
  let contextLen;
  try { contextLen = parseContextLenElement($("context-len")); }
  catch (e) {
    if (silent) throw e;
    toast(e.message, true);
    return;
  }
  // 自定义供应商**必须**有模型名:空串发上去,上游只会回一句「未知模型」,而人看不出是自己把框清空了
  // (2026-10-20 真机:模型框空着还能点保存 → 一次手滑就把 model 写成空,整套槽位全废)。
  if ($("provider").value === "openai_compat" && !model) {
    if (!silent) toast("模型名不能为空:点「拉取可用模型」选一个,或填服务器上真实存在的 id", true);
    return;
  }
  const resp = await jreq("PUT", "/api/config", {
    root: DATA.root, provider: $("provider").value, model,
    base_url: $("base-url").value.trim() || null,
    chapter_chars: parseInt($("chapter-chars").value) || 800,
    context_len: contextLen,
    api_key: key || null,
  });
  DATA = resp;        // 落盘已经成功(服务端 200 回来了),后面这些都只是"重画"
  if (!silent) toast(key ? "后端 + API Key 已保存" : "后端已保存");
  if (resp.model_warning) toast(resp.model_warning, true);   // 软提示(如把 v4-flash 填进 DeepSeek),不阻断
  // 2026-10-21:重画放在自己的 try 里。原来它在同一段顺下来,任何一处界面取空都会被
  // 外面 catch 成"保存后端失败"——**存明明成功了**,却把人吓得以为没存上、还挡住了写章。
  try { render(); } catch (e) { console.error("保存后端成功,但重画界面时出错(不影响保存):", e); }
  if (CUR) { try { updateWordCount(); } catch (e) { /* 同上,不影响保存 */ } }
}
async function saveBackend() { await persistBackend(false); }

// 「拉取可用模型」:OpenAI 兼容的实时打 GET /models,CLI 类返回预设。结果灌进下拉。
async function fetchModels() {
  const p = $("provider").value;
  const btn = $("btn-fetch-models");
  const old = btn.innerHTML; btn.disabled = true; btn.textContent = "拉取中…";
  try {
    const d = await jreq("POST", "/api/backend/models", {
      root: DATA.root, provider: p,
      base_url: $("base-url").value.trim() || null,
      api_key: $("api-key").value.trim() || null,
    });
    if (d.ok && d.models && d.models.length) {
      // 拉取成功:先把**原来填的那个**留住——以前无条件 selectedIndex=0,
      // 等于「刷新一下就把你钉好的模型冲掉」,而且冲掉的那一刻看不出来(2026-10-20 真机踩到)。
      const prev = currentModelValue();
      const ids = d.models.map((m) => m.id);
      fillModelOptions(d.models);
      if (prev && ids.includes(prev)) {
        setModelValue(prev);                 // 还在清单里 → 原样保留
        toast(`拉到 ${d.models.length} 个模型；当前用的「${prev}」在清单里`);
      } else if (prev) {
        // 原来那个已经不在服务器清单里了(模型被删/改名)——这正是「未知模型」的现场,必须喊出来
        $("model").selectedIndex = 0;
        $("model-custom").classList.add("hidden");
        toast(`拉到 ${d.models.length} 个模型,但你原来填的「${prev}」不在清单里`
          + `(可用:${ids.join(" / ")});已换成可用清单,选一个再点保存`, true);
      } else {
        $("model").selectedIndex = 0;
        $("model-custom").classList.add("hidden");
        toast(d.message || `拉到 ${d.models.length} 个模型,下拉里选一个`);
      }
      $("model").focus();
    } else {
      toast(d.message || "没拉到模型(检查 base_url / API Key)", true);
    }
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.innerHTML = old; }
}

// 按供应商显隐:key 框(deepseek/自定义)、base_url(仅自定义)、拉取模型按钮、检测连接(claude/codex)
function applyProviderUI(provider) {
  const spec = providerSpec(provider);
  const needsKey = spec ? spec.needs_key : (provider === "deepseek");
  const isCustom = provider === "openai_compat";
  const isCli = spec ? spec.kind === "cli" : (provider === "claude" || provider === "codex");
  $("api-key").classList.toggle("hidden", !needsKey);
  $("base-url").classList.toggle("hidden", !isCustom);
  $("btn-fetch-models").classList.toggle("hidden", !(spec && spec.can_list_models));
  $("btn-probe").classList.toggle("hidden", !isCli);
  if (!needsKey || !isCli) { $("backend-status").textContent = ""; $("backend-status").className = "backend-status"; }
  if (needsKey) {
    const keySet = providerKeyed(provider);
    const label = spec ? spec.label : provider;
    $("api-key").placeholder = keySet ? "API Key 已设置" : `填 ${label} 的 API Key`;
  }
  // 每家供应商的「怎么拿 key」提示(注册表 hint 下发)
  $("key-hint").textContent = (spec && spec.hint) || "";
  if (isCustom && spec && spec.hint) $("base-url").title = spec.hint;
}
async function probeBackend() {
  const p = $("provider").value;
  const s = $("backend-status");
  s.className = "backend-status"; s.textContent = "检测中…";
  try {
    const d = await jreq("GET", `/api/backend/probe?provider=${encodeURIComponent(p)}`);
    s.className = "backend-status " + (d.ok ? "ok" : "bad");
    s.innerHTML = icon(d.ok ? "check" : "cross", "stat-ico") + " " + escHtml(d.message || "");
    s.title = d.hint || "";
    if (!d.ok && d.hint) toast(d.hint, true);   // 没装/没登录 → 把"该跑哪条命令"也弹出来
  } catch (e) {
    s.className = "backend-status bad";
    s.innerHTML = icon("cross", "stat-ico") + " " + escHtml(e.message);
  }
}

// ---------- 章内搜索 ----------
let _searchOn = false, _matches = [], _mi = 0;
function openSearch() {
  if (!CUR) return;
  _searchOn = true;
  $("search-bar").classList.add("on");
  $("search-input").focus(); $("search-input").select();
  runSearch();
}
function closeSearch() {
  _searchOn = false;
  $("search-bar").classList.remove("on");
  _matches = []; $("search-count").textContent = "";
}
function runSearch() {
  const q = $("search-input").value;
  const text = $("editor").value;
  _matches = [];
  if (q) {
    const hay = text.toLowerCase(), needle = q.toLowerCase();
    let idx = hay.indexOf(needle, 0);
    while (idx !== -1) { _matches.push(idx); idx = hay.indexOf(needle, idx + Math.max(1, needle.length)); }
  }
  _mi = 0;
  updateSearchCount();
  if (_matches.length) gotoMatch(0);
}
function updateSearchCount() {
  const c = $("search-count");
  c.textContent = _matches.length ? `${_mi + 1}/${_matches.length}`
    : ($("search-input").value ? "无结果" : "");
}
function gotoMatch(i) {
  if (!_matches.length) return;
  _mi = (i + _matches.length) % _matches.length;
  const start = _matches[_mi], q = $("search-input").value;
  const ed = $("editor");
  ed.focus();
  ed.setSelectionRange(start, start + q.length);
  // 让选区滚进视野:按行号近似定位
  const lineNo = ed.value.slice(0, start).split("\n").length - 1;
  const lh = parseFloat(getComputedStyle(ed).lineHeight) || 32;
  ed.scrollTop = Math.max(0, lineNo * lh - ed.clientHeight / 2);
  updateSearchCount();
}

// ---------- 命令面板:mac 是 ⌘K,Windows 是 Ctrl+K ----------
const K_MOD = /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ? "⌘" : "Ctrl";
function kbd(k) { return K_MOD + k; }
let _cmds = [], _filtered = [], _cmdSel = 0;
function openCmdk() {
  buildCmds();
  $("cmdk-input").value = "";
  renderCmds("");
  $("cmdk").classList.remove("hidden");
  $("cmdk-input").focus();
}
function closeCmdk() { $("cmdk").classList.add("hidden"); }
function buildCmds() {
  const c = [];
  const inProj = !$("app").classList.contains("hidden");
  if (inProj && DATA) {
    c.push({ label: `写下一章(第${DATA.next_chapter}章)`, key: kbd("↵"), run: () => writeChapter(DATA.next_chapter, false) });
    if (CUR && CUR.chapter != null) {
      c.push({ label: `重写本章(覆盖第${CUR.chapter}章)`, run: () => confirmRewriteChapter(CUR.chapter) });
      c.push({ label: "学这章的手改 learn", run: () => learn(CUR.chapter) });
      c.push({ label: "本章细纲(看/改分镜)", keywords: "outline 大纲", run: () => openOutline(CUR.chapter) });
      c.push({ label: "本章版本历史", keywords: "回滚 快照 history", run: openHistory });
      c.push({ label: "违禁词自检(本章)", keywords: "敏感词 审核", run: scanSensitive });
    }
    if (CUR && CUR.editable) c.push({ label: "保存", key: kbd("S"), run: saveFile });
    if (CUR) {
      c.push({ label: "章内搜索", key: kbd("F"), run: openSearch });
      c.push({ label: CUR.preview ? "切到纯文本编辑" : "切到渲染预览", keywords: "preview markdown", run: () => setPreview(!CUR.preview) });
    }
    (DATA.chapters || []).forEach((ch) => {
      c.push({ label: `打开第${ch.n}章${ch.title ? " · " + ch.title : ""}`, keywords: "切换 章节 跳转 goto",
               run: () => openFile(`正文/第${ch.n}章.md`, true, ch.n) });
    });
    c.push({ label: "喂样本 / 继承指纹 seed", run: openSeed });
    c.push({ label: "脉络 · 时间轴(故事到哪了)", keywords: "编年史 回顾 studio 书房", run: () => openStudio("timeline") });
    c.push({ label: "脉络 · 伏笔账本", keywords: "埋设 回收 悬空 studio 书房", run: () => openStudio("foreshadow") });
    c.push({ label: "脉络 · 专名册", keywords: "人名 境界 地名 studio 书房", run: () => openStudio("names") });
    c.push({ label: "设置(后端 / 模型 / API Key / 字数)", keywords: "配置 供应商 config settings", run: openSettings });
    c.push({ label: "导出全书", run: openExport });
    c.push({ label: "备份整本", run: backupBook });
    c.push({ label: "环境自检 doctor", run: runDoctor });
    c.push({ label: "专注模式", key: kbd("."), run: toggleFocus });
    c.push({ label: "回首页(换一本书)", keywords: "切换项目 换书 项目 退出 主页", run: () => { localStorage.removeItem("loom_root"); showWelcome(); } });
  }
  c.push({ label: "切换明暗主题", run: toggleTheme });
  _cmds = c;
}
function renderCmds(q) {
  const list = $("cmdk-list"); list.innerHTML = ""; _cmdSel = 0;
  _filtered = _cmds.filter((c) => (c.label + " " + (c.keywords || "")).toLowerCase().includes(q.toLowerCase()));
  if (!_filtered.length) { list.innerHTML = `<div class="cmdk-empty">没有匹配的命令</div>`; return; }
  _filtered.forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "cmdk-item" + (i === 0 ? " sel" : "");
    el.dataset.i = i;
    const lab = document.createElement("span"); lab.textContent = c.label; el.appendChild(lab);
    if (c.key) { const k = document.createElement("span"); k.className = "ci-key"; k.textContent = c.key; el.appendChild(k); }
    el.onclick = () => { closeCmdk(); c.run(); };
    list.appendChild(el);
  });
}
function cmdkKey(e) {
  if (e.key === "ArrowDown") { e.preventDefault(); moveCmdSel(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveCmdSel(-1); }
  else if (e.key === "Enter") { e.preventDefault(); const c = _filtered[_cmdSel]; if (c) { closeCmdk(); c.run(); } }
  else if (e.key === "Escape") { e.preventDefault(); closeCmdk(); }
}
function moveCmdSel(d) {
  if (!_filtered.length) return;
  _cmdSel = (_cmdSel + d + _filtered.length) % _filtered.length;
  [...$("cmdk-list").children].forEach((el, i) => el.classList.toggle("sel", i === _cmdSel));
  const sel = $("cmdk-list").children[_cmdSel];
  if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: "nearest" });
}

// ---------- 脉络:时间轴 / 伏笔账本 / 专名册(只读投影) ----------
let _studio = null;
// ---------- 镜台:它有多懂你 ----------
// 只读投影,数据一次到位(GET /api/mirror 给全屏四块)。曲线手画 SVG——
// 本地优先、零外部依赖,不引任何图表库(index.html 里除 iconfont 外没有第三方 script)。
let _mirror = null;

async function openMirror() {
  $("mirror-body").innerHTML = `<div class="hint">读取中…</div>`;
  $("mirror-overlay").classList.remove("hidden");
  try {
    _mirror = await jreq("GET", `/api/mirror?root=${encodeURIComponent(DATA.root)}`);
  } catch (e) { $("mirror-body").innerHTML = `<div class="error">${escHtml(e.message)}</div>`; return; }
  renderMirror();
}

// 双线折线图。x=章号(按顺序等距,不按章号数值——跳章不该在图上留空洞),y=比率。
// 上界取两条线的最大值,地板 0.2:全程低手改时别把一条贴地的线放大成剧烈起伏。
function mirrorChart(rows) {
  const W = 496, H = 132, PL = 8, PR = 8, PT = 12, PB = 20;
  const iw = W - PL - PR, ih = H - PT - PB;
  const top = Math.max(0.2, ...rows.map((r) => Math.max(r.改写率, r.增删率)));
  const x = (i) => PL + (rows.length === 1 ? iw / 2 : (iw * i) / (rows.length - 1));
  const y = (v) => PT + ih - (ih * Math.min(v, top)) / top;
  const path = (key) => rows.map((r, i) => `${x(i)},${y(r[key])}`).join(" ");
  const dots = (key, cls) => rows.map((r, i) =>
    `<circle class="mi-dot ${cls}" cx="${x(i)}" cy="${y(r[key])}"/>`).join("");
  const line = (key, cls) => `<polyline class="mi-line ${cls}" points="${path(key)}"/>`;
  const firstLab = `第${rows[0].章}章`, lastLab = `第${rows[rows.length - 1].章}章`;
  return `<svg class="mi-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <line class="mi-grid" x1="${PL}" y1="${PT}" x2="${W - PR}" y2="${PT}"/>
    <line class="mi-axis" x1="${PL}" y1="${PT + ih}" x2="${W - PR}" y2="${PT + ih}"/>
    ${line("增删率", "is-ink")}${line("改写率", "is-seal")}
    ${dots("增删率", "is-ink")}${dots("改写率", "is-seal")}
    <text class="mi-xlab" x="${PL}" y="${H - 6}">${firstLab}</text>
    ${rows.length > 1 ? `<text class="mi-xlab" x="${W - PR}" y="${H - 6}" text-anchor="end">${lastLab}</text>` : ""}
  </svg>`;
}

function renderMirror() {
  const box = $("mirror-body"); box.innerHTML = "";
  if (!_mirror) return;
  const rows = _mirror.曲线 || [], cov = _mirror.覆盖 || {}, fp = _mirror.指纹 || {}, ps = _mirror.人格 || [];
  const pct = (v) => `${Math.round(v * 100)}%`;
  const parts = [];

  // —— 曲线(主) ——
  if (!rows.length) {
    // 空态顺手把核心循环教一遍:写 → 手改 → learn → 这条线开始长
    parts.push(`<div class="hint">写一章、按你的意思改、再点「学这章的手改」——这条线就开始长了。<br/>` +
      `它记的是<b>你还要改多少</b>:改得越少,说明它越懂你。</div>`);
  } else {
    const last = rows[rows.length - 1];
    parts.push(`<div class="mi-sec">
      <div class="mi-hero">
        <div class="mi-stat"><span class="mi-val is-seal">${pct(last.改写率)}</span>
          <span class="mi-key">改写 · 像不像你</span></div>
        <div class="mi-stat"><span class="mi-val">${pct(last.增删率)}</span>
          <span class="mi-key">增删 · 懂不懂你要什么</span></div>
      </div>
      ${rows.length > 1 ? mirrorChart(rows) + `<div class="mi-legend">
        <span><i class="mi-swatch is-seal"></i>改写率(它像不像你)</span>
        <span><i class="mi-swatch is-ink"></i>增删率(它懂不懂你要什么)</span>
      </div>` : `<div class="hint">这是第 ${last.章} 章的数。再学一章,这里就长出走势线——` +
        `你要看的不是某一章多少,是它有没有在往下走。</div>`}
      <div class="mi-cover">这条线画了 ${cov.已学 ?? 0} 章 · 全书 ${cov.总章 ?? 0} 章` +
        `${(cov.总章 ?? 0) > (cov.已学 ?? 0) ? "(其余还没点过「学这章的手改」)" : ""}</div>
    </div>`);
  }

  // —— 写作指纹 ——
  const srcName = { default: "中性默认(还没懂你)", sample: "你的样本",
    inherit: "继承自另一本书", reference: "别人的范文(起点)" }[fp.来源] || fp.来源 || "";
  parts.push(`<div class="mi-sec">
    <div class="mi-head">写作指纹 <span class="mi-sub">${escHtml(srcName)} · ${fp.规则数 ?? 0} 条规则</span></div>
    ${(fp.anchor || []).length
      ? (fp.anchor || []).map((a) => `<div class="mi-anchor">${escHtml(a)}</div>`).join("")
      : `<div class="hint">还没有 anchor 例句。它们是你亲手写下、被指纹逐字记住的句子。</div>`}
  </div>`);

  // —— 人格增补 ——
  parts.push(`<div class="mi-sec">
    <div class="mi-head">它跟你学到的写法 <span class="mi-sub">${ps.length ? ps.length + " 个人格" : ""}</span></div>
    ${ps.length
      ? ps.map((r) => `<div class="mi-role">${escHtml(r.角色)} · ${r.增补条数} 条` +
            (r.可撤销
              ? `<button class="mi-revert" data-role="${escHtml(r.角色)}">撤销最近一次</button>`
              : "") +
          `</div>` +
          `<div class="mi-extra">${(r.增补 || []).map((l) => escHtml(l)).join("<br/>")}</div>`).join("")
      : `<div class="hint">还没有。等某个角色攒够证据（你在同一类地方反复改），领航员会在对话里提出来，问你答不答应把这种改法写进它的「个人增补」。</div>`}
  </div>`);

  box.innerHTML = parts.join("");
  // 撤销键在这一屏里是【唯一的写操作】——镜台其余部分是只读投影,别把它做成整屏可编辑。
  box.querySelectorAll(".mi-revert").forEach((b) => { b.onclick = () => revertPersonaExtra(b.dataset.role, b); });
}

// 撤销某人格最近一次「学改法」落盘。后端 `.进化/历史/` 的快照是【一次性】的(撤完即清),
// 所以只撤得回最近那一次——文案要说清,别让作者以为有回退栈。
async function revertPersonaExtra(role, btnEl) {
  if (!DATA || !DATA.root || !role) return;
  if (!confirm(`撤销「${role}」最近一次学到的写法?\n\n只撤最近这一次(更早的学到的会留着),基座提示词不受影响。`)) return;
  const root = DATA.root;
  if (btnEl) btnEl.disabled = true;
  try {
    await jreq("POST", "/api/evolve/revert", { root, 角色: role });
    if (!DATA || DATA.root !== root) return;   // 已换书:迟到的响应绝不上屏
    toast(`已撤销「${role}」最近一次学到的写法`);
    await openMirror();                        // 重开一屏:曲线/覆盖/增补一起刷新
  } catch (e) {
    if (!DATA || DATA.root !== root) return;
    toast(e.message, true);
    if (btnEl) btnEl.disabled = false;
  }
}

async function openStudio(tab) {
  $("studio-body").innerHTML = `<div class="hint">读取中…</div>`;
  $("studio-overlay").classList.remove("hidden");
  try {
    _studio = await jreq("GET", `/api/studio?root=${encodeURIComponent(DATA.root)}`);
  } catch (e) { $("studio-body").innerHTML = `<div class="error">${escHtml(e.message)}</div>`; return; }
  renderStudio(tab);
}
function renderStudio(tab) {
  ["timeline", "foreshadow", "names", "statebook"].forEach((t) =>
    $("studio-tab-" + t).classList.toggle("on", t === tab));
  const box = $("studio-body"); box.innerHTML = "";
  if (!_studio) return;
  if (tab === "timeline") {
    const rows = _studio.timeline || [];
    if (!rows.length) { box.innerHTML = `<div class="hint">卡章纲里还没有「- 第N章:…」的章行。写规划、learn 之后,这里会长成整本书的时间轴。</div>`; return; }
    rows.forEach((r) => {
      const el = document.createElement("div"); el.className = "st-row st-click";
      el.innerHTML = `<span class="st-ch">第${r.n}章</span><div class="st-body">` +
        `<div class="st-plan">${escHtml(r.plan || "")}</div>` +
        (r.recap ? `<div class="st-recap">${escHtml(r.recap)}</div>` : "") + `</div>`;
      el.onclick = () => { $("studio-overlay").classList.add("hidden"); openFile(`正文/第${r.n}章.md`, true, r.n); };
      box.appendChild(el);
    });
  } else if (tab === "foreshadow") {
    const d = _studio.foreshadow || {}; const rows = d.rows || []; const staleSet = new Set(d.stale || []);
    if (!rows.length) { box.innerHTML = `<div class="hint">还没有伏笔记录。每章 learn 后,写后摘要会把 [埋设/推进/回收] 自动记进卡章纲,这里就有账可查了。</div>`; return; }
    rows.forEach((r) => {
      const isStale = r.kind === "埋设" && staleSet.has((r.text || "").slice(0, 60));
      const el = document.createElement("div"); el.className = "st-row";
      el.innerHTML = `<span class="st-ch">第${r.chapter}章</span>` +
        `<span class="st-kind st-${r.kind === "埋设" ? "plant" : r.kind === "回收" ? "pay" : "push"}">${r.kind}</span>` +
        `<div class="st-body">${escHtml(r.text)}${isStale ? '<span class="st-stale">悬空</span>' : ""}</div>`;
      box.appendChild(el);
    });
  } else if (tab === "names") {
    const d = _studio.names || {};
    if ((d.roster || []).length) {
      const h = document.createElement("div"); h.className = "sub-head"; h.textContent = "人物专名册(逐字直送写手的那份)"; box.appendChild(h);
      const wrap = document.createElement("div"); wrap.className = "st-chips";
      d.roster.forEach((n) => { const c = document.createElement("span"); c.className = "st-chip"; c.textContent = n; wrap.appendChild(c); });
      box.appendChild(wrap);
    }
    (d.sections || []).forEach((s) => {
      const h = document.createElement("div"); h.className = "sub-head"; h.textContent = s.title; box.appendChild(h);
      const b = document.createElement("div"); b.className = "recap-block"; b.textContent = s.body; box.appendChild(b);
    });
    if (!(d.roster || []).length && !(d.sections || []).length) {
      box.innerHTML = `<div class="hint">世界观里还没识别到硬设定小节(如「## 力量体系」「## 地理」),人物卡也没有「## 类型 · 名字」式的角色。填了之后,这里就是写手照抄的专名册。</div>`;
    }
  } else if (tab === "statebook") {
    const rows = _studio.statebook || [];
    if (!rows.length) { box.innerHTML = `<div class="hint">还没有账本——写完一章自动除虫后,这里会按章记录 物品/状态/规则/时钟。</div>`; return; }
    rows.forEach((ch) => {
      const h = document.createElement("div"); h.className = "bug-head"; h.textContent = `第${ch.n}章`;
      box.appendChild(h);
      ch.lines.forEach((l) => {
        const d = document.createElement("div"); d.className = "bug-ev";
        d.textContent = `[${l.kind}] ${l.text}`; box.appendChild(d);
      });
    });
  }
}

// ---------- 除虫:跨章连续性报告(非阻断);「按建议改」联动行内改写 ----------
async function debugChapter() {
  if (!CUR || CUR.chapter == null) return;
  toast("除虫中:对照前几章与状态账本…十几秒");
  try {
    if (CUR.chapter != null && _dirty) await autosave();  // 先把手改落盘,别让除虫扫到旧稿
    const d = await jreq("POST", "/api/chapter/debug", { root: DATA.root, chapter: CUR.chapter });
    if (d.state_lines && d.state_lines.length && d.ledger_written === false) toast("账本已有本章记录,未覆盖——内容有变请打开状态账本删掉该章「## 第N章」节后重扫");
    renderDebugReport(CUR.chapter, d.issues || []);
  } catch (e) { toast(e.message, true); }
}
function renderDebugReport(chapter, issues) {
  $("debug-title").textContent = `除虫报告 · 第${chapter}章` + (issues.length ? `(${issues.length} 条)` : "");
  const box = $("debug-body"); box.innerHTML = "";
  if (!issues.length) {
    box.innerHTML = `<div class="hint">未发现跨章矛盾。报告已留档 .审稿留痕/。</div>`;
  }
  issues.forEach((it) => {
    const div = document.createElement("div"); div.className = "bug-item";
    const head = document.createElement("div"); head.className = "bug-head";
    head.textContent = `${"⭐".repeat(it["星"] || 3)} ${it["类别"]}:${it["问题"]}`;
    const ev = document.createElement("div"); ev.className = "bug-ev";
    ev.textContent = `本章:「${it["本章证据"] || ""}」 · 前情:${it["前情证据"] || ""}`;
    div.appendChild(head); div.appendChild(ev);
    if (it["修改示例"]) {
      const fx = document.createElement("div"); fx.className = "bug-fix";
      fx.textContent = `修改示例:${it["修改示例"]}`; div.appendChild(fx);
    }
    const act = document.createElement("div"); act.className = "row-end";
    const btn = document.createElement("button"); btn.className = "ghost";
    btn.textContent = it["落点"] === "设定" ? "打开设定" : "按建议改";
    btn.onclick = () => applyBugFix(chapter, it);
    act.appendChild(btn); div.appendChild(act);
    box.appendChild(div);
  });
  $("debug-overlay").classList.remove("hidden");
}
async function applyBugFix(chapter, it) {
  $("debug-overlay").classList.add("hidden");
  if (it["落点"] === "设定") {
    // 修改示例里点名了 xx.md 且在侧栏设定清单里 → 打开它;否则打开状态账本
    const m = (it["修改示例"] || "").match(/[一-龥A-Za-z0-9·]+\.md/);
    const known = (DATA.brain || []).flatMap((b) => b.children ? b.children.map((c) => c.rel) : [b.rel]);
    const rel = m && known.find((r) => r.endsWith("/" + m[0])) || "外置大脑/状态账本.md";
    try { await openFile(rel, true, null); } catch (e) { toast(e.message, true); }
    return;
  }
  await openFile(`正文/第${chapter}章.md`, true, chapter);
  const ed = $("editor");
  const needle = (it["本章证据"] || "").trim();
  let idx = needle ? ed.value.indexOf(needle) : -1;
  if (idx < 0) { toast("没定位到证据句——请手动选中要改的段落,再用「重写选中」", true); return; }
  // 吸附到所在段落(空行为界),预填修改示例进行内改写面板
  const before = ed.value.lastIndexOf("\n\n", idx);
  const after = ed.value.indexOf("\n\n", idx + needle.length);
  const start = before < 0 ? 0 : before + 2;
  const end = after < 0 ? ed.value.length : after;
  ed.setSelectionRange(start, end); ed.focus();
  openRewrite();
  $("rewrite-instruction").value = it["修改示例"] || `修掉:${it["问题"]}`;
}

// ---------- 设置抽屉 / 输入退场 / 朱批浮条 ----------
// ---------------------------------------------------------------- R21 按用途分模型
let ROUTE_ROWS = [];

async function loadRoutes() {
  const box = $("route-slots");
  if (!DATA.root) { box.textContent = "先打开一本书。"; return; }
  box.textContent = "读槽表…";
  try {
    const d = await jreq("GET", `/api/routes?root=${encodeURIComponent(DATA.root)}`);
    ROUTE_ROWS = (d && d["槽"]) || [];
    const provs = (d && d["供应商"]) || [];
    paintConcurrency(d);     // 并发上限那一格(2026-10-25)
    box.textContent = "";
    ROUTE_ROWS.forEach((r) => box.append(routeRow(r, provs)));
    if (!ROUTE_ROWS.length) box.textContent = "后端没返回槽表。";
  } catch (e) { box.textContent = e.message; }
}

function routeRow(r, provs) {
  const row = document.createElement("div");
  row.className = "route-row" + (r["必须独立"] ? " required" : "");
  row.title = r["说明"] || "";

  const name = document.createElement("span");
  name.className = "route-name";
  name.textContent = r["用途"];
  // 「这一格到底谁在用」直接印在名字下面(2026-10-20 需求确认:只写「按用途分模型」看不出该配哪个)
  const who = document.createElement("span");
  who.className = "route-who";
  who.textContent = (r["消费方"] || []).join(" · ");
  if (who.textContent) { name.appendChild(document.createElement("br")); name.appendChild(who); }

  // 2026-10-20 需求确认:取消「跟随默认」勾选框。
  // 语义没变——三个框都留空 = 跟随默认(后端 routes.resolve 就是这么判的),
  // 少一个控件就少一处「勾着却又能选」的歧义。当前用的是谁,直接写在下拉第一项里,
  // 原来那行灰色小字(provider / model)随之去掉,一格一行。
  const following = !!r["跟随默认"];
  const provLabel = following
    ? `默认 · ${r["model"] || "跟随本章默认"}`
    : `${r["provider"] || "默认"} · ${r["model"] || ""}`;

  const prov = document.createElement("select");
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = provLabel;
  prov.append(empty);
  (provs || []).forEach((p2) => {
    const o = document.createElement("option");
    o.value = p2["id"] || p2;
    o.textContent = p2["label"] || p2["名字"] || p2["id"] || String(p2);
    prov.append(o);
  });
  prov.value = (r["写明"] && r["写明"]["provider"]) || "";

  const model = document.createElement("input");
  model.type = "text";
  model.placeholder = "模型名";
  model.value = (r["写明"] && r["写明"]["model"]) || "";

  const baseUrl = document.createElement("input");
  baseUrl.type = "text";
  baseUrl.placeholder = "base_url";
  baseUrl.value = (r["写明"] && r["写明"]["base_url"]) || "";

  // 工具协议(R25):弱模型靠它少一整类失败。以前这里没入口,saveRoute 也不发 ——
  // 于是任何一次「保存」都会把已经钉好的协议悄悄抹掉(交叉审查 P1)。
  const contextLen = document.createElement("input");
  contextLen.type = "text";
  contextLen.inputMode = "numeric";
  contextLen.autocomplete = "off";
  contextLen.placeholder = "上下文 token";
  contextLen.value = (r["写明"] && r["写明"]["context_len"]) || "";
  const contextLenStatus = document.createElement("span");
  contextLenStatus.className = "route-now context-len-status";
  contextLenStatus.textContent = contextLenSummary(r["context_limit"], r["context_len_source"]);

  const proto = document.createElement("select");
  const _p0 = document.createElement("option");
  _p0.value = "";
  // 2026-10-21:默认不是"一定用文本协议",而是**自动** —— 后端支持 tools 时就走原生函数调用。
  _p0.textContent = "工具协议:默认";
  proto.append(_p0);
  [["text", "text"], ["json", "json"], ["native", "native"]].forEach(([v, t]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = "工具协议:" + t;
    proto.append(o);
  });
  proto.value = (r["写明"] && r["写明"]["tool_protocol"]) || "";
    // 2026-10-21:native 已开放(需求确认)。后端没有 tools 能力时会自动降级并明确告知,所以不用禁用。

  const save = document.createElement("button");
  save.className = "mini";
  save.textContent = "保存";
  save.title = "只存这一行";
  save.onclick = () => saveRoute(r["用途"], {
    provider: prov.value, model: model.value, base_url: baseUrl.value,
    tool_protocol: proto.value, context_len: contextLen.value,
    context_len_bad_input: !!(contextLen.validity && contextLen.validity.badInput),
  });
  if (r["本版无消费方"]) row.title = (r["说明"] || "") + "\n本版还没有地方用它(配了不生效)";

  row.append(name, prov, model, baseUrl, contextLen, contextLenStatus, proto, save);
  return row;
}

// 2026-10-25 需求确认:在网关设置里改一项并保存后,窗口别跳回顶部。
// 做法:重绘前把「所有真的能滚的容器」的 scrollTop 记下来,重绘后原样放回去
// (两帧:一帧给 DOM 换完,一帧给浏览器做完布局——只放一帧在长列表里会跳)。
function _captureScroll(root) {
  const out = [];
  const se = document.scrollingElement;
  if (se) out.push([se, se.scrollTop]);
  if (!root) return out;
  if (root.scrollHeight > root.clientHeight + 4) out.push([root, root.scrollTop]);
  root.querySelectorAll('*').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 4) out.push([el, el.scrollTop]);
  });
  return out;
}
function _restoreScroll(saved) {
  saved.forEach(([el, top]) => { if (el && el.isConnected) el.scrollTop = top; });
}

async function saveRoute(purpose, v) {
  const body = { root: DATA.root, purpose };
  // 「跟随默认」不再有自己的勾选框(2026-10-20):三个框都留空就是跟随默认——
  // 这与后端 routes.resolve 的判据完全一致,前端不再自己发明第二种口径。
  const provider = v.provider || "";
  const model = (v.model || "").trim();
  const base_url = (v.base_url || "").trim();
  const protocol = (v.tool_protocol || "").trim();
  let context_len;
  try {
    if (v.context_len_bad_input) {
      const error = new Error("单会话上下文上限必须是正整数；留空使用默认 128000。");
      error.code = "invalid_context_len";
      throw error;
    }
    context_len = parseContextLenInput(v.context_len);
  }
  catch (e) { toast(e.message, true); return; }
  // 跟随默认判据还必须包含用途级窗口；只填窗口同样是有效的独立槽。
  // 以前只比 provider/model/base_url —— 于是「只把工具协议改成 native、其余留空」会被当成
  // 「三个框都留空 = 恢复跟随默认」,body.tool_protocol 被清成空:界面弹一句「跟着默认」再跳回 text,
  // 作者以为存不进去。协议本身是**按格独立**的一列,不该被模型那三框的留空规则连坐。
  const follow = !provider && !model && !base_url && !protocol && !context_len;
  if (follow) { body.model = ""; body.provider = ""; body.base_url = ""; body.tool_protocol = ""; body.context_len = 0; }
  else {
    body.provider = provider;
    body.model = model;
    body.base_url = base_url;
    body.tool_protocol = protocol;   // 别丢:丢了等于把人家钉好的协议抹了
    body.context_len = context_len;
  }
  try {
    const d = await jreq("POST", "/api/routes", body);
    if (d && d.ok === false) { toast(d.reason || d.error || "没存进去。", true); return; }
    // 状态报告用**后端**的口径:三个框留空时后端就是「恢复跟随默认」,
    // 前端若照自己的判断说「钉住了」,就是在撒谎。
    // 2026-10-21:模型跟默认与协议钉住是两件事,分开说 —— 否则「模型跟随默认 + 协议 native」
    // 这种合法状态会被报成「现在跟随默认」,读起来像没存上。
    const slot = ((d && d["槽"]) || []).find((x) => x["用途"] === purpose);
    const followNow = slot ? !!slot["跟随默认"] : follow;
    const pinned = slot ? (((slot["写明"] || {}).tool_protocol) || "") : protocol;
    if (followNow && pinned) toast(`「${purpose}」模型跟随默认;工具协议已钉住 ${pinned}。`);
    else if (followNow) toast(`「${purpose}」现在是跟随默认。`);
    else toast(`「${purpose}」钉住了:${slot ? slot["provider"] + " / " + slot["model"] : "已保存"}${pinned ? " · 协议 " + pinned : ""}`);
    // 存完重读:让界面显示后端真实的口径,而不是我刚输入的那份。
    // 重绘会把列表整段换掉 → 先记滚动位置,画完再放回去(否则作者每次保存都被弹回顶部)。
    const _savedScroll = _captureScroll($("settings-overlay"));
    await loadRoutes();
    _restoreScroll(_savedScroll);
    requestAnimationFrame(() => _restoreScroll(_savedScroll));
  } catch (e) { toast(e.message, true); }
}

// ── 并发上限(2026-10-25 加;2026-10-27 作者改成 2–8 并升级成**全局闸**)────────
// 它管的不再只是"议事时几个角色在跑",而是**所有会调用模型的动作**共用一个上限
// (闸装在 backends 层,见 loom/concurrency.py)。写章流水线本来就是串行,不吃这一格。
function paintConcurrency(d) {
  const sel = $("max-concurrency");
  const hint = $("conc-hint");
  if (!sel) return;
  const cur = Number((d && d["并发上限"]) || 2);
  sel.value = String(cur);
  if (hint) {
    hint.textContent = cur >= 5
      ? `${cur} 个同时:主要给托管 API;本机单卡通常是抢显存、反而更慢。`
      : `同时最多 ${cur} 个模型调用;多出来的排队,不会丢。`;
  }
}

async function saveConcurrency() {
  const sel = $("max-concurrency");
  if (!sel) return;
  if (!DATA || !DATA.root) { toast("先打开一本书", true); return; }
  const v = Number(sel.value) || 2;
  try {
    const d = await jreq("POST", "/api/routes/concurrency", { root: DATA.root, value: v });
    if (d && d.ok === false) { toast(d.error || d.reason || "没存进去。", true); return; }
    paintConcurrency({ "并发上限": (d && d["并发上限"]) || v });
    if (_panel) { _panel.cap = (d && d["并发上限"]) || v; paintPanelStrip(); }   // 议事厅正在开会 → 立刻用新上限
    toast((d && d.message) || `并发上限改成 ${v} 了`);
  } catch (e) { toast(e.message, true); }
}

function openSettings() {
  $("settings-overlay").classList.remove("hidden");
  const sel = $("max-concurrency");
  if (sel) sel.onchange = saveConcurrency;
  loadRoutes();          // 槽表每次都现读:别让界面拿着过期的配置
  loadWritingMode();     // A1 写作模式三档:每次打开现读(书级覆盖可能刚被别处改过)
  // F2:记忆库栏(默认折叠;展开才取数)。每次开设置重挂一次,换书后读的自然是新书的根。
  const mph = $("mem-panel-host");
  if (mph && window.StudyCore && StudyCore.memoryPanel) {
    mph.replaceChildren(StudyCore.memoryPanel(DATA && DATA.root));
  }
}

// ── 写作模式只读显示(2026-11-03 作者裁定·模式终身绑定)──────────────────
// GET /api/writing_mode → {global_mode, book_mode, effective, busy}。
// 设置页 #wm-fold 组已删、setWritingMode 已删——模式开书时选定终身绑定，
// 写作页左上角 #wm-readonly-badge 只读显示，不可点。
let _wmLoaded = "";     // 上次画进 badge 的档,避免重复改 DOM

async function loadWritingMode() {
  if (!DATA || !DATA.root) return;
  let d;
  try { d = await jreq("GET", `/api/writing_mode?root=${encodeURIComponent(DATA.root)}`); }
  catch (e) { const b = $("wm-readonly-badge"); if (b) b.title = "读不到写作模式:" + e.message; return; }
  paintWritingMode(d);
}

function paintWritingMode(d) {
  /*2026-11-03（模式终身绑定）：只服务写作页左上角只读 badge #wm-readonly-badge。
    无 #wm-select/#wm-badge/#wm-hint（设置页组已删）、无 #wm-quick（早删）。 */
  const badge = $("wm-readonly-badge");
  if (!badge) return;
  const eff = (d && d.effective) || "FullAuto";
  badge.textContent = "AI 介入：" + eff;
  badge.title = "这本书开书时选定的 AI 介入程度，终身绑定，不可更改";
  badge.classList.remove("hidden");
  _wmLoaded = eff;
}

// ── A2 分卷分章树(2026-11-02 作者手绘图)──────────────────────────────
// GET /api/chapters/tree → {writing_mode, 卷:[{卷,起,止,状态,章:[…],引导}]}
// 卷头「卷名（n/m 章）」可折叠;章行 = 状态图标 + 章名;已完成可点开;
// 可写章行内「编织这章/编织细纲」;教学=按钮隐藏、✎ 仍显示。
// 每卷默认显示前 10 章,尾部「显示全部」弹浮窗。
const VT_SHOW_N = 10;
const VT_EXPANDED_KEY = "loom_voltree_expanded";
let _vtData = null;
let _vtMode = "FullAuto";   // ③混合流:当前书写作模式缓存(paintVolumeTree 刷)

function _vtExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem(VT_EXPANDED_KEY) || "[]")); }
  catch (_) { return new Set(); }
}
function _vtSetExpanded(set) {
  try { localStorage.setItem(VT_EXPANDED_KEY, JSON.stringify([...set])); } catch (_) { /* 忽略 */ }
}

async function loadVolumeTree() {
  const box = $("vol-tree");
  if (!box) return;
  if (!DATA || !DATA.root) { box.innerHTML = ""; return; }
  let d;
  try { d = await jreq("GET", `/api/chapters/tree?root=${encodeURIComponent(DATA.root)}`); }
  catch (e) { box.innerHTML = `<p class="hint">分卷没读出来:${escHtml(e.message)}</p>`; return; }
  if (!DATA || !DATA.root) return;   // 迟到响应不许画到另一本书上
  _vtData = d;
  // 首次展开：默认把第一卷有章的展开（手绘图预期一进来就看得见章）；用户折叠过的记住他的选择
  const expanded = _vtExpanded();
  if (!localStorage.getItem(VT_EXPANDED_KEY)) {
    const first = ((d && d["卷"]) || []).find((v) => (v["章"] || []).length);
    if (first) expanded.add(first["卷"]);
    _vtSetExpanded(expanded);
  }
  paintVolumeTree(d);
}

function paintVolumeTree(d) {
  const box = $("vol-tree");
  if (!box) return;
  box.innerHTML = "";
  const mode = (d && d.writing_mode) || "FullAuto";
  _vtMode = mode;   // ③混合流:树行审查按钮/教学点章行为按模式走(保存 manual 分流也读它)
  const expanded = _vtExpanded();
  const vols = (d && d["卷"]) || [];
  if (!vols.length) {
    const guide = (d && d["引导"]) || "还没排卷纲。先找领航员或大纲师确认卷纲，才会生成分卷结构。";
    box.innerHTML = `<p class="hint">${escHtml(guide)}</p>`;
    return;
  }

  for (const v of vols) {
    const det = document.createElement("details");
    det.className = "vt-vol";
    det.open = expanded.has(v["卷"]);
    det.addEventListener("toggle", () => {
      const s = _vtExpanded();
      if (det.open) s.add(v["卷"]); else s.delete(v["卷"]);
      _vtSetExpanded(s);
    });
    const sum = document.createElement("summary");
    const missing = v["状态"] === "卷纲缺起止";
    sum.innerHTML = `<span class="vt-vol-name">${escHtml(v["卷"] || "(未命名卷)")}</span>` +
      `<span class="side-sub">${escHtml(missing ? "缺起止" : (v["状态"] + " 章"))}</span>`;
    det.appendChild(sum);

    if (missing || (v["引导"] && !(v["章"] || []).length)) {
      const p = document.createElement("p");
      p.className = "hint vt-guide";
      p.textContent = v["引导"] || "这一卷没写「起止:第a-b章」，先找大纲师补。";
      det.appendChild(p);
      box.appendChild(det);
      continue;
    }

    const ul = document.createElement("ul");
    ul.className = "list vt-chapters";
    /*2026-11-02（作者截图裁定）：侧栏树是**进度视图**——锁定章（·）不显示，
    只留已完成（✓）与当前可写（✎ + 编织按钮）。全量清单在「显示全部」浮窗里看。 */
    const chapters = v["章"] || [];
    const visible = chapters.filter((c) => c["状态"] !== "锁定");
    const shown = visible.slice(0, VT_SHOW_N);
    for (const c of shown) ul.appendChild(vtChapterRow(c, mode, false));
    if (visible.length > shown.length) {
      const more = document.createElement("li");
      more.className = "vt-more";
      more.textContent = `显示全部 ${chapters.length} 章`;
      more.onclick = () => showAllChapters(v, mode);
      ul.appendChild(more);
    }
    det.appendChild(ul);
    box.appendChild(det);
  }
}

function vtChapterRow(c, mode, showLocked) {
  /*showLocked=false（侧栏进度视图）时锁定章不渲染；浮窗传 true 显示全量。*/
  if (!showLocked && c["状态"] === "锁定") return null;
  const li = document.createElement("li");
  li.className = "vt-ch";
  const iconTxt = c["状态"] === "已完成" ? "✓" : (c["可写"] ? "✎" : "·");
  const stateCls = c["状态"] === "已完成" ? " done" : (c["可写"] ? " writable" : " locked");
  const label = document.createElement("span");
  label.className = "vt-ch-label" + stateCls;
  label.innerHTML = `<span class="vt-ico${stateCls}">${iconTxt}</span> 第${c["章"]}章`;
  li.appendChild(label);
  /*2026-11-02（作者裁定③）：「纲」徽标**可点**——弹只读浮窗显示该章卡章纲内容
  （GET /api/chapters/outline，db749946 加的端点）。之前是纯标记点不开。 */
  if (c["有卡章纲"]) {
    const outline = document.createElement("button");
    outline.className = "vt-outline";
    outline.type = "button";
    outline.textContent = "纲";
    outline.title = "看这一章的卡章纲";
    outline.onclick = (e) => { e.stopPropagation(); showChapterOutline(c["章"]); };
    label.appendChild(outline);
  }
  /*③混合流（2026-11-03）：有审稿留痕的章给「审」徽标——点开只读浮窗渲染 .审稿留痕/第N章.md。 */
  if (c["有审稿"]) {
    const rv = document.createElement("button");
    rv.className = "vt-outline vt-review";
    rv.type = "button";
    rv.textContent = "审";
    rv.title = "看这一章的审稿意见（责任编辑/编辑总监留痕）";
    rv.onclick = (e) => { e.stopPropagation(); showChapterReview(c["章"]); };
    label.appendChild(rv);
  }
  if (c["状态"] === "已完成") {
    /*③教学模式：点章直接开**该章细纲**（作者从细纲写起），编辑器顶部提示。
      辅助/FullAuto 照旧开正文。 */
    const rel = _vtMode === "教学"
      ? `正文/.细纲/第${c["章"]}章.md`
      : c["路径"];
    label.title = _vtMode === "教学" ? "打开这一章的细纲(教学模式从细纲写起)" : "打开这一章";
    label.style.cursor = "pointer";
    label.onclick = () => openFile(rel, true, c["章"], null);
  } else if (c["可写"] && mode !== "教学") {
    const btn = document.createElement("button");
    btn.className = "mini vt-weave";
    btn.textContent = mode === "FullAuto" ? "编织这章" : "编织细纲";
    btn.title = mode === "FullAuto"
      ? `织完整章（六棒跑满,会调模型）`
      : `织到章节设定师停（细纲+设定校验,正文你自己来）`;
    btn.onclick = (e) => {
      e.stopPropagation();
      writeChapter(c["章"], false);
    };
    li.appendChild(btn);
  }
  /*③混合流：辅助/教学模式的可写或已完成章 → 手动审查按钮（跑完即停,会调模型）。
    教学两颗都在（后端 403 兜底）;辅助只给责任编辑（总监审全文走编辑总监）。 */
  if (mode !== "FullAuto" && (c["可写"] || c["状态"] === "已完成")) {
    const steps = mode === "教学" ? ["责任编辑", "编辑总监"] : ["责任编辑"];
    for (const step of steps) {
      const b = document.createElement("button");
      b.className = "mini vt-step";
      b.textContent = step === "责任编辑" ? "跑责任编辑" : "跑编辑总监";
      b.title = `手动跑「${step}」这一棒:按现在的细纲/正文打分纠错,跑完即停（会调模型）`;
      b.onclick = (e) => { e.stopPropagation(); runStep(c["章"], step); };
      li.appendChild(b);
    }
  }
  return li;
}

/* ── ③混合流：手动单棒 + 手写落位 + 审稿留痕展示（2026-11-03）─────────────
   run-step:从指定棒起跑跑完即停(NDJSON 同 /api/write,复用织章浮窗消费);
   manual:编辑器保存正文/细纲时按模式分流(非 FullAuto 才走,warning 软提示);
   审稿:点「审」徽标弹只读浮窗 mdToHtml 渲染。 */
async function runStep(n, step) {
  if (_weaving) { toast("这一章还在跑,等它跑完,或先点「停」", true); return; }
  if (!(await _ensureBook())) return;
  try { await persistBackend(true); }
  catch (e) {
    if (isContextLenInputError(e)) { toast(e.message, true); return; }
    console.warn("顺手保存后端失败(不阻断):", e);
  }
  if (!(await preflightCheck(n, false))) return;
  if (DATA.writing_unlocked === false) { openGateGuide(DATA.missing || []); return; }
  // F1:混合流单棒入口同样受第一章地基硬门约束(后端 /api/chapter/run-step 也拦);审稿棒按 T1 豁免
  const _fpre = await foundationPrecheck(n, step);
  if (_fpre) { handleFoundationBlock(_fpre, () => runStep(n, step)); return; }

  let resp;
  try {
    resp = await fetch("/api/chapter/run-step", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: DATA.root, chapter: n, step }),
    });
  } catch (e) {
    logRun("连接失败:" + e.message, "err"); showRunClose();
    _weaving = false; renderJourney(); return;
  }
  const ct = resp.headers.get("content-type") || "";
  if (!ct.includes("ndjson")) {
    const d = await resp.json().catch(() => ({}));
    if (handleFoundationBlock(d, () => runStep(n, step))) return;   // F1：门禁不闪流式态
    openRunUI(n, `第 ${n} 章 · 手动跑「${step}」…`);
    logRun(d.error || `失败 (${resp.status})`, "err", "cross");
    $("run-title").textContent = `「${step}」没能开始`;
    $("run-force").classList.add("hidden");
    showRunClose(); _weaving = false; renderJourney();
    return;
  }
  openRunUI(n, `第 ${n} 章 · 手动跑「${step}」…`);   // 确认是 NDJSON：到这里才进流式态
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let broken = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev = null;
        try { ev = JSON.parse(line); } catch (e) { continue; }
        handleEvent(ev);
      }
    }
  } catch (e) {
    broken = true;
    $("run-strip").classList.add("err");
    logRun("跑到一半断了:" + (e.message || e), "err", "cross");
  } finally {
    _stopRunTicker(); hideStrip(); _runMinimized = false;
    showRunClose(broken);
    _weaving = false; renderJourney();
    loadVolumeTree();   // 审稿留痕可能出现:「审」徽标要亮出来
  }
}

/* 「审」徽标浮窗:读 .审稿留痕/第N章.md,mdToHtml 渲染(套路同 showChapterOutline)。 */
async function showChapterReview(chapter) {
  if (!DATA || !DATA.root) return;
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.id = "vt-review-overlay";
  const card = document.createElement("div");
  card.className = "run-card review-card";
  const pane = document.querySelector(".editor-pane");
  const fitReview = () => {
    if (!pane) return;
    const rect = pane.getBoundingClientRect();
    overlay.style.left = Math.max(0, rect.left) + "px";
    overlay.style.width = Math.min(rect.width, window.innerWidth) + "px";
  };
  fitReview();
  const observer = window.ResizeObserver && pane ? new ResizeObserver(fitReview) : null;
  observer?.observe(pane);
  window.addEventListener("resize", fitReview);
  const dismiss = () => { observer?.disconnect(); window.removeEventListener("resize", fitReview); overlay.remove(); };
  overlay.addEventListener("keydown", (ev) => { if (ev.key === "Escape") dismiss(); });
  const h = document.createElement("h3");
  h.textContent = `第 ${chapter} 章 · 审稿意见`;
  card.appendChild(h);
  const body = document.createElement("div");
  body.className = "md-preview";
  body.textContent = "读取中…";
  card.appendChild(body);
  const row = document.createElement("div");
  row.className = "row-end";
  const close = document.createElement("button");
  close.className = "ghost"; close.textContent = "关闭";
  close.onclick = dismiss;
  row.appendChild(close); card.appendChild(row);
  overlay.appendChild(card);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) dismiss(); });
  document.body.appendChild(overlay);
  close.focus({ preventScroll: true });
  try {
    const d = await jreq("GET", `/api/file?root=${encodeURIComponent(DATA.root)}&rel=${encodeURIComponent(".审稿留痕/第" + chapter + "章.md")}`);
    if (d && d.content && d.content.trim()) body.innerHTML = mdToHtml(d.content);
    else body.textContent = "这一章还没有审稿留痕。";
  } catch (e) {
    body.textContent = "读不到审稿留痕：" + e.message;
  }
}

/* 该章卡章纲只读浮窗（作者裁定③：点「纲」看大纲）。走现有 mdToHtml 渲染。 */
async function showChapterOutline(chapter) {
  if (!DATA || !DATA.root) return;
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.id = "vt-outline-overlay";
  const card = document.createElement("div");
  card.className = "run-card settings-card";
  card.style.maxHeight = "78vh";
  card.style.overflowY = "auto";
  const h = document.createElement("h3");
  h.textContent = `第 ${chapter} 章 · 卡章纲`;
  card.appendChild(h);
  const body = document.createElement("div");
  body.className = "md-preview";
  body.textContent = "读取中…";
  card.appendChild(body);
  const row = document.createElement("div");
  row.className = "row-end";
  const close = document.createElement("button");
  close.className = "ghost";
  close.textContent = "关闭";
  close.onclick = () => overlay.remove();
  row.appendChild(close);
  card.appendChild(row);
  overlay.appendChild(card);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  try {
    const d = await jreq("GET", `/api/chapters/outline?root=${encodeURIComponent(DATA.root)}&chapter=${chapter}`);
    if (d && d["有"] && d["内容"]) body.innerHTML = mdToHtml(d["内容"]);
    else body.textContent = "这一章还没有卡章纲。";
  } catch (e) {
    body.textContent = "读不到卡章纲：" + e.message;
  }
}

function showAllChapters(vol, mode) {
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.id = "vt-all-overlay";
  const card = document.createElement("div");
  card.className = "run-card settings-card";
  card.style.maxHeight = "80vh";
  card.style.overflowY = "auto";
  const h = document.createElement("h3");
  h.textContent = vol["卷"] + "（" + vol["状态"] + " 章）";
  card.appendChild(h);
  const ul = document.createElement("ul");
  ul.className = "list vt-chapters";
  /*「显示全部」浮窗 = 查全貌：含锁定章（与侧栏进度视图职责不同）。*/
  for (const c of vol["章"] || []) {
    const row = vtChapterRow(c, mode, true);
    if (row) ul.appendChild(row);
  }
  card.appendChild(ul);
  const row = document.createElement("div");
  row.className = "row-end";
  const close = document.createElement("button");
  close.className = "ghost";
  close.textContent = "关闭";
  close.onclick = () => overlay.remove();
  row.appendChild(close);
  card.appendChild(row);
  overlay.appendChild(card);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

//2026-11-03（模式终身绑定）：写作模式切档绑定已删——设置页下拉组删除，模式终身绑定不可改。
// 引导用户去填 key:先打开设置,再聚焦 + 闪对应输入框
function flashSetting(id) {
  openSettings();
  setTimeout(() => {
    const k = $(id);
    if (k) { k.focus(); k.classList.add("flash"); setTimeout(() => k.classList.remove("flash"), 1600); }
  }, 60);
}

let _typingTimer = null;
function noteTyping() {
  if (!CUR || !CUR.editable) return;
  document.body.classList.add("typing");
  clearTimeout(_typingTimer);
  _typingTimer = setTimeout(() => document.body.classList.remove("typing"), 1500);
}

// ---------- 编辑器行带测量(镜像 div):textarea 软换行下,逻辑行 → 视觉像素带 ----------
// 一行一个同排版 div(\n 本就强制断行,逐行分块不改变各行的软换行),offsetTop/Height 即视觉带。
let _bandsCache = null;   // {text, width, bands} 内容与宽度没变就复用,input/resize 自然失效
function lineBands() {
  const ed = $("editor");
  const width = ed.clientWidth;
  if (_bandsCache && _bandsCache.text === ed.value && _bandsCache.width === width) return _bandsCache.bands;
  const cs = getComputedStyle(ed);
  let m = document.getElementById("editor-mirror");
  if (!m) { m = document.createElement("div"); m.id = "editor-mirror"; document.body.appendChild(m); }
  ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
   "paddingTop", "paddingBottom", "paddingLeft", "paddingRight", "wordBreak", "overflowWrap"]
    .forEach((p) => { m.style[p] = cs[p]; });
  Object.assign(m.style, { position: "absolute", visibility: "hidden", left: "-9999px", top: "0",
                           boxSizing: "border-box", width: width + "px", whiteSpace: "pre-wrap" });
  m.innerHTML = "";
  const lines = ed.value.split("\n");
  const rows = lines.map((line) => {
    const d = document.createElement("div");
    d.textContent = line || "\u200b";   // 空行塞零宽空格,也占一行高(用转义写法,别粘不可见字符)
    m.appendChild(d);
    return d;
  });
  const bands = []; let pos = 0;
  lines.forEach((line, i) => {
    bands.push({ start: pos, end: pos + line.length, top: rows[i].offsetTop, bottom: rows[i].offsetTop + rows[i].offsetHeight });
    pos += line.length + 1;
  });
  _bandsCache = { text: ed.value, width, bands };
  return bands;
}

// ---------- 行内改写面板(chapter/brain 两用;中央弹层已退役) ----------
let _rwMode = "chapter";   // chapter=正文(快照铁律走 /api/rewrite/apply) | brain=设定改写 | brain-continue=设定续写
function showInlinePanel({ src, placeholder, goLabel }) {
  _rewriteText = "";
  $("rewrite-src").textContent = src || "";
  $("rewrite-src").classList.toggle("hidden", !src);
  $("rewrite-instruction").value = "";
  $("rewrite-instruction").placeholder = placeholder;
  $("rewrite-error").textContent = "";
  $("rewrite-result").textContent = "";
  $("rewrite-result").classList.add("hidden");
  $("rewrite-go").textContent = goLabel || "重写";
  $("rewrite-go").classList.remove("hidden");
  $("rewrite-again").classList.add("hidden");
  $("rewrite-apply").classList.add("hidden");
  hideSealBar();
  positionInlinePanel();
  $("rewrite-instruction").focus();
}
function hideInlinePanel() { $("rewrite-inline").classList.add("hidden"); _rewriteSel = null; _rewriteText = ""; }
function positionInlinePanel() {
  const ed = $("editor"), panel = $("rewrite-inline");
  const bands = lineBands();
  let li = ed.value.slice(0, ed.selectionEnd).split("\n").length - 1;
  li = Math.min(li, bands.length - 1);
  const r = ed.getBoundingClientRect();
  panel.classList.remove("hidden");                       // 先显示才能量高度
  let y = r.top + (bands[li] ? bands[li].bottom : 0) - ed.scrollTop + 6;
  const ph = panel.offsetHeight || 180;
  y = Math.max(r.top + 8, Math.min(y, r.bottom - ph - 8));
  panel.style.top = y + "px";
  panel.style.left = Math.round(r.left + 24) + "px";
  panel.style.width = Math.max(320, r.width - 48) + "px";
}

function hideSealBar() { $("seal-bar").classList.remove("on"); }
function maybeSealBar() {
  const ed = $("editor"), bar = $("seal-bar");
  // 只在可编辑的正文章节上出;选区太短(<2 字)不值得打扰
  if (!CUR || CUR.chapter == null || ed.readOnly || ed.classList.contains("hidden")) return hideSealBar();
  const s = ed.selectionStart, e = ed.selectionEnd;
  if (e - s < 2) return hideSealBar();
  // 按行号近似定位选区首行(软换行会有偏差,夹在编辑器内即可——从简,不引库)
  const lineNo = ed.value.slice(0, s).split("\n").length - 1;
  const lh = parseFloat(getComputedStyle(ed).lineHeight) || 32;
  const padTop = parseFloat(getComputedStyle(ed).paddingTop) || 0;
  const r = ed.getBoundingClientRect();
  let y = r.top + padTop + lineNo * lh - ed.scrollTop - 44;
  y = Math.max(r.top + 8, Math.min(y, r.bottom - 52));
  bar.classList.add("on");                      // 先显示才能量宽度
  const bw = bar.offsetWidth || 140;            // 显式居中,不用 transform(会跟出场动画打架)
  bar.style.top = y + "px";
  bar.style.left = Math.round(r.left + r.width / 2 - bw / 2) + "px";
}

// ---------- 悬停行级「改」入口:鼠标移到正文某行,行右缘浮出;点击=选中该逻辑行并就地改 ----------
let _hoverLine = -1, _lineBtnRaf = 0;
function lineAtY(clientY) {
  const ed = $("editor");
  const y = clientY - ed.getBoundingClientRect().top + ed.scrollTop;
  const bands = lineBands();
  for (let i = 0; i < bands.length; i++) if (y >= bands[i].top && y < bands[i].bottom) return i;
  return -1;
}
function maybeLineEditBtn(e) {
  const ed = $("editor"), btn = $("line-edit-btn");
  // 只在可编辑正文章节、非预览、面板没开时出;打字中由 body.typing 的 CSS 压住
  if (!CUR || CUR.chapter == null || ed.readOnly || ed.classList.contains("hidden")
      || !$("rewrite-inline").classList.contains("hidden")) return hideLineEditBtn();
  const i = lineAtY(e.clientY);
  const bands = lineBands();
  if (i < 0) return hideLineEditBtn();   // 行号越界
  const lineText = ed.value.slice(bands[i].start, bands[i].end);
  if (lineText.trim().length < 2) return hideLineEditBtn();   // 空行/纯空白行不值得改
  const r = ed.getBoundingClientRect();
  const y = r.top + bands[i].top - ed.scrollTop;
  if (y < r.top || y > r.bottom - 24) return hideLineEditBtn();               // 行带滚出视口
  _hoverLine = i;
  btn.style.top = Math.round(y) + "px";
  btn.style.left = Math.round(r.right - 44) + "px";
  btn.classList.add("on");
}
function hideLineEditBtn() { _hoverLine = -1; $("line-edit-btn").classList.remove("on"); }
function lineEditClick() {
  if (_hoverLine < 0) return;
  const b = lineBands()[_hoverLine];
  if (!b || $("editor").value.slice(b.start, b.end).trim().length < 2) return hideLineEditBtn();
  const ed = $("editor");
  ed.setSelectionRange(b.start, b.end);
  ed.focus();
  hideLineEditBtn();
  openRewrite();
}

// ---------- 全局快捷键 ----------
function anyOverlayOpen() {
  return [...document.querySelectorAll(".overlay")].some((o) => !o.classList.contains("hidden"));
}
// Esc 关闭时要做清理的面板(只写有副作用的那几个;其余默认就是加 hidden)
const OVERLAY_CLOSERS = {
  "merge-overlay": () => closeMergePanel(),
  "style-overlay": () => closeStylePanel(),
  "know-overlay": () => $("know-overlay").classList.add("hidden"),
  "align-overlay": () => $("align-overlay").classList.add("hidden"),
  "snap-overlay": () => closeSnapPanel(),
};

function closeTopOverlay() {
  if (!$("cmdk").classList.contains("hidden")) { closeCmdk(); return true; }
  if ($("seal-bar").classList.contains("on")) { hideSealBar(); return true; }
  if (!$("rewrite-inline").classList.contains("hidden")) { hideInlinePanel(); return true; }
  // 按 DOM 里真实开着的 .overlay 关最上面那个。以前是手写名单,新面板加进来忘了登记就 Esc 关不掉
  // (合并/文风/快照/选目录/自检/镜像 六个都漏过)。收尾动作挂在 CLOSERS 上,别的地方不用再记一份名单。
  const _open = [...document.querySelectorAll(".overlay")].filter((o) => !o.classList.contains("hidden"));
  for (let i = _open.length - 1; i >= 0; i--) {
    const el = _open[i];
    const id = el.id;
    if (id === "run-overlay" && $("run-close").classList.contains("hidden")) { minimizeRun(); return true; } // 写作中 Esc=收起后台织,不误关
    // 半截稿正在抽 = 正在花钱。Esc 一关,「停」就藏起来了,人只能看着账烧完(交叉审查 P1)。
    if (id === "half-overlay" && HALF_BUSY) {
      toast("正在抽(在花钱):先点「停」再关。", true);
      return true;
    }
    // 知识顾问/大纲复核正在调模型 = 正在花钱。Esc 一关,进度和「还没看到的结果」都没了。
    if (id === "know-overlay" && KNOW_BUSY) {
      toast("顾问正在想(在花钱):等它回完再关。", true);
      return true;
    }
    if (id === "align-overlay" && ALIGN_BUSY) {
      toast("正在逐章对照(在花钱):等它回完再关。", true);
      return true;
    }
    const closer = OVERLAY_CLOSERS[id];
    if (closer) closer();
    else el.classList.add("hidden");
    return true;
  }
  if (_searchOn) { closeSearch(); return true; }
  if (document.body.classList.contains("focus-mode")) { exitFocus(); return true; }
  return false;
}
function globalKeys(e) {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); $("cmdk").classList.contains("hidden") ? openCmdk() : closeCmdk(); return; }
  if (e.key === "Escape") { if (closeTopOverlay()) e.preventDefault(); return; }
  // 命令面板开着时,其余快捷键交给它自己处理
  if (!$("cmdk").classList.contains("hidden")) return;
  // 以下仅在已进入项目时生效
  if ($("app").classList.contains("hidden")) return;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); if (CUR && CUR.editable) saveFile(); return; }
  if (mod && e.key === "Enter") { e.preventDefault(); if (!anyOverlayOpen() && $("rewrite-inline").classList.contains("hidden") && DATA) writeChapter(DATA.next_chapter, false); return; }
  if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); if (CUR) openSearch(); return; }
  if (mod && e.key === ".") { e.preventDefault(); toggleFocus(); return; }
}

// ---------- 新手引导(onboarding):T0 就地提示 + T1 样例首跑 ----------
// 都用 localStorage 只显示一次。T0 不挡屏(JIT 气泡);T1 暗背景聚光引导样例。
const _seenOnce = (k) => localStorage.getItem(k) === "1";
const _markOnce = (k) => localStorage.setItem(k, "1");

// 把浮层放到目标附近:优先下方,放不下翻到上方;水平夹在视口内,箭头对准目标中心。
function _placeBubble(bubble, target, gap = 12) {
  const r = target.getBoundingClientRect();
  const bw = bubble.offsetWidth, bh = bubble.offsetHeight;
  let top = r.bottom + gap, below = true;
  if (top + bh > window.innerHeight - 8) { top = r.top - bh - gap; below = false; }
  let left = r.left + r.width / 2 - bw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
  bubble.style.top = Math.max(8, top) + "px";
  bubble.style.left = left + "px";
  bubble.classList.toggle("below", below);
  bubble.classList.toggle("above", !below);
  const arrowX = r.left + r.width / 2 - left;  // 箭头相对浮层左缘
  bubble.style.setProperty("--arrow-x", Math.max(16, Math.min(arrowX, bw - 16)) + "px");
}

// T0:写完一章、章稿落进编辑器那一刻,教 learn 闭环(只第一次)。
function maybeCoachLearnLoop() {
  if (_seenOnce("loom_coach_learnloop")) return;
  const btn = $("btn-learn");
  if (!btn || btn.classList.contains("hidden")) return;  // learn 按钮没出来就不提示
  _markOnce("loom_coach_learnloop");
  const pop = document.createElement("div");
  pop.className = "coach-pop";
  pop.innerHTML =
    `<div class="cp-body">这版是 <b>AI 写的,还不像你</b>。在编辑器里按你的习惯改几句,` +
    `再点上面的 <b>「学这章的手改」</b>——它只学<b>你的改动</b>、蒸馏进写作指纹,` +
    `于是<b>越写越像你</b>。这是 Loom 的灵魂。</div>` +
    `<div class="cp-foot"><button class="primary mini" id="cp-ok">懂了,去改稿</button></div>`;
  pop.prepend(agentAvatar("领航员", "jc-ava", "jc-fallback"));   // 写后气泡挂领航员头像(Task 3)
  document.body.appendChild(pop);
  btn.classList.add("pulse-gold");
  _placeBubble(pop, btn);
  const reposition = () => _placeBubble(pop, btn);
  const close = () => { pop.remove(); btn.classList.remove("pulse-gold"); window.removeEventListener("resize", reposition); };
  window.addEventListener("resize", reposition);
  $("cp-ok").onclick = close;
  btn.addEventListener("click", close, { once: true });  // 真去点 learn 了也撤掉
}