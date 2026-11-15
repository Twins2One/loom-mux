"use strict";

/* 知识顾问：文件/粘贴取稿 → 明确确认 → 理解并沉淀；地址不引外部工具。 */
window.StudyRoles = (() => {
  const { TIMEOUT, makeNode, safeFetch, heading, toast, API, metaBlock } = window.StudyCore;
  let seq = 0;
  let busy = false;
  let videoBusy = false;
  let abort = null;
  let videoAbort = null;
  let watchdog = null;
  let videoWatchdog = null;
  let capability = null;

  function field(label, input) {
    const wrap = makeNode("label", "field-label", label);
    wrap.appendChild(input);
    return wrap;
  }

  function renderAdvisor() {
    const current = ++seq;
    clearTimeout(watchdog);
    clearTimeout(videoWatchdog);
    window.StudyPartner.leave();
    window.StudyCore.setRole("advisor");
    document.querySelectorAll(".nav-item").forEach((n) =>
      n.classList.toggle("on", n.dataset.role === "advisor"));
    const panel = heading("知识顾问", "把视频字幕或转写稿整理成能回查的笔记");
  //2026-10-30（作者）：补「读什么 / 产出什么」，格式对齐作者智库。
  panel.appendChild(metaBlock("视频地址 · 字幕文件 · 粘贴的文本", "个人图书馆里的一份笔记"));
    const workspace = makeNode("section", "role-workspace");
    const steps = makeNode("div", "advisor-steps");
    for (const [name, desc] of [
      ["① 选视频", "地址、字幕文件或粘贴文本"], ["② 取稿", "地址取稿会访问 B 站，本地文件不花钱"],
      ["③ 理解", "模型整理，确认后一次调用"], ["④ 沉淀", "写入个人图书馆，重名不覆盖"],
    ]) {
      const item = makeNode("div", "advisor-step");
      item.appendChild(makeNode("b", "", name));
      item.appendChild(makeNode("span", "", desc));
      steps.appendChild(item);
    }
    const inputCard = makeNode("section", "work-card");
    inputCard.appendChild(makeNode("h3", "", "选一段要学的材料"));
    const source = makeNode("input");
    source.id = "advisorSource";
    source.placeholder = "视频地址（可选，仅记录来源）";
    source.setAttribute("aria-label", "来源地址");
    const address = makeNode("button", "ghost", "从地址取稿");
    address.id = "advisorFetch";
    address.type = "button";
    address.onclick = fetchVideo;
    const videoHint = makeNode("p", "muted");
    videoHint.id = "advisorVideoHint";
    videoHint.setAttribute("role", "status");
    const file = makeNode("input");
    file.id = "advisorFile";
    file.type = "file";
    file.accept = ".txt,.md,.srt,.vtt,.json,text/plain,text/markdown,application/json";
    file.onchange = async () => {
      const selected = file.files && file.files[0];
      if (!selected) return;
      document.getElementById("advisorTitle").value = selected.name.replace(/\.[^.]+$/, "");
      document.getElementById("advisorText").value = await selected.text();
      document.getElementById("advisorStatus").textContent = "已读入本地稿子，尚未调用模型。";
    };
    const title = makeNode("input");
    title.id = "advisorTitle";
    title.placeholder = "笔记标题";
    title.setAttribute("aria-label", "笔记标题");
    const subject = makeNode("input");
    subject.id = "advisorSubject";
    subject.value = "取稿笔记";
    subject.setAttribute("aria-label", "沉淀科目");
    const text = makeNode("textarea");
    text.id = "advisorText";
    text.placeholder = "粘贴字幕或转写稿；也可以从上方选择本地文件";
    text.setAttribute("aria-label", "字幕或转写稿");
    inputCard.append(field("视频地址 / 来源（可选）", source), address, videoHint,
      field("本地字幕 / 转写文件（读取本地，不花钱）", file),
      field("笔记标题", title), field("沉淀到个人图书馆的科目", subject), field("取稿正文", text));
    const action = makeNode("div", "row-end");
    const go = makeNode("button", "primary", "理解并沉淀（1 次模型调用）");
    go.id = "advisorRun";
    go.type = "button";
    go.onclick = ingest;
    action.appendChild(go);
    inputCard.appendChild(action);
    const status = makeNode("p", "muted");
    status.id = "advisorStatus";
    status.setAttribute("role", "status");
    const note = makeNode("article", "archive-text md-preview");
    note.id = "advisorNote";
    note.hidden = true;
    const open = makeNode("button", "mini hidden", "打开沉淀的笔记");
    open.id = "advisorOpen";
    open.type = "button";
    workspace.append(steps, inputCard, status, note, open);
    panel.appendChild(workspace);
    loadCapabilities(current);
  }

  async function loadCapabilities(current) {
    const data = await safeFetch(API.videoReady, { timeout: TIMEOUT.file });
    if (current !== seq) return;
    capability = data || { "取稿": false, hint: "取稿能力检查没做成，可选本地字幕文件或直接贴文本。" };
    const hint = document.getElementById("advisorVideoHint");
    const button = document.getElementById("advisorFetch");
    if (!hint || !button) return;
    if (capability["取稿"] === true && capability["画面"] === true) {
      hint.textContent = "取稿能力已就绪：点按钮会访问 B 站，不调用模型。";
    } else if (capability["取稿"] === true) {
      hint.textContent = "能取文字；缺少 ffmpeg，暂时看不了画面。";
    } else {
      hint.textContent = capability.hint || "这台机器还没配取稿能力，请选本地字幕文件或直接贴文本。";
      button.title = hint.textContent;
    }
  }

  async function fetchVideo() {
    if (videoBusy || busy) return;
    const target = document.getElementById("advisorSource").value.trim();
    const status = document.getElementById("advisorStatus");
    const hint = document.getElementById("advisorVideoHint");
    if (!target) { status.textContent = "先填视频地址，或改用本地字幕文件 / 粘贴文本。"; return; }
    if (!capability || capability["取稿"] !== true) {
      status.textContent = capability?.hint || "这台机器还没配取稿能力，请选本地字幕文件或直接贴文本。";
      return;
    }
    if (!window.confirm("取稿会访问 B 站、下载字幕或做本地转写，可能耗时；这次不调用模型。要继续吗？")) return;
    const current = ++seq;
    videoBusy = true;
    const button = document.getElementById("advisorFetch");
    button.disabled = true;
    button.textContent = "正在取稿…";
    status.textContent = "正在取稿，可以留在这页等。";
    hint.textContent = "取稿会外发到 B 站；不会上传本地稿子。";
    const ctl = new AbortController();
    videoAbort = ctl;
    let buffer = "", result = null, errorText = "取稿没有完成；不会自动重试。";
    function arm() {
      clearTimeout(videoWatchdog);
      videoWatchdog = setTimeout(() => {
        errorText = "取稿等太久了，已停止等待；不会自动重试。";
        ctl.abort();
      }, TIMEOUT.videoIdle);
    }
    arm();
    function handle(ev) {
      if (current !== seq) return;
      if (ev.t === "status") { arm(); status.textContent = ev.text || ""; }
      else if (ev.t === "done") result = ev;
      else if (ev.t === "error") {
        const messages = {
          no_bvpy: "这台机器还没配取稿能力，请选本地字幕文件或直接贴文本。",
          no_target: "先填一个视频地址。",
          timeout: "这个视频太长或取稿超时，可以填一个时间段再试。",
          fetch_failed: ev.text || "取稿失败了，可以先拖字幕文件进来。",
        };
        errorText = messages[ev.code] || ev.text || errorText;
        throw new Error("video_event");
      }
    }
    try {
      const r = await fetch(API.videoFetch, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, start: null, end: null, ocr: true }), signal: ctl.signal,
      });
      if (!r.ok || !r.body) throw new Error("video");
      const reader = r.body.getReader(), decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          if (current !== seq) return;
          handle(JSON.parse(line));
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) handle(JSON.parse(buffer));
      if (!result || typeof result["文本"] !== "string") throw new Error("incomplete");
      if (current !== seq) return;
      document.getElementById("advisorText").value = result["文本"];
      if (!document.getElementById("advisorTitle").value.trim()) {
        document.getElementById("advisorTitle").value = "视频笔记";
      }
      status.textContent = "取稿完成（" + (result["字数"] || result["文本"].length) +
        " 字），还没花模型钱；下一步理解会调用模型 1 次。";
      hint.textContent = "可以检查或修改稿子，再点「理解并沉淀」。";
    } catch (_) {
      if (current === seq) status.textContent = errorText;
      else toast(errorText);
    } finally {
      clearTimeout(videoWatchdog);
      videoAbort = null;
      videoBusy = false;
      if (current === seq) {
        button.disabled = false;
        button.textContent = "从地址取稿";
      }
    }
  }

  async function ingest() {
    if (busy) return;
    const title = document.getElementById("advisorTitle").value.trim();
    const text = document.getElementById("advisorText").value.trim();
    if (!title || !text) {
      document.getElementById("advisorStatus").textContent = "先填笔记标题，并读入或粘贴一份稿子。";
      return;
    }
    if (!window.confirm("这次会整理一份笔记，调用模型 1 次，并写入个人图书馆。要继续吗？")) return;
    const current = ++seq;
    busy = true;
    const button = document.getElementById("advisorRun");
    button.disabled = true;
    button.textContent = "正在理解…";
    const status = document.getElementById("advisorStatus");
    status.textContent = "正在整理，等待模型返回。";
    const note = document.getElementById("advisorNote");
    note.hidden = false;
    note.textContent = "";
    document.getElementById("advisorOpen").classList.add("hidden");
    const ctl = new AbortController();
    abort = ctl;
    let answer = "", buffer = "", saved = null;
    let errorText = "这次没有整理完，已有文字会留着；不会自动重试。";
    function arm() {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => {
        errorText = "等了一会儿还没有下文，已显示的文字会留着。";
        ctl.abort();
      }, TIMEOUT.answerIdle);
    }
    arm();
    function event(ev) {
      if (current !== seq) return;
      if (ev.t === "delta" && typeof ev.text === "string") {
        arm();
        answer += ev.text;
        note.textContent = answer;
      } else if (ev.t === "status") status.textContent = ev.text || "";
      else if (ev.t === "done") saved = ev;
      else if (ev.t === "error") {
        errorText = ev.text || errorText;
        throw new Error("material_event");
      }
    }
    try {
      const r = await fetch(API.material, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, text,
          subject: document.getElementById("advisorSubject").value.trim() || "取稿笔记",
          source: document.getElementById("advisorSource").value.trim() }),
        signal: ctl.signal,
      });
      if (!r.ok || !r.body) throw new Error("material");
      const reader = r.body.getReader(), decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          if (current !== seq) return;
          event(JSON.parse(line));
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) event(JSON.parse(buffer));
      if (!saved || typeof saved["路径"] !== "string") throw new Error("incomplete");
      note.innerHTML = window.StudyCore.mdToHtml(answer);
      status.textContent = "已沉淀到「个人图书馆 · " + (saved["科目"] || "取稿笔记") + "」。";
      const open = document.getElementById("advisorOpen");
      open.classList.remove("hidden");
      open.onclick = () => window.StudyCore.readVolume(saved["路径"], saved["标题"] || title, {
        label: () => "← 知识顾问", action: renderAdvisor,
      });
    } catch (_) {
      if (current === seq) status.textContent = errorText;
      else window.StudyCore.toast(errorText);
    } finally {
      clearTimeout(watchdog);
      abort = null;
      busy = false;
      if (current === seq) {
        button.disabled = false;
        button.textContent = "理解并沉淀（1 次模型调用）";
      } else if (saved) {
        window.StudyCore.toast("笔记已沉淀到个人图书馆。");
        window.StudyCore.setRole(null);
        window.renderCategory("material");
      }
    }
  }

  return { renderAdvisor };
})();
