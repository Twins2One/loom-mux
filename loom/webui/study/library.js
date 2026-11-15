"use strict";

/* 知识库位置：启动迁移弹层 + 设置里的「知识库位置」区块。
 *
 * 契约（知识 49ac09cf 消息 审查记录）与后端实现（现读核过）：
 *   GET  /api/library/migration        api/projects.py:677 → libroot.detect_migration()
 *   POST /api/library/root             api/projects.py:660 → libroot.migrate()/repoint()
 *   GET  /api/library/root             api/projects.py:643 → libroot.status()
 *   POST /api/library/migration/dismiss api/projects.py:697 → libroot.dismiss_migration()
 *   POST /api/library/root/revert      api/projects.py:707 → libroot.revert()
 *
 * 五条硬口径（需求裁定 审查记录）：
 *   ①只有 code=library_migration_available 才弹；code=none 一律不弹（`原因` 只进 console，不上界面）；
 *   ②成功只看回执 ok===true，**不凭 HTTP 200**；
 *   ③「不用了」只让**那一个**旧位置闭嘴（后端设计如此）；
 *   ④迁移同步阻塞、无进度端点 ⇒ 在途禁点、失败可重试，文案不写"几分钟"这种没依据的话；
 *   ⑤本模块任何文案都不得出现"重开服务/后端进程"之类的错误引导——知识库只是目录指针，
 *     迁移是文件复制+校验，没有需要重开的服务（用词也避开那个禁词，便于全仓 grep 把关）。
 *
 * 只在书房页加载；书内首页不弹（知识库属书房域，写书时不该被迁移弹层打断）。
 */
window.StudyLibrary = (() => {
  const CORE = window.StudyCore || {};
  const makeNode = CORE.makeNode || ((tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    el.textContent = String(text == null ? "" : text);
    return el;
  });
  const safeFetch = CORE.safeFetch || (async () => null);
  const toast = CORE.toast || (() => {});

  const API = {
    detect: "/api/library/migration",
    rootStatus: "/api/library/root",
    rootSet: "/api/library/root",
    dismiss: "/api/library/migration/dismiss",
    revert: "/api/library/root/revert",
  };

  // 本次会话已经处理/关闭过的旧位置（只为不重复弹；跨通道的"别再说了"是后端 dismiss 的职责）
  const _handled = new Set();
  let _busy = false;                 // 迁移/切换的在途闸（同一时刻只允许一个动作）

  function _el(tag, cls, text) { return makeNode(tag, cls, text); }
  function _p(text, cls) { return _el("p", cls || "hint", text); }

  function _ok(r) { return !!r && r.ok === true; }   // ②只看回执，不看 HTTP 状态

  function _resultText(r) {
    // `结果` 与 `提醒` 同等对待：有就显示。`提醒` 是 repoint 到空目录时后端加的
    // （"切换后知识库会是空的…要搬内容请用「迁移」"），漏掉它用户会以为自己的书没了。
    const parts = [];
    if (r && typeof r["结果"] === "string" && r["结果"].trim()) parts.push(r["结果"].trim());
    if (r && typeof r["提醒"] === "string" && r["提醒"].trim()) parts.push(r["提醒"].trim());
    if (!parts.length && r && typeof r["错误"] === "string" && r["错误"].trim()) {
      parts.push(r["错误"].trim());
    }
    return parts.join("　");
  }

  async function _post(url, body) {
    const opt = { method: "POST", timeout: 10 * 60 * 1000 };
    if (body) {
      opt.headers = { "Content-Type": "application/json" };
      opt.body = JSON.stringify(body);
    }
    return safeFetch(url, opt);
  }

  /* ── 启动检测 ─────────────────────────────────────────────────────── */
  async function detect() {
    const d = await safeFetch(API.detect, { timeout: 5000 });
    if (!d || d.code !== "library_migration_available") {
      // code=none：什么都不弹。`原因` 仅供排查，按契约不上界面。
      if (d && d["原因"]) console.debug("[知识库位置] 不提示迁移：", d["原因"]);
      return null;
    }
    const oldRoot = String(d.old_root || "");
    if (oldRoot && _handled.has(oldRoot)) return null;
    showMigrationGate(d);
    return d;
  }

  /* ── 迁移弹层 ─────────────────────────────────────────────────────── */
  function showMigrationGate(d) {
    const oldRoot = String(d.old_root || "");
    const newRoot = String(d.new_root || "");
    const old = document.getElementById("lib-migrate-gate");
    if (old) {
      // 2026-11-16（复审附录）：直接 remove 旧弹层会把它挂的 Esc 监听留在 document 上
      //（闭包还引用着旧 overlay，直到下一次按 Esc 才白跑一次）。先摘监听再删节点。
      if (old.__gateKeyHandler) {
        document.removeEventListener("keydown", old.__gateKeyHandler);
        old.__gateKeyHandler = null;
      }
      old.remove();
    }

    const overlay = _el("div", "overlay");
    overlay.id = "lib-migrate-gate";
    const card = _el("div", "run-card settings-card");
    card.style.maxHeight = "78vh";
    card.style.overflowY = "auto";

    card.appendChild(_el("h3", "", "知识库位置"));

    // 文案直接用后端 `问`（中文+反斜杠自己拼容易错，作者就不敢点了）
    const q = String(d["问"] || "").trim();
    card.appendChild(_p(q || "发现旧知识库位置。", "hint"));

    // 2026-11-16（独立复审 P3-1）：弹层从来不显示"新位置"是哪个目录——作者被问
    // "要迁移到新位置吗"，却看不见目标路径，而迁移会往那里复制文件。补一行。
    if (newRoot) card.appendChild(_p("迁移到：" + newRoot, "hint"));

    // 候选多于一个给下拉，默认 old_root（契约要求）
    const cands = Array.isArray(d["候选"]) ? d["候选"].filter(Boolean) : [];
    let pick = null;
    if (cands.length > 1) {
      const lab = _el("label", "field-label", "要迁移哪个旧位置");
      pick = document.createElement("select");
      cands.forEach((c) => {
        const o = document.createElement("option");
        o.value = String(c);
        o.textContent = String(c);      // 选项文本＝候选本身（这是选择器，不是二次拼文案）
        pick.appendChild(o);
      });
      pick.value = oldRoot || String(cands[0]);
      card.append(lab, pick);
    }
    // 单选时刻意**不**再加一行「旧位置：…」：后端 `问` 里已经含 old_root，
    // 前端再拼一次会出现"发现旧知识库（A）…发现旧知识库（A）…"的重复（需求方 审查记录 追加要求①）。

    const msg = _p("", "hint");
    const row = _el("div", "row-end");

    const mkBtn = (label, cls, fn) => {
      const b = _el("button", cls, label);
      b.type = "button";
      b.onclick = fn;
      return b;
    };

    const btns = [];

    // 2026-11-16（独立复审 P3-5）：这个弹层不是 dialog 体系，Esc 对它无效；
    // 键盘用户唯一的"什么都不做地关掉"只有「不用了」（会写 dismiss 记录，有副作用）。
    function closeGate() {
      document.removeEventListener("keydown", onKey);
      overlay.__gateKeyHandler = null;
      overlay.remove();
    }
    function onKey(e) {
      if (e.key === "Escape" && !_busy) closeGate();
    }

    function finish(closeIt) {
      if (closeIt) {
        document.removeEventListener("keydown", onKey);
        overlay.__gateKeyHandler = null;
        overlay.remove();
      }
      _busy = false;
      btns.forEach((b) => { b.disabled = false; });
      // 失败后重新解锁时，别把「用新位置」又点亮（目标还不存在，点了只会再失败一次）
      if (d["新位置存在"] === false) bRepoint.disabled = true;
    }

    // 迁移 / 用新位置 共用一个执行器：action='migrate' | 'repoint'
    async function run(action) {
      if (_busy) return;                                  // ④在途禁点（后端也有互斥，双保险）
      _busy = true;
      btns.forEach((b) => { b.disabled = true; });
      // 通用规则（知识 审查记录）：后续动作一律用"当前下拉选中的那个候选"；
      // old_root 只用于"默认选中谁"和展示文案。
      const picked = pick ? String(pick.value || "") : oldRoot;
      const body = { root: newRoot, migrate: action === "migrate" };
      // **首次启动迁移必须带 source**：不传时后端拿"当前知识库根"当迁移源，而它正是那个
      // 还空着的新默认位置 ⇒ 被"源与目标不能是同一个目录"拦死（知识已为此加测试钉住）。
      if (picked) body.source = picked;
      msg.textContent = action === "migrate"
        ? "正在复制并校验，请勿关闭窗口…"
        : "正在切换位置，请勿关闭窗口…";
      const r = await _post(API.rootSet, body);
      if (r === null) {
        msg.textContent = "没收到回应（超时或接口异常）。可以再点一次。";
        finish(false);
        return;
      }
      if (!_ok(r)) {
        // ①失败一律原样展示后端 `结果`（里面常有具体原因），不自己编"请重试"
        msg.textContent = _resultText(r) || "这一步没成。";
        finish(false);                                    // 失败留在弹层里，可重试
        return;
      }
      msg.textContent = _resultText(r) || "已就绪。";
      const warn = (r && typeof r["提醒"] === "string") ? r["提醒"].trim() : "";
      if (action === "migrate") {
        toast(warn ? "知识库已迁移，但请看上面那行提醒" : "知识库已迁移到新位置");
      } else {
        toast(warn ? "位置已切换，但库是空的 —— 看上面那行提醒" : "知识库位置已切换");
      }
      // 记下"这个位置已处理过"，避免同一候选再弹一次。
      // 旧串是 `oldRoot || target`，而 `target` 在这个作用域里**不存在** ⇒
      // old_root 为空时（后端 code=available 但没给候选）求值 `target` 抛 ReferenceError，
      // 整个成功分支中断：结果已落盘、toast 已弹，但 finish 没跑、弹层关不掉。
      // 审查探针 E4 抓到（`rej:target is not defined`）。改用 newRoot —— 本次切/迁的目标位置。
      _handled.add(oldRoot || newRoot);
      if (warn) {
        // 2026-11-16（独立复审 P2-4）：后端加「提醒」的原话就是"否则作者会以为自己的书没了"，
        // 而这里原来 600ms 就关层 —— 警告一闪而过，等于把这条保护作废。
        // 有关键提醒时不自动关，留一个「知道了」让作者读完再关。
        _busy = false;
        bMigrate.disabled = bRepoint.disabled = bDismiss.disabled = true;
        row.append(mkBtn("知道了", "primary", () => finish(true)));
        return;
      }
      setTimeout(() => finish(true), 600);
    }

    async function dismiss() {
      if (_busy) return;
      _busy = true;
      btns.forEach((b) => { b.disabled = true; });
      // 传"作者当前选中的那个候选"，不是死传 old_root：dismiss 按位置记账、不按弹层记账，
      // 死传 old_root 的话作者下次启动会被同一个 B 再问一遍（知识 审查记录 第4点）。
      const picked = pick ? String(pick.value || "") : oldRoot;
      msg.textContent = "正在记下…";
      const r = await _post(API.dismiss, { old_root: picked || oldRoot });
      if (r === null) {
        msg.textContent = "没收到回应（超时或接口异常）。可以再点一次。";
        finish(false);
        return;
      }
      if (!_ok(r)) {
        msg.textContent = _resultText(r) || "这一步没成。";
        finish(false);
        return;
      }
      _handled.add(picked || oldRoot);
      finish(true);
    }

    const bMigrate = mkBtn("迁移", "primary", () => run("migrate"));
    const bRepoint = mkBtn("用新位置", "ghost", () => run("repoint"));
    const bDismiss = mkBtn("不用了", "ghost", dismiss);
    btns.push(bMigrate, bRepoint, bDismiss);
    // 2026-11-16（复审 P3-2）：`msg` 原来和三个按钮挤在同一行 `.row-end` 里，
    // 920×600（产品最小窗）下按钮被压成 48×87、文字逐字竖排 —— 恰恰在出错/警告时最难认。
    // 让回执独占一行，按钮另起一行。
    // 2026-11-16（独立复审 P2-3）：后端 repoint 要求目标已存在，而 detect 把"不存在"也算可迁移，
    // 于是首启弹层里点「用新位置」必然失败（"目标目录不存在"）—— 引导成了死路。
    // 这里按后端回传的「新位置存在」把那个按钮禁用并说明，把它变成一句能照着做的指引。
    const targetMissing = d["新位置存在"] === false;
    if (targetMissing) {
      bRepoint.disabled = true;
      bRepoint.title = "目标位置还不存在，先用「迁移」把它建出来";
    }

    row.append(bMigrate, bRepoint, bDismiss);
    card.append(msg, row);
    card.appendChild(_p("迁移会复制文件并逐个校验，期间别关窗口。", "hint"));
    if (targetMissing) {
      card.appendChild(_p("新位置还不存在：点「迁移」会连内容一起搬过去并建出这个目录；"
                          + "只想切指针的话，那个目录得先在磁盘上。", "hint"));
    }

    overlay.appendChild(card);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay && !_busy) closeGate();   // 在途不让点背景关掉
    });
    overlay.__gateKeyHandler = onKey;
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    return overlay;
  }

  /* ── 设置里的「知识库位置」区块 ───────────────────────────────────── */
  const SOURCE_TEXT = {
    env: "环境变量",
    配置: "配置文件",
    默认: "默认位置",
  };

  function _renderStatus(host, d, note) {
    host.replaceChildren();
    if (note) host.appendChild(_p(note, "hint"));      // 动作回执要留下：重绘不能把它冲掉
    if (!d) {
      host.appendChild(_p("读不到知识库位置（接口没回应）。"));
      return;
    }
    if (d["错误"]) {
      host.appendChild(_p("读数出错：" + d["错误"]));
    }
    const rows = _el("div", "mem-rows");
    const add = (k, v) => {
      const r = _el("div", "mem-row");
      r.appendChild(_el("b", "", k + "："));
      r.appendChild(_el("span", "hint", String(v == null || v === "" ? "—" : v)));
      rows.appendChild(r);
    };
    add("当前位置", d["根"]);
    const src = String(d["来源"] || "");
    add("来源", SOURCE_TEXT[src] || (src || "—"));
    add("目录存在", d["存在"] === true ? "是" : d["存在"] === false ? "否" : "—");
    add("默认位置", d["默认"]);
    if (d["迁移时间"]) add("上次迁移", d["迁移时间"]);
    if (d["回退目标"]) add("可回退到", d["回退目标"]);
    host.appendChild(rows);

    const box = _el("div", "");
    const lab = _el("label", "field-label", "改到别的位置（填一个目录）");
    const input = document.createElement("input");
    input.type = "text";
    input.setAttribute("data-lib-root", "1");
    input.placeholder = "例如 D:\\Loom\\知识库";
    input.id = "study-lib-root";
    lab.htmlFor = input.id;
    const pathRow = _el("div", "study-path-row");
    const browse = _el("button", "ghost", "浏览…");
    browse.type = "button";
    browse.disabled = src === "env";
    browse.onclick = () => window.LoomBrowse.pick(input);
    pathRow.append(input, browse);
    box.append(lab, pathRow);

    const msg = _p("", "hint");
    const row = _el("div", "row-end");
    const btns = [browse];
    let busy = false;

    const mk = (label, cls, fn) => {
      const b = _el("button", cls, label);
      b.type = "button";
      b.onclick = fn;
      btns.push(b);
      return b;
    };

    const locked = src === "env";      // 来源被环境变量锁定：按钮禁用，并把原因写出来

    // 2026-11-16（复审 P3-3）：失败回执后不能一律解锁 ——「回退」能不能用取决于 d["可回退"]。
    // 原来统一 `disabled = locked`，会把没有可回退位置时的「回退」点亮，按了只得到
    // 后端一句"没有可回退的位置"（这正是 status 注释里说要避免的"按钮亮着但按了没反应"）。
    function unlockBtns() {
      busy = false;
      btns.forEach((b) => { b.disabled = locked; });
      if (!locked && d["可回退"] !== true) bRevert.disabled = true;
    }

    async function doSet(migrate) {
      if (busy) return;
      const root = input.value.trim();
      if (!root) { msg.textContent = "先填一个目录。"; return; }
      const cur = String((d && d["根"]) || "").trim();
      if (migrate && !cur) {
        // 设置页迁移的源＝**当前库**（不是弹层里的 old_root）。后端现在严格必填，空就拒。
        msg.textContent = "读不到当前位置，没法确定要迁移的源目录。先点「刷新」再试。";
        return;
      }
      busy = true;
      btns.forEach((b) => { b.disabled = true; });
      msg.textContent = migrate ? "正在复制并校验，请勿关闭窗口…" : "正在切换位置，请勿关闭窗口…";
      // 三种传参别混（知识 审查记录）：
      //   设置页迁「当前库」→ 带 source=当前根；只切指针 → 不带 source（目标必须先存在）
      const body = migrate ? { root, migrate: true, source: cur } : { root, migrate: false };
      const r = await _post(API.rootSet, body);
      unlockBtns();
      if (r === null) { msg.textContent = "没收到回应（超时或接口异常）。可以再试。"; return; }
      if (!_ok(r)) { msg.textContent = _resultText(r) || "这一步没成。"; return; }
      const okText = _resultText(r) || "已就绪。";
      msg.textContent = okText;
      await refreshSettings(host, "上一次操作：" + okText);   // 回执带进重绘，别被冲掉
    }

    async function doRevert() {
      if (busy) return;
      busy = true;
      btns.forEach((b) => { b.disabled = true; });
      msg.textContent = "正在回退…";
      const r = await _post(API.revert);
      unlockBtns();
      if (r === null) { msg.textContent = "没收到回应（超时或接口异常）。可以再试。"; return; }
      if (!_ok(r)) { msg.textContent = _resultText(r) || "这一步没成。"; return; }
      const okText = _resultText(r) || "已回退。";
      msg.textContent = okText;
      await refreshSettings(host, "上一次操作：" + okText);
    }

    const bRepoint = mk("用这个位置", "primary", () => doSet(false));
    const bMigrate = mk("迁移到它", "ghost", () => doSet(true));
    const bRevert = mk("回退到上一个位置", "ghost", doRevert);

    if (locked) {
      // 环境变量指定时：**连输入框一起禁用**，并写清"改了也不生效"——否则用户改完没反应会当成 bug。
      input.disabled = true;
      bRepoint.disabled = bMigrate.disabled = true;
      bRevert.disabled = true;
      msg.textContent = "当前位置由环境变量指定：界面这里改了也不生效（要改请改环境变量，或去掉它）。";
    } else if (d["可回退"] !== true) {
      bRevert.disabled = true;
      bRevert.title = d["回退目标"] ? "回退目标已不在磁盘上" : "还没有可回退的位置";
    }

    row.append(bRepoint, bMigrate, bRevert);
    box.append(row, msg);
    box.appendChild(_p("「用这个位置」只切指针；「迁移到它」会复制并校验。", "hint"));
    host.appendChild(box);
  }

  async function refreshSettings(host, note) {
    if (!host) return;
    const d = await safeFetch(API.rootStatus, { timeout: 5000 });
    _renderStatus(host, d, note);
  }

  /* 设置框由 workbench.openSettings 打开；这里只挂一个附加监听，不去改它。 */
  function bindSettings() {
    const btn = document.getElementById("btnSettings");
    const host = document.getElementById("lib-root-host");
    if (!btn || !host) return;
    btn.addEventListener("click", () => {
      // 让 workbench 自己的 onclick 先开框，再渲染本区块
      setTimeout(() => { if (host.isConnected) refreshSettings(host); }, 0);
    });
  }

  function init() {
    bindSettings();
    // 启动检测只做一次，不轮询
    detect();
  }

  return { init, detect, showMigrationGate, refreshSettings };
})();
