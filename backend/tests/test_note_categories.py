"""笔记分类服务单测（config/note_categories.json 元数据层）。

设计约束（见 app/services/note_categories.py）：
- 单选分类：一篇文章一个分类，None = 未分类；
- 删除分类只断开关联，笔记本身不动；
- 分类不存在时的归类是 no-op（自动化默认分类可能指向已删除的分类）。
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.note_categories import (  # noqa: E402
    MAX_CATEGORIES,
    NoteCategoryError,
    NoteCategoryStore,
)


@pytest.fixture
def store(tmp_path):
    return NoteCategoryStore(tmp_path / "note_categories.json")


def test_create_list_and_color(store):
    a = store.create("学习")
    b = store.create(" 工作 ")
    assert a["id"] and a["name"] == "学习" and a["color"]
    assert b["name"] == "工作"
    assert [c["name"] for c in store.list_categories()] == ["学习", "工作"]
    assert store.list_categories()[0]["color"] != store.list_categories()[1]["color"]


def test_create_rejects_empty_duplicate_and_overflow(tmp_path, store):
    with pytest.raises(NoteCategoryError):
        store.create("   ")
    store.create("学习")
    with pytest.raises(NoteCategoryError):
        store.create("学习")
    with pytest.raises(NoteCategoryError):
        store.create("x" * 31)

    tiny = NoteCategoryStore(tmp_path / "tiny.json")
    for i in range(MAX_CATEGORIES):
        tiny.create(f"c{i}")
    with pytest.raises(NoteCategoryError):
        tiny.create("overflow")


def test_rename(store):
    cat = store.create("学习")
    renamed = store.rename(cat["id"], "课程")
    assert renamed["name"] == "课程"
    assert store.list_categories()[0]["name"] == "课程"
    other = store.create("工作")
    with pytest.raises(NoteCategoryError):
        store.rename(other["id"], "课程")
    with pytest.raises(NoteCategoryError):
        store.rename("nope", "x")


def test_assign_and_unassign_many(store):
    cat = store.create("学习")
    res = store.assign(["t1", "t2"], cat["id"])
    assert res["updated"] == 2
    assert store.assignments() == {"t1": cat["id"], "t2": cat["id"]}

    store.assign(["t1"], None)
    assert store.assignments() == {"t2": cat["id"]}

    # 空列表 / 空 id 不写盘
    assert store.assign([], cat["id"])["updated"] == 0
    assert store.assign(["  "], cat["id"])["updated"] == 0


def test_assign_unknown_category_is_noop(store):
    assert store.assign(["t1"], "missing")["updated"] == 0
    assert store.assignments() == {}


def test_delete_category_detaches_notes(store):
    a = store.create("学习")
    b = store.create("工作")
    store.assign(["t1", "t2"], a["id"])
    store.assign(["t3"], b["id"])
    res = store.delete(a["id"])
    assert res["removed_assignments"] == 2
    assert store.assignments() == {"t3": b["id"]}
    assert [c["id"] for c in store.list_categories()] == [b["id"]]
    with pytest.raises(NoteCategoryError):
        store.delete(a["id"])


def test_remove_task(store):
    cat = store.create("学习")
    store.assign(["t1", "t2"], cat["id"])
    assert store.remove_task("t1") is True
    assert store.remove_task("t1") is False
    assert store.assignments() == {"t2": cat["id"]}


def test_persistence_and_version(tmp_path):
    path = tmp_path / "note_categories.json"
    s1 = NoteCategoryStore(path)
    assert s1.version()
    v0 = s1.version()
    cat = s1.create("学习")
    v1 = s1.version()
    assert v1 != v0
    s2 = NoteCategoryStore(path)
    assert [c["id"] for c in s2.list_categories()] == [cat["id"]]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["categories"][0]["name"] == "学习"


def test_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "note_categories.json"
    path.write_text("{not json", encoding="utf-8")
    store = NoteCategoryStore(path)
    assert store.list_categories() == []
    assert store.assignments() == {}
    # 损坏后仍能正常创建（read() 对坏文件返回 {}，等价于空库）
    cat = store.create("学习")
    assert cat["id"]
