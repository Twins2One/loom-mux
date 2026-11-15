/* Shared directory picker for the study page; uses the existing read-only API. */
window.LoomBrowse = {
  async pick(input) {
    if (!input || input.disabled) return;
    const opener = document.activeElement;
    const dlg = document.createElement("dialog");
    dlg.className = "study-picker";
    dlg.setAttribute("aria-label", "选择文件夹");
    dlg.innerHTML = '<h3>选择文件夹</h3><p class="hint">浏览这台运行 Loom 的电脑，选中后回填目录。</p><p class="browse-location"></p><div class="row-end"><button type="button" data-home>常用位置</button><button type="button" data-up>上一级</button></div><div class="browse-entries"></div><p class="error" role="status"></p><div class="row-end"><button type="button" data-cancel>取消</button><button type="button" data-select class="primary" disabled>选择此文件夹</button></div>';
    document.body.appendChild(dlg);
    const q = (s) => dlg.querySelector(s);
    let current = "", parent = "", seq = 0;
    const load = async (path) => {
      const ticket = ++seq;
      q("[data-select]").disabled = true;
      q(".error").textContent = "";
      q(".browse-entries").textContent = "读取中…";
      try {
        const response = await fetch("/api/fs/browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
        const data = await response.json();
        if (ticket !== seq || !dlg.open) return;
        if (!response.ok || data.ok !== true) throw new Error(data.error || "目录不可用");
        current = data.current || ""; parent = data.parent || "";
        q(".browse-location").textContent = current || "常用位置";
        q("[data-up]").disabled = !current;
        q("[data-select]").disabled = !current;
        const entries = q(".browse-entries"); entries.replaceChildren();
        for (const item of data.dirs || []) {
          const button = document.createElement("button");
          button.type = "button"; button.textContent = item.label; button.title = item.path;
          button.onclick = () => load(item.path); entries.appendChild(button);
        }
        if (!entries.children.length) entries.textContent = "这一层没有子文件夹。仍可选择当前文件夹。";
      } catch (error) {
        if (ticket !== seq || !dlg.open) return;
        q(".browse-entries").replaceChildren();
        q(".error").textContent = "读不到目录：" + error.message + "。可返回常用位置重新选择。";
      }
    };
    q("[data-home]").onclick = () => load("");
    q("[data-up]").onclick = () => load(parent);
    q("[data-cancel]").onclick = () => dlg.close();
    q("[data-select]").onclick = () => {
      if (!current || q("[data-select]").disabled) return;
      input.value = current;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      dlg.close();
    };
    dlg.addEventListener("close", () => { seq++; dlg.remove(); if (opener?.isConnected) opener.focus(); }, { once: true });
    dlg.showModal();
    await load(input.value.trim());
  },
};

// ── 目录浏览浮窗(0.9.0 从 app.js L843-898 拆出,4.5 大文件整理第一步)──
// 依赖:app.js 的 $() 与 jreq() 两个工具函数——script 顺序必须在 app.js 之后。
// 被谁调:app.js 的 openBrowseOverlay(设置页「浏览…」按钮、导入书)。
// 后端:/api/fs/browse。

// ── 目录浏览浮窗（浏览器模式选文件夹；后端 /api/fs/browse）────────────
let _browseTarget = null;   // 要回填的 input 元素
let _browsePath = "";       // 当前所在目录(空=常用位置根)

async function openBrowseOverlay(inp) {
  _browseTarget = inp || null;
  _browsePath = inp && inp.value.trim() ? inp.value.trim() : "";
  $("browse-error").textContent = "";
  $("browse-overlay").classList.remove("hidden");
  await browseLoad(_browsePath);
}

async function browseLoad(path) {
  $("browse-list").innerHTML = `<p class="hint">读取中…</p>`;
  $("browse-error").textContent = "";
  try {
    const d = await jreq("POST", "/api/fs/browse", { path });
    _browsePath = d.current || "";
    $("browse-current").textContent = "当前位置：" + (_browsePath || "常用位置");
    $("browse-up").classList.toggle("hidden", !d.parent);
    const ul = document.createElement("ul");
    ul.className = "list";
    (d.dirs || []).forEach((it) => {
      const li = document.createElement("li");
      li.className = "browse-row";
      li.textContent = it.label;
      li.title = it.path;
      li.onclick = () => browseLoad(it.path);
      ul.appendChild(li);
    });
    $("browse-list").replaceChildren(
      (d.dirs || []).length ? ul : Object.assign(document.createElement("p"),
        { className: "hint", textContent: "这一层没有子文件夹了。" }));
  } catch (e) {
    $("browse-list").innerHTML = "";
    $("browse-error").textContent = "读不到目录：" + e.message;
  }
}

// 建书页路径候选(服务端给真实绝对路径,浏览器也能一键填对)
let _favDirs = [];
async function loadFavDirs() {
  const dl = $("new-parent-dirs");
  if (!dl) return;
  try {
    const d = await jreq("GET", "/api/fs/dirs");
    _favDirs = (d && d.dirs) || [];
  } catch (e) { _favDirs = []; }
  dl.innerHTML = "";
  _favDirs.forEach((it) => {
    const o = document.createElement("option");
    o.value = it.path;
    o.label = it.label;
    dl.appendChild(o);
  });
}

