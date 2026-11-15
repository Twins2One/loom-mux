/*2026-10-30（作者第4条）：阅读笔记的弹窗与增删查。

一份独立的小模块，不被读书那边的逻辑污染：
- load(path, cb)取一册的笔记；
- open(path, book, para, items, repaint, isNew)开弹窗（看这一段的笔记 /写新的）；
- 笔记不单开搜索入口：内容由 /api/study/find 一并返回，走侧栏综合搜索。

为什么要弹窗而不直接改正文：笔记与原文必须分离，
删笔记的时候不能碰原文一个字节。 */
window.StudyNotes = (function () {
  const { makeNode, safeFetch, toast } = window.StudyCore;

  function load(path, cb) {
    safeFetch('/api/study/notes?path=' + encodeURIComponent(path), { timeout: 5000 })
      .then((d) => cb((d && d.笔记) || []))
      .catch(() => cb([]));
  }

  function pad() {
    let box = document.getElementById('notePad');
    if (box) return box;
    box = makeNode('div', 'note-pad');
    box.id = 'notePad';
    box.hidden = true;
    document.body.appendChild(box);
    return box;
  }

  function close() {
    const b = document.getElementById('notePad');
    if (b) b.hidden = true;
  }

  function open(path, book, para, items, repaint, isNew) {
    const box = pad();
    box.hidden = false;
    box.replaceChildren();
    const head = makeNode('div', 'note-head');
    head.appendChild(makeNode('b', '', '第 ' + (para + 1) + ' 段的笔记'));
    const x = makeNode('button', 'ghost', '关闭');
    x.type = 'button';
    x.onclick = close;
    head.appendChild(x);
    box.appendChild(head);

    const mine = (items || []).filter((n) => Number(n.段) === para);
    if (mine.length) {
      const ul = makeNode('div', 'note-list');
      mine.forEach((n) => {
        const row = makeNode('div', 'note-row');
        row.appendChild(makeNode('p', '', n.文));
        const del = makeNode('button', 'ghost', '删');
        del.type = 'button';
        del.onclick = async () => {
          const got = await safeFetch('/api/study/notes/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, id: n.id }), timeout: 5000,
          });
          if (got && got.ok) { load(path, (all) => { items.length = 0; all.forEach((z) => items.push(z)); repaint(); open(path, book, para, items, repaint, false); }); }
          else toast('没删成，再试一次。', true);
        };
        row.appendChild(del);
        ul.appendChild(row);
      });
      box.appendChild(ul);
    }

    const ta = makeNode('textarea', 'note-input');
    ta.placeholder = '记一句：这一段哪里好 / 哪里怪 / 和哪一节重复了…';
    box.appendChild(ta);
    const row = makeNode('div', 'note-foot');
    const save = makeNode('button', 'primary', '存这条');
    save.type = 'button';
    save.onclick = async () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      save.disabled = true;
      const got = await safeFetch('/api/study/notes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, book, para, text }), timeout: 5000,
      });
      save.disabled = false;
      if (got && got.ok) {
        ta.value = '';
        load(path, (all) => { items.length = 0; all.forEach((z) => items.push(z)); repaint(); open(path, book, para, items, repaint, false); });
      } else toast('没存成，再试一次。', true);
    };
    row.appendChild(save);
    box.appendChild(row);
    if (isNew) ta.focus();
  }


  return { load, open, close };
})();
