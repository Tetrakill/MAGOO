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
