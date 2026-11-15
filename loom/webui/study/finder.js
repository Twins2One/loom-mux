/*2026-10-30（作者第1、2、3条）：综合搜索的结果列表。

只做一件事：把命中的书段与笔记列在面板里，点一条跳过去。

为什么是列表而不是弹窗：搜索是「找东西」，找到了就要看得到全貌；
弹窗只能装几条，而且点完还得再关一次。

只接一个短句（用户在侧栏输入的），**不开放详细参数**——
那些参数留给书房伙伴的「搜书房」工具。 */
window.StudyFinder = (function () {
  const { makeNode, safeFetch, heading } = window.StudyCore;

  function typeLabel(t) {
    return t === '笔记' ? '笔记' : '书';
  }

  function row(item, word) {
    const box = makeNode('button', 'find-hit');
    box.type = 'button';
    const top = makeNode('div', 'hit-top');
    top.appendChild(makeNode('span', 'hit-kind hit-' + (item.类型 === '笔记' ? 'note' : 'book'), typeLabel(item.类型)));
    const where = [item.分类, item.科目, item.册].filter(Boolean).join(' / ');
    if (where) top.appendChild(makeNode('span', 'hit-where', where));
    if (item.节) top.appendChild(makeNode('span', 'hit-sec', String(item.节)));
    box.appendChild(top);
    box.appendChild(makeNode('p', 'hit-text', String(item.片 || '')));
    box.onclick = function () {
      if (!item.路径) return;
      if (window.StudyCore && window.StudyCore.readVolume) {
        /*2026-11-04修（BUG①）：搜索进来的返回——字样与动作都指向本次搜索页，
          传函数（动态求值）保持联动语义一致。 */
        window.StudyCore.readVolume(item.路径, item.册 || '未命名', () => ({
          label: '← 搜「' + word + '」',
          action: () => window.StudyFinder.show(word),
        }));
      }
    };
    return box;
  }

  //2026-10-30：注意 `heading()` 返回的是 **panel 本身**（它内部已经清空并 append 了标题）。
  //不能拿它当节点再往 panel 里塞——那是把 panel 塞进自己，
  //会抛 HierarchyRequestError，整个函数就死在那一行（实测踩到）。
  async function show(word) {
    if (window.StudyCore.restoreTopbar) window.StudyCore.restoreTopbar();
    const panel = document.getElementById('panel');
    if (!panel) return;
    heading('搜「' + word + '」', '正在翻书…');
    const data = await safeFetch('/api/study/find?q=' + encodeURIComponent(word), { timeout: 20000 });
    const hits = (data && data.命中) || [];
    heading('搜「' + word + '」',
      hits.length ? ('找到 ' + hits.length + ' 处') : '没找到。换个说法试试。');
    if (!hits.length) {
      panel.appendChild(makeNode('p', 'muted', '可以试：一个短句（如「怎么把故事讲得不按时间顺序」）或一个词（如「悬念」）。'));
      return;
    }
    const list = makeNode('div', 'find-list');
    for (const it of hits) list.appendChild(row(it, word));
    panel.appendChild(list);
  }

  return { show };
})();
