"use strict";

/* 书房伙伴：进入/切页取数；对话只在作者点发送时发起。历史仅留内存。 */
window.StudyPartner = (() => {
  const { API, TIMEOUT, makeNode, safeFetch } = window.StudyCore;
  let seq = 0;
  let history = [];
  let answerAbort = null;
  let asking = false;
  let activeIds = { input: "partnerInput", send: "partnerSend", hint: "partnerAskHint", scroll: "partnerScroll" };
  let activeVolume = "";

  function abortAnswer() {
    if (answerAbort) answerAbort.abort();
    answerAbort = null;
    asking = false;
  }

  function leave() {
    seq++;
    window.StudyCore.setRole(null);
    window.StudyCore.invalidateVolumes();
    abortAnswer();
  }

  function bubble(role, text) {
    const row = makeNode("div", "pc-row" + (role === "user" ? " pc-user" : ""));
    row.appendChild(makeNode("div", "pc-avatar", role === "user" ? "你" : "伴"));
    const textNode = makeNode("div", "pc-bubble", text);
    row.appendChild(textNode);
    return { row, textNode };
  }

  function render() {
    activeIds = { input: "partnerInput", send: "partnerSend", hint: "partnerAskHint", scroll: "partnerScroll" };
    activeVolume = "";
    window.StudyCore.setRole("partner");
    window.StudyCore.invalidateVolumes();
    const current = ++seq;
    const panel = document.getElementById("panel");
    panel.replaceChildren();
    const head = makeNode("div", "panel-head");
    head.appendChild(makeNode("h2", "", "书房伙伴"));
    head.appendChild(makeNode("p", "", "答疑 · 启发 · 不代笔"));
    //2026-10-30（作者）：补详细描述，格式对齐作者智库（读什么/产出什么）。
    const meta = makeNode("div", "rmeta");
    const m1 = makeNode("div", "rmeta-row");
    m1.appendChild(makeNode("b", "", "读"));
    m1.appendChild(document.createTextNode("你正读的那一册 · 整个知识库"));
    const m2 = makeNode("div", "rmeta-row");
    m2.appendChild(makeNode("b", "", "产出"));
    m2.appendChild(document.createTextNode("一段能接下去的话"));
    meta.append(m1, m2);
    head.appendChild(meta);
    head.appendChild(makeNode("p", "role-duty", "书房的对话口。你问，它翻书后回答；你卡住，它递个话头。"));
    const chat = makeNode("section", "journey-card partner-chat");
    chat.setAttribute("aria-label", "书房伙伴对话");
    const scroll = makeNode("div", "pc-scroll");
    scroll.id = "partnerScroll";
    scroll.setAttribute("aria-live", "polite");
    if (!history.length) scroll.appendChild(makeNode("div", "pc-empty", "还没开口。想问什么，写一句就行——它只答疑、启发，不动笔。"));
    for (const message of history) {
      if (message.memory) {
        const feedback = window.StudyCore.memoryFeedback(message.memory);
        if (feedback) scroll.appendChild(feedback);
      }
      scroll.appendChild(bubble(message.role, message.text).row);
    }
    const inputRow = makeNode("div", "pc-input-row");
    const input = makeNode("textarea", "pc-input");
    input.id = "partnerInput";
    window.StudyCore.autoSizeInput(input);
    input.placeholder = "正在看看书房的对话是否就绪";
    input.setAttribute("aria-label", "书房伙伴提问");
    input.disabled = true;
    input.onkeydown = (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); ask(current); }
    };
    const send = makeNode("button", "jc-btn pc-send", "发送");
    send.id = "partnerSend";
    send.type = "button";
    send.disabled = true;
    send.onclick = () => ask(current);
    //2026-10-30 作者第5 条：「落库」按钮。落的是**这段讨论**→个人图书馆，落完自动续一轮。
    const save = makeNode("button", "jc-btn", "落库");
    save.id = "partnerSave";
    save.type = "button";
    save.title = "把这段讨论整理成笔记，落进个人图书馆（已有约束：一次模型调用）";
    save.onclick = () => saveToLibrary(current);
    inputRow.append(input, send, save);
    const hint = makeNode("div", "muted");
    hint.id = "partnerAskHint";
    hint.setAttribute("role", "status");
    chat.append(scroll, inputRow, hint);
    panel.append(head, chat);
    document.querySelectorAll(".nav-item").forEach((n) =>
      n.classList.toggle("on", n.dataset.role === "partner"));
    loadReady(current);
  }

  async function loadReady(current) {
    const previousFocus = document.activeElement;
    const data = await safeFetch(API.askReady);
    if (!data || current !== seq || !window.StudyCore.isRole("partner") || asking) return;
    const input = document.getElementById(activeIds.input);
    const send = document.getElementById(activeIds.send);
    const hint = document.getElementById(activeIds.hint);
    if (!input || !send || !hint) return;
    if (data.ready === true) {
      const saveBtn = document.getElementById("partnerSave");
      if (saveBtn) saveBtn.disabled = false;
      input.disabled = false;
      send.disabled = false;
      if (document.activeElement === previousFocus && !document.querySelector(".overlay:not(.hidden), dialog[open]")) input.focus({ preventScroll: true });
      input.dispatchEvent(new Event("input"));
      input.placeholder = "问点什么，或让我去查。";
      hint.textContent = "";
      send.title = "发送会调用一次模型";
      //2026-10-30（作者第5条）：语义检索的状态与机器压力，**小字、不抢眼**。
      renderSemanticNote(data);
    } else hint.textContent = typeof data.hint === "string" ? data.hint : "";
  }


  //语义检索的小字注解：能用就写一句「语义检索已开」；
  //机器偏弱就提一句可退化——**不自动退**，让作者选（退了就搜不到换词的东西，不能替他做主）。
  function renderSemanticNote(data) {
    const box = document.getElementById(activeIds.hint);
    if (!box) return;
    const sem = data && data["语义"];
    const perf = data && data["性能"];
    if (!sem || sem["可语义检索"] !== true) return;
    box.classList.remove("sem-note");
    box.classList.add("sem-note");
    let text = "语义检索已开。";
    if (perf && perf["建议退化"]) {
      text += "这台机器偏紧（" + (perf["偏弱"] || []).join("、") + "），点「退化到字面搜索」可以省内存。";
    }
    box.textContent = text;
  }

  function sources(row, list) {
    if (!list.length) return;
    const line = makeNode("div", "pc-proposal-note");
    line.appendChild(document.createTextNode("出处："));
    list.forEach((source, index) => {
      if (index) line.appendChild(document.createTextNode(" · "));
      const link = makeNode("button", "ghost", [source["科目"], source["卷"], source["节"]].filter(Boolean).join(" · "));
      link.type = "button";
      link.onclick = () => {
        if (activeVolume && source["路径"] === activeVolume) {
          const drawer = document.getElementById("volumePartner");
          if (drawer) drawer.hidden = false;
          return;
        }
        window.StudyCore.readVolume(source["路径"], source["卷"] || "原文", {
          label: () => "← 书房伙伴", action: render,
        });
      };
      line.appendChild(link);
    });
    row.appendChild(line);
  }

 async function ask(current, opts) {
    if (asking || current !== seq || !window.StudyCore.isRole("partner")) return;
    const input = document.getElementById(activeIds.input);
    const send = document.getElementById(activeIds.send);
    const question = (opts && opts.problem ? String(opts.problem) : input.value).trim();
    if (!question || input.disabled) return;
    asking = true;
    input.disabled = send.disabled = true;
    send.textContent = "思考中…";
    document.getElementById(activeIds.hint).textContent = "";
    const scroll = document.getElementById(activeIds.scroll);
    const keepHistory = !!(opts && opts.silentUser);
    if (!keepHistory) {
      scroll.replaceChildren();
      for (const message of history) {
      if (message.memory) {
        const feedback = window.StudyCore.memoryFeedback(message.memory);
        if (feedback) scroll.appendChild(feedback);
      }
      scroll.appendChild(bubble(message.role, message.text).row);
    }
    }
    scroll.appendChild(bubble((opts && opts.silentUser) ? "resume" : "user", (opts && opts.silentUser) ? "接着上一轮往下走" : question).row);
    const assistant = bubble("assistant", "");
    scroll.appendChild(assistant.row);
    input.value = "";
    input.dispatchEvent(new Event("input"));
    const ctl = new AbortController();
    answerAbort = ctl;
    let answer = "", buffer = "", done = false, watchdog = null;
    let memory = null;
    let errorText = "这次没接上，已显示的字会留着。";
    function armWatchdog() {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => {
        errorText = "等了一会儿还没有下文，已显示的字会留着。";
        ctl.abort();
      }, TIMEOUT.answerIdle);
    }
    armWatchdog();
    function event(ev) {
      if (current !== seq || !window.StudyCore.isRole("partner")) return;
      if (ev.t === "memory_retrieval") {
        if (memory) return;
        const feedback = window.StudyCore.memoryFeedback(ev);
        if (ev.origin !== "automatic") return;
        memory = ev;
        if (feedback) scroll.insertBefore(feedback, assistant.row);
        armWatchdog();
      } else if (ev.t === "delta" && typeof ev.text === "string") {
        armWatchdog();
        answer += ev.text;
        assistant.textNode.appendChild(document.createTextNode(ev.text));
        scroll.scrollTop = scroll.scrollHeight;
      } else if (ev.t === "sources" && Array.isArray(ev["引用"])) sources(assistant.row, ev["引用"]);
      else if (ev.t === "status") document.getElementById(activeIds.hint).textContent = ev.text || "";
      else if (ev.t === "error") {
        errorText = ev.text || errorText;
        throw new Error("ask_event");
      } else if (ev.t === "done") done = true;
    }
    try {
      const r = await fetch(API.ask, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history: history.slice(-12).map(({ role, text }) => ({ role, text })), volume: activeVolume || undefined }), signal: ctl.signal,
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
          if (current !== seq || !window.StudyCore.isRole("partner")) return;
          event(JSON.parse(line));
        }
      }
      buffer += decoder.decode();
      if (current !== seq || !window.StudyCore.isRole("partner")) return;
      if (buffer.trim()) event(JSON.parse(buffer));
      if (!done) throw new Error("incomplete");
      history.push({ role: "user", text: question }, { role: "assistant", text: answer, memory });
      history = history.slice(-12);
      document.getElementById(activeIds.hint).textContent = "";
    } catch (_) {
      if (current === seq && window.StudyCore.isRole("partner")) {
        document.getElementById(activeIds.hint).textContent = errorText;
      }
    } finally {
      clearTimeout(watchdog);
      answerAbort = null;
      if (current === seq && window.StudyCore.isRole("partner")) {
        asking = false;
        input.disabled = false;
        send.disabled = false;
        send.textContent = "发送";
        input.focus();
      }
    }
  }

 //2026-10-30 作者第5 条「落库（即本轮讨论完毕）后自动续一轮」。乙方案：落库=整理成笔记进个人图书馆；落就续。
 let saving = false;

 function conversationText() {
    return history.filter((m) => String(m.text || "").trim())
      .map((m) => (m.role === "user" ? "作者：" : "书房伙伴：") + String(m.text).trim())
      .join("\n\n");
 }

 async function saveToLibrary(current) {
    if (saving || asking || current !== seq || !window.StudyCore.isRole("partner")) return;
    const hint = document.getElementById(activeIds.hint);
    const text = conversationText();
    if (!text) { hint.textContent = "还没聊东西，没什么可落库的。"; return; }
    if (!window.confirm("把这段讨论整理成笔记、落进个人图书馆？\n\n(用一次模型调用；落完会自动接着往下一轮)") ) return;
    saving = true;
    hint.textContent = "正在整理并落库…";
    let savedPath = "";
    try {
      const body = JSON.stringify({
        title: "书房伙伴讨论 " + new Date().toLocaleString("zh-CN", { hour12: false }),
        text, subject: "取稿笔记", source: "书房伙伴",
      });
      const r = await fetch(API.material, {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
      if (!r.ok || !r.body) throw new Error("接口没回应");
      const reader = r.body.getReader(), decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const got = await reader.read();
        if (got.done) break;
        buf += decoder.decode(got.value, { stream: true });
        const rows = buf.split("\n");
        buf = rows.pop();
        for (const line of rows) {
            if (!line.trim()) continue;
            const ev = JSON.parse(line);
            if (ev.t === "status" && ev.text) hint.textContent = ev.text;
            if (ev.t === "done") savedPath = ev["路径"] || ev.path || "";
            if (ev.t === "error") throw new Error(ev.text || "整理没成");
        }
      }
    } catch (e) {
      saving = false;
      hint.textContent = "没落成：" + (e && e.message ? e.message : "未知错误") + "。这段讨论还在。";
      return;
    }
    saving = false;
    hint.textContent = "已落库" + (savedPath ? "：" + savedPath : "") + "。接着往下一轮…";
    const scroll = document.getElementById(activeIds.scroll);
    scroll.appendChild(bubble("resume", "已落库，接着往下一轮").row);
    // 落就续（作者定）：用一句“往前推一步”当推动，不伪造作者发言
    await ask(current, {
      problem: "继续：把上面这段讨论往前推一步。不要重复已经说过的，直接给下一步。",
      silentUser: true,
    });
 }


  function makeDrawer(volume) {
    const drawer = makeNode("aside", "volume-partner floating");
    drawer.id = "volumePartner";
    drawer.hidden = true;
    drawer.setAttribute("aria-label", "当前册提问");
    const head = makeNode("div", "volume-partner-head");
    head.appendChild(makeNode("span", "muted", "正在读：" + volume.title));
    const close = makeNode("button", "ghost", "收起");
    close.type = "button";
    close.onclick = () => { drawer.hidden = true; };
    head.appendChild(close);
    const scroll = makeNode("div", "pc-scroll");
    scroll.id = "volumePartnerScroll";
    scroll.setAttribute("aria-live", "polite");
    scroll.appendChild(makeNode("div", "pc-empty", "就这一册提问。它会带着你正在读的这册回答。"));
    for (const message of history) {
      if (message.memory) {
        const feedback = window.StudyCore.memoryFeedback(message.memory);
        if (feedback) scroll.appendChild(feedback);
      }
      scroll.appendChild(bubble(message.role, message.text).row);
    }
    const input = makeNode("textarea", "pc-input");
    input.id = "volumePartnerInput";
    window.StudyCore.autoSizeInput(input);
    input.placeholder = "正在看看书房的对话是否就绪";
    input.setAttribute("aria-label", "就当前这一册提问");
    input.disabled = true;
    const send = makeNode("button", "jc-btn pc-send", "发送");
    send.id = "volumePartnerSend";
    send.type = "button";
    send.disabled = true;
    const hint = makeNode("div", "muted");
    hint.id = "volumePartnerHint";
    hint.setAttribute("role", "status");
    const row = makeNode("div", "pc-input-row");
    row.append(input, send);
    drawer.append(head, scroll, row, hint);
    //2026-10-30（作者第1条）：右下角球。想问就点它，不想问就不占版面。
    const ball = makeNode("button", "partner-ball", "问");
    ball.id = "volumePartnerBall";
    ball.type = "button";
    ball.title = "问问书房伙伴";
    ball.setAttribute("aria-label", "问问书房伙伴");
    ball.onclick = () => toggleDrawer(volume);
    const shell = makeNode("div", "partner-float");
    shell.append(drawer, ball);
    return shell;
  }

  function toggleDrawer(volume) {
    const drawer = document.getElementById("volumePartner");
    if (!drawer) return;
    if (drawer.hidden && asking) {
      const hint = document.getElementById("volumePartnerHint");
      if (hint) hint.textContent = "这段回答还没说完，先等它说完。";
      return;
    }
    drawer.hidden = !drawer.hidden;
    if (drawer.hidden) return;
    abortAnswer();
    activeVolume = volume.path;
    activeIds = { input: "volumePartnerInput", send: "volumePartnerSend",
      hint: "volumePartnerHint", scroll: "volumePartnerScroll" };
    const current = ++seq;
    window.StudyCore.setRole("partner");
    const input = document.getElementById(activeIds.input);
    input.onkeydown = (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) { ev.preventDefault(); ask(current); }
    };
    document.getElementById(activeIds.send).onclick = () => ask(current);
    loadReady(current);
    input.focus();
    /*2026-11-04（使用向导 L1「问我」入口）：prefill(text) 的落点——
      L1 欢迎卡「问我」先 renderRole('partner')（走到这），再调 StudyPartner.prefill 填字。 */
    if (window._partnerPrefill) {
      const t = window._partnerPrefill; window._partnerPrefill = null;
      input.value = t;
      input.dispatchEvent(new Event("input"));
    }
  }

  /*2026-11-04（L1）：预填下一轮输入（若伙伴页未开，先记着，render 时落框）。 */
  function prefill(text) {
    const input = document.getElementById(activeIds.input);
    if (input && window.StudyCore.isRole("partner")) { input.value = String(text || ""); input.dispatchEvent(new Event("input")); input.focus(); }
    else window._partnerPrefill = String(text || "");
  }

  return { render, leave, abortAnswer, makeDrawer, toggleDrawer, prefill };
})();
