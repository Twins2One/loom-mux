

class _FakeTextBackend:
    """假后端：第一次要求用工具，第二次才真回答。"""

    def __init__(self):
        self.rounds =0

    def complete(self, system, user, on_chunk=None):
        self.rounds +=1
        if '【工具' in user:
            out = '读到了五种非线性叙事形态。'
        else:
            out = '用：读一册' + chr(10) + '册名：非线性叙事的五种形态'
        return out


def test_text_protocol_loop_reads_a_book_and_counts():
    from loom.study import ask as A
    ev = []
    b = _FakeTextBackend()
    text = A._chat_with_tools(b, '系统', '怎么把故事讲得不按时间顺序',
            ev.append, '20261030-000000')
    #两次调用：一次要工具、一次真回答
    assert b.rounds ==2
    assert '五种非线性' in text
    assert '用：读一册' not in text


def test_status_lines_are_human_not_technical():
    from loom.study import ask as A
    ev = []
    b = _FakeTextBackend()
    A._chat_with_tools(b, 'sys', '问', ev.append, '20261030-000000')
    notes = [e['text'] for e in ev if e['t'] == 'status']
    assert notes
    joined = ' '.join(notes)
    assert '正在用「' not in joined
    assert 'tool' not in joined.lower()
    assert ('翻' in joined) or ('看书架' in joined)


def test_call_count_appears_from_second_call():
    from loom.study import ask as A
    assert '第' not in A._reading_note('读一册',1)
    assert '第 2 次' in A._reading_note('读一册',2)


def test_total_note_reports_how_many_books_turned():
    from loom.study import ask as A
    ev = []
    b = _FakeTextBackend()
    A._chat_with_tools(b, 'sys', '问', ev.append, '20261030-000000')
    joined = ' '.join(e['text'] for e in ev if e['t'] == 'status')
    assert '翻了' in A._TOTAL_NOTE %3 and '次书' in A._TOTAL_NOTE %3
