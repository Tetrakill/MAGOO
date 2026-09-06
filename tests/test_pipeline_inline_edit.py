"""Pipelines page inline editing of runs/BPC, ME and TE (2026-09-01):
editable whenever invention is off, read-only (badged) while the values
are materialized from an invention choice."""

from test_invention import _state, decryptor_named


def _pipeline(c, name):
    return c.execute(
        "SELECT * FROM pipeline WHERE name = ?", (name,)
    ).fetchone()


def _pin(c, blueprint_id):
    return c.execute(
        "SELECT me_level, te_level FROM blueprint_setting "
        "WHERE blueprint_id = ?",
        (blueprint_id,),
    ).fetchone()


def test_inputs_render_only_while_invention_is_off(seeded_client, ref):
    c = _state()
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert 'aria-label="Runs per BPC — Hulk"' in page
    assert 'aria-label="ME — Hulk"' in page
    assert 'aria-label="TE — Hulk"' in page
    pid = _pipeline(c, "Hulk")["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "none"}
    )
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert 'aria-label="Runs per BPC — Hulk"' not in page
    assert page.count('title="derived from the invention choice">inv') >= 3
    c.close()


def test_runs_per_bpc_inline_edit(seeded_client, ref):
    c = _state()
    pid = _pipeline(c, "Hulk")["pipeline_id"]
    assert seeded_client.post(
        f"/pipelines/{pid}/runs_per_bpc", data={"runs_per_bpc": "3"}
    ).status_code == 302
    assert _pipeline(c, "Hulk")["runs_per_bpc"] == 3
    # Blank = uncapped.
    seeded_client.post(f"/pipelines/{pid}/runs_per_bpc", data={"runs_per_bpc": ""})
    assert _pipeline(c, "Hulk")["runs_per_bpc"] is None
    # Refusals save nothing.
    for bad in ("0", "abc"):
        resp = seeded_client.post(
            f"/pipelines/{pid}/runs_per_bpc", data={"runs_per_bpc": bad}
        )
        assert resp.status_code == 422
    assert _pipeline(c, "Hulk")["runs_per_bpc"] is None
    c.close()


def test_me_te_inline_edit_keeps_the_other_level(seeded_client, ref):
    c = _state()
    pid = _pipeline(c, "Hulk")["pipeline_id"]
    bp = ref.blueprint_for_product(ref.type_id("Hulk")).blueprint_id
    assert _pin(c, bp) is None  # seeded without a paste pin
    # A ship with no pin starts from 0/0: posting TE alone keeps ME 0.
    assert seeded_client.post(
        f"/pipelines/{pid}/me_te", data={"te": "4"}
    ).status_code == 302
    assert tuple(_pin(c, bp)) == (0, 4)
    seeded_client.post(f"/pipelines/{pid}/me_te", data={"me": "7"})
    assert tuple(_pin(c, bp)) == (7, 4)
    # Clamps match the paste contract.
    assert seeded_client.post(
        f"/pipelines/{pid}/me_te", data={"me": "11"}
    ).status_code == 422
    assert seeded_client.post(
        f"/pipelines/{pid}/me_te", data={"te": "21"}
    ).status_code == 422
    assert seeded_client.post(
        f"/pipelines/{pid}/me_te", data={"te": "x"}
    ).status_code == 422
    assert seeded_client.post(
        f"/pipelines/{pid}/me_te", data={}
    ).status_code == 422
    assert tuple(_pin(c, bp)) == (7, 4)
    # The page shows the edited values.
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert 'aria-label="ME — Hulk"' in page and 'value="7"' in page
    c.close()


def test_inline_edits_refused_while_invention_on(seeded_client, ref):
    c = _state()
    pid = _pipeline(c, "Hulk")["pipeline_id"]
    seeded_client.post(f"/pipelines/{pid}/runs_per_bpc", data={"runs_per_bpc": "5"})
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": str(accelerant.type_id)}
    )
    materialized = _pipeline(c, "Hulk")["runs_per_bpc"]
    assert materialized == 2  # 1 + Accelerant's +1
    resp = seeded_client.post(
        f"/pipelines/{pid}/runs_per_bpc", data={"runs_per_bpc": "9"}
    )
    assert resp.status_code == 422 and b"invention" in resp.data
    resp = seeded_client.post(f"/pipelines/{pid}/me_te", data={"me": "1"})
    assert resp.status_code == 422
    row = _pipeline(c, "Hulk")
    assert row["runs_per_bpc"] == materialized
    assert row["manual_runs_per_bpc"] == 5  # the stash survives untouched
    c.close()


def test_paste_help_explains_the_columns(seeded_client, ref):
    """The paste help (2026-09-05): a column legend and the invention
    shortcut replace the old one-sentence description."""
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert "product and quantity are all you\n    need" in page
    for heading in ("<th>Column</th>", "<th>Meaning</th>", "<th>If blank</th>"):
        assert heading in page
    for column in ("Product", "Quantity per run", "Runs per BPC", "ME / TE"):
        assert f"<b>{column}</b>" in page
    assert "no cap (a BPO, or plenty of copies)" in page
    assert "only its quantity changes" in page


# --- paste parser: blank interior columns (review 2026-09-05) ----------------


def _pasted(client, c, ref, products):
    """POST one pasted sheet and return the Ishtar pipeline's
    (runs_per_bpc, me_level, te_level) — the row plus its blueprint pin."""
    resp = client.post("/pipelines", data={"products": products})
    assert resp.status_code == 302, resp.status_code
    row = _pipeline(c, "Ishtar")
    assert row is not None, "the paste did not add the Ishtar pipeline"
    blueprint = ref.blueprint_for_product(ref.type_id("Ishtar"))
    pin = _pin(c, blueprint.blueprint_id)
    return row["runs_per_bpc"], pin["me_level"], pin["te_level"]


def test_paste_blank_interior_column_is_an_omitted_value(seeded_client, ref):
    """An Excel row with an EMPTY runs/BPC cell pastes as
    "Ishtar\t40\t\t4\t8". The parser used to drop every empty field, so
    ME 4 slid into runs/BPC and TE 8 into ME (and TE fell back to the ship
    default). Interior blanks are omitted columns; only trailing blanks are
    dropped."""
    c = _state()
    assert _pasted(seeded_client, c, ref, "Ishtar\t40\t\t4\t8") == (None, 4, 8)
    # the comma form behaves identically
    assert _pasted(seeded_client, c, ref, "Ishtar,40,,4,8") == (None, 4, 8)
    # a trailing blank (Excel appends one for an empty last column) is
    # still just an omitted column: ME/TE fall back to the ship default 0/0
    assert _pasted(seeded_client, c, ref, "Ishtar\t40\t10\t\t") == (10, 0, 0)
    # the short forms are unchanged
    assert _pasted(seeded_client, c, ref, "Ishtar\t40\t10") == (10, 0, 0)
    assert _pasted(seeded_client, c, ref, "Ishtar 40 10 3 6") == (10, 3, 6)
    # a blank QUANTITY is still a parse error, not a silent shift
    resp = seeded_client.post(
        "/pipelines", data={"products": "Ishtar\t\t10\t4\t8"},
        follow_redirects=True,
    )
    assert "can&#39;t parse" in resp.get_data(as_text=True) or \
        "can't parse" in resp.get_data(as_text=True)
    c.close()
