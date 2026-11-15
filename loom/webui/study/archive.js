"use strict";

/* 文风档案（语料分析师的档案管理视图）：三层只读 + 删除；
   取样/蒸馏操作已走语料分析师的对话（2026-11-01 作者裁定 agent 化）。 */
window.StudyArchive = (() => {
  const { API, TIMEOUT, makeNode, safeFetch, heading, metaBlock } = window.StudyCore;
  const LAYERS = [
    { key: "raw", api: "原文", name: "① 原文", who: "溯源 · 只读" },
    { key: "distilled", api: "蒸馏原稿", name: "② 蒸馏原稿", who: "审计用 · 可回查" },
    { key: "writer", api: "写手参考", name: "③ 写手参考", who: "后端写作时只注入这一层" },
  ];
  let seq = 0;
  let name = "";
  let selected = new Set();
  let selectedArchive = "";
  let distilling = false;
  let distillAbort = null;
  let distillWatchdog = null;

  function select() {
    window.StudyPartner.leave();
    window.StudyCore.setRole("analyst");
    document.querySelectorAll(".nav-item").forEach((n) =>
      n.classList.toggle("on", n.dataset.role === "analyst"));
  }

  function empty(panel, sampled = false) {
    const box = makeNode("div", "archive-empty");
    box.appendChild(makeNode("p", "muted", sampled ? "这份档案还没有取样。" : "还没有档案。"));
    if (!sampled) box.appendChild(makeNode("p", "muted", "（语料分析师还没蒸馏过。）"));
    const create = makeNode("button", "", "新建档案");
    create.type = "button";
    create.disabled = true;
    create.title = "本版只读，暂不提供新建";
    box.appendChild(create);
    panel.appendChild(box);
  }

  function time(value) {
    const ms = Number(value) * (Number(value) < 100000000000 ? 1000 : 1);
    const d = new Date(ms);
    return Number.isFinite(ms) && ms > 0 && !Number.isNaN(d.getTime())
      ? d.toLocaleString("zh-CN", { hour12: false }) : "";
  }

  async function renderList() {
    if (window.StudyCore.restoreTopbar) window.StudyCore.restoreTopbar();
    select();
    const current = ++seq;
    name = "";
    const panel = heading("语料分析师", "从自己的文本与语料中整理可执行的写手参考");
  //2026-10-30（作者）：补「读什么 / 产出什么」。
  panel.appendChild(metaBlock("作者自己的文本 · 他人语料", "证据/特征/置信度 → 写手参考"));
    const data = await safeFetch(API.archives);
    if (current !== seq || !window.StudyCore.isRole("analyst")) return;
    if (distilling) { window.StudyCore.toast("蒸馏正在进行，请等它结束，别重复提交。"); return; }
    if (!Array.isArray(data?.archives) || !data.archives.length) {
      await renderLibrary("主档案");
      return;
    }
    const list = makeNode("div", "archive-list");
    for (const archive of data.archives) {
      if (!archive || typeof archive.name !== "string" || !archive.name) continue;
      const button = makeNode("button", "archive-item");
      button.type = "button";
      button.onclick = () => renderLibrary(archive.name);
      const main = makeNode("div");
      main.appendChild(makeNode("div", "archive-name", archive.name));
      if (!archive.has_writer) main.appendChild(makeNode("div", "archive-note", "只有原稿，写手参考未生成"));
      const counts = archive.layer_counts || {};
      main.appendChild(makeNode("div", "archive-note",
        "原文 " + (counts.raw || 0) + " · 蒸馏 " + (counts.distilled || 0) + " · 参考 " + (counts.writer || 0)));
      button.appendChild(main);
      button.appendChild(makeNode("span", "archive-meta", time(archive.mtime)));
      list.appendChild(button);
    }
    if (list.children.length) panel.appendChild(list);
    else empty(panel);
  }

  function layerView(layer, count, note) {
    const details = makeNode("details", "layer archive-layer");
    //2026-10-30（作者第1条）：三层**全部默认折叠**。
 //原来「写手参考」那层默认展开，而它正文最长（可能上千字），一进来就被它占满屏，
 //反而看不到上面的取样/蒸馏。
 details.open = false;
    details.appendChild(makeNode("summary", "", layer.name + (count == null ? "" : "（" + count + " 篇）")));
    if (note) details.appendChild(makeNode("p", "layer-note", note));
    const body = makeNode("div", "archive-docs");
    details.appendChild(body);
    return { details, body };
  }

  function docTitle(title) {
    const value = String(title || "");
    return value.toLowerCase().endsWith(".md") ? value : value + ".md";
  }

  async function openFile(archiveName, layer, title, current) {
    const url = API.archiveFile + "?name=" + encodeURIComponent(archiveName) + "&layer=" + encodeURIComponent(layer)
      + (title ? "&file=" + encodeURIComponent(docTitle(title)) : "");
    const data = await safeFetch(url, { timeout: TIMEOUT.file });
    if (current !== seq || !window.StudyCore.isRole("analyst") || name !== archiveName) return;
    const panel = heading(title || "写手参考", archiveName + " · " + layer, {
      label: "← " + archiveName, action: () => renderLibrary(archiveName),
    });
    if (!data || data.ok === false || typeof data.text !== "string") {
      panel.appendChild(makeNode("p", "muted", "打不开"));
      return;
    }
    const text = makeNode("article", "archive-text");
    text.innerHTML = window.StudyCore.mdToHtml(data.text);
    panel.appendChild(text);
  }

  function addDoc(body, archiveName, layer, doc, current) {
    if (!doc || typeof doc["标题"] !== "string" || doc["标题"].startsWith("_")) return;
    const row = makeNode("div", "archive-doc");
    row.dataset.layer = layer;
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.appendChild(makeNode("span", "", doc["标题"]));
    const meta = [doc["字数"] ? window.fmt(doc["字数"]) + " 字" : "",
      doc["已蒸馏"] === false ? "未蒸馏" : "",
      doc["细致度"] ? "细致度 " + doc["细致度"] : ""].filter(Boolean).join(" · ");
    row.appendChild(makeNode("span", "doc-meta", meta));
    row.onclick = () => openFile(archiveName, layer, doc["标题"], current);
    row.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); row.click(); }
    };
    body.appendChild(row);
  }

  function loadWriter(details, archiveName, current) {
    let requested = false;
    const load = async () => {
      if (requested) return;
      requested = true;
      const data = await safeFetch(API.archiveFile + "?name=" + encodeURIComponent(archiveName)
        + "&layer=" + encodeURIComponent("写手参考"), { timeout: TIMEOUT.file });
      if (current !== seq || !window.StudyCore.isRole("analyst") || name !== archiveName) return;
      const text = makeNode("article", "archive-text");
      if (data && data.ok !== false && typeof data.text === "string") text.innerHTML = window.StudyCore.mdToHtml(data.text);
      else text.textContent = "打不开";
      details.appendChild(text);
    };
    details.addEventListener("toggle", () => { if (details.open) load(); });
    if (details.open) load();
  }

  function workCard(title, note) {
    const card = makeNode("section", "work-card");
    card.appendChild(makeNode("h3", "", title));
    if (note) card.appendChild(makeNode("p", "hint", note));
    return card;
  }

  function appendWorkspace(panel, archiveName, data) {
    /*2026-11-01（作者裁定）：取样/导入/蒸馏的操作面已走语料分析师的对话（agent 化），
    这里只保留**只读管理视图**：文档清单 + 三层查看 + 写手参考说明。 */
    const workspace = makeNode("div", "role-workspace");
    const grid = makeNode("div", "work-grid");
    const docs = workCard("② 文档库", "已放入档案的文本；已蒸馏的标记会随真实档案更新。取样与蒸馏都在对话里完成。");
    const list = makeNode("div", "archive-docs");
    list.id = "style-doc-selection";
    for (const doc of data["文档"] || []) {
      if (!doc || typeof doc["标题"] !== "string" || doc["标题"].startsWith("_")) continue;
      const label = makeNode("label", "check");
      const metadata = [doc["字数"] ? window.fmt(doc["字数"]) + " 字" : "",
        doc["已蒸馏"] ? "已蒸馏" : "未蒸馏", doc["蒸馏于"] || "", doc["细致度"] || ""].filter(Boolean).join(" · ");
      label.append(document.createTextNode(doc["标题"] + " · " + metadata));
      const remove = makeNode("button", "mini ghost", "删除");
      remove.type = "button";
      remove.onclick = () => deleteSample(archiveName, doc["标题"]);
      label.appendChild(remove);
      list.appendChild(label);
    }
    if (!list.children.length) list.appendChild(makeNode("p", "muted", "这份档案还没有取样。在语料分析师的对话里让它取样。"));
    docs.appendChild(list);
    grid.append(docs);
  const strength = workCard("④ 写手参考",
 "后端写作时只注入这一层。弱约900 字 / 中约3200 字 / 强约9000 字。");
 //2026-10-28作者第6条：注入量这类设置留在书内「文风档案」，书房这一页只管理与生产档案。
 // 所以这里不再摆一个点不动的下拉框（看得见点不了的控件只会催人去找），只留一句据实说明。
 strength.appendChild(makeNode("p", "muted",
 "具体每本书用多少，回书内「文风档案」里调。"));
    workspace.append(grid, strength);
    panel.appendChild(workspace);
  }

  /*2026-11-01（作者裁定）：取样（addSample/importSamples）、蒸馏（distillSelected）的操作面
  已全部走语料分析师的对话（agent 化），页面只留只读视图与删除。原函数退役——
  能力在后端 partner_tools 里由 agent 调用，这里不再有 UI 入口。 */
  async function deleteSample(archiveName, title) {
    if (!window.confirm("只删除「" + archiveName + "」里的样本「" + title + "」及其蒸馏稿吗？其他文件不动。")) return;
    const data = await safeFetch("/api/style/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: archiveName, title }),
    });
    if (!data || data.ok !== true) { window.StudyCore.toast(data?.reason || data?.error || "这篇没删掉，可能已改名或不在库中。"); return; }
    selected.delete(title);
    window.StudyCore.toast("已从这份档案移除该样本。");
    await renderLibrary(archiveName);
  }

  // ── 0.9.0 B2 分块蒸馏工作台(设计稿 v2.1 §9) ─────────────────────────
  async function b2Call(action, body) {
    return safeFetch("/api/style/b2/" + action, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ name: archiveName2 || name }, body || {})),
    });
  }
  let archiveName2 = "";   // b2Call 用的档案名(renderLibrary 时同步)

  async function appendB2Panel(panel, archiveName) {
    const card = workCard("⑤ 分块蒸馏（B2）",
      "整本按章分块→逐块蒸馏→语义去重→归纳。增量：只蒸新增/变动的章。");
    const statusBox = makeNode("div", "archive-b2-status muted", "读取状态…");
    card.appendChild(statusBox);
    const btnRow = makeNode("div", "archive-b2-actions");
    const mk = (label, fn, title) => {
      const b = makeNode("button", "mini", label);
      b.type = "button"; b.title = title || ""; b.onclick = fn;
      btnRow.appendChild(b);
      return b;
    };
    const st = async () => {
      statusBox.textContent = "读取状态…";
      const d = await b2Call("status");
      if (!d || d.ok === false) { statusBox.textContent = d?.error || "读不到状态"; return; }
      statusBox.textContent = "共 " + d["块数"] + " 块 · 已蒸 " + d["已蒸"]
        + " · 待蒸 " + d["待蒸"] + " · 失败 " + d["failed"]
        + (d["human_edited"] ? " · 人工改 " + d["human_edited"] : "")
        + " · 预计 " + d["预计调用"];
    };
    const migrate = mk("按新版重蒸(迁移)", async () => {
      if (!window.confirm("旧稿可能有你手动改过的内容——旧版将以 .v0旧版 后缀保留、永不自动删。\n新版蒸馏完成后请对照新旧内容再点「确认收下」。继续迁移?")) return;
      const d = await b2Call("migrate");
      window.StudyCore.toast(d?.ok ? "已迁移到新版结构,开始蒸馏即可。" : (d?.error || "迁移没成"));
      st();
    }, "旧结构档案才可用");
    const run = mk("开始蒸馏", async () => {
      const deep = window.confirm("深度蒸馏 = 多层粗到细分析(每块多 1 次调用)?【确定=开深度 / 取消=一般蒸馏】");
      const st0 = await b2Call("status");
      if (st0?.ok && window.confirm("预计 " + st0["待蒸"] + " 块 ≈ "
        + (deep ? st0["待蒸"] * 2 : st0["待蒸"]) + "~" + (st0["待蒸"] * 2 + 6)
        + " 次模型调用。开始?")) {
        window.StudyCore.toast("分块蒸馏进行中(串行,大书可能要几十分钟),完成后回来看状态。");
        const d = await b2Call("run", { deep });
        window.StudyCore.toast(d?.ok
          ? "本轮完成:蒸 " + d.done + " 失败 " + d.failed + (d.aborted ? " · 中途中止" : "")
          : (d?.error || "没跑成"));
        st();
      }
    }, "增量:只蒸新增/变动的章");
    const merge = mk("合并去重", async () => {
      const d = await b2Call("merge");
      window.StudyCore.toast(d?.ok ? "合并完成(剔除 " + d.removed + " 条重复,调用 " + d.calls + " 次)" : (d?.error || "没成"));
    }, "全部块产出→语义去重→归纳成蒸馏原稿");
    const cards = mk("生成卡片集", async () => {
      const d = await b2Call("cards");
      window.StudyCore.toast(d?.ok ? "卡片集已生成(" + d.chars + " 字),写作时按注入量使用" : (d?.error || "没成"));
    }, "蒸馏原稿→六节写手参考");
    const l1 = mk("覆盖率检查", async () => {
      const d = await b2Call("verify");
      if (d?.ok) window.StudyCore.toast(d["红线"] ? "⚠ 红线:块 " + d["块齐全率"] + " 禁用 " + d["禁用转录率"] + " 实体 " + d["实体命中率"]
        : "L1 通过:块 " + d["块齐全率"] + " 禁用 " + d["禁用转录率"] + " 实体 " + d["实体命中率"]);
      else window.StudyCore.toast(d?.error || "没成");
    }, "零成本自动检查");
    const l2 = mk("验一验(仿写)", async () => {
      window.StudyCore.toast("仿写生成中(1 次调用)…");
      const d = await b2Call("l2");
      if (d?.ok) openFile(archiveName, "写手参考", "L2 仿写对比:" + (d.title || ""), ++seq && seq);
      else window.StudyCore.toast(d?.reason || d?.error || "没成");
    }, "按卡片集仿写一块,与原文对照看像不像");
    // 初始按钮可见性:旧结构才显示迁移
    fetch("/api/style/b2/status", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: archiveName }) })
      .then((r) => r.json()).then((d) => { if (d?.ok) migrate.classList.add("hidden"); }).catch(() => {});
    card.appendChild(btnRow);
    card.appendChild(makeNode("p", "muted",
      "流程:开始蒸馏 → 合并去重 → 生成卡片集 → 覆盖率检查/验一验。旧档案先「按新版重蒸」。"));
    panel.appendChild(card);
    st();
  }

  async function renderLibrary(archiveName) {
    if (window.StudyCore.restoreTopbar) window.StudyCore.restoreTopbar();
    select();
    const current = ++seq;
    if (name !== archiveName) { selected.clear(); selectedArchive = archiveName; }
    name = archiveName;
    const panel = heading(archiveName, "取样 →文档库 →蒸馏 →写手参考", {
      label: "← 语料分析师", action: renderList,
    });
    const switcher = makeNode("div", "archive-switcher");
    const nameInput = makeNode("input");
    nameInput.id = "style-archive-jump";
    nameInput.value = archiveName;
    nameInput.setAttribute("aria-label", "档案名");
    const open = makeNode("button", "mini", "打开 / 新建档案");
    open.type = "button";
    open.onclick = () => {
      const next = nameInput.value.trim();
      if (next) renderLibrary(next);
    };
    switcher.append(nameInput, open);
    panel.appendChild(switcher);
    const data = await safeFetch(API.archive + "?name=" + encodeURIComponent(archiveName));
    if (current !== seq || !window.StudyCore.isRole("analyst") || name !== archiveName) return;
    if (!data || data.ok === false) {
      panel.appendChild(makeNode("p", "muted", "档案工作区暂时没有展开。"));
      return;
    }
    if (data["存在"] === false) {
      appendWorkspace(panel, archiveName, { "文档": [], "篇数": 0 });
      return;
    }
    appendWorkspace(panel, archiveName, data);
    if (data["超大跳过"] > 0) {
      panel.appendChild(makeNode("p", "muted", "另有 " + data["超大跳过"] + " 篇超过 2 MB，未列出"));
    }
    const layers = makeNode("div", "layers");
    const writer = layerView(LAYERS[2], null, "后端写作时只注入这一层");
    layers.appendChild(writer.details);
    for (const layer of [LAYERS[1], LAYERS[0]]) {
      const view = layerView(layer, 0, layer.who);
      layers.appendChild(view.details);
      for (const doc of data["文档"] || []) {
        if (layer.key === "raw" || doc["已蒸馏"] === true) addDoc(view.body, archiveName, layer.api, doc, current);
      }
      if (layer.key === "distilled" && !view.body.children.length) {
        view.body.appendChild(makeNode("p", "muted", "还没有蒸馏原稿。"));
      }
      view.details.querySelector("summary").textContent = layer.name + "（" + view.body.querySelectorAll(".archive-doc").length + " 篇）";
    }
    if (data["篇数"] === 0) panel.appendChild(makeNode("p", "muted", "这份档案还没有取样。"));
    panel.appendChild(layers);
    if (data["有档案"] === false) writer.details.appendChild(makeNode("p", "muted", "只有原稿，写手参考未生成"));
    else loadWriter(writer.details, archiveName, current);
    // 0.9.0 B2 分块蒸馏工作台
    archiveName2 = archiveName;
    appendB2Panel(panel, archiveName);
  }

  return { renderList, renderLibrary, isBusy: () => distilling };
})();
