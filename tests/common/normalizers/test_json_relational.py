import pytest
from dlt.common.sources import DLT_METADATA_FIELD, with_table_name

from dlt.common.utils import digest128, uniq_id
from dlt.common.schema import Schema
from dlt.common.schema.utils import new_table

from dlt.common.normalizers.json.relational import JSONNormalizerConfigPropagation, _flatten, _get_child_row_hash, _normalize_row, normalize

from tests.utils import create_schema_with_name

@pytest.fixture
def schema() -> Schema:
    return Schema("default")


def test_flatten_fix_field_name(schema: Schema) -> None:
    row = {
        "f-1": "!  30",
        "f 2": [],
        "f!3": {
            "f4": "a",
            "f-5": "b",
            "f*6": {
                "c": 7,
                "c v": 8,
                "c x": []
            }
        }
    }
    flattened_row = _flatten(schema, "mock_table", row)
    assert "f_1" in flattened_row
    assert "f_2" in flattened_row
    assert "f_3__f4" in flattened_row
    assert "f_3__f_5" in flattened_row
    assert "f_3__f_6__c" in flattened_row
    assert "f_3__f_6__c_v" in flattened_row
    assert "f_3__f_6__c_x" in flattened_row
    assert "f_3" not in flattened_row


def test_preserve_complex_value(schema: Schema) -> None:
    # add table with complex column
    schema.update_schema(
        new_table("with_complex",
            columns = [{
                "name": "value",
                "data_type": "complex",
                "nullable": "true"
            }])
    )
    row_1 = {
        "value": 1
    }
    flattened_row = _flatten(schema, "with_complex", row_1)
    assert flattened_row["value"] == 1

    row_2 = {
        "value": {"complex": True}
    }
    flattened_row = _flatten(schema, "with_complex", row_2)
    assert flattened_row["value"] == row_2["value"]
    # complex value is not flattened
    assert "value__complex" not in flattened_row


def test_preserve_complex_value_with_hint(schema: Schema) -> None:
    # add preferred type for "value"
    schema._settings.setdefault("preferred_types", {})["re:^value$"] = "complex"
    schema._compile_regexes()
    print(schema._compiled_preferred_types)

    row_1 = {
        "value": 1
    }
    flattened_row = _flatten(schema, "any_table", row_1)
    assert flattened_row["value"] == 1

    row_2 = {
        "value": {"complex": True}
    }
    flattened_row = _flatten(schema, "any_table", row_2)
    assert flattened_row["value"] == row_2["value"]
    # complex value is not flattened
    assert "value__complex" not in flattened_row


def test_child_table_linking(schema: Schema) -> None:
    row = {
        "f": [{
            "l": ["a", "b", "c"],
            "v": 120,
            "o": [{"a": 1}, {"a": 2}]
        }]
    }
    # request _dlt_root_id propagation
    add_dlt_root_id_propagation(schema)

    rows = list(_normalize_row(schema, row, {}, "table"))
    # should have 7 entries (root + level 1 + 3 * list + 2 * object)
    assert len(rows) == 7
    # root elem will not have a root hash if not explicitly added, "extend" is added only to child
    root_row = next(t for t in rows if t[0][0] == "table")
    # root row must have parent table none
    assert root_row[0][1] is None

    root = root_row[1]
    assert "_dlt_root_id" not in root
    assert "_dlt_parent_id" not in root
    assert "_dlt_list_idx" not in root
    # record hash will be autogenerated
    assert "_dlt_id" in root
    row_id = root["_dlt_id"]
    # all child entries must have _dlt_root_id == row_id
    assert all(e[1]["_dlt_root_id"] == row_id for e in rows if e[0][0] != "table")
    # all child entries must have _dlt_id
    assert all("_dlt_id" in e[1] for e in rows if e[0][0] != "table")
    # all child entries must have parent hash and pos
    assert all("_dlt_parent_id" in e[1] for e in rows if e[0][0] != "table")
    assert all("_dlt_list_idx" in e[1] for e in rows if e[0][0] != "table")
    # filter 3 entries with list
    list_rows = [t for t in rows if t[0][0] == "table__f__l"]
    assert len(list_rows) == 3
    # all list rows must have table_f as parent
    assert all(r[0][1] == "table__f" for r in list_rows)
    # get parent for list
    f_row = next(t for t in rows if t[0][0] == "table__f")
    # parent of the list must be "table"
    assert f_row[0][1] == "table"
    f_row_v = f_row[1]
    # parent of "f" must be row_id
    assert f_row_v["_dlt_parent_id"] == row_id
    # all elems in the list must have proper parent
    assert all(e[1]["_dlt_parent_id"] == f_row_v["_dlt_id"] for e in list_rows)
    # all values are there
    assert [e[1]["value"] for e in list_rows] == ["a", "b", "c"]


def test_child_table_linking_primary_key(schema: Schema) -> None:
    row = {
        "id": "level0",
        "f": [{
            "id": "level1",
            "l": ["a", "b", "c"],
            "v": 120,
            "o": [{"a": 1}, {"a": 2}]
        }]
    }
    schema.merge_hints({"primary_key": ["id"]})
    schema._compile_regexes()

    rows = list(_normalize_row(schema, row, {}, "table"))
    root = next(t for t in rows if t[0][0] == "table")[1]
    # record hash must be derived from natural key
    assert root["_dlt_id"] == digest128("level0")

    # table at "f"
    t_f = next(t for t in rows if t[0][0] == "table__f")[1]
    assert t_f["_dlt_id"] == digest128("level1")
    # we use primary key to link to parent
    assert "_dlt_parent_id" not in t_f
    assert "_dlt_list_idx" not in t_f
    assert "_dlt_root_id" not in t_f

    list_rows = [t for t in rows if t[0][0] == "table__f__l"]
    assert all(e[1]["_dlt_parent_id"] == digest128("level1") for e in list_rows)
    assert all(r[0][1] == "table__f" for r in list_rows)
    obj_rows = [t for t in rows if t[0][0] == "table__f__o"]
    assert all(e[1]["_dlt_parent_id"] == digest128("level1") for e in obj_rows)
    assert all(r[0][1] == "table__f" for r in obj_rows)


def test_yields_parents_first(schema: Schema) -> None:
    row = {
        "id": "level0",
        "f": [{
            "id": "level1",
            "l": ["a", "b", "c"],
            "v": 120,
            "o": [{"a": 1}, {"a": 2}]
        }],
        "g": [{
            "id": "level2_g",
            "l": ["a"]
        }]
    }
    rows = list(_normalize_row(schema, row, {}, "table"))
    tables = list(r[0][0] for r in rows)
    # child tables are always yielded before parent tables
    expected_tables = ['table', 'table__f', 'table__f__l', 'table__f__l', 'table__f__l', 'table__f__o', 'table__f__o', 'table__g', 'table__g__l']
    assert expected_tables == tables


def test_yields_parent_relation(schema: Schema) -> None:
    row = {
        "id": "level0",
        "f": [{
            "id": "level1",
            "l": ["a"],
            "o": [{"a": 1}],
            "b": {
                "a": [ {"id": "level5"}],
            }
        }],
        "d": {
            "a": [ {"id": "level4"}],
            "b": {
                "a": [ {"id": "level5"}],
            },
            "c": "x"
        },
        "e": [{
            "o": [{"a": 1}],
            "b": {
                "a": [ {"id": "level5"}],
            }
        }]
    }
    rows = list(_normalize_row(schema, row, {}, "table"))
    # normalizer must return parent table first and move in order of the list elements when yielding child tables
    # the yielding order if fully defined
    expected_parents = [
        ("table", None),
        ("table__f", "table"),
        ("table__f__l", "table__f"),
        ("table__f__o", "table__f"),
        # "table__f__b" is not yielded as it is fully flattened into table__f
        ("table__f__b__a", "table__f"),
        # same for table__d -> fully flattened into table
        ("table__d__a", "table"),
        ("table__d__b__a", "table"),
        # table__e is yielded it however only contains linking information
        ("table__e", "table"),
        ("table__e__o", "table__e"),
        ("table__e__b__a", "table__e")
    ]
    parents = list(r[0] for r in rows)
    assert parents == expected_parents

    # make sure that table__e is just linking
    table__e = [r[1] for r in rows if r[0][0] == "table__e"]
    assert all(not f.startswith("_dlt") for f in table__e.values()) is True

    # check if linking is correct when not directly derived
    table__e__b__a = [r[1] for r in rows if r[0][0] == "table__e__b__a"]
    assert table__e__b__a["_dlt_parent_id"] == table__e["__dlt_id"]

    table__f = [r[1] for r in rows if r[0][0] == "table__f"]
    table__f__b__a = [r[1] for r in rows if r[0][0] == "table__f__b__a"]
    assert table__f__b__a["_dlt_parent_id"] == table__f["__dlt_id"]


def test_child_table_linking_compound_primary_key(schema: Schema) -> None:
    row = {
        "id": "level0",
        "offset": 12102.45,
        "f": [{
            "id": "level1",
            "item_no": 8129173987192873,
            "l": ["a", "b", "c"],
            "v": 120,
            "o": [{"a": 1}, {"a": 2}]
        }]
    }
    schema.merge_hints({"primary_key": ["id", "offset", "item_no"]})
    schema._compile_regexes()

    rows = list(_normalize_row(schema, row, {}, "table"))
    root = next(t for t in rows if t[0][0] == "table")[1]
    # record hash must be derived from natural key
    assert root["_dlt_id"] == digest128("level0_12102.45")
    t_f = next(t for t in rows if t[0][0] == "table__f")[1]
    assert t_f["_dlt_id"] == digest128("level1_8129173987192873")


def test_list_position(schema: Schema) -> None:
    row = {
        "f": [{
            "l": ["a", "b", "c"],
            "v": 120,
            "lo": [{"e": "a"}, {"e": "b"}, {"e":"c"}]
        }]
    }
    rows = list(_normalize_row(schema, row, {}, "table"))
    # root has no pos
    root = [t for t in rows if t[0][0] == "table"][0][1]
    assert "_dlt_list_idx" not in root

    # all other have pos
    others = [t for t in rows if t[0][0] != "table"]
    assert all("_dlt_list_idx" in e[1] for e in others)

    # f_l must be ordered as it appears in the list
    for pos, elem in enumerate(["a", "b", "c"]):
        row = next(t[1] for t in rows if t[0][0] == "table__f__l" and t[1]["value"] == elem)
        assert row["_dlt_list_idx"] == pos

    # f_lo must be ordered - list of objects
    for pos, elem in enumerate(["a", "b", "c"]):
        row = next(t[1] for t in rows if t[0][0] == "table__f__lo" and t[1]["e"] == elem)
        assert row["_dlt_list_idx"] == pos


def test_child_row_deterministic_hash(schema: Schema) -> None:
    row_id = uniq_id()
    # directly set record hash so it will be adopted in unpacker as top level hash
    row = {
        "_dlt_id": row_id,
        "f": [{
            "l": ["a", "b", "c"],
            "v": 120,
            "lo": [{"e": "a"}, {"e": "b"}, {"e":"c"}]
        }]
    }
    rows = list(_normalize_row(schema, row, {}, "table"))
    children = [t for t in rows if t[0][0] != "table"]
    # all hashes must be different
    distinct_hashes = set([ch[1]["_dlt_id"] for ch in children])
    assert len(distinct_hashes) == len(children)

    # compute hashes for all children
    for (table, _), ch in children:
        expected_hash = digest128(f"{ch['_dlt_parent_id']}_{table}_{ch['_dlt_list_idx']}")
        assert ch["_dlt_id"] == expected_hash

    # direct compute one of the
    el_f = next(t[1] for t in rows if t[0][0] == "table__f" and t[1]["_dlt_list_idx"] == 0)
    f_lo_p2 = next(t[1] for t in rows if t[0][0] == "table__f__lo" and t[1]["_dlt_list_idx"] == 2)
    assert f_lo_p2["_dlt_id"] == digest128(f"{el_f['_dlt_id']}_table__f__lo_2")

    # same data with same table and row_id
    rows_2 = list(_normalize_row(schema, row, {}, "table"))
    children_2 = [t for t in rows_2 if t[0][0] != "table"]
    # corresponding hashes must be identical
    assert all(ch[0][1]["_dlt_id"] == ch[1][1]["_dlt_id"] for ch in zip(children, children_2))

    # change parent table and all child hashes must be different
    rows_4 = list(_normalize_row(schema, row, {}, "other_table"))
    children_4 = [t for t in rows_4 if t[0][0] != "other_table"]
    assert all(ch[0][1]["_dlt_id"] != ch[1][1]["_dlt_id"] for ch in zip(children, children_4))

    # change parent hash and all child hashes must be different
    row["_dlt_id"] = uniq_id()
    rows_3 = list(_normalize_row(schema, row, {}, "table"))
    children_3 = [t for t in rows_3 if t[0][0] != "table"]
    assert all(ch[0][1]["_dlt_id"] != ch[1][1]["_dlt_id"] for ch in zip(children, children_3))


def test_keeps_dlt_id(schema: Schema) -> None:
    h = uniq_id()
    row = {
        "a": "b",
        "_dlt_id": h
    }
    rows = list(_normalize_row(schema, row, {}, "table"))
    root = [t for t in rows if t[0][0] == "table"][0][1]
    assert root["_dlt_id"] == h


def test_propagate_hardcoded_context(schema: Schema) -> None:
    row = {"level": 1, "list": ["a", "b", "c"], "comp": [{"_timestamp": "a"}]}
    rows = list(_normalize_row(schema, row, {"_timestamp": 1238.9, "_dist_key": "SENDER_3000"}, "table"))
    # context is not added to root element
    root = next(t for t in rows if t[0][0] == "table")[1]
    assert "_timestamp" not in root
    assert "_dist_key" not in root
    # the original _timestamp field will be overwritten in children
    assert all(e[1]["_timestamp"] == 1238.9 and e[1]["_dist_key"] == "SENDER_3000" for e in rows if e[0][0] != "table")


def test_propagates_root_context(schema: Schema) -> None:
    add_dlt_root_id_propagation(schema)
    # add timestamp propagation
    schema._normalizers_config["json"]["config"]["propagation"]["root"]["timestamp"] = "_partition_ts"
    # add propagation for non existing element
    schema._normalizers_config["json"]["config"]["propagation"]["root"]["__not_found"] = "__not_found"

    row = {"_dlt_id": "###", "timestamp": 12918291.1212, "dependent_list":[1, 2,3], "dependent_objects": [{"vx": "ax"}]}
    normalized_rows = list(_normalize_row(schema, row, {}, "table"))
    # all non-root rows must have:
    non_root = [r for r in normalized_rows if r[0][1] is not None]
    assert all(r[1]["_dlt_root_id"] == "###" for r in non_root)
    assert all(r[1]["_partition_ts"] == 12918291.1212 for r in non_root)
    assert all("__not_found" not in r[1] for r in non_root)


def test_propagates_table_context(schema: Schema) -> None:
    add_dlt_root_id_propagation(schema)
    prop_config: JSONNormalizerConfigPropagation = schema._normalizers_config["json"]["config"]["propagation"]
    prop_config["root"]["timestamp"] = "_partition_ts"
    # for table "table__lvl1" request to propagate "vx" and "partition_ovr" as "_partition_ts" (should overwrite root)
    prop_config["tables"]["table__lvl1"] = {
        "vx": "__vx",
        "partition_ovr": "_partition_ts",
        "__not_found": "__not_found"
    }

    row = {
            "_dlt_id": "###",
            "timestamp": 12918291.1212,
            "lvl1": [{
                    "vx": "ax",
                    "partition_ovr": 1283.12,
                    "lvl2": [{
                        "_partition_ts": "overwritten"
                    }]
                }]
            }

    normalized_rows = list(_normalize_row(schema, row, {}, "table"))
    non_root = [r for r in normalized_rows if r[0][1] is not None]
    # _dlt_root_id in all non root
    assert all(r[1]["_dlt_root_id"] == "###" for r in non_root)
    # __not_found nowhere
    assert all("__not_found" not in r[1] for r in non_root)
    # _partition_ts == timestamp only at lvl1
    assert all(r[1]["_partition_ts"] == 12918291.1212 for r in non_root if r[0][0] == "table__lvl1")
    # _partition_ts == partition_ovr and __vx only at lvl2
    assert all(r[1]["_partition_ts"] == 1283.12 and r[1]["__vx"] == "ax" for r in non_root if r[0][0] == "table__lvl1__lvl2")
    assert any(r[1]["_partition_ts"] == 1283.12 and r[1]["__vx"] == "ax" for r in non_root if r[0][0] != "table__lvl1__lvl2") is False


def test_removes_normalized_list(schema: Schema) -> None:
    # after normalizing the list that got unpacked into child table must be deleted
    row = {"comp": [{"_timestamp": "a"}]}
    # get iterator
    normalized_rows_i = _normalize_row(schema, row, {}, "table")
    # yield just one item
    root_row = next(normalized_rows_i)
    # root_row = next(r for r in normalized_rows if r[0][1] is None)
    assert "comp" not in root_row[1]


def test_preserves_complex_types_list(schema: Schema) -> None:
    # the exception to test_removes_normalized_list
    # complex types should be left as they are
    # add table with complex column
    schema.update_schema(new_table("event_slot",
        columns = [{
                "name": "value",
                "data_type": "complex",
                "nullable": "true"
            }])
    )
    row = {
        "value": ["from", {"complex": True}]
    }
    normalized_rows = list(_normalize_row(schema, row, {}, "event_slot"))
    # make sure only 1 row is emitted, the list is not unpacked
    assert len(normalized_rows) == 1
    # value is kept in root row -> market as complex
    root_row = next(r for r in normalized_rows if r[0][1] is None)
    assert root_row[1]["value"] == row["value"]


def test_extract_with_table_name_meta() -> None:
    row = {
        "id": "817949077341208606",
        "type": 4,
        "name": "Moderation",
        "position": 0,
        "flags": 0,
        "parent_id": None,
        "guild_id": "815421435900198962",
        "permission_overwrites": []
    }
    # force table name
    rows = list(
        normalize(create_schema_with_name("discord"), with_table_name(row, "channel"), "load_id")
    )
    # table is channel
    assert rows[0][0][0] == "channel"
    normalized_row = rows[0][1]
    # _dlt_meta must be removed must be removed
    assert DLT_METADATA_FIELD not in normalized_row
    assert normalized_row["guild_id"] == "815421435900198962"
    assert "_dlt_id" in normalized_row
    assert normalized_row["_dlt_load_id"] == "load_id"


def test_table_name_meta_normalized() -> None:
    row = {
        "id": "817949077341208606",
    }
    # force table name
    rows = list(
        normalize(create_schema_with_name("discord"), with_table_name(row, "channelSURFING"), "load_id")
    )
    # table is channel
    assert rows[0][0][0] == "channel_surfing"


def test_parse_with_primary_key() -> None:
    schema = create_schema_with_name("discord")
    schema.merge_hints({"primary_key": ["id"]})
    schema._compile_regexes()
    add_dlt_root_id_propagation(schema)

    row = {
        "id": "817949077341208606",
        "w_id":[{
            "id": 9128918293891111,
            "wo_id": [1, 2, 3]
            }]
    }
    rows = list(normalize(schema, row, "load_id"))
    # get root
    root = next(t[1] for t in rows if t[0][0] == "discord")
    assert root["_dlt_id"] == digest128("817949077341208606")
    assert "_dlt_parent_id" not in root
    assert root["_dlt_load_id"] == "load_id"

    el_w_id = next(t[1] for t in rows if t[0][0] == "discord__w_id")
    # this also has primary key
    assert el_w_id["_dlt_id"] == digest128("9128918293891111")
    assert "_dlt_root_id" not in el_w_id
    assert "_dlt_parent_id" not in el_w_id
    assert "_dlt_list_idx" not in el_w_id

    # this must have deterministic child key
    f_wo_id = next(t[1] for t in rows if t[0][0] == "discord__w_id__wo_id" and t[1]["_dlt_list_idx"] == 2)
    assert f_wo_id["value"] == 3
    assert f_wo_id["_dlt_root_id"] == digest128("817949077341208606")
    assert f_wo_id["_dlt_parent_id"] == digest128("9128918293891111")
    assert f_wo_id["_dlt_id"] == _get_child_row_hash(digest128("9128918293891111"), "discord__w_id__wo_id", 2)


def test_keeps_none_values() -> None:
    row = {"a": None, "timestamp": 7}
    rows = list(normalize(create_schema_with_name("other"), row, "1762162.1212"))
    table_name = rows[0][0][0]
    assert table_name == "other"
    normalized_row = rows[0][1]
    assert normalized_row["a"] is None
    assert normalized_row["_dlt_load_id"] == "1762162.1212"


def add_dlt_root_id_propagation(schema: Schema) -> None:
    schema._normalizers_config["json"] = {
        "config": {
            "propagation": {
                "root": {
                    "_dlt_id": "_dlt_root_id"
                },
                "tables": {}
            }
        }
    }
