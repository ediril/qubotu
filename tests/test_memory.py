from qubotu.memory import fts_query


async def test_add_search_update_forget(engine):
    mem = engine.memory
    a = await mem.add("The user lives in Oakland", "fact")
    b = await mem.add("The user prefers short bullet summaries", "preference")
    await mem.add("Working on a project called Qubotu", "project")

    hits = await mem.search("where does the user live")
    assert hits[0]["id"] == a

    hits = await mem.search("summaries")
    assert hits[0]["id"] == b

    assert await mem.update(a, text="The user lives in Berkeley")
    hits = await mem.search("Berkeley")
    assert hits[0]["id"] == a and "Berkeley" in hits[0]["text"]

    assert mem.delete(a)
    assert all(h["id"] != a for h in await mem.search("Berkeley"))
    assert not mem.delete(a)


async def test_profile_puts_pinned_first(engine):
    mem = engine.memory
    a = await mem.add("old fact")
    await mem.add("newer fact")
    await mem.update(a, pinned=True)
    assert mem.profile()[0]["id"] == a


def test_fts_query_is_safe():
    assert fts_query('drop "table"; OR AND *') == '"drop" OR "table" OR "or" OR "and"'
    assert fts_query("!!") is None
