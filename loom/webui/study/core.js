"use strict";

/* 书房共享工具：只读请求、DOM 安全构造、主题与正文导航。 */
window.StudyCore = (() => {
  let role = null;
  let volumeSeq = 0;
  let volumeReturn = null;

  const API = {
    askReady: "/api/study/ask/ready",
    ask: "/api/study/ask",
    taskSnapshot: "/api/study/ask/task",   /* 夜间补缺缺口4:书房任务快照(零模型) */
    reject: "/api/agent/reject",           /* 夜间补缺缺口4:对话内拒绝原话通道 */
    volume: "/api/study/volume",
    archives: "/api/style/archives",
    archive: "/api/style/library",
    archiveFile: "/api/style/file",
    material: "/api/study/material",
    videoReady: "/api/study/video/ready",
    videoFetch: "/api/study/video/fetch",
    chapterOutline: "/api/chapters/outline",
  };
  const TIMEOUT = { probe: 800, file: 5000, answerIdle: 120000, videoIdle: 600000 };

  function makeNode(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = String(text == null ? "" : text);
    return el;
  }

  async function safeFetch(url, options = {}) {
    try {
      const r = await fetch(url, {
        ...options,
        cache: options.cache || "no-store",
        signal: options.signal || AbortSignal.timeout(options.timeout || TIMEOUT.probe),
      });
      if (!r.ok) return null;
      const data = await r.json();
      return data && typeof data === "object" ? data : null;
    } catch (_) { return null; }
  }

  /* 同一本书的根比较（H-01 首页自动回书）：注册表存的是 resolve 过的路径，localStorage
     存的是服务端回显的原始路径，Windows 下大小写/分隔符/末尾斜杠都可能不同。所以先精确比、
     再规范化比；否则会把**在架**的书误判成“不在架”，反而挡住正常自动回书。
     首页 app.js 与书房 workbench.js 共用这一份，避免两处判据漂开。 */
  function sameRoot(a, b) {
    const norm = (s) => String(s || "").replace(/[\\/]+/g, "/").replace(/\/+$/, "").toLowerCase();
    if (!a || !b) return false;
    return a === b || norm(a) === norm(b);
  }

  /* ── 记忆库面板（F2，契约见 知识侧 审查记录）───────────────────────
     端点：GET /api/memory/health?root=（root 可空＝只看全库）；POST /api/memory/recover（无参，同步短阻塞）。
     硬口径（按契约，别自行发明）：
       ①不支持 = 值为 null + unsupported 里一句原因 → 显示"不支持（原因）"，**绝不渲染成 0**；
       ②读数失败时 总条数=null 且 错误 非空 → 与"库确实是空"分开说；
       ③置顶/软删除本版无字段（null+unsupported）→ 直接不显示这两行；过时是另一概念，单列；
       ④recover 判据只看 loaded/ok，**HTTP 200 不算成功**；attempted=false=压根没试载；
       ⑤无进度端点、无事件流 → **不轮询**；超时≠失败，超时后按钮恢复可点并再取真实读数；
       ⑥语义引擎是进程内 ONNX，没有服务可重启 → 文案不许写"重启服务"。 */
  function memoryPanel(root) {
    const box = makeNode("details", "route-fold mem-panel");
    const sum = makeNode("summary", "");
    sum.appendChild(makeNode("span", "rf-title", "记忆库"));
    sum.appendChild(makeNode("span", "rf-badge", "本地向量检索"));
    sum.appendChild(makeNode("span", "rf-chev", "▸"));
    box.appendChild(sum);
    const host = makeNode("div", "mem-panel-body");
    box.appendChild(host);
    let loaded = false;
    // 默认折叠：展开才取数（派单要求），且只自动取一次；之后用面板里的「刷新」。
    box.addEventListener("toggle", () => {
      if (!box.open || loaded) return;
      loaded = true;
      loadMemoryHealth(host, root);
    });
    return box;
  }

  function _memUnsupported(d, key) {
    const u = (d && d.unsupported) || {};
    return (u && typeof u === "object" && u[key]) ? String(u[key]) : "";
  }

  // null → "不支持"（带原因）；0 是真实读数，照渲染。两者绝不混。
  function _memValue(d, key) {
    const v = d ? d[key] : null;
    if (v === null || v === undefined) {
      const why = _memUnsupported(d, key);
      return why ? `不支持（${why}）` : "不支持";
    }
    return String(v);
  }

  function _memRows(host, pairs) {
    const wrap = makeNode("div", "mem-rows");
    pairs.forEach(([k, v]) => {
      const row = makeNode("div", "mem-row");
      row.appendChild(makeNode("b", "", k + "："));
      row.appendChild(makeNode("span", "hint", v));
      wrap.appendChild(row);
    });
    host.appendChild(wrap);
  }

  async function loadMemoryHealth(host, root, note, noteIsError) {
    host.replaceChildren();
    if (note) {
      const _np = makeNode("p", "hint", note);
      if (noteIsError) _np.style.color = "#c0392b";   // 红字报错（设计稿 §2.2/§2.3）
      host.appendChild(_np);
    }
    host.appendChild(makeNode("p", "hint", "读取中…"));
    const url = "/api/memory/health" + (root ? "?root=" + encodeURIComponent(root) : "");
    const d = await safeFetch(url, { timeout: TIMEOUT.file });
    if (!d) {
      host.replaceChildren();
      if (note) {
        const _np = makeNode("p", "hint", note);
        if (noteIsError) _np.style.color = "#c0392b";
        host.appendChild(_np);
      }
      host.appendChild(makeNode("p", "hint", "状态读不到（接口没回应）。可以点「刷新」再试。"));
      host.appendChild(_memFoot(host, root));
      return;
    }
    renderMemoryHealth(host, d, root, note, noteIsError);
  }

  function renderMemoryHealth(host, d, root, note, noteIsError) {
    host.replaceChildren();
    if (note) {
      const _np = makeNode("p", "hint", note);
      if (noteIsError) _np.style.color = "#c0392b";   // 红字报错
      host.appendChild(_np);
    }

    const err = d && d["错误"];
    if (err) host.appendChild(makeNode("p", "hint", "读数出错了：" + err + "（下面标「不支持」的项因此读不到）"));

    const scope = (d && d["范围"]) || {};
    const pairs = [];
    if (scope && (scope["本书"] != null || scope["全局"] != null)) {
      pairs.push(["范围", `本书 ${scope["本书"] == null ? "?" : scope["本书"]} · 全局 ${scope["全局"] == null ? "?" : scope["全局"]}`]);
    }
    pairs.push(["总条数", _memValue(d, "总条数")]);
    pairs.push(["已向量化", _memValue(d, "已向量化")]);
    pairs.push(["维度", _memValue(d, "维度")]);
    pairs.push(["过时", _memValue(d, "过时")]);
    // 置顶 / 软删除：本版无字段，按契约"不显示"，不写 0 也不写"不支持"
    _memRows(host, pairs);

    const eng = (d && d["引擎"]) || {};
    const okv = eng["可用"];
    // 三态：true=可用 / false=不可用 / null=读不到。null 不许渲染成"不可用"（那是替读不到下结论）。
    const ep = [["引擎", okv === true ? "可用" : okv === false ? "不可用" : "未知（读不到）"]];
    if (eng["已加载"] != null) ep.push(["已加载", eng["已加载"] ? "是" : "否"]);
    if (eng["形态"] != null) ep.push(["形态", String(eng["形态"])]);
    const miss = Array.isArray(eng["缺"]) ? eng["缺"].filter(Boolean) : [];
    if (miss.length) ep.push(["缺", miss.join("、")]);
    if (eng["last_err"]) ep.push(["最近错误", String(eng["last_err"])]);
    _memRows(host, ep);

    host.appendChild(_memFoot(host, root));
  }

  function _memFoot(host, root) {
    // 三按钮一组（设计稿 §2.1）：①重载语义引擎 ②重建记忆索引 ③导出记忆数据
    // ＋刷新。同一面板、同一状态读数下方。
    const wrap = makeNode("div", "");
    const row = makeNode("div", "row-end");
    const msg = makeNode("span", "hint", "");
    const fix = makeNode("button", "mini", "重载向量引擎");
    fix.type = "button";
    fix.onclick = () => doMemoryRecover(host, root, fix, msg);
    const rebuild = makeNode("button", "mini", "重建记忆索引");
    rebuild.type = "button";
    rebuild.onclick = () => doMemoryRebuild(host, root, rebuild, msg);
    const exportBtn = makeNode("button", "mini", "导出记忆数据");
    exportBtn.type = "button";
    exportBtn.onclick = () => doMemoryExport(host, root, exportBtn, msg);
    const refresh = makeNode("button", "ghost", "刷新");
    refresh.type = "button";
    refresh.onclick = () => loadMemoryHealth(host, root);
    row.append(fix, rebuild, exportBtn, refresh);
    wrap.append(row, msg);
    return wrap;
  }

  // 修复动作的模块级在途闸（重绘会换掉按钮节点，不能只靠 btn.disabled —— 审查 P2）
  let _recoverBusy = false;

  async function _recoverCall() {
    try {
      const r = await fetch("/api/memory/recover", {
        method: "POST", cache: "no-store", signal: AbortSignal.timeout(60000),
      });
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      return { kind: r.ok ? "ok" : "http", status: r.status, data };
    } catch (e) {
      return { kind: (e && (e.name === "TimeoutError" || e.name === "AbortError")) ? "timeout" : "error", data: null };
    }
  }

  async function doMemoryRecover(host, root, btn, msg) {
    // 审查 P2：闸必须在**模块作用域**。面板每次刷新都会重画（`_memFoot` 造新按钮），
    // 只靠 `btn.disabled` 的话"修复中 → 点刷新 → 再点修复"会绕过它发出第二个请求。
    if (_recoverBusy) return;
    _recoverBusy = true;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "修复中…";
    msg.textContent = "正在重载模型并重探依赖（同步短阻塞，别重复点）…";
    const res = await _recoverCall();
    const data = res.data;
    // 判据：**必须先是可信回应（HTTP 2xx）**，再看 loaded/ok。
    // 审查 P1：原来不看 res.kind，于是"HTTP 500 但响应体恰好写着 ok:true/loaded:true"会显示
    // "已重载成功"——500 的响应体不可信，一律按失败说。
    const fixed = res.kind === "ok" && !!(data && (data.loaded === true || data.ok === true));
    _recoverBusy = false;
    btn.disabled = false; btn.textContent = label;
    let note;
    if (fixed) {
      note = "向量引擎重载成功；记忆库数据库状态需以下方重新读取的结果为准";
    } else if (res.kind === "timeout") {
      note = "没及时收到回应——**超时不等于失败**。下面是重新读到的状态；若仍未就绪，可以再点一次修复。";
    } else if (res.kind === "http") {
      note = `修复接口报错（HTTP ${res.status}）。下面是重新读到的状态，可以再试。`;
    } else if (res.kind === "error") {
      note = "没收到可信回应（网络或接口异常）。下面是重新读到的状态，可以再试。";
    } else {
      const why = [];
      if (data && data.attempted === false) why.push("依赖或模型缺失，这次压根没试载");
      const m = data && Array.isArray(data["缺"]) ? data["缺"].filter(Boolean) : [];
      if (m.length) why.push("缺：" + m.join("、"));
      if (data && data.last_err) why.push("最近错误：" + data.last_err);
      note = "没修好。" + (why.join("；") || "引擎仍未就绪") + "。下面是重新读到的状态。";
    }
    msg.textContent = note.replace(/\*\*/g, "");
    await loadMemoryHealth(host, root, note.replace(/\*\*/g, ""));
  }

  // ── 按钮② 重建记忆索引（可取消后台任务）＋ 按钮③ 导出记忆数据 ──────────
  // 模块级在途闸（同 _recoverBusy：重绘会换按钮节点，不能只靠 btn.disabled）
  let _rebuildBusy = false;
  let _exportBusy = false;

  async function _rebuildStatusCall() {
    try {
      const r = await fetch("/api/memory/rebuild/status", { cache: "no-store", signal: AbortSignal.timeout(15000) });
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      return { kind: r.ok ? "ok" : "http", status: r.status, data };
    } catch (e) {
      return { kind: (e && (e.name === "TimeoutError" || e.name === "AbortError")) ? "timeout" : "error", data: null };
    }
  }

  async function _rebuildStartCall(root) {
    try {
      const url = "/api/memory/rebuild/start" + (root ? "?root=" + encodeURIComponent(root) : "");
      const r = await fetch(url, { method: "POST", cache: "no-store", signal: AbortSignal.timeout(30000) });
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      return { kind: r.ok ? "ok" : "http", status: r.status, data };
    } catch (e) {
      return { kind: (e && (e.name === "TimeoutError" || e.name === "AbortError")) ? "timeout" : "error", data: null };
    }
  }

  async function _rebuildCancelCall() {
    try {
      const r = await fetch("/api/memory/rebuild/cancel", { method: "POST", cache: "no-store", signal: AbortSignal.timeout(15000) });
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      return { kind: r.ok ? "ok" : "http", status: r.status, data };
    } catch (e) {
      return { kind: (e && (e.name === "TimeoutError" || e.name === "AbortError")) ? "timeout" : "error", data: null };
    }
  }

  const _sleep = (ms) => new Promise((res) => setTimeout(res, ms));

  async function doMemoryRebuild(host, root, btn, msg) {
    if (_rebuildBusy) return;
    _rebuildBusy = true;
    const label = btn.textContent;
    btn.disabled = true;
    // 二次确认（设计稿 §2.1：②需二次确认）
    if (!window.confirm("重建会重算全部记忆的向量（量大时耗时），期间可继续写作。\n确定开始重建记忆索引吗？")) {
      _rebuildBusy = false; btn.disabled = false;
      return;
    }
    btn.textContent = "重建中…";
    msg.textContent = "正在启动重建（只从速记本原文重建，绝不从旧库反推）…";
    const started = await _rebuildStartCall(root);
    if (started.kind !== "ok" || !(started.data && started.data.ok)) {
      const why = (started.data && started.data["结果"])
        || (started.kind === "http" ? `接口报错（HTTP ${started.status}）`
           : (started.kind === "timeout" ? "启动超时（不等于失败，可查状态）" : "没收到可信回应"));
      _rebuildBusy = false; btn.disabled = false; btn.textContent = label;
      msg.textContent = "重建没启动：" + why;
      await loadMemoryHealth(host, root, "重建没启动：" + why, true);   // 红字
      return;
    }
    // 加一个「取消」小按钮（重建期间可见）
    const cancelBtn = makeNode("button", "ghost", "取消重建");
    cancelBtn.type = "button";
    msg.appendChild(document.createTextNode(""));
    msg.appendChild(cancelBtn);
    cancelBtn.onclick = async () => {
      cancelBtn.disabled = true;
      cancelBtn.textContent = "取消中…";
      await _rebuildCancelCall();
    };
    // 轮询进度（N/M），直到 done/cancelled/failed
    let final = null;
    for (let i = 0; i < 4000; i++) {          // 上限 ~100 分钟，防死循环
      const s = await _rebuildStatusCall();
      const d = (s.kind === "ok") ? s.data : null;
      if (d && d.status === "running") {
        const n = d.done != null ? d.done : 0;
        const m = d.total != null ? d.total : "?";
        msg.firstChild && (msg.firstChild.nodeValue = `已重建 ${n}/${m} 条…`);
        await _sleep(1500);
        continue;
      }
      if (d && (d.status === "done" || d.status === "cancelled" || d.status === "failed")) {
        final = d;
        break;
      }
      // 状态读不到（idle/异常）：给一次宽限再判，避免竞态误判
      if (i > 0 && d && d.status === "idle") { final = d; break; }
      await _sleep(1500);
    }
    cancelBtn.disabled = true; cancelBtn.textContent = "取消重建";
    btn.disabled = false; btn.textContent = label;
    _rebuildBusy = false;
    let note, isErr = false;
    if (!final) {
      note = "重建进度读不到（超时）。旧库原样保留，可点「刷新」看状态，或再点重建。";
      isErr = true;
    } else if (final.status === "done") {
      note = (final["结果"] || "重建完成。")
        + (final["提示"] ? "（" + final["提示"] + "）" : "");
      isErr = false;
    } else if (final.status === "cancelled") {
      note = "重建未完成（已取消）：新库已丢弃，旧库原样保留。";
      isErr = true;
    } else {   // failed
      note = "重建失败：" + (final["错误"] || "未知原因")
        + "。旧库已保留，可点「导出记忆数据」抢救原文。";
      isErr = true;
    }
    msg.firstChild && (msg.firstChild.nodeValue = note);
    await loadMemoryHealth(host, root, note, isErr);
  }

  async function doMemoryExport(host, root, btn, msg) {
    if (_exportBusy) return;
    _exportBusy = true;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "导出中…";
    msg.textContent = "正在导出记忆数据（只读：不写书、不写库）…";
    let res;
    try {
      const url = "/api/memory/export" + (root ? "?root=" + encodeURIComponent(root) : "");
      const r = await fetch(url, { method: "POST", cache: "no-store", signal: AbortSignal.timeout(120000) });
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      res = { kind: r.ok ? "ok" : "http", status: r.status, data };
    } catch (e) {
      res = { kind: (e && (e.name === "TimeoutError" || e.name === "AbortError")) ? "timeout" : "error", data: null };
    }
    _exportBusy = false; btn.disabled = false; btn.textContent = label;
    let note, isErr = false;
    const d = res.data;
    if (res.kind === "ok" && d && d.ok) {
      note = `导出完成：${d["书数"] ?? "?"} 本书、${d["文件数"] ?? "?"} 个文件 → ${d["目录"]}`
        + (d["说明"] ? "（" + d["说明"] + "）" : "");
      isErr = false;
    } else if (res.kind === "ok" && d && !d.ok) {
      note = "导出未成功：" + (d["结果"] || "未知原因")
        + (Array.isArray(d["已写入"]) && d["已写入"].length ? "。已写入：" + d["已写入"].join("、") : "");
      isErr = true;
    } else if (res.kind === "timeout") {
      note = "导出超时（不等于失败）。可点「刷新」看状态，或再试。";
      isErr = true;
    } else if (res.kind === "http") {
      note = `导出接口报错（HTTP ${res.status}）。`;
      isErr = true;
    } else {
      note = "没收到可信回应（网络或接口异常）。";
      isErr = true;
    }
    msg.textContent = note;
    await loadMemoryHealth(host, root, note, isErr);
  }

  // Request-local automatic retrieval feedback; no task identity or cross-turn cache.
  function memoryFeedback(ev) {
    if (!ev || ev.t !== "memory_retrieval" || ev.origin !== "automatic"
        || !["hybrid", "keyword", "unavailable", "empty_query"].includes(ev.mode)) return null;
    if (ev.mode === "empty_query") return null;
    const row = makeNode("div", "pc-row pc-memory-feedback");
    row.dataset.origin = "automatic";
    row.dataset.mode = ev.mode;
    row.appendChild(makeNode("div", "pc-avatar", "检"));
    const body = makeNode("div", "pc-bubble");
    body.appendChild(makeNode("strong", "", "自动记忆检索"));
    const count = Number.isInteger(ev.count) && ev.count >= 0 ? ev.count : null;
    const text = ev.mode === "hybrid"
      ? (count === null ? "已完成记忆检索。" : `已完成记忆检索，找到 ${count} 条相关记忆。`)
      : String(ev.notice || (ev.mode === "keyword"
        ? "向量记忆不可用，已关键词兜底，请检查设置。"
        : "记忆检索暂不可用，关键词兜底也未完成，请检查设置。"));
    body.appendChild(makeNode("div", "", text));
    row.appendChild(body);
    return row;
  }

  function toast(message) {
    const t = makeNode("div", "toast", message);
    t.setAttribute("role", "status");
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 5200);
  }

  /*批C 引导注册表消费端·书房半（2026-11-03）：GET /api/guides/pending?context=study
    → 非阻塞引导卡（右下角滑入，「知道了」回执 POST seen），一次一条不轰炸。
    会话内存去重 + 后端 last_seen 兜底。织布机半在 app.js pollGuides（走 guide-overlay）。 */
  const _studyGuidesShown = new Set();
  async function pollStudyGuides(context) {
    try {
      const d = await safeFetch("/api/guides/pending?context=" + encodeURIComponent(context || "study"));
      const items = (d && (d.guides || d.pending)) || [];   // 兼容两种键名(后端实回 pending)
      for (const g of items) {
        if (_studyGuidesShown.has(g.id)) continue;
        _studyGuidesShown.add(g.id);
        showGuideCard(g);
        break;
      }
    } catch (_) { /* 引导接口没挂上:静默 */ }
  }
  function showGuideCard(g) {
    const card = makeNode("div", "study-guide-card");
    card.setAttribute("role", "dialog");
    const head = makeNode("div", "sgc-head");
    head.appendChild(makeNode("span", "sgc-who", g.who || "书房伙伴"));
    const x = makeNode("button", "sgc-x", "×");
    x.type = "button"; x.title = "知道了,不再弹";
    x.onclick = () => { card.remove(); guideSeen(g.id); };
    head.appendChild(x);
    card.appendChild(head);
    card.appendChild(makeNode("div", "sgc-title", g.title || (g.who ? g.who + "想对你说" : "小提示")));
    const body = makeNode("div", "sgc-body");
    body.innerHTML = mdToHtml(g.say || "");
    card.appendChild(body);
    /*2026-11-04（使用向导 L1 落地）：first-shelf 欢迎卡加三个「现在就试试」入口——
      方案定稿（使用向导方案与四问答案-20261103.md §问题①）：L1 不是纯告知，
      要给新作者三个即刻出口：开新书 / 看教材 / 问我。其他引导卡不带。 */
    if (g.id === "first-shelf") {
      const acts = makeNode("div", "sgc-actions");
      const mk = (label, fn) => {
        const b = makeNode("button", "sgc-act", label);
        b.type = "button";
        b.onclick = () => { card.remove(); guideSeen(g.id); fn(); };
        acts.appendChild(b);
      };
      mk("开新书", () => { const el = document.getElementById("btnNewBook"); if (el) el.click(); });
      mk("看教材", () => { window.renderCategory && window.renderCategory("textbook"); });
      mk("问我", () => {
        window.renderRole && window.renderRole("partner");
        window.StudyPartner && window.StudyPartner.prefill &&
          window.StudyPartner.prefill("我刚来书房，还不知道从哪开始——带我转一圈？");
      });
      card.appendChild(acts);
    }
    const foot = makeNode("div", "sgc-foot");
    const ok = makeNode("button", "sgc-ok", "知道了");
    ok.type = "button";
    ok.onclick = () => { card.remove(); guideSeen(g.id); };
    foot.appendChild(ok);
    card.appendChild(foot);
    document.body.appendChild(card);
  }
  async function guideSeen(id) {
    try {
      await fetch("/api/guides/seen", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      // first-shelf 的完成即"用过书房"——顺带置首启向导完成开关(后端 prefs.wizard_done)
      if (id === "first-shelf") {
        try { await fetch("/api/guides/wizard-done", { method: "POST" }); } catch (_) {}
      }
    } catch (_) { /* 静默 */ }
  }
  /*showGuideCardOnce：when=never 的引导（如 import-folder）在动作入口主动展示——
    「知道了」先回执再继续原动作(continu)；与自动弹的 showGuideCard 区别在这。 */
  function showGuideCardOnce(g, continu) {
    if (_studyGuidesShown.has(g.id)) { if (continu) continu(); return; }
    _studyGuidesShown.add(g.id);
    const card = makeNode("div", "study-guide-card");
    const head = makeNode("div", "sgc-head");
    head.appendChild(makeNode("span", "sgc-who", g.who || "书房伙伴"));
    const x = makeNode("button", "sgc-x", "×");
    x.type = "button";
    x.onclick = () => { card.remove(); guideSeen(g.id); if (continu) continu(); };
    head.appendChild(x);
    card.appendChild(head);
    card.appendChild(makeNode("div", "sgc-title", g.title || (g.who ? g.who + "想对你说" : "小提示")));
    const body = makeNode("div", "sgc-body");
    body.innerHTML = mdToHtml(g.say || "");
    card.appendChild(body);
    const foot = makeNode("div", "sgc-foot");
    const ok = makeNode("button", "sgc-ok", "知道了,继续");
    ok.type = "button";
    ok.onclick = () => { card.remove(); guideSeen(g.id); if (continu) continu(); };
    foot.appendChild(ok);
    card.appendChild(foot);
    document.body.appendChild(card);
  }

  function heading(title, subtitle, back) {
    const panel = document.getElementById("panel");
    panel.replaceChildren();
    if (back) {
      const link = makeNode("button", "back", back.label);
      link.type = "button";
      link.onclick = back.action;
      panel.appendChild(link);
    }
    if (title || subtitle) {
      const head = makeNode("div", "panel-head");
      if (title) head.appendChild(makeNode("h2", "", title));
      if (subtitle) head.appendChild(makeNode("p", "", subtitle));
      panel.appendChild(head);
    }
    return panel;
  }

  function invalidateVolumes() { volumeSeq++; }

  /*2026-11-05（作者报：顶栏返回钮在很多场景没法返回）：readVolume 把返回钮+册
    标题+提纲灌进顶栏后，离开阅读页的路径（renderCategory/renderSubject/renderRole/
    搜索/档案）从不恢复顶栏——旧返回钮挂着死闭包，再点=重读上一册；标题也停在旧册。
    修法：恢复逻辑做成公共动作（回到静态三按钮：退出登录/设置/主题），由宿主页
    在每个离开阅读的渲染入口调用（window.StudyCore.restoreTopbar）。 */
  function restoreTopbar() {
    volumeReturn = null;
    const topbar = document.querySelector(".solid-topbar");
    if (!topbar) return;
    /*三个静态按钮都是活节点（readVolume 摘下后仍可引用），统一放回。 */
    const keep = ["btnLogout", "btnSettings", "btnTheme"].map((id) => document.getElementById(id));
    topbar.replaceChildren();
    const tools = makeNode("div", "topbar-tools");
    tools.style.marginLeft = "auto";
    for (const b of keep) { if (b) { b.style.display = ""; tools.appendChild(b); } }
    topbar.appendChild(tools);
    /*2026-11-05（全量排查 BUG #1）：笔记浮层开着时离开阅读视图没人关——
      它 fixed 右下 z-index 71，会一直浮在新视图上。恢复顶栏=离开阅读的统一时机，顺手关。 */
    if (window.StudyNotes && window.StudyNotes.close) window.StudyNotes.close();
  }

  /*2026-11-04（BUG①）：默认返回目标解析——由宿主页（study.html）注册，
    点击/渲染时调用，拿**当下**的科目/分类状态，不再用进入时的快照。 */
  let defaultReturnResolver = () => ({ label: "← 返回", action: () => {} });
  function setDefaultReturn(fn) { if (typeof fn === "function") defaultReturnResolver = fn; }
  function resolveSubjectReturn() {
    try { return defaultReturnResolver() || { label: "← 返回", action: () => {} }; }
    catch (e) { return { label: "← 返回", action: () => {} }; }
  }

  async function readVolume(path, title, back = null) {
    if (window.StudyPartner) window.StudyPartner.abortAnswer();
    const seq = ++volumeSeq;
    /*2026-11-04（作者报 BUG①）：返回按钮字样与动作**不随返回目标联动**。
      原因：label 在进入阅读时快照成字符串，且默认分支读 window.CUR_SUBJECT——
      它是 study.html 的 let 局部变量根本不在 window 上（L986 老注释自认过），
      于是搜索/书目入口进来的阅读页永远显示「← 返回」且点了没反应。
      修法：back 支持传 **函数**（点击时求值，返回当下的 {label,action}）；
      默认分支改为动态解析 window.StudyCore.resolveSubjectReturn()。 */
    volumeReturn = back || resolveSubjectReturn;
    const renderBack = () => {
      const target = volumeReturn;
      if (target && typeof target.action === "function") target.action();
    };
    const panel = heading("", null, null);   //2026-11-02：返回按钮进顶栏，文档流不再放（panel 会清空重建）
    panel.appendChild(makeNode("p", "muted", "读取中…"));
    window.StudyCore.setRole("reading");
    const data = await safeFetch(API.volume + "?path=" + encodeURIComponent(path), { timeout: TIMEOUT.file });
    if (seq !== volumeSeq) return;
    if (!data || data.error || typeof data.text !== "string") {
      document.getElementById("panel").appendChild(makeNode("p", "muted", "打不开"));
      return;
    }
    const wrap = makeNode("div", "volume-layout");
    const article = makeNode("article", "volume-article");
    const toolbar = makeNode("div", "volume-toolbar");
    const headingText = makeNode("h1", "volume-title", title);
    const text = makeNode("div", "md-preview");
    text.innerHTML = mdToHtml(data.text);
    //2026-10-30（作者第4条）：给每个段落精标记。渲染完再挂，不改 mdToHtml。
    mountNotes(text, path, title);
    //2026-10-30（作者第3条）：阅读时加「提纲」快速跳转。
    //从**已渲染的标题**取目录（不另解析一遍 md，免得两套解析对不上），
    //标题本身没锚点，所以现场编号挂 id，选一下就滚过去。
    const outline = makeNode("select", "volume-outline");
    outline.setAttribute("aria-label", "提纲：跳到某一节");
    outline.appendChild(new Option("提纲 ·跳转", ""));
    const heads = text.querySelectorAll("h1, h2, h3, h4");
    heads.forEach((h, idx) => {
      h.id = "vol-h-" + idx;
      const label = (h.textContent || "").trim().slice(0,46);
      if (label) outline.appendChild(new Option(label, h.id));
    });
    outline.onchange = () => {
      const target = outline.value && document.getElementById(outline.value);
      if (target && target.scrollIntoView) target.scrollIntoView({ block: "start", behavior: "smooth" });
    };
    if (heads.length <3) outline.hidden = true; //只有一两节就别占地方
    /*2026-11-02（作者裁定④）：「问问书房伙伴」红按钮删除——阅读时的询问走右下角球
    （StudyPartner.makeDrawer 已带球）。 */
    toolbar.append(headingText, outline);
    article.append(toolbar, text);
    wrap.appendChild(article);
    /*2026-11-02（作者裁定③）：返回栏、册标题、提纲下拉**搬进实体顶栏**——
    原来在文档流里，下翻就看不到；顶栏常驻后随时能跳走/换节。
    顶栏右侧的设置/主题按钮保留，导航三件套放左侧。
    注意：btnSettings/btnTheme 的 id 必须保留（workbench.js 绑定找的就是这两个 id）；
    每次进阅读页 replaceChildren 后重建，事件由 workbench.js 的 init 绑定在元素上
    ——重建会丢绑定，所以这里改用**移动原节点**而不是重建。 */
    const topbar = document.querySelector(".solid-topbar");
    if (topbar) {
      /*三个静态按钮都是活节点：readVolume 摘下后原样保留（restoreTopbar 再放回）。
        退出登录按钮原来会被 replaceChildren 弄丢——阅读页从此也可见。 */
      const keep = ["btnLogout", "btnSettings", "btnTheme"].map((id) => document.getElementById(id));
      topbar.replaceChildren();
      const navGroup = makeNode("div", "topbar-nav");
      /*2026-11-04（BUG①）：back 支持两层函数——volumeReturn 整体为函数（点击时求值
        拿当下的 {label,action}），label 也可以是函数（渲染时求值字样）。 */
      const lbl = typeof volumeReturn === "function" ? volumeReturn() : volumeReturn;
      const rawLabel = lbl && lbl.label;
      const labelText = typeof rawLabel === "function" ? rawLabel() : (rawLabel || "← 返回");
      const backLink = makeNode("button", "back", labelText);
      backLink.type = "button";
      backLink.onclick = renderBack;
      navGroup.appendChild(backLink);
      navGroup.appendChild(makeNode("span", "topbar-title", title));
      navGroup.appendChild(outline);
      topbar.appendChild(navGroup);
      const tools = makeNode("div", "topbar-tools");
      for (const b of keep) { if (b) tools.appendChild(b); }
      topbar.appendChild(tools);
    }
    wrap.appendChild(article);
    /*2026-11-05（全量排查 BUG #2）：makeDrawer 每次新建 shell 挂 body，
      换册从不移除旧的——CDP 实测连续两次 shells 0→1→2，右下角「问」球叠堆。
      修法：挂新壳前把旧壳（.partner-float，含 drawer+球）整体移除。 */
    document.querySelectorAll("body > .partner-float").forEach((old) => old.remove());
    const drawer = window.StudyPartner.makeDrawer({ path, title });
  //2026-10-30（作者第1条）：浮球+浮窗**不放进 volume-layout**。
  //它是 flex 容器，放进去就算 position:fixed 也会先占一个格子，正文还被挤。
  //直接挂到 body，它才是真正的浮层。
  document.body.appendChild(drawer);
    document.getElementById("panel").appendChild(wrap);
    /*2026-11-04（裁定③）：理论跳转按钮委托——点击滚到第一部分对应条目。
      挂在 article 上一次委托，不必逐按钮绑。 */
    article.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".th-jump");
      if (!btn) return;
      const t = document.getElementById(btn.dataset.target);
      if (t && t.scrollIntoView) t.scrollIntoView({ block: "start", behavior: "smooth" });
    });
    const main = document.querySelector("main.panel");
    if (main) main.scrollTop = 0;
  }

  /* 与书内 mdToHtml 同规则，另保留教材的表格/代码块；先转义再拼已知标签。 */
 //2026-10-30：给正文每一块精标记，并把已有笔记回填。
 function mountNotes(container, path, book) {
  const blocks = [].slice.call(container.children).filter(function (el) {
    return !/^(H1|H2|H3|H4)$/.test(el.tagName);
  });
  if (!blocks.length) return;
  const state = { items: [] };
  function paint() {
    blocks.forEach(function (el, idx) {
      const old = el.querySelectorAll(':scope > .note-flag');
      for (let k = 0; k < old.length; k++) old[k].remove();
      const mine = state.items.filter(function (n) { return Number(n["段"]) === idx; });
      if (!mine.length) return;
      const flag = makeNode('button', 'note-flag', String(mine.length));
      flag.type = 'button';
      flag.title = mine.map(function (n) { return '> ' + n["文"]; }).join('\n\n');
      flag.onclick = function () { window.StudyNotes.open(path, book, idx, state.items, paint, false); };
      el.appendChild(flag);
    });
  }
  blocks.forEach(function (el, idx) {
    el.classList.add('note-anchor');
    const add = makeNode('button', 'note-add', '+');
    add.type = 'button';
    add.title = '给这一段记笔记';
    add.onclick = function () { window.StudyNotes.open(path, book, idx, state.items, paint, true); };
    el.appendChild(add);
  });
  window.StudyNotes.load(path, function (items) { state.items = items || []; paint(); });
 }


 function mdToHtml(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    let html = "", i = 0, inUl = false, inOl = false, inCode = false, code = [];
    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    /*2026-11-04（裁定③）：跳转按钮渲染。① [文字](#锚点) → 站内跳转链接（只放行
      #th- 前缀锚点，外链一律不放——正文来源不可信，防 XSS/外跳）；② 行首的
      锚点占位 <a id="th-N"></a> 在块级层还原（inline 全转义了，这里白名单重建）。 */
    const inline = (s) => esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\(#(th-[\w-]+)\)/g,
        '<button class="th-jump" data-target="$2">$1</button>');
    const close = () => {
      if (inUl) { html += "</ul>"; inUl = false; }
      if (inOl) { html += "</ol>"; inOl = false; }
    };
    while (i < lines.length) {
      const raw = lines[i], l = raw.trim();
      if (l.startsWith("```")) {
        close();
        if (inCode) { html += "<pre>" + esc(code.join("\n")) + "</pre>"; code = []; }
        inCode = !inCode; i++; continue;
      }
      if (inCode) { code.push(raw); i++; continue; }
      if (l.startsWith("|") && l.endsWith("|") && i + 1 < lines.length
          && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim())) {
        close();
        const headers = l.replace(/^\||\|$/g, "").split("|");
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|")); i++;
        }
        html += "<table><thead><tr>" + headers.map((c) => "<th>" + inline(c.trim()) + "</th>").join("") + "</tr></thead><tbody>";
        for (const row of rows) html += "<tr>" + row.map((c) => "<td>" + inline(c.trim()) + "</td>").join("") + "</tr>";
        html += "</tbody></table>";
        continue;
      }
      let m;
      if (!l) { close(); i++; continue; }
      if (m = l.match(/^(#{1,6})\s+(.*)$/)) { close(); const lv = m[1].length; html += `<h${lv}>${inline(m[2])}</h${lv}>`; i++; continue; }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(l)) { close(); html += "<hr>"; i++; continue; }
      if (m = l.match(/^>\s?(.*)$/)) {
        close(); const q = [];
        while (i < lines.length && (m = lines[i].trim().match(/^>\s?(.*)$/))) { q.push(inline(m[1])); i++; }
        html += `<blockquote>${q.join("<br>")}</blockquote>`; continue;
      }
      if (m = l.match(/^[-*+]\s+(.*)$/)) {
        if (!inUl) { close(); html += "<ul>"; inUl = true; }
        const ind = raw.length - raw.trimStart().length;
        html += `<li${ind >= 2 ? ` style="margin-left:${Math.min(ind, 12) * 7}px"` : ""}>${inline(m[1])}</li>`; i++; continue;
      }
      if (m = l.match(/^\d+[.)]\s+(.*)$/)) {
        if (!inOl) { close(); html += "<ol>"; inOl = true; }
        html += `<li>${inline(m[1])}</li>`; i++; continue;
      }
      close(); const p = [];
      while (i < lines.length) {
        const t = lines[i].trim();
        if (!t || /^(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|-{3,}$|\*{3,}$|_{3,}$|```)/.test(t)) break;
        p.push(inline(t)); i++;
      }
      html += `<p>${p.join("<br>")}</p>`;
    }
    close();
    if (inCode) html += "<pre>" + esc(code.join("\n")) + "</pre>";
    /*2026-11-04（裁定③+修）：理论条目锚点还原。esc 只转义 & < >（引号原样保留），
      所以转义后形态是 &lt;a id="th-…"&gt;&lt;/a&gt;。两种引号形态都认（防 esc 口径变化）。 */
    html = html.replace(/&lt;a id=(?:&quot;|")(th-[\w-]+)(?:&quot;|")&gt;&lt;\/a&gt;/g, '<a id="$1"></a>');
    return html;
  }

  /*与书内同源：图标映射表照抄 app.js的 IC（单一改名处），书房只用到这几个。 */
 const IC = {
 theme: "icon-sun", themeDark: "icon-moon", skills: "icon-tool",
 back: "icon-arrow-left", plus: "icon-plus", close: "icon-close",
 };

 function icon(name, cls) {
 const id = IC[name] || name;
 return `<svg class="icon${cls ? " " + cls : ""}" aria-hidden="true"><use xlink:href="#${id}"></use></svg>`;
 }

 /*把 [data-ico]占位符填成 svg。iconfont.js没加载成功时保持空白，不留坏图。 */
 function hydrateIcons(root) {
 (root || document).querySelectorAll("[data-ico]").forEach((el) => {
 if (el.dataset.icoDone) return;
 el.innerHTML = icon(el.dataset.ico);
 el.dataset.icoDone = "1";
 });
 }

 /*主题：与书内共用同一个 localStorage键，两页之间切换不会各说各话。 */
 function applyTheme(t) {
 document.documentElement.setAttribute("data-theme", t);
 try { localStorage.setItem("loom_theme", t); } catch (_) { /*隐私模式忽略 */ }
 const b = document.getElementById("btnTheme");
 if (b) b.innerHTML = icon(t === "dark" ? "themeDark" : "theme");
 /* 登录页的深浅切换按钮（2026-11-05 作者裁定③）与顶栏同源，图标一起刷。 */
 const l = document.getElementById("loginTheme");
 if (l) l.innerHTML = icon(t === "dark" ? "themeDark" : "theme");
 }

 function currentTheme() {
 return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
 }

 function initTheme() {
 let saved = null;
 try { saved = localStorage.getItem("loom_theme"); } catch (_) { /*同上 */ }
 const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
 applyTheme(saved || (dark ? "dark" : "light"));
 }

 function toggleTheme() { applyTheme(currentTheme() === "dark" ? "light" : "dark"); }



  /*2026-10-30（作者）：「读什么 / 产出什么」两格。
     三个角色都用它，格式与作者智库那套对齐；放一处就不会三个页面各写一样。 */
  function metaBlock(reads, produces) {
    const box = makeNode('div', 'rmeta');
    const a = makeNode('div', 'rmeta-row');
    a.appendChild(makeNode('b', '', '读'));
    a.appendChild(document.createTextNode(reads || '—'));
    const b = makeNode('div', 'rmeta-row');
    b.appendChild(makeNode('b', '', '产出'));
    b.appendChild(document.createTextNode(produces || '—'));
    box.append(a, b);
    return box;
  }

  /*2026-11-02（作者裁定①返工）：书卡构建**同源函数**。
     织布机欢迎页（app.js renderRecent）与书房书架（workbench.openShelf）必须同一套
     书封样式——之前书房复制了一份旧实现，作者截图指认"改了没生效"就是这里。
     样式见 style.css 的 .shelf .book（3:2 线描卡 + 横排书名 + 章数徽标）。
     onOpen(book) 由调用方传（欢迎页进编辑器 / 书房页跳链接）。 */
  /*2026-11-03（作者裁定·书封改向）：bookHue 已删——线描米白方案不再用色相。
     buildBookCard 仍为同源函数,书卡样式见 style.css .shelf .book（3:2 线描卡）。 */
  function confirmAction(title, description, label = "确认") {
    return new Promise((resolve) => {
      const opener = document.activeElement;
      const dlg = makeNode("dialog", "study-confirm");
      dlg.setAttribute("aria-label", title);
      dlg.append(makeNode("h3", "", title), makeNode("p", "confirm-description", description));
      const row = makeNode("div", "row-end");
      const cancel = makeNode("button", "ghost", "取消"); cancel.type = "button";
      const yes = makeNode("button", "primary"); yes.type = "button"; yes.disabled = true;
      const readyAt = performance.now() + 5000;
      const tick = () => {
        const left = Math.max(0, Math.ceil((readyAt - performance.now()) / 1000));
        yes.disabled = left > 0;
        yes.textContent = left ? label + "（" + left + " 秒）" : label;
      };
      tick();
      const timer = setInterval(tick, 100);
      cancel.onclick = () => dlg.close("cancel");
      yes.onclick = () => { if (performance.now() >= readyAt) dlg.close("confirm"); };
      dlg.addEventListener("close", () => {
        clearInterval(timer); const confirmed = dlg.returnValue === "confirm";
        dlg.remove(); if (opener?.isConnected) opener.focus(); resolve(confirmed);
      }, { once: true });
      row.append(cancel, yes); dlg.append(row); document.body.append(dlg); dlg.showModal(); cancel.focus();
    });
  }

  function autoSizeInput(input) {
    const resize = () => {
      if (!input.isConnected || !input.getClientRects().length) return;
      input.style.height = "auto";
      const limit = Math.max(140, Math.min(320, window.innerHeight * .35));
      input.style.height = Math.min(input.scrollHeight + 2, limit) + "px";
      input.style.overflowY = input.scrollHeight > limit ? "auto" : "hidden";
    };
    input.addEventListener("input", resize);
    input.addEventListener("focus", resize);
    requestAnimationFrame(resize);
    return resize;
  }

  function buildBookCard(book, onOpen) {
    const li = makeNode("li", "book" + (book.exists ? "" : " book-gone"));
    li.title = (book.root || "") + (book.exists ? "" : "\n（文件夹不在原处了）");
    const deco = makeNode("span", "book-deco");
    deco.setAttribute("aria-hidden", "true");
    li.appendChild(deco);
    const title = makeNode("span", "book-title", book.title || book.root || "未命名");
    li.appendChild(title);
    li.appendChild(makeNode("span", "book-meta",
      book.exists ? ((book.chapters || 0) + " 章") : "文件夹不在原处"));
    if (book.exists && onOpen) {
      li.onclick = () => onOpen(book);
    } else if (!book.exists) {
      li.onclick = () => toast("这本书的文件夹不在原处了，得先把它找回来。");
    }
    const x = makeNode("button", "book-x", "×");
    x.type = "button";
    x.title = "移出书架（不删除书稿）";
    x.setAttribute("aria-label", "移出书架：" + (book.title || book.root || "未命名"));
    x.onclick = (e) => { e.stopPropagation(); if (window.onShelfRemove) window.onShelfRemove(book, x); };
    li.appendChild(x);
    return li;
  }
 return {
    API, TIMEOUT, makeNode, safeFetch, metaBlock, toast, heading, confirmAction, autoSizeInput, readVolume, invalidateVolumes, mdToHtml, memoryFeedback, sameRoot, memoryPanel,
 icon, hydrateIcons, initTheme, toggleTheme, currentTheme, buildBookCard, pollStudyGuides, showGuideCardOnce,
    setDefaultReturn, resolveSubjectReturn, restoreTopbar,   /* 2026-11-04 BUG① 返回联动 + 2026-11-05 顶栏恢复 */
    setRole(value) { role = value; },
    isRole(value) { return role === value; },
    hasRole() { return !!role; },
  };
})();
