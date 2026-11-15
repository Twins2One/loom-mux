"use strict";

/* 书房工作台：主题、模型设置、开新书与书架。付费模型只由作者明确提问触发。 */
window.StudyWorkbench = (() => {
  const { makeNode, safeFetch, toast, TIMEOUT } = window.StudyCore;
  let dialog = null;
  let opener = null;
  let dialogSeq = 0;
  let connection = null;
  let prefsLoaded = false;            // /api/prefs 是否真的读到了（读不到就不许保存，见 savePrefs）
  const _shelfRemoving = new Set();   // 书架移除在途集合（防双击重复提交）

  function parseContextLenInput(value) {
    const raw = String(value == null ? "" : value).trim();
    if (!raw) return 0;
    if (!/^[1-9]\d*$/.test(raw)) {
      throw new Error("单会话上下文上限必须是正整数；留空使用默认 128000。");
    }
    const parsed = Number(raw);
    if (!Number.isSafeInteger(parsed)) {
      throw new Error("单会话上下文上限太大，必须填写安全范围内的正整数。");
    }
    return parsed;
  }

  function parseContextLenElement(input) {
    if (input && input.validity && input.validity.badInput) {
      throw new Error("单会话上下文上限必须是正整数；留空使用默认 128000。");
    }
    return parseContextLenInput(input ? input.value : "");
  }

  function contextLenSummary(limit, source) {
    return "当前 " + String(limit || 128000) + " token · " + String(source || "默认");
  }

  function formatHttpError(data, status) {
    const direct = data?.error || data?.reason;
    if (typeof direct === "string" && direct.trim()) return direct.trim();
    const detail = data?.detail;
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
    return `请求失败（HTTP ${status}）`;
  }

  async function fetchJsonDetailed(url, options = {}) {
    try {
      const response = await fetch(url, {
        ...options,
        cache: options.cache || "no-store",
        signal: options.signal || AbortSignal.timeout(options.timeout || TIMEOUT.file),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        return { ok: false, error: formatHttpError(data, response.status) };
      }
      if (!data || typeof data !== "object") return { ok: false, error: "接口没有返回可读取的结果。" };
      return { ok: true, data };
    } catch (error) {
      return { ok: false, error: error?.message || "请求没有回应。" };
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("loom_theme", theme);
    const button = document.getElementById("btnTheme");
    if (button) {
      if (window.StudyCore && window.StudyCore.icon) {
      //2026-10-28作者第1 条:与书内顶栏同形——图标按钮,不是文字。
      button.innerHTML = window.StudyCore.icon(theme === "dark" ? "themeDark" : "theme");
      }
      button.title = theme === "dark" ? "切换到浅色主题" : "切换到深色主题";
    }
  }

  function closeDialog() {
    if (!dialog) return;
    dialogSeq++;
    dialog.classList.add("hidden");
    dialog = null;
    if (opener && opener.isConnected) opener.focus();
    opener = null;
  }

  function openDialog(id, trigger) {
    if (dialog) closeDialog();
    opener = trigger || document.activeElement;
    dialog = document.getElementById(id);
    dialog.classList.remove("hidden");
    const target = dialog.querySelector("input:not([type=password]), button, select, textarea");
    if (target) target.focus();
  }

  function bindDialogs() {
    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
      button.onclick = closeDialog;
    });
    document.querySelectorAll(".overlay").forEach((overlay) => {
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      const title = overlay.querySelector("h3");
      if (title) overlay.setAttribute("aria-label", title.textContent);
      overlay.addEventListener("mousedown", (ev) => { if (ev.target === overlay) closeDialog(); });
    });
    document.addEventListener("keydown", (ev) => {
      if (document.querySelector("dialog[open]")) return;
      if (ev.key === "Escape" && dialog) { ev.preventDefault(); closeDialog(); }
      if (ev.key === "Tab" && dialog) {
        const nodes = [...dialog.querySelectorAll("button:not(:disabled), input, select, textarea, a[href]")]
          .filter((el) => !el.closest(".hidden"));
        if (!nodes.length) return;
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
        else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
      }
    });
  }

async function openShelf(trigger) {
    // 代际要在 openDialog **之后**取：openDialog 里若已有对话框会先 closeDialog()，
    // 而 closeDialog 自己会 `dialogSeq++`。原来的 `++dialogSeq` 放在前面，于是"移除一本后
    // 重开书架"这条自身路径必然自我作废（current ≠ dialogSeq）⇒ 列表永远停在"正在看书架…"，
    // × 也就再点不了。同文件 openCreate（第 161 行）本来就是"先开再取代际"，此处对齐。
    openDialog("study-shelf", trigger);
    const current = dialogSeq;
    const box = document.getElementById("studyShelfList");
    box.replaceChildren(makeNode("p", "muted", "正在看书架…"));
    const data = await safeFetch("/api/projects");
    if (current !== dialogSeq || dialog?.id !== "study-shelf") return;
    const projects = data && Array.isArray(data.projects) ? data.projects : [];
    box.replaceChildren();
    if (!projects.length) {
        box.appendChild(makeNode("p", "muted", "还没有书。先开一本，再回来接着写。"));
        return;
    }
    /*2026-11-02（作者裁定①返工）：书卡改用 core.js 的 **buildBookCard 同源函数**——
    与织布机欢迎页 renderRecent 同一套书封（色带+横排书名+章数徽标）。
    之前书房复制了一份旧实现，作者改了欢迎页这边看不到，截图指认"没落盘"就是这个原因。
    移除回执走 window.onShelfRemove（下方赋值），点书跳 ?root= 进织布机。 */
    const shelf = makeNode("ul", "shelf");
    window.onShelfRemove = async (book, btn) => {
      const key = String((book && book.root) || "");
      if (_shelfRemoving.has(key)) return;      // 审查反例③：在途时吞掉重复点击
      _shelfRemoving.add(key);
      const confirmed = await window.StudyCore.confirmAction("移出书架", "将「" + (book.title || book.root || "未命名") + "」从书架移出。仅移除书架登记，不删除书稿或文件夹；以后可重新导入。\n位置：" + key, "确认移出");
      if (!confirmed) { _shelfRemoving.delete(key); return; }
      if (btn) btn.disabled = true;             // 在途禁用按钮（finally 恢复,失败还能再点）
      let ok = false;
      try {
        const r = await safeFetch("/api/projects/forget", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ root: book.root }), timeout: TIMEOUT.file });
        // 验收 A1 缺陷：safeFetch 在 !r.ok 与任何异常时都返回 **null**，原来写 `!r || r.ok !== false`
        // 会把 null 判成成功 ⇒ 失败时照样清 loom_root 且不弹反馈（与下面注释正好相反）。
        // 回执必须明确 ok=true，再重新读取书架核验；任何空回应都保留继续写作记录。
        if (r?.ok === true) {
          const check = await safeFetch("/api/projects", { timeout: TIMEOUT.file });
          ok = Array.isArray(check?.projects) && !check.projects.some((item) => window.StudyCore.sameRoot(item.root, book.root));
        }
      } catch (e) { ok = false; } finally {
        _shelfRemoving.delete(key);
        if (btn) btn.disabled = false;
      }
      /*H-01:书房移除也要清残留键,和首页同一判据——否则织布机下次开幕又自动进这本
        （书房页自己不会自动回书,但织布机读的是同一个 localStorage.loom_root）。
        审查 P2(补):**只在服务端真移除成功时清**;失败时书还在注册表里,清键白丢信息。 */
      if (ok) {
        try {
          if (window.StudyCore.sameRoot(localStorage.getItem("loom_root"), book.root)) {
            localStorage.removeItem("loom_root");
          }
        } catch (e) { /* localStorage 不可用就当没有残留键 */ }
      } else {
        toast("未能确认移出结果，已保留继续写作记录。请刷新书架查看后重试。", true);
      }
      openShelf(trigger);
    };
    /*2026-11-03（书架定稿）：按打开时间排序——最近打开在前。
      /api/projects 的 last_opened（无值/解析失败排最后），不加按钮不显示时间。 */
    const t = (b) => {
      const v = b.last_opened;
      const n = v ? Date.parse(v) : NaN;
      return Number.isNaN(n) ? 0 : n;      // 0=最老，沉底
    };
    projects.sort((a, b) => t(b) - t(a));
    for (const book of projects) {
      shelf.appendChild(window.StudyCore.buildBookCard(book, (b) => {
        location.href = "/?root=" + encodeURIComponent(b.root);
      }));
    }
  //2026-10-31：拼好的书架必须挂上去（上一版替换时这行被误删，列表是空的）
  box.appendChild(shelf);
}

  async function openCreate(trigger) {
    // 代际在 openDialog **之后**取：openDialog 里若已有对话框会先 closeDialog()，
    // 而 closeDialog 自己 `dialogSeq++` ⇒ 原来写在前面会把本次渲染自己判成过期（v4 修，同 openShelf）。
    openDialog("study-create", trigger);
    const current = dialogSeq;
    document.getElementById("create-error").textContent = "";
    const data = await safeFetch("/api/genres");
    if (current !== dialogSeq || dialog?.id !== "study-create") return;
    const select = document.getElementById("new-genre");
    select.replaceChildren(new Option("不选题材", ""));
    for (const genre of data?.genres || []) select.appendChild(new Option(genre, genre));
    const dirs = await safeFetch("/api/fs/dirs");
    if (current !== dialogSeq || dialog?.id !== "study-create") return;
    const list = document.getElementById("new-parent-dirs");
    list.replaceChildren();
    for (const dir of dirs?.dirs || []) {
      const option = document.createElement("option");
      option.value = dir.path;
      option.label = dir.label;
      list.appendChild(option);
    }
  }

  async function createBook() {
    const name = document.getElementById("new-name").value.trim();
    const parent = document.getElementById("new-parent").value.trim();
    const error = document.getElementById("create-error");
    if (!name) { error.textContent = "先给这本书起个名字。"; return; }
    const button = document.getElementById("btn-create");
    const current = dialogSeq;
    button.disabled = true;
    const data = await safeFetch("/api/project/create", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, parent, genre: document.getElementById("new-genre").value || null,
        idea: "" }),
    });
    if (current !== dialogSeq || dialog?.id !== "study-create") return;
    button.disabled = false;
    if (!data || !data.root) { error.textContent = "这本书还没建好，请换个书名或位置再试。"; return; }
    /*2026-11-02（作者裁定②返工）：新书写作模式——与织布机 createProject 同款：
    创建成功后 PUT /api/writing_mode 书级覆盖（复用现有校验），失败 toast 不阻断。 */
    const wm = document.getElementById("new-writing-mode");
    const mode = wm ? wm.value : "";
    if (mode && data.root) {
      try {
        await safeFetch("/api/writing_mode", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ root: data.root, mode }),
        });
      } catch (e) { toast("书写模式没写进配置(可在设置里再改)。"); }
    }
    location.href = "/?root=" + encodeURIComponent(data.root);
  }

  /*2026-11-03（作者裁定·已有资料夹入口）：书房新建书浮窗 → 织布机导入资料夹全链。
    不复制浮窗；落 / 并带标记，织布机启动时见标记自动弹 folder-overlay（一屏一事）。
    批C：import-folder 引导 when=never——首点时前端主动拉一次 pending 让后端判,
    若该引导未 seen 会先弹说明卡（下一次点击或 seen 后不再拦路）。 */
  async function goImportFolder() {
    try {
      const d = await safeFetch("/api/guides/pending?context=import-folder");
      const items = (d && (d.guides || d.pending)) || [];
      const g = items.find((x) => x.id === "import-folder");
      if (g && window.StudyCore && window.StudyCore.pollStudyGuides) {
        // 直接展示这条(绕过 context 过滤),用户点「知道了」后照常跳转
        window.StudyCore.showGuideCardOnce(g, () => {
          try { localStorage.setItem("loom_open_folder_import", "1"); } catch (e) {}
          location.href = "/";
        });
        return;
      }
    } catch (e) { /* 引导接口不在:直接走 */ }
    try { localStorage.setItem("loom_open_folder_import", "1"); } catch (e) {}
    location.href = "/";
  }

  function modelChoice(value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label || value || "默认";
    return option;
  }

  function fillModels(spec, current) {
    const select = document.getElementById("cn-model");
    select.replaceChildren();
    for (const model of spec?.models || []) select.appendChild(modelChoice(model.id, model.label));
    select.appendChild(modelChoice("__custom__", "手动输入…"));
    if (current && ![...select.options].some((o) => o.value === current)) {
      select.insertBefore(modelChoice(current), select.lastChild);
    }
    select.value = current || spec?.default_model || "__custom__";
    document.getElementById("cn-model-custom").classList.toggle("hidden", select.value !== "__custom__");
    if (select.value === "__custom__") document.getElementById("cn-model-custom").value = current || "";
  }

 //2026-10-30 作者发现的缺陷：书房设置里**根本没有「拉取可用模型」这个按钮**，
 // 只有一个空下拉。书内早就有这个功能（app.js 的 fetchModels），这里是漏做。
 // 套路照搬书内那一份：**拉完不把你选好的模型冲掉**；不在清单里就明说。
 async function fetchModels() {
    const button = document.getElementById("cn-fetch-models");
    const hint = document.getElementById("cn-model-hint");
    const provider = document.getElementById("cn-provider").value;
    const prev = document.getElementById("cn-model").value === "__custom__"
      ? document.getElementById("cn-model-custom").value.trim()
      : document.getElementById("cn-model").value;
    button.disabled = true;
    button.textContent = "拉取中…";
    hint.textContent = "";
    try {
      const data = await safeFetch("/api/backend/models", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          base_url: document.getElementById("cn-base-url").value.trim() || null,
          api_key: document.getElementById("cn-api-key").value.trim() || null,
        }),
 timeout:20000, //拉模型清单要真打供应商 /models，5秒不够（2026-10-30实测）
      });
 if (!data) { hint.textContent = "拉取没成功：没拿到回应（可能是 key 被拒、base_url 不对、或超过20 秒）。先在书内设置里确认这家能拉到，拉不到就手填模型名。"; return; }
      if (data.error) { hint.textContent = "拉取没成功：" + data.error; return; }
      const models = Array.isArray(data.models) ? data.models : [];
      if (!models.length) { hint.textContent = "拉到0 个模型。这家供应商可能不支持 /models，直接手填模型名也行。"; return; }
      // 先把拉到的清单灌进下拉，再把原来选的那个还回去
      fillModels({ models }, prev || models[0].id);
      const ids = models.map((m) => m.id);
      if (prev && ids.includes(prev)) {
        hint.textContent = "拉到 " + models.length + " 个模型；当前用的「" + prev + "」在清单里。";
      } else if (prev) {
        hint.textContent = "拉到 " + models.length + " 个模型，但你原来填的「" + prev + "」不在清单里；请选一个再点保存。";
      } else {
        hint.textContent = data.message || ("拉到 " + models.length + " 个模型，下拉里选一个。");
      }
    } finally {
      button.disabled = false;
      button.textContent = "拉取可用模型";
    }
 }

  function applyProvider() {
    const provider = document.getElementById("cn-provider").value;
    const spec = (connection?.providers || []).find((p) => p.id === provider);
    document.getElementById("cn-api-key").classList.toggle("hidden", !spec?.needs_key);
    document.getElementById("cn-key-label").classList.toggle("hidden", !spec?.needs_key);
    document.getElementById("cn-base-url").classList.toggle("hidden", provider !== "openai_compat");
    document.getElementById("cn-key-hint").textContent = spec?.hint || "";
    document.getElementById("cn-api-key").placeholder = connection?.keys_set?.[provider] ? "已设置，留空不改" : "填入本机使用的 API Key";
    fillModels(spec, spec?.default_model || "");
  }

 //2026-10-30 作者第1 条：按角色分模型，两级折叠菜单。
 // 第一级 = 角色（一行一个，点开才展开）；第二级 = 供应商 + 模型两个下拉。
 let _routeRows = [];

 async function loadStudyRoutes(options = {}) {
    const box = document.getElementById("study-route-body");
    if (!box) return false;
    try {
      const data = await safeFetch("/api/study/routes", { timeout: TIMEOUT.file });
      if (!data || !Array.isArray(data["槽"])) {
        if (options.preserveOnError) return false;
        box.replaceChildren(makeNode("p", "hint", "槽表读不到，当前配置状态未知。"));
        return false;
      }
      _routeRows = data["槽"];
      const providers = data["供应商"] || [];
      box.replaceChildren();
      for (const row of _routeRows) box.appendChild(routeCard(row, providers));
      return true;
    } catch (e) {
      // 槽表是辅助功能，坏了不该让整个设置框白掉，但也不能静默。
      if (options.preserveOnError) return false;
      box.replaceChildren(makeNode("p", "hint", "槽表没展开（" + (e && e.message ? e.message : "未知错误") + "），当前配置状态未知。"));
      return false;
    }
 }

 function routeCard(row, providers) {
    //2026-10-30（作者第2条）：按书内「按用途分模型」那套重排——
    //一行一个用途卡，四件齐全（供应商 /模型名 / base_url /工具协议）+保存。
    //原来那版是 details折叠 +只有两个输入，跟书内不一致，看着也别扭。
    const card = makeNode("div", "route-card");
    const head = makeNode("div", "route-card-head");
    const who = String(row["用途"] || "");
    head.appendChild(makeNode("b", "route-card-name", who));
    const cur = row["跟随默认"]
      ? ("默认 · " + String(row["model"] || "跟随默认"))
      : (String(row["provider"] || "") + " · " + String(row["model"] || ""));
    head.appendChild(makeNode("span", "route-card-cur", cur));
    card.appendChild(head);
    if (row["说明"]) card.appendChild(makeNode("p", "route-card-note", String(row["说明"])));

    const prov = document.createElement("select");
    prov.className = "route-field";
    const empty = new Option("跟随默认", "");
    prov.appendChild(empty);
    for (const item of providers) prov.appendChild(new Option(item.label || item.id, item.id));
    prov.value = String(row["写明"]?.provider || "");

    const model = document.createElement("input");
    model.className = "route-field";
    model.placeholder = "模型名";
    model.value = String(row["写明"]?.model || "");

    const baseUrl = document.createElement("input");
    baseUrl.className = "route-field";
    baseUrl.placeholder = "base_url";
    baseUrl.value = String(row["写明"]?.base_url || "");

    const contextLen = document.createElement("input");
    contextLen.className = "route-field";
    contextLen.type = "text";
    contextLen.inputMode = "numeric";
    contextLen.autocomplete = "off";
    contextLen.placeholder = "上下文 token";
    contextLen.value = String(row["写明"]?.context_len || "");
    const contextLenStatus = makeNode("span", "hint context-len-status",
      contextLenSummary(row.context_limit, row.context_len_source));

    const proto = document.createElement("select");
    proto.className = "route-field";
    proto.appendChild(new Option("工具协议：默认", ""));
    proto.appendChild(new Option("text", "text"));
    proto.appendChild(new Option("json", "json"));
    proto.appendChild(new Option("native", "native"));
    proto.value = String(row["写明"]?.tool_protocol || "");

    const rowEnd = makeNode("div", "route-card-foot");
    const save = makeNode("button", "ghost", "保存");
    save.type = "button";
    const hint = makeNode("span", "hint", "");
    save.onclick = async () => {
      let contextLenValue;
      try { contextLenValue = parseContextLenElement(contextLen); }
      catch (error) { hint.textContent = error.message; return; }
      save.disabled = true;
      const result = await fetchJsonDetailed("/api/study/routes", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          purpose: who, provider: prov.value, model: model.value.trim(),
          base_url: baseUrl.value.trim(), tool_protocol: proto.value,
          context_len: contextLenValue,
        }),
        timeout: TIMEOUT.file,
      });
      save.disabled = false;
      if (!result.ok || result.data?.ok === false) {
        hint.textContent = "没存成：" + (result.error || result.data?.reason || "接口没回应");
        return;
      }
      hint.textContent = "已记下，正在读取实际设置…";
      const refreshed = await loadStudyRoutes({ preserveOnError: true });
      if (!refreshed) hint.textContent = "已保存，但暂时读不回实际设置；当前卡片保留，稍后请刷新确认。";
    };
    rowEnd.append(save, hint);
    card.append(prov, model, baseUrl, contextLen, contextLenStatus, proto, rowEnd);
    return card;
 }


  async function openSettings(trigger) {
    // 代际在 openDialog **之后**取（v4 修，同 openShelf/openCreate）：这是作者会反复打开的对话框，
    // 原来写在前面 ⇒ 二次打开时自己的渲染被判过期、内容卡在占位态。
    openDialog("study-settings", trigger);
    const current = dialogSeq;
    document.getElementById("cn-error").textContent = "";
    const data = await safeFetch("/api/backend/default");
    if (current !== dialogSeq || dialog?.id !== "study-settings") return;
    if (!data) { document.getElementById("cn-error").textContent = "设置暂时没展开。"; return; }
    connection = data;
    loadStudyRoutes(); // 设置框一开就拉槽表：不靠 details 的 toggle 事件（它可能没触发）
    const select = document.getElementById("cn-provider");
    select.replaceChildren();
    for (const provider of data.providers || []) select.appendChild(modelChoice(provider.id, provider.label));
    select.value = data.provider || "deepseek";
    document.getElementById("cn-api-key").value = "";
    document.getElementById("cn-base-url").value = data.base_url || "";
    document.getElementById("cn-context-len").value = data.context_len || "";
    document.getElementById("cn-context-len-hint").textContent =
      contextLenSummary(data.context_limit, data.context_len_source);
    applyProvider();
    fillModels((data.providers || []).find((p) => p.id === select.value), data.model || "");
    loadBiliCred();
    loadPrefs();   // 2026-10-31：行动模式 + 记忆库维护开关
    /*F2:记忆库栏（默认折叠，展开才取数）。书房这台不绑书 root —— 按契约 root 为空＝看全库。 */
    const mph = document.getElementById("mem-panel-host-study");
    if (mph && window.StudyCore && StudyCore.memoryPanel) {
      mph.replaceChildren(StudyCore.memoryPanel(""));
    }
  }

  //2026-10-31（作者裁定）：AI思考强度三档 + 长程维护开关（用户级 prefs）。
  const _MODE_HINTS = {
    "主动思考": "AI 调用更多工具、深入讨论、记忆更多细节，消耗最大。",
    "默认": "AI 在工具调用和讨论深度上取中间值，平衡质量与消耗。",
    "安静": "AI 以你的方向为中心，快速出多个方案让你选，消耗最低。",
  };
  function _updateModeHint() {
    const picked = document.querySelector('input[name="cn-mode"]:checked');
    const hint = document.getElementById("cn-mode-hint");
    if (hint && picked) hint.textContent = _MODE_HINTS[picked.value] || _MODE_HINTS["默认"];
  }
  async function loadPrefs() {
    // 2026-11-16（复审 P3-③）：开头就清标记——重开设置后、新 GET 还没回来的那一小段，
    // 若保持上一次的 true，极快地点保存会按旧值写。
    prefsLoaded = false;
    const data = await safeFetch("/api/prefs", { timeout: TIMEOUT.file });
    if (!data) {
      // 2026-11-16（独立复审 P2-2）：原来直接 return，界面上既没有选中项也没报错，
      // 保存时就会把「默认 / 不开机维护」这两条默认值静默写回去，把作者的档位和开关改掉。
      // 现在：标记"没读到"，保存时拒绝写，并当场说明。
      prefsLoaded = false;
      const errorBox = document.getElementById("cn-error");
      if (errorBox && !errorBox.textContent) {
        errorBox.textContent = "AI思考强度没读到（接口没回应）：这一项先别保存，重开设置再试。";
      }
      return;
    }
    prefsLoaded = true;
    // 2026-11-16（复审 P3-②）：后端可能给出界面不认识的档位（老版本残留/手改过文件）。
    // 若放任出现"没有任何选中项"，保存就等于把作者的档位改成默认、还报成功 —— 仍是静默改档，
    // 只是需要一个异常值才触发。认不出来的档位一律禁保存并说明。
    const KNOWN_MODES = ["主动思考", "默认", "安静"];
    const mode = data.mode || "默认";
    const radio = KNOWN_MODES.includes(mode)
      ? document.querySelector(`input[name="cn-mode"][value="${mode}"]`)
      : null;
    if (!radio) {
      prefsLoaded = false;
      const errorBox = document.getElementById("cn-error");
      if (errorBox && !errorBox.textContent) {
        errorBox.textContent = `AI思考强度读到了不认识的档位「${mode}」：这一项先别保存，免得把它改掉。`;
      }
      return;
    }
    radio.checked = true;
    _updateModeHint();
    const cb = document.getElementById("cn-mem-startup");
    if (cb) cb.checked = !!data.memory_startup;
  }

  async function savePrefs() {
    // 没读到就不写：写下去等于用默认值覆盖作者原来的选择（复审 P2-2）。
    // 返回三态，好让调用方说清"是没读到"还是"写失败"（复审 P3-①）。
    if (!prefsLoaded) return "not-loaded";
    const picked = document.querySelector('input[name="cn-mode"]:checked');
    const r = await safeFetch("/api/prefs", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: picked ? picked.value : "默认",
        memory_startup: !!document.getElementById("cn-mem-startup")?.checked,
      }),
      timeout: TIMEOUT.file,
    });
    return r ? "ok" : "save-failed";
  }

  async function saveSettings() {
    const provider = document.getElementById("cn-provider").value;
    const select = document.getElementById("cn-model");
    const model = select.value === "__custom__" ? document.getElementById("cn-model-custom").value.trim() : select.value;
    const errorBox = document.getElementById("cn-error");
    errorBox.textContent = "";
    let contextLen;
    try { contextLen = parseContextLenElement(document.getElementById("cn-context-len")); }
    catch (error) { errorBox.textContent = error.message; return; }
    if (!model) { errorBox.textContent = "先填一个模型名。"; return; }
    const result = await fetchJsonDetailed("/api/backend/default", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, model,
        base_url: document.getElementById("cn-base-url").value.trim() || null,
        api_key: document.getElementById("cn-api-key").value.trim() || null,
        context_len: contextLen }),
      timeout: TIMEOUT.file,
    });
    if (!result.ok || result.data?.ok === false) {
      errorBox.textContent = result.error || result.data?.error || "还没保存。";
      return;
    }
    const actual = await fetchJsonDetailed("/api/backend/default", { timeout: TIMEOUT.file });
    if (!actual.ok) {
      errorBox.textContent = "设置已保存，但重新读取失败：" + actual.error;
      return;
    }
    connection = actual.data;
    document.getElementById("cn-context-len").value = actual.data.context_len || "";
    document.getElementById("cn-context-len-hint").textContent =
      contextLenSummary(actual.data.context_limit, actual.data.context_len_source);
    saveBiliCred();
    // 2026-11-16（复审 P2-2 + P3-①）：原来不 await 就直接弹"已记住…AI思考强度"，
    // 哪怕是读失败导致没写、或写失败，作者看到的也是成功；而且两种失败被说成同一句。
    const prefsState = await savePrefs();   // 行动模式 + 维护开关
    document.getElementById("cn-api-key").value = "";
    errorBox.textContent = "";
    closeDialog();
    if (prefsState === "ok") {
      toast("已记住模型设置与AI思考强度。");
    } else if (prefsState === "not-loaded") {
      toast("模型设置已记住；AI思考强度这次没存上（偏好没读到），重开设置再试。");
    } else {
      toast("模型设置已记住；AI思考强度这次没存上（写入失败或被拒），重开设置再试。");
    }
  }

  function init() {
    applyTheme(localStorage.getItem("loom_theme") ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
    bindDialogs();
    document.getElementById("btnTheme").onclick = () =>
      applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
    document.getElementById("btnSettings").onclick = (ev) => openSettings(ev.currentTarget);
    document.getElementById("btnNewBook").onclick = (ev) => openCreate(ev.currentTarget);
  //2026-10-30（作者第1、2、3条）：侧栏综合搜索 + 最近读过。
  //原来那个「全部笔记」按钮已删——笔记现在走综合搜索，不再单开入口。
  bindFinder();
  loadRecent();
    //批C:书房首启引导(first-shelf 欢迎词等,后端按 prefs.wizard_done 判定)
    if (window.StudyCore && window.StudyCore.pollStudyGuides) {
      setTimeout(() => window.StudyCore.pollStudyGuides("study"), 1500);
    }
    document.getElementById("btnContinue").onclick = (ev) => openShelf(ev.currentTarget);
    document.getElementById("btn-create").onclick = createBook;
    document.getElementById("new-parent-browse").onclick = () => window.LoomBrowse.pick(document.getElementById("new-parent"));
    document.getElementById("btn-study-import-folder").onclick = goImportFolder;
    document.getElementById("cn-provider").onchange = applyProvider;
    document.getElementById("cn-model").onchange = () => {
      document.getElementById("cn-model-custom").classList.toggle("hidden",
        document.getElementById("cn-model").value !== "__custom__");
    };
    document.getElementById("cn-fetch-models").onclick = fetchModels;
    //2026-10-30：这一块不再折叠（改成平铺卡片，跟书内那套一致），
    //所以没有 toggle 事件可听——打开设置时 loadStudyRoutes 会自己拉。
    document.getElementById("cn-save").onclick = saveSettings;
    document.querySelectorAll('input[name="cn-mode"]').forEach(r => r.addEventListener("change", _updateModeHint));
  }



  /*2026-10-30：搜索条。只接短句/单词，结果把面板换成列表。 */


  /*2026-10-31（作者）：B站凭据——每人自己初始化。
    只存在用户目录、不进包；界面上永不回显完整值，只显尾四位。
    没填也能用（公开信息照取），只是字幕会少 —— 降级优先，不报错。 */
  async function loadBiliCred() {
    const inp = document.getElementById("cn-bili-sessdata");
    if (!inp) return;
    inp.value = "";
    const d = await safeFetch("/api/study/bili/credential", { timeout: 5000 });
    const hint = document.getElementById("cn-bili-hint");
    if (d && d.已填) {
      inp.placeholder = "已填（尾四位 " + (d.尾四位 || "—") + "），想换就重新粘";
      /*2026-10-31（F1 复核）：原来这里写"当前已填凭据（尾四位 abcd）",
        与 placeholder 的尾四位重复一遍，一屏里同一信息出现两次。
        改成只说"存哪、安不安全"，状态交给 placeholder 报。 */
      if (hint) hint.textContent = "凭据保存在本机；取字幕时会发送给 B 站用于登录验证。留空不改现有值。";
    } else {
      inp.placeholder = "不填也能用，只是能取到的字幕会少";
      /*同样去掉"还没填"（placeholder 已说过不填的后果）；
        并且不写死"未填"——接口没读到时也走这一支，那时说"还没填"是假话。 */
      if (hint) hint.textContent = "部分 B站 CC 字幕需要登录凭据。凭据保存在本机，取字幕时发给 B 站验证。";
    }
  }

  async function saveBiliCred() {
    const inp = document.getElementById("cn-bili-sessdata");
    if (!inp) return;
    const v = (inp.value || "").trim();
    if (!v) return;   // 空 = 不改动现有的
    const d = await safeFetch("/api/study/bili/credential", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessdata: v }), timeout: 5000,
    });
    if (d && d.ok) {
      inp.value = "";
      loadBiliCred();
    } else {
      const e = document.getElementById("cn-error");
      if (e) e.textContent = (d && d.text) || "B站凭据没存上。";
    }
  }
  function bindFinder() {
    const form = document.getElementById("sideFind");
    if (!form) return;
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      const input = document.getElementById("findInput");
      const word = (input.value || '').trim();
      if (!word) return;
      if (window.StudyFinder) window.StudyFinder.show(word);
    });
    watchSearchIndex();
  }


  /*2026-10-30（作者第4条）：语义索引没建好时，搜索按钮灰着并显示「初始化搜索…」。
    建好之后自动恢复成「搜」。不轮询太快，4 秒一次就够。 */
  let findTimer = null;
  async function watchSearchIndex() {
    const btn = document.getElementById("findGo");
    if (!btn) return;
    //2026-10-31（作者）：按钮**始终可点**——引擎没热时走字面检索，照样能搜。
    //按钮下方一行灰字说明引擎状态，不占按钮本身的位置。
    const hint = document.getElementById("findHint");
    const paint = (ready, note, pct) => {
      btn.disabled = false;
      if (ready) {
        btn.textContent = "语义搜索";
        btn.title = "在教材、素材、语料与笔记里一起找";
        if (hint) { hint.hidden = true; hint.textContent = ""; }
      } else {
        const n = Number(pct);
        btn.textContent = "搜索";
        btn.title = "语义引擎正在初始化，现在先用字面检索";
        if (hint) {
          hint.hidden = false;
          /*2026-10-31（F3 复核）：侧栏宽 ~206px，一整句会被从"字面检索"中间折断。
            这里显式分两行（.find-hint 有 white-space: pre-line），断在括号前，读起来是两句。 */
          hint.textContent = "语义引擎预热中…" +
            ((n > 0 && n < 100) ? (" " + n + "%") : "") + "\n（先用字面检索）";
        }
      }
    };
    const tick = async () => {
      const d = await safeFetch('/api/study/embed/status', { timeout: 8000 });
      if (!d) { paint(true, ''); return; }
      if (d.就绪) { paint(true, ''); return; }
      if (d.不可用) { paint(false, '语义模型未就绪（只有字面检索）', 0); if (hint) hint.textContent = "语义模型未就绪\n（只有字面检索）"; return; }
      paint(false, '正在预热搜索引擎（约几分钟），建好就能用', d.进度);
      if (findTimer) clearTimeout(findTimer);
      findTimer = setTimeout(tick, 4000);
    };
    tick();
  }


  /*2026-10-30：最近读过的前三本。没有就把这一栏藏起来。 */
  async function loadRecent() {
    const title = document.getElementById("recentTitle");
    const box = document.getElementById("recentList");
    if (!title || !box) return;
    const data = await safeFetch('/api/study/recent', { timeout: TIMEOUT.file });
    const books = (data && data.书) || [];
    if (!books.length) { title.hidden = true; box.hidden = true; return; }
    title.hidden = false;
    box.hidden = false;
    box.replaceChildren();
    for (const b of books) {
    const row = makeNode('button', 'recent-item');
    row.type = 'button';
    row.appendChild(makeNode('span', 'ri-name', String(b.名 || '未命名')));
    if (b.类) row.appendChild(makeNode('span', 'ri-kind', String(b.类)));
    row.title = String(b.路径 || '');
    row.onclick = function () {
      if (window.StudyCore && window.StudyCore.readVolume) {
        /*2026-11-04修（BUG①）：「最近读过」进来的返回——回书房首页（默认视图）。
          动态函数，保持联动语义。 */
        window.StudyCore.readVolume(b.路径, b.名 || '未命名', () => ({
          label: '← 书房',
          action: () => { try { window.loadLibrary && window.loadLibrary(); } catch (e) {} },
        }));
      }
    };
    box.appendChild(row);
    }
  }
  return { init };
})();
