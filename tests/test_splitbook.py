# -*- coding: utf-8 -*-
"""教材拆分（0.8.3，2026-10-30）。

守四件：
1.能认 **任意层级** 标题（实测29册里25册用的是 `#### `，只认 `## ` 会整册拆不开）；
2.“方法 N”这类讲方法本身的段归理论，不归应用；
3.没有子标题的整册归理论（不能当成拆解某个作品）；
4.**不写盘**：只读与判断，落盘由调用方做。
"""
from __future__ import annotations

from loom.study import splitbook as S


def test_detects_four_level_headings(tmp_path):
    f = tmp_path / "a.md"
    #四级标题字面量拼出来，免得被录入时吞掉空格
    h4 = chr(35) *4 + chr(32)
    text = chr(35) + chr(32) + "主题" + chr(10) + chr(10)
    text += h4 + "1.1 第一节" + chr(10) + "正文一" + chr(10) + chr(10)
    text += h4 + "1.2 第二节" + chr(10) + "正文二" + chr(10)
    f.write_text(text, encoding="utf-8")
    secs = S.split_sections(f.read_text(encoding="utf-8"))
    assert len(secs) ==3
    assert "1.1" in secs[1][0]

def test_method_heading_is_theory():
    assert S.classify("二、方法1：分支叙事（58.6s）", "《某作品》《另一个》《第三个》") == "theory"


def test_whole_book_without_subheads_goes_theory(tmp_path):
    f = tmp_path / "b.md"
    f.write_text("# 只有一个标题" + chr(10) + "正文" + chr(10), encoding="utf-8")
    r = S.split_book(f)
    assert r["统计"]["应用段"] ==0
    assert r["统计"]["理论段"] ==1


def test_theory_hint_beats_many_work_names():
    #方法论段落常举很多例子，不能因为作品名多就当拆解
    title = "2.7b 起承转合的操作化：高潮／反转"
    body = "《肖申克的救赎》《荒岛余生》《你的名字》"
    assert S.classify(title, body) == "theory"

def test_head_block_goes_nowhere():
    #作者裁定：开头元信息块**两边都不放**
    assert S.classify("（开头：科目 /来源 /一句话）", "科目：故事技法") == "head"

def test_cross_reference_goes_to_note():
    #作者裁定：交叉引用**备注到相关段落**
    assert S.classify("九、和另外几支的呼应", "x") == "xref"

def test_three_moves_is_theory_not_apply():
    #作者裁定2.13b「打斗三招」是理论（三招是写法）
    title = "2.13b 打斗三招：冲突 → 画面 → 证明（补2.13 缺的\"失败案例\"）"
    assert S.classify(title, "《一》《二》《三》") == "theory"

def test_two_suggestions_goes_apply():
    #作者裁定6I.3「她的两条建议」进图书馆
    assert S.classify("6I.3 她的两条建议（15:54–16:51）", "x") == "apply"


def test_special_projects_are_named_not_guessed():
    #作者裁定：这4条不是"某部作品"，项目名按材料性质定，都归 其它/
    cases = {
        "2.23.5 案例：摄像头式写法": ("其它", "改稿案例·摄像头式写法"),
        "4.5.1 案例诊断：场景太大": ("其它", "投稿诊断·场景太大"),
        "6I.3 她的两条建议（15:54–16:51）": ("其它", "女频平台红线·过来人的建议"),
        "九组\"用道具讲政治\"的实例": ("其它", "用道具讲政治·九组实例"),
    }
    for title, want in cases.items():
        assert S.pick_special(title) == want, title


def test_real_works_still_use_the_work_name():
    #没被点名的照旧按作品名走，特殊表不能误伤
    assert S.pick_special("2.10.3《三体》水滴") is None
    assert S.pick_project("《三体》水滴《三体》") == "《三体》拆解"


def test_split_book_applies_special_naming(tmp_path):
    h4 = chr(35) *4 + chr(32)
    text = chr(35) + chr(32) + "主题" + chr(10) + chr(10)
    text += h4 + "4.5.1 案例诊断：场景太大" + chr(10) + "《某部作品》正文" + chr(10)
    f = tmp_path / "c.md"
    f.write_text(text, encoding="utf-8")
    r = S.split_book(f)
    assert len(r["应用"]) == 1
    assert r["应用"][0]["载体"] == "其它"
    assert r["应用"][0]["项目"] == "投稿诊断·场景太大"
