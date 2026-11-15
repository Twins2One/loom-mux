"use strict";

/* 书房独立 agent 页（知识顾问 / 语料分析师）。
   作者 2026-11-01 手绘图结构：大对话窗 + 双便条粘贴框 + 待处理列表。
   对话走 POST /api/study/ask + role 字段（ndjson，事件形状与书房伙伴一致）。
   便条提交 = 直接把便条内容作为 question 发给该 agent（作者裁定：甲+反问——
   agent 拿不准会自己在对话里反问）。待处理列表只落 localStorage，不进后端。
   蒸馏/取稿/落笔记都会花钱；确认权在 agent 的对话里（提示词已教它先问）。 */
window.StudyAgents = (() => {
  const { API, TIMEOUT, makeNode, safeFetch } = window.StudyCore;

  const ROLES = {
    advisor: {
      key: "advisor",
      name: "知识顾问",
      tag: "学视频 · 取稿落笔记",
      askRole: "知识顾问",
      placeholder: "粘贴 B站链接或文本，我来分析",
      memoPlaceholder: "粘贴 B站链接或文本，我来分析",
      emptyText: "把要学的视频链接或一段文字粘上来（或点发送直接问）。取稿和落笔记会调模型，它先问你才花钱。",
      tools: "取稿 · 落笔记 · 查知识 · 记要点 · 想起",
    },
    analyst: {
      key: "analyst",
      name: "语料分析师",
      tag: "量文风 · 蒸馏档案",
      askRole: "语料分析师",
      placeholder: "问它怎么蒸馏、看哪份档案都行",
      memoPlaceholder: "粘贴想分析的中短文本",
      emptyText: "把想分析的文本粘进便条框，或直接发问。蒸馏会按份调模型（逐份报进度），它先确认再动手。",
      tools: "蒸馏 · 看档案 · 看数据 · 记要点 · 想起",
    },
  };

  /* ── 待处理列表：localStorage 持久化，按角色分桶 ── */
  function memoKey(roleKey) { return "study_agent_memos_" + roleKey; }

  function loadMemos(roleKey) {
    try {
      const raw = JSON.parse(localStorage.getItem(memoKey(roleKey)) || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch (_) { return []; }
  }

  function saveMemos(roleKey, list) {
    try { localStorage.setItem(memoKey(roleKey), JSON.stringify(list.slice(-30))); } catch (_) { /* 隐私模式忽略 */ }
  }

  function addMemo(roleKey, text) {
    const list = loadMemos(roleKey);
    const clean = text.trim();
    const title = clean.length > 24 ? clean.slice(0, 24) + "…" : clean;
    const preview = clean.length > 72 ? clean.slice(24, 96) + "…" : clean.slice(24);
    const item = { id: "m" + Date.now() + "_" + Math.floor(Math.random() * 1e4),
      title, preview, text: clean, state: "pending", at: Date.now() };
    list.push(item);
    saveMemos(roleKey, list);
    return item;
  }

  function markMemo(roleKey, id, state) {
    const list = loadMemos(roleKey);
    const it = list.find((m) => m.id === id);
    if (it) { it.state = state; saveMemos(roleKey, list); }
  }

  function removeMemo(roleKey, id) {
    saveMemos(roleKey, loadMemos(roleKey).filter((m) => m.id !== id));
  }

  /* ── 会话状态（每角色独立，内存不落盘）── */
  const sessions = { advisor: { seq: 0, history: [], abort: null, asking: false },
                     analyst: { sess: null } };
  sessions.analyst = { seq: 0, history: [], abort: null, asking: false };

  /* ── 任务准备架构（2026-11-08 夜间补缺缺口4）── */
  /* 显式任务判定只是前端粗筛（后端 _TASK_GOAL_RE 才是唯一判定;前端只管把 goal 传上去,
     传了不合法的 goal 后端会当简单问答,零破坏）。 */
  const GOAL_RE = /重写|重做|改成|给(我|出)|写(一|这|下|第|个)?(章|段|份|版|个)|做(一|这)?(套|个|版)|整理|分析|设计|规划|列(一|个)?(份|个|表)|补充|完善|修(改|订|复)|更新|替换|扩写|压缩|蒸馏|取稿|落笔记|查(证|一下)|搜(一|之)?(下|书房)|提(案|候选)|出(一|几)?(套|个|版|案)|继续|任务/;
  const QA_RE = /[吗呢嘛]\s*[?？!！。]?\s*$|[?？]\s*$|^(什么|怎么|为什么|是啥|啥)|是什么|是什么意思|干嘛的?|干什么的?/;
  function goalOf(text) {
    const t = (text || "").trim();
    if (!t || t.length < 4 || QA_RE.test(t) || !GOAL_RE.test(t)) return "";
    return t;
  }
  /* 对话内拒绝:轻量前缀判别 → 走 /api/agent/reject 原话通道(§13.3) */
  const REJECT_RE = /^(拒绝|不要了|不要|否了|否决|不同意|作废)/;
  let lastProposal = {};   /* roleKey -> 最近一张候选卡 id（从事件流记;无卡时拒绝只落记录） */

  async function loadTaskSnapshot(roleKey) {
    /* 启动/刷新时拉快照端点显示当前任务行（刷新恢复,§13.2 零模型）。
       夜间补缺最终班:端点无 task_id 时返回 {tasks:[...]}——取第一份活动任务(无活动则
       最新一份),读不到就不显示。 */
    try {
      const r = await fetch(API.taskSnapshot + "?role=" + encodeURIComponent(
        ROLES[roleKey].askRole) + "&conversation_id=" + encodeURIComponent(roleKey));
      if (!r.ok) return;
      const d = await r.json();
      const tasks = (d && Array.isArray(d.tasks)) ? d.tasks : [];
      if (!tasks.length) return;
      const task = tasks.find((t) => t && t.is_active !== false &&
        !["completed", "cancelled", "paused"].includes(t.status || "")) || tasks[0];
      if (!task || !task.task_id) return;
      const hint = document.getElementById(roleKey + "AgentHint");
      if (hint && !hint.textContent) {
        hint.textContent = "当前任务:" + (task.goal || "(无目标)") +
          " · " + (task.status || "?") + " · v" + (task.revision || 0);
      }
    } catch (_) { /* 快照只是恢复,失败静默 */ }
  }

  async function rejectActive(roleKey, originalText) {
    try {
      const r = await fetch(API.reject, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal_id: lastProposal[roleKey] || "", role: ROLES[roleKey].askRole,
                               original_text: originalText, root: (window.StudyCore.ROOT || "") }),
      });
      const d = await r.json();
      const hint = document.getElementById(roleKey + "AgentHint");
      if (hint) hint.textContent = d && d.recorded
        ? "已记下你的拒绝,任务侧会承接。" : "收到（无候选卡可拒,仅记录）。";
    } catch (_) { /* 拒绝失败不拦对话 */ }
  }

  function abortAll() {
    for (const k of Object.keys(sessions)) {
      const s = sessions[k];
      s.seq++;
      if (s.abort) { s.abort.abort(); s.abort = null; }
      s.asking = false;
    }
  }

  function leave() { abortAll(); window.StudyCore.setRole(null); }

  function bubble(role, text, who) {
    const row = makeNode("div", "pc-row" + (role === "user" ? " pc-user" : ""));
    row.appendChild(makeNode("div", "pc-avatar", who || (role === "user" ? "你" : "它")));
    const textNode = makeNode("div", "pc-bubble", text);
    row.appendChild(textNode);
    return { row, textNode };
  }

  /* 进行中指示气泡（作者 2026-11-08：agent 工作时没有任何"正在干活"的标志）。
     纯 CSS 动画三个点 + 可变状态文字；完成时整行摘掉。 */
  function workingBubble() {
    const row = makeNode("div", "pc-row agent-working");
    row.appendChild(makeNode("div", "pc-avatar", "它"));
    const b = makeNode("div", "pc-bubble agent-working-bubble");
    const dots = makeNode("span", "agent-working-dots");
    dots.appendChild(document.createElement("i"));
    dots.appendChild(document.createElement("i"));
    const label = makeNode("span", "agent-working-label", "正在思考…");
    b.append(dots, label);
    row.appendChild(b);
    return { row, label };
  }

  /* ── 页面渲染 ── */
  function render(roleKey) {
    const R = ROLES[roleKey];
    if (!R) return;
    const sess = sessions[roleKey];
    sess.seq++;
    const current = sess.seq;
    window.StudyCore.setRole(R.askRole);
    window.StudyCore.invalidateVolumes();

    const panel = document.getElementById("panel");
    panel.replaceChildren();

    const head = makeNode("div", "panel-head");
    head.appendChild(makeNode("h2", "", R.name));
    head.appendChild(makeNode("p", "", R.tag));

    const meta = window.StudyCore.metaBlock("你粘的链接与文本 · 它的档案", "它自己动工具，干完向你汇报");
    head.appendChild(meta);
    head.appendChild(makeNode("p", "role-duty", R.emptyText));
    if (roleKey === "advisor") {
      const help = makeNode("button", "ghost", "B站凭据怎么获取？"); help.type = "button";
      help.onclick = () => {
        document.getElementById("btnSettings").click();
        const details = document.getElementById("bili-credential-help");
        details.open = true; details.querySelector("summary").focus();
        details.scrollIntoView({ block: "center" });
      };
      head.appendChild(help);
    }
    panel.appendChild(head);

    /* 对话窗（大块） */
    const chat = makeNode("section", "journey-card agent-chat");
    chat.setAttribute("aria-label", R.name + "对话");
    const scroll = makeNode("div", "pc-scroll agent-scroll");
    scroll.setAttribute("aria-live", "polite");
    if (!sess.history.length) scroll.appendChild(makeNode("div", "pc-empty", R.emptyText));
    for (const m of sess.history) {
      if (m.memory) {
        const feedback = window.StudyCore.memoryFeedback(m.memory);
        if (feedback) scroll.appendChild(feedback);
      }
      scroll.appendChild(bubble(m.role, m.text).row);
    }

    const inputRow = makeNode("div", "pc-input-row");
    const input = makeNode("textarea", "pc-input");
    input.id = roleKey + "AgentInput";
    window.StudyCore.autoSizeInput(input);
    input.placeholder = R.placeholder;
    input.setAttribute("aria-label", R.name + "提问");
    input.onkeydown = (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); ask(current, roleKey); }
    };
    const send = makeNode("button", "jc-btn pc-send", "发送");
    send.id = roleKey + "AgentSend";
    send.type = "button";
    send.onclick = () => ask(current, roleKey);
    inputRow.append(input, send);
    const hint = makeNode("div", "muted");
    hint.id = roleKey + "AgentHint";
    hint.setAttribute("role", "status");
    chat.append(scroll, inputRow, hint);
    panel.appendChild(chat);

    /* 便条框 ×2（并排；窄屏上下） */
    const memos = makeNode("section", "agent-memos");
    memos.setAttribute("aria-label", "便条粘贴");
    for (let i = 0; i < 2; i++) {
      const box = makeNode("div", "agent-memo");
      const ta = makeNode("textarea", "agent-memo-input");
      window.StudyCore.autoSizeInput(ta);
      ta.id = roleKey + "Memo" + i;
      ta.placeholder = i === 0 ? R.memoPlaceholder : "第二条便条（可选，一起提交）";
      ta.setAttribute("aria-label", R.name + "便条" + (i + 1));
      const rowBtn = makeNode("div", "agent-memo-actions");
      const submit = makeNode("button", "jc-btn", "交给它");
      submit.type = "button";
      submit.disabled = true;
      ta.addEventListener("input", () => { submit.disabled = !ta.value.trim(); });
      ta.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey) && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); submit.click(); }
      });
      submit.onclick = () => {
        const text = ta.value.trim();
        if (!text) return;
        const memo = addMemo(roleKey, text);
        renderMemoList(roleKey);
        ta.value = "";
        ta.dispatchEvent(new Event("input"));
        submit.disabled = true;
        /* 作者裁定（甲+反问）：直接把便条内容交给 agent；它拿不准会在对话里反问 */
        ask(current, roleKey, { problem: text, memoId: memo.id, fromMemo: true });
      };
      rowBtn.appendChild(submit);
      box.append(ta, rowBtn);
      memos.appendChild(box);
    }
    panel.appendChild(memos);

    /* 待处理列表 */
    const pending = makeNode("section", "journey-card agent-pending");
    pending.setAttribute("aria-label", "待处理便条");
    pending.appendChild(makeNode("div", "agent-pending-title", "待处理"));
    const list = makeNode("div", "agent-pending-list");
    list.id = roleKey + "PendingList";
    pending.appendChild(list);
    panel.appendChild(pending);
    renderMemoList(roleKey);

    document.querySelectorAll(".nav-item").forEach((n) =>
      n.classList.toggle("on", n.dataset.role === roleKey));

    input.focus({ preventScroll: true });
    loadReady(current, roleKey);
    /* 夜间补缺最终班(§2g):刷新恢复——启动即拉任务快照,当前任务行零模型回显。 */
    loadTaskSnapshot(roleKey);
  }

  function renderMemoList(roleKey) {
    const list = document.getElementById(roleKey + "PendingList");
    if (!list) return;
    list.replaceChildren();
    const memos = loadMemos(roleKey);
    if (!memos.length) {
      list.appendChild(makeNode("p", "muted", "暂无便条。粘贴一条，它会出现在这里并直接交给" +
        (roleKey === "advisor" ? "知识顾问" : "语料分析师") + "。"));
      return;
    }
    for (const m of memos.slice().reverse()) {
      const row = makeNode("div", "agent-memo-item" + (m.state === "done" ? " done" : ""));
      const main = makeNode("div", "agent-memo-main");
      main.appendChild(makeNode("div", "agent-memo-title", (m.state === "done" ? "✓ " : "… ") + m.title));
      if (m.preview) main.appendChild(makeNode("div", "agent-memo-preview", m.preview));
      row.appendChild(main);
      const del = makeNode("button", "ghost agent-memo-del", "删");
      del.type = "button";
      del.title = "删除这条便条";
      del.onclick = () => { removeMemo(roleKey, m.id); renderMemoList(roleKey); };
      row.appendChild(del);
      list.appendChild(row);
    }
  }

  /* ── 提问（对话流；与 partner.js 同构，多带 role）── */
  async function loadReady(current, roleKey) {
    const data = await safeFetch(API.askReady);
    const input = document.getElementById(roleKey + "AgentInput");
    const send = document.getElementById(roleKey + "AgentSend");
    const hint = document.getElementById(roleKey + "AgentHint");
    if (!input || !send || !hint) return;
    if (!data || current !== sessions[roleKey].seq) return;
    if (data.ready === true) {
      input.disabled = false;
      send.disabled = false;
      input.placeholder = ROLES[roleKey].placeholder;
      hint.textContent = "";
    } else {
      hint.textContent = typeof data.hint === "string" ? data.hint : "对话还没就绪。";
    }
  }

  async function ask(current, roleKey, opts) {
    const R = ROLES[roleKey];
    const sess = sessions[roleKey];
    if (!R || sess.asking || current !== sess.seq) return;
    const input = document.getElementById(roleKey + "AgentInput");
    const send = document.getElementById(roleKey + "AgentSend");
    const hint = document.getElementById(roleKey + "AgentHint");
    const scroll = document.querySelector(".agent-scroll");
    if (!input || !send || !scroll) return;

    const fromMemo = !!(opts && opts.fromMemo);
    const question = fromMemo ? String(opts.problem) : input.value.trim();
    if (!question) return;
    if (!fromMemo && input.disabled) return;

    sess.asking = true;
    if (!fromMemo) { input.disabled = send.disabled = true; send.textContent = "思考中…"; }
    else { send.textContent = "思考中…"; }
    /* 对话内拒绝（夜间补缺缺口4）:前缀「拒绝/不要了/否了…」走原话拒绝通道,不当提问发出 */
    if (REJECT_RE.test(question)) {
      if (!fromMemo) input.disabled = send.disabled = true;
      sess.asking = false;
      if (!fromMemo) { input.value = ""; input.disabled = false; send.disabled = false; send.textContent = "发送"; }
      hint.textContent = "";
      scroll.appendChild(bubble("assistant", "").row);
      rejectActive(roleKey, question);
      return;
    }
    hint.textContent = "";
    scroll.replaceChildren();
    for (const m of sess.history) {
      if (m.memory) {
        const feedback = window.StudyCore.memoryFeedback(m.memory);
        if (feedback) scroll.appendChild(feedback);
      }
      scroll.appendChild(bubble(m.role, m.text).row);
    }
    scroll.appendChild(bubble("user", question).row);
    const assistant = bubble("assistant", "");
    scroll.appendChild(assistant.row);
    /* 进行中指示（作者 2026-11-08）：let 以便 status 阶段重建（审查 #8） */
    let working = workingBubble();
    scroll.appendChild(working.row);
    if (!fromMemo) { input.value = ""; input.dispatchEvent(new Event("input")); }
    scroll.scrollTop = scroll.scrollHeight;

    const ctl = new AbortController();
    sess.abort = ctl;
    let answer = "", buffer = "", done = false, watchdog = null;
    let memory = null;
    let errorText = "这次没接上，已显示的字会留着。";
    const armWatchdog = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => {
        errorText = "等了一会儿还没有下文，已显示的字会留着。";
        ctl.abort();
      }, TIMEOUT.answerIdle);
    };
    armWatchdog();
    const event = (ev) => {
      if (current !== sess.seq) return;
      if (ev.t === "memory_retrieval") {
        if (memory) return;
        const feedback = window.StudyCore.memoryFeedback(ev);
        if (ev.origin !== "automatic") return;
        memory = ev;
        if (feedback) scroll.insertBefore(feedback, assistant.row);
        armWatchdog();
      } else if (ev.t === "delta" && typeof ev.text === "string") {
        armWatchdog();
        /* 审查 #8：正文首 delta 摘掉气泡，但工具执行阶段（status 事件持续）保持可见——
           文本协议模型先输出工具行也触发 delta，之后靠 asking 状态维持工作态。 */
        if (working.row.parentNode) working.row.remove();
        answer += ev.text;
        assistant.textNode.appendChild(document.createTextNode(ev.text));
        scroll.scrollTop = scroll.scrollHeight;
      } else if (ev.t === "status") {
        hint.textContent = ev.text || "";
        /* 工具进展同步进进行中气泡；delta 已到后若工具仍在跑（asking=true 且无新正文），
           重建一个工作态气泡（审查 #8：工具全过程统一进行中标志） */
        if (!working.row.parentNode && sess.asking && ev.text) {
          const w2 = workingBubble();
          working = w2;
          scroll.appendChild(w2.row);
        }
        if (working.row.parentNode && ev.text) working.label.textContent = ev.text;
        scroll.scrollTop = scroll.scrollHeight;
      } else if (ev.t === "task_prepared" || ev.t === "task_status" || ev.t === "plan_updated") {
        /* 任务准备架构（2026-11-08 §9）:轻量"当前任务/正在做什么"一行,简单问答不出现(无该事件) */
        const zh = { ready: "待接续", running: "进行中", waiting_author: "等你拍板",
                     paused: "已暂停", blocked: "受阻", completed: "已完成", cancelled: "已取消" };
        let line = ev.t === "task_prepared" ? ("任务就绪" + (ev.goal ? "：" + ev.goal : ""))
          : ev.t === "plan_updated" ? (ev.text || ev.summary || "计划已更新")
          : ("任务" + (zh[ev.status] || ev.status || "") + (ev.stop_reason ? "（" + ev.stop_reason + "）" : ""));
        hint.textContent = line + " 〔任务 v" + (ev.revision || 0) + "〕";
        if (ev.t === "task_status" && ev.status === "waiting_author") {
          hint.textContent += "（不想做可以直接回「拒绝」）";
        }
      }
      else if (ev.t === "error") { errorText = ev.text || errorText; throw new Error("ask_event"); }
      else if (ev.t === "done") done = true;
    };
    try {
      const r = await fetch(API.ask, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question, history: sess.history.slice(-12).map(({ role, text }) => ({ role, text })), role: R.askRole,
          conversation_id: roleKey,   /* 书房任务子区按 role+conversation 隔离(§13.1) */
          goal: goalOf(question),     /* 夜间补缺缺口4:显式任务目标传给后端(后端 _TASK_GOAL_RE 终判) */
        }),
        signal: ctl.signal,
      });
      if (!r.ok || !r.body) throw new Error("ask");
      const reader = r.body.getReader(), decoder = new TextDecoder();
      while (true) {
        const { value, done: ended } = await reader.read();
        if (ended) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          if (current !== sess.seq) return;
          event(JSON.parse(line));
        }
      }
      buffer += decoder.decode();
      if (current !== sess.seq) return;
      if (buffer.trim()) event(JSON.parse(buffer));
      if (!done) throw new Error("incomplete");
      sess.history.push({ role: "user", text: question }, { role: "assistant", text: answer, memory });
      sess.history = sess.history.slice(-12);
      hint.textContent = "";
      if (fromMemo && opts.memoId) {
        markMemo(roleKey, opts.memoId, "done");
        renderMemoList(roleKey);
      }
    } catch (_) {
      if (current === sess.seq) hint.textContent = errorText;
      if (fromMemo && opts.memoId) markMemo(roleKey, opts.memoId, "failed");
    } finally {
      if (working.row.parentNode) working.row.remove();   /* 无论成败，进行中指示收尾摘掉 */
      sess.asking = false;
      sess.abort = null;
      const s2 = document.getElementById(roleKey + "AgentSend");
      const i2 = document.getElementById(roleKey + "AgentInput");
      if (s2) s2.textContent = "发送";
      if (i2 && current === sess.seq) i2.disabled = false;
      if (s2 && current === sess.seq) s2.disabled = false;
      clearTimeout(watchdog);
    }
  }

  return { render, leave };
})();
