"""Repair legacy channel-based material types using retained document evidence."""

import json

from reportagent.services.document_metadata import infer_material_type, metadata_dict


def migrate_material_types(conn):
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(reports)")}
    names = (
        "id",
        "title",
        "abstract",
        "full_text",
        "arxiv_id",
        "material_type",
        "material_type_info_json",
    )
    fields = [
        f"substr({name},1,8000) AS {name}"
        if name in {"full_text", "abstract"} and name in columns
        else name
        if name in columns
        else f"NULL AS {name}"
        for name in names
    ]
    rows = (
        conn.exec_driver_sql("SELECT " + ",".join(fields) + " FROM reports")
        .mappings()
        .all()
    )
    for row in rows:
        info = metadata_dict(row["material_type_info_json"])
        if (
            row["material_type"] not in {None, "", "unknown"}
            and info.get("source") != "typed_feed"
        ):
            continue
        kind, evidence = infer_material_type(dict(row))
        if kind == row["material_type"] and evidence == info:
            continue
        conn.exec_driver_sql(
            "UPDATE reports SET material_type=?,material_type_info_json=? WHERE id=?",
            (kind, json.dumps(evidence, ensure_ascii=False), row["id"]),
        )
