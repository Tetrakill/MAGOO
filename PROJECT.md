# Project Magoo — Pipeline Production Planner

**Standalone industry planning application for EVE Online**

Stack: Python · Flask · SQLite · SciPy · Jinja2
Status: v1.23 built and live (engine, MILP allocation, sizing feedback
loop iterated to convergence, alchemy — landed route comparison,
lag-based costing, capital + structure pricing, Upwell structures /
rigs / components in scope, two-venue buying (Jita vs C-J6 landed) with
landed-price savings, Planning tab (today's-prices Profit + steady-state
Slot Planner), ESI tab with per-corp/-character count toggles, runs
lifecycle with superseded/discard, per-item deficit ledger, first-run
onboarding with a crash-free fresh install and a one-click game-data
download with live progress, full web UI restyled, accessibility- and
audit-hardened; packaged as a Windows desktop application — installer +
portable zip, native WebView2 window, shared-client-id PKCE login in the
user's own browser, versioned schema with pre-migration backups)
Last updated: 2026-08-30

---

## 1. Purpose

Existing EVE industry tools answer *"is this item profitable to build?"* — a
single-item, point-in-time calculation. Magoo answers a different question:
**"what do I buy and which jobs do I start, right now, to keep my production
line running?"**

The user operates a pipelined production system:

- Stockpiles are maintained at *every* stage of a build chain — raw materials,
  each tier of intermediate components, and finished products.
- On each **index run**, every stage is worked simultaneously: raw materials
  are bought, stage-1 intermediates are built from existing raw stock, stage-2
  intermediates from existing stage-1 stock, and so on to the final product.
- Each stage advances one step per run, offset by one from the stage ahead.
- Once primed, every index run yields a batch of finished product *and* refills
  every stage behind it.

Magoo plans each cycle and tracks what the resulting products actually cost.

### Why standalone

The feature was originally scoped as an addition to EVE Isk per Hour (EVE-IPH),
a VB.NET/WinForms application. That was abandoned after measuring the codebase:
116,000 lines across 166 files, of which 32,000 lines are WinForms designer
code across 46 forms, with `frmMain.vb` alone carrying 21,900 lines of event
handlers entangled with business logic. WinForms is Windows-only and cannot be
built or run on Linux, making iteration impossible in this environment, and any
substantial addition would permanently diverge from upstream.

Building standalone in Python removes all of that: the application runs
anywhere, iterates quickly, and owns its own data pipeline.

---

## 2. Feature Specification

1. **Pipeline definition** — Define one or more final products (typically
   ships). The full build chain is derived automatically from blueprint data,
   never hand-entered.

2. **Unified / shared tracking** — Pipelines are not isolated. Where two
   pipelines share an intermediate or raw material, demand is merged into a
   single combined target and deficit. Manufacturing and reaction job slots are
   pooled across all active pipelines.

3. **Derived stockpile targets** — Targets are not set per item. The pipeline
   determines the minimum required at each stage; the user supplies a single
   **global percentage buffer**:

   `target = merged_min_required × (1 + buffer%)`

   The buffer applies to intermediates (raw materials' buffered targets are
   superseded in Phase 7 by just-in-time purchase sizing) — **final
   products of active pipelines target their exact per-run quantity**
   (revised 2026-08-15: the buffer protects the feeder stages, not the
   finished output). Final products also **ignore current stock and
   in-flight jobs**: the line advances every cycle, always building the
   requested quantities — finished and in-progress ships are the previous
   wave, bound for sale. Stock and in-progress output still fully count for
   every intermediate and raw material.

4. **ESI-based stock tracking, scoped by system** — Current stock is read from
   ESI assets. A global list of tracked solar systems constrains which assets
   count.

5. **Index run output** — Each run produces a concrete action list: what to
   buy, and which jobs to install (blueprint, run count, job count), sized to
   close the gap between current stock and target.

6. **Job slot constraints** — Plans respect real capacity: the user-entered
   manufacturing and reaction slot pools, minus MULTI-CYCLE jobs still running
   past the next index run (revised 2026-08-20; single-cycle jobs deliver
   before planning by design).

7. **Max run duration** — The user sets the interval until the next index run.
   Because all jobs are installed at once and cannot be restarted mid-cycle,
   this determines how many blueprint runs fit into a single job, and therefore
   how many parallel slots an item requires.

8. **Low stock alerts** — Any buildable stage whose projected post-run stock
   (net of this cycle's planned consumption) would fall short of sustaining
   the *next* run is flagged. Suppressed until the pipeline's first executed
   run (every stage legitimately reads low while priming), and never raised
   for just-in-time raws. The consumption feedback loop (§7) re-sizes
   suppliers to cover the actual planned draw at every tier until the
   deficits converge (2026-08-28), so once the loop converges a surviving
   flag is a genuinely uncoverable shortfall; only the capped
   no-fixed-point case under slot contention (§7) can still leave the
   single-pass era's sizing residue behind.

9. **Profit tracking with vintage costing** — Realized cost is lag-based
   (v1.5): each input of a hull delivered at executed run N is priced from
   the price/fee snapshot of the run its chain depth lags behind, clamped
   during spin-up. A finished product's profit reflects what
   its materials actually cost several runs ago, not today's prices. (FIFO
   lot genealogy was the original design; its machinery remains as dormant
   Phase-8 primitives.)

10. **Alchemy (v1.4)** — When reaction slots are left over, cheaper Unrefined
    reaction routes substitute for direct composite reactions: run the
    unrefined formula, manually reprocess its output into the composite plus
    recovered inputs. Route selection is by unit-cost comparison; a per-type
    job cap throttles it; a contended reaction pool disables it (§7 Phase 6.5).

11. **Production blacklist (v1.3)** — Per-category checkboxes plus a per-item
    "buy, don't build" list; blacklisted sub-chains are pruned at expansion.
    Never applies to a pipeline's final product.

12. **Buy-vs-build economics** — An intermediate whose vertically-integrated
    chain cost exceeds its market price is bought instead of built, contended
    or not (2026-08-20/21). Pipeline finals NEVER flip to buy (2026-08-21):
    they pre-allocate their slots ahead of the MILP, their negative paper
    margin surfaces as a badge, and any shortfall stays a flagged unmet
    build — the plan never tells the user to market-buy their own product.

13. **Capital pricing (v1.6)** — Capital-class hulls (capitals, freighters,
    JFs, Orca) are sell-quoted from a structure market (C-J6MT preset or
    custom) with their own fee pair and a fixed per-hull movement cost.

14. **Structures scope (v1.9)** — Upwell structures (category 65), Standup
    rigs / service / weapon modules (category 66) and Structure Components
    (group 536) are first-class pipeline products and chain items: their own
    item class `structures` (CCP's "Structures" rig filter), an Outpost
    Construction skill level, a dedicated Structure Components section on
    the run tabs (built AND bought rows), sub-capital sell pricing with the
    800,000 m³ XL hulls freight-exempt, a region-wide price fallback for raw
    leaves with no hub quote (NPC-seeded goods), and an ESI stock toggle
    that leaves fitted/installed items and anchored structures out.

15. **Two-venue buying (v1.10)** — Every bought input is priced at whichever
    of the Jita hub and the structure market (C-J6MT, the same market that
    sell-quotes capitals) is cheaper **landed** — order price plus that
    venue's flat freight-in on packaged volume. The structure's best price
    is used when it wins, and its sell ladder is judged for depth: a buy is
    flagged *shallow* when fewer units land at or below the Jita landed
    price than the plan buys. Venue is stamped per item, the Buy list shows
    it, Multibuy exports one block per market, freight splits per venue in
    both profit views. Finals are never compared (their quote is a sell
    reference).

16. **T2/T3 invention (v1.22)** — Per pipeline whose final is
    invention-capable (its manufacturing blueprint has at least one
    `ref_invention` source), the user
    picks a decryptor, "no decryptor", or off (a plain dropdown — the
    inline nine-option comparison was removed 2026-08-31 as clutter; the
    chosen option's economics show on the save flash and both profit
    views; the run pages carry no invention information — user ruling
    2026-09-01). Multi-source finals — T3 subsystems/hulls invented
    from relic tiers (Intact/Malfunctioning/Wrecked), and the seven T2
    targets with several T1 sources — add a **source select** beside the
    decryptor; relics are consumed one per attempt (bought like datacores,
    no copy job). Note the hull-batch consequence: an Intact-relic ship
    final builds in whole 20-hull batches (runs-per-BPC is the ship batch
    unit). The
    choice **overrides** the pipeline's runs-per-BPC and the blueprint's
    ME/TE (materialized at config time; §5 Invention, §7 Invention pass).
    Each run persists its invention cost VINTAGE and amortizes the
    computed cost per hull on both profit views (`kind='invention'`
    lines; the manual BPC-cost figure stays for bought copies) — while
    all production and purchasing lives on the **Invention tab** (v1.23):
    a live BPC stockpile workbench that targets
    `ceil(one cycle's copies × the T1/T2 BPC Stockpile Overbuild
    settings)` (default 400%, range 100–1000%), netted against BPCs on
    hand (ESI-counted; T1 copies = stack minus the BPO) and in-flight
    lab jobs, and lists the datacores/decryptors/relics to buy (with
    Multibuy) plus the copy jobs to install — recomputed from current
    stock on every load, never persisted. Lab jobs consume no slot pool.

---

## 3. Architecture

```
magoo/
  data/
    sde/                  downloaded SDE archives (cache)
    magoo.sqlite          all application state + imported reference data
  magoo/
    config.py             verified industry constants, paths, activity IDs
    sdeimport.py          CCP JSONL SDE download + import into reference tables
    refdata.py            read layer over imported reference data
    industry.py           ME/TE/facility/rig math, job cost
    bom.py                multi-stage BOM expansion (Phase 2)
    engine.py             index run planning (Phases 2-7 + consumption
                          feedback loop; Phase 8 FIFO primitives dormant;
                          Phase 1 snapshots arrive via esi.py/market.py)
    costing.py            lag-based per-hull realized cost + sell-side
                          fee model (v1.5/v1.6) + the buy-venue chooser
                          (v1.10, pure)
    esi.py                OAuth2 PKCE; corp+char assets, industry jobs,
                          wallets; structure resolution + market orders
    market.py             price snapshots: regional hub quotes, the
                          structure market's best prices + sell ladders,
                          and the per-type buy quote (v1.10)
    store.py              schema creation and state persistence
    web.py                Flask routes
    templates/            Jinja2 views
  run.py                  entry point
```

**Single SQLite database.** Reference data imported from the SDE and the
application's own state live in one file. Reference tables are rebuilt wholesale
on SDE update; state tables are never touched by import.

**Layering.** `industry.py` and `bom.py` are pure math over an injected
`Refdata` handle; `engine.py` additionally reads settings and state through
`store.py` helpers and persists runs itself with direct SQL against the
state tables (whose schema `store.py` owns). None of the three ever touch
SDE file formats or vendor table shapes — if the SDE format changes again,
only `sdeimport.py` changes.

**No ORM.** Direct SQL against `sqlite3`, matching the scale of the problem.

### Dependencies

All available in the target environment without installation:

| Purpose | Package |
|---|---|
| Web framework | Flask, Jinja2 |
| Slot allocation (MILP) | SciPy (`scipy.optimize.milp`), NumPy |
| HTTP / ESI | httpx |
| Token validation | PyJWT |
| Data handling | stdlib `sqlite3`, `json`, `zipfile` |

---

## 4. Reference Data

### Source

CCP's official Static Data Export, JSON Lines format, pulled directly from
`developers.eveonline.com`. No third-party redistributor.

### Auto-pull protocol

Documented at `developers.eveonline.com/docs/services/static-data/`:

1. Fetch `https://developers.eveonline.com/static-data/tranquility/latest.jsonl`.
   The record keyed `"sde"` holds the current build number; the `"_meta"` record
   carries `lastBuildNumber` for change detection.
2. Compare against the stored build number. If unchanged, skip.
3. Download
   `https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-<build>-jsonl.zip`
   (shorthand `.../static-data/eve-online-static-data-latest-jsonl.zip` redirects
   to latest).
4. Extract and import; record the build number. The drop-rebuild-import runs
   as ONE `BEGIN IMMEDIATE` transaction (2026-08-21): a failure anywhere rolls
   back to the previous working build, and a concurrently running app keeps
   reading the old build via WAL until the single commit.

**JSONL encoding note:** JSON keys must be strings, so integer-keyed records are
encoded with `_key` and `_value` fields. The importer must handle this.

### Datasets consumed

`blueprints`, `types`, `groups`, `categories`, `dogmaAttributes`, `typeDogma`,
`mapSolarSystems`, `industryModifierSources`, `industryTargetFilters` (the
last two added 2026-08-15 — see Rig applicability in §5), `typeMaterials`
(added 2026-08-17 for alchemy — reprocessing outputs; records carrying only
`randomizedMaterials`, i.e. mineral alchemy with min/max ranges, are skipped
at import).

Archive members are matched by **exact basename**: the archive also carries
`shipTreeGroups.jsonl`, `marketGroups.jsonl`, `metaGroups.jsonl`, … which a
suffix match would wrongly hit.

### Reference schema (imported)

| Table | Contents |
|---|---|
| `ref_type` | type_id, name, group_id, category_id, volume, packaged_volume, published |
| `ref_group` / `ref_category` | names and hierarchy |
| `ref_blueprint` | blueprint_id, product_id, activity_id, portion_size, base_time, max_runs |
| `ref_blueprint_material` | blueprint_id, activity_id, material_id, quantity, consumed |
| `ref_type_attribute` | type_id, attribute_id, attribute_name, value |
| `ref_blueprint_skill` | blueprint_id, activity_id, skill_type_id, level — skill prerequisites (job-time math and invention chance, never demand) |
| `ref_invention` | v1.22: blueprint_id (T1 source), product_blueprint_id (invented blueprint TYPE), probability, runs (per invented copy), time — one row per invention edge; datacores/skills live in the two tables above at activity_id 8 |
| `ref_dogma_attribute` | attribute_id, name, default_value — dogma attribute definitions |
| `ref_industry_modifier` | source_type_id, activity_id, kind, dogma_attribute_id, filter_id |
| `ref_industry_target_filter` | filter_id, name, kind (category/group), ref_id |
| `ref_type_material` | type_id, material_id, quantity — reprocessing outputs (alchemy) |
| `ref_solar_system` | system_id, name, security, region_id |
| `ref_sde_build` | build number and import timestamp |

### Blueprint structure in the SDE

Each blueprint record carries `blueprintTypeID`, `maxProductionLimit`, and an
`activities` object with `manufacturing`, `reaction`, `copying`, `invention`,
`research_material`, `research_time`. Each activity has `time`, `materials[]`,
`products[]`, `skills[]`. Product `quantity` is the portion size.

Magoo imports `manufacturing` (activity 1) and `reaction` (activity 11) into
`ref_blueprint`, and (v1.22) `invention` (activity 8) into its own
`ref_invention` table — invention products are blueprint TYPES and one source
can carry several (T3 relics), so merging them into `ref_blueprint` would
poison `blueprint_for_product`'s `MIN(activity_id)` pick. Invention rows with
no probability or an unpublished product are skipped (only unpublished
sources carry them in live data). Copying imports no blueprint rows (it has
only `time`), but both lab activities' structure/rig bonuses land in
`ref_industry_modifier` via `SDE_MODIFIER_ACTIVITY_IDS`, feeding the
invention/copy job-fee math through the ordinary `structure_multiplier`
machinery (Raitaru cost ×0.97). Research activities stay out of scope.

### Three import rules that matter

- **Unpublished blueprints are excluded.** The SDE ships internal junk —
  notably "Test Reaction Blueprint" (45732, published: false), which
  produces Tungsten Carbide at portion 20 instead of the real formula's
  10,000. Found live 2026-08-15: it was picked arbitrarily for TC and
  inflated Rolled Tungsten Alloy / Sulfuric Acid demand ~500× (262M-unit
  buy recommendations). Blueprints whose blueprint type has
  `published: false` are skipped at import (drops ~810 of 4,968); this
  also removed every same-activity duplicate blueprint.
- **Skills are not materials.** Skill requirements appear alongside real
  materials but are prerequisites. Everything in category 16 is excluded, or
  BOM expansion will try to stockpile "Industry" as if it were a component.
- **Non-consumed materials are not demand.** Materials flagged as surviving the
  job (some reaction inputs) must be on hand but are never drawn down, so they
  are excluded from deficit calculation.

---

## 5. Industry Formulas

All values below were read from game data (dogma attributes) rather than
recalled, and verified against hand-checked worked examples.

### Material requirement

Rounding is applied **once per job**, not per run. A job always needs at least
`runs` units of each material.

```
required = max(runs, ceil(round(runs × base_qty × me_mult × struct_mult × rig_mult, 2)))

me_mult     = 1 − ME/100
struct_mult = 0.99 for engineering complexes (manufacturing only), else 1.0
rig_mult    = 1 − (rig_material_bonus × security_multiplier)/100
```

### Job time

```
seconds = base_time × runs × (1 − TE/100) × struct_time_mult × rig_time_mult
          × skill_time_mult

skill_time_mult = Industry −4%/lvl × Advanced Industry −3%/lvl × each required
science/construction skill's manufactureTimePerLevel (−1%/lvl); reactions use
Reactions −4%/lvl instead (user-entered levels, v1.1).
```

### Verified constants

| Value | Source | Number |
|---|---|---|
| T1 engineering rig ME | `attributeEngRigMatBonus` | −2.0% |
| T2 engineering rig ME | `attributeEngRigMatBonus` | −2.4% |
| Engineering rig security mult, highsec | `hiSecModifier` | 1.0 |
| Engineering rig security mult, lowsec | `lowSecModifier` | 1.9 |
| Engineering rig security mult, nullsec/WH | `nullSecModifier` | 2.1 |
| Reaction rig security mult, lowsec | `lowSecModifier` (refinery rigs) | 1.0 |
| Reaction rig security mult, nullsec/WH | `nullSecModifier` (refinery rigs) | 1.1 — no highsec band; refineries cannot deploy there (corrected 2026-08-20) |
| Per-job run ceiling (both activities) | user-verified in client 2026-08-21 | ceil(30 days of modified time / time per run) — last run may overhang; single-run exception applies |
| Thukker rig ME, capital components (873/913) | `attributeThukkerEngRigMatBonus` | −3.7% |
| Thukker rig ME, other components (334/964) | `attributeEngRigMatBonus` | −2.0% |
| Thukker rig TE | `attributeEngRigTimeBonus` | −20% |
| Thukker rig security mult | `hiSecModifier`/`lowSecModifier`/`nullSecModifier` (Thukker rigs) | 0.1 highsec / 1.9 lowsec / 0.1 null-WH (verified live SDE 2026-08-21) |
| Raitaru time multiplier | `strEngTimeBonus` | 0.85 |
| Azbel time multiplier | `strEngTimeBonus` | 0.80 |
| Sotiyo time multiplier | `strEngTimeBonus` | 0.70 |
| Tatara reaction time multiplier | `strReactionTimeMultiplier` | 0.75 |
| Athanor reaction time | (no attribute) | 1.0 |
| Engineering complex ME role bonus | EVE University wiki | 1% |
| SCC surcharge (job cost) | EVE University wiki; NOT in the SDE or ESI — a settings value since 2026-08-21 (`industry_scc_surcharge`, default 0.04) pending in-client verification | 4% |
| NPC station facility tax | EVE University wiki | 0.25% |

### Rig applicability

> **Corrected 2026-08-15 against the live SDE.** The original design derived
> applicability from `canFitShipGroup01…08` matched against the product's
> group. Inspection of real data shows those attributes name the *structures
> the rig can be fitted to* (Citadel 1657, Engineering Complex 1404, Refinery
> 1406) — matching product groups against them would mean no rig ever applies.

**At planning time the app performs no rig-applicability filtering at all**:
the user asserts one rig tier per item class (2026-08-15 design), so the rig
bonus applied is the asserted tier's percentage × the rig family's security
band. `industryModifierSources` feeds STRUCTURE bonuses only (unfiltered
entries; filtered entries are ignored).

As game-data background — and how the constants were originally verified —
applicability lives in CCP data, no heuristics: `industryModifierSources.jsonl`
maps each source type (structure or rig) to the dogma attributes carrying its
bonuses per activity and kind (material/time/cost), each optionally restricted
by a `filterID` into `industryTargetFilters.jsonl` — named sets of
category/group IDs. A rig applies to a product iff the product's group or
category is in the filter. Verified: the L-Set Basic Large Ship ME rig's
material bonus carries filter 9 "Large T1 Ships" {27, 513, 941}; the Hulk's
group (Exhumer 543) is in filter 8 "Medium T2 Ships", so it would not apply —
asserting the right tier per class is the user's responsibility.

Bonus magnitudes:

- **Structures** carry resolved multipliers directly on the type
  (`strEngMatBonus` 0.99, `strEngTimeBonus` 0.85 on a Raitaru, etc.) —
  read from data, not hardcoded.
- **Rigs** reference per-family multiplier attributes (e.g. 2548) that have
  no static value — the client computes them at fit time. Magnitude therefore
  still comes from `attributeEngRigMatBonus` / `attributeEngRigTimeBonus`
  (percentages) × the rig's own security-band modifier attributes, per the
  formula above.

**Only the single best rig of each kind applies** — bonuses do not stack; the
game enforces one rig per group via `maxGroupFitted = 1`.

### Job installation cost

```
cost = EIV × ((system_cost_index × structure_cost_mult) + facility_tax + SCC_surcharge)
```

EIV uses **base** (pre-ME) quantities against CCP adjusted prices.
`structure_cost_mult` is the structure's cost bonus read from data (Raitaru
0.97 / Azbel 0.96 / Sotiyo 0.95; refineries 1.0); rig cost bonuses are not
modeled for manufacturing/reactions (`attributeEngRigCostBonus` is 0 on
those rig families) — the LAB rig families do carry one and are modeled
for the invention/copying fees (−10%/−12% on the engineering security
bands, see §5 Invention). The SCC surcharge is the
`industry_scc_surcharge` setting (default 0.04).

### Invention (v1.22)

```
P(success) = base_probability × (1 + (sci1 + sci2)/30 + encryption/40) × decryptor_prob_mult
```

clamped at 1.0. `base_probability` and the base runs per invented copy come
from `ref_invention`; the required skills from `ref_blueprint_skill` at
activity 8 — the "… Encryption Methods" skill supplies the /40 term (its own
`skill_encryption` setting), the two datacore sciences the /30 terms, each
resolved through the same `_per_bp_skill_level` name families the time math
uses (racial Starship Engineering / Tech 2 Science levels).

The invented copy is **ME 2 / TE 4** with `base_runs` licensed runs, plus
the chosen decryptor's modifiers (dogma attrs 1112
`inventionPropabilityMultiplier` — CCP's typo, verbatim — 1113 ME, 1114 TE,
1124 runs; the 8 generic decryptors are group 1304).

Per attempt: the activity-8 datacores, one decryptor (if chosen), one 1-run
T1 copy (BPO on hand — only the copy job fee is charged), and the invention
job fee.

**Relic sources (T3, 2026-08-31).** The 56 relic-invented targets (48
subsystems, 4 strategic cruisers, 4 tactical destroyers) each offer three
sources whose tier is uniform across every family — Intact 0.26 / 20 runs,
Malfunctioning 0.21 / 10, Wrecked 0.14 / 3 (time 3600s) — with identical
datacores and skills per family (Sleeper Encryption Methods rides
`skill_encryption`; decryptors apply unrestricted). A relic is an ITEM
consumed one per attempt: it is priced landed and bought like a datacore,
there is **no copy job** (copy fee 0), and the invention fee base is **2%
of the INVENTED blueprint's product manufacturing EIV** (a relic has no
manufacturing activity of its own; user decision 2026-08-31, pending
in-client verification). Relics are detected by carrying no
`ref_blueprint` rows at all. The seven multi-T1-source T2 targets use the
same source picker with the ordinary T1 fee/copy math — the choice is
which BPO you own. Each lab fee reads its own class row — `invention` and `copying`
(split 2026-08-31: copying has its own per-system cost index in game; on an
existing database the copying row seeds from the invention row it used to
share) — and the standard fee formula with the base scaled to **2% of the
T1 blueprint's manufacturing EIV** (`JOB_FEE_EIV_FRACTION`, pending
in-client verification like the SCC surcharge), each with its own
activity's structure cost bonus and its class's asserted **cost-rig tier**
(2026-08-31): every Standup Invention / Blueprint Copy / Laboratory
Optimization rig, M/L/XL alike, carries `attributeEngRigCostBonus` −10%
(T1) / −12% (T2) on the engineering security bands 1.0/1.9/2.1 (verified
live SDE) — `LAB_RIG_COST_PERCENT`, stored in the lab class's `me_rig`
column. Lab job TIME bonuses stay unmodeled.

Cost (engine `_invention_pass` / `costing.invention_cost`):

```
cost_per_licensed_run = attempt_cost / (P × runs_per_copy)
```

Sizing (v1.23, the live **Invention tab** — `engine.invention_stockpile`,
never persisted, recomputed from current stock on every load):

```
cycle_copies   = ceil(ceil(output_qty_per_run / portion) / runs_per_copy)
T2 target      = ceil(cycle_copies × t2_bpc_overbuild)        -- setting, default 400%
to_invent      = target − BPCs on hand − floor(in-flight attempts × P)
attempts       = ceil(to_invent / P)
T1 target runs = ceil(attempts × t1_bpc_overbuild)           -- one run per attempt
runs_to_make   = T1 target − (stack − 1 on hand) × max_runs − licensed runs copying
copy jobs      = ceil(runs_to_make / max_runs)               -- copies at maxProductionLimit
invention jobs = ceil(attempts / max_runs)                   -- one attempt per run of a max-run copy
```

BPC stock is ESI-counted like any other material (blueprint types flow
into `on_hand` untouched); each stocked copy counts as one copy at the
CONFIGURED runs_per_copy, in-flight invention attempts convert at the
CONFIGURED chance, and the T1 BPO is assumed to sit with its copy stack
in a tracked system (stack-minus-one — user decisions 2026-09-01).
Pipelines sharing a source or invented blueprint draw from ONE shared
pool, never double-credited; datacores/decryptors/relics net once across
pipelines against stock + in-flight. The index run itself injects no buy
rows and sizes no production — `copies_needed`/`attempts` persist as
informational cycle figures, and the realized replay prices the
CONTINUOUS expectation `1/(P × runs_per_copy × portion)` from the
vintage row, so overbuild cycles never spike per-hull cost and
stock-covered cycles never dip it.

Datacore/decryptor prices in the invention cost are **landed** (venue raw +
that venue's flat ISK/m³ × packaged m³) — the invention lines carry their
own freight, and the freight-in aggregation only reads `material` lines, so
nothing double-counts. Missing prices count 0 and surface as `unpriced`
badges. Lab jobs contend for no slot pool (decision "T2 invention chain").

### Alchemy mechanics (v1.4)

Verified against the live SDE and CCP/forum sources (2026-08-17):

- 17 published "Unrefined X Reaction Formula" reaction blueprints produce
  1 unrefined unit per 21,600s run (vs 200 composite units per 10,800s run
  for the direct formula) from cheap moon goo — e.g. Unrefined Ferrofluid:
  5 Hydrogen Fuel Block + 100 Cadmium + 100 Hafnium, where direct
  Ferrofluid consumes 100 Hafnium + 100 **Dysprosium**.
- The unrefined item reprocesses into the composite plus recovered inputs;
  base outputs come from `typeMaterials` (e.g. Unrefined Ferrofluid → 73
  Ferrofluid + 173 Hafnium).
- Reprocessing follows **scrapmetal rules**: flat yield, max 55% (50%
  structure base × Scrap Metal Processing V); reprocessing rigs and ore
  bonuses do NOT apply. The yield is a user-asserted setting
  (`alchemy_reprocess_yield`, default 0.55) like the class settings.
- At 55% an alchemy slot therefore yields ~40 composite/run/6h vs 200/run/3h
  direct — roughly 10× less slot-efficient. Alchemy is a price play (it
  swaps the rare R64 input for cheap goo), never a throughput play, which is
  why it only ever runs in slots that would otherwise sit idle.
- The 8 mineral alchemy formulas (Unrefined Tritanium etc.) reprocess with
  randomized min/max outputs; their randomized reprocess records are dropped
  at import (`typeMaterials` entries carrying only `randomizedMaterials`), so
  no route ever forms — the formulas themselves import like any other
  reaction blueprint.

### Worked verification

Hulk, ME10 / TE20, in a Sotiyo in nullsec, with a Large Ship ME rig fitted:

- Construction Blocks: `150 × 0.90 × 0.99 = 133.65 → 134` ✓
- Build time before skills: `240,000s × 0.80 × 0.70 = 37.33h` ✓ — the
  planner then multiplies in skill_time_mult (default all-V: Industry 0.80 ×
  Adv Industry 0.85 × Laser Physics 0.95 × Gallente Starship Engineering
  0.95 = 0.6137 → 22.9h planned).
- No rig factor was applied because the governing class's asserted ME-rig
  tier was `none`. (Game background: a Large Ship rig would not cover an
  Exhumer anyway — which is why asserting tiers per class is the user's
  responsibility.)

---

## 6. Data Model

Reference tables are described in §4. Application state follows.

### Pipelines

**`pipeline`**

| Column | Notes |
|---|---|
| `pipeline_id` | PK |
| `name` | |
| `final_product_type_id` | |
| `output_qty_per_run` | Desired finished output per index run |
| `is_active` | Inactive pipelines excluded from planning (cost history stays attributable) |
| `runs_per_bpc` | v1.1: runs on the final's blueprint copy — caps runs/job and is the batch rounding unit; NULL = uncapped. v1.22: MATERIALIZED from the invention math while `use_invention` is on |
| `bpc_cost_isk` | v1.5: all-in ISK per BPC, amortized per hull as bpc_cost_isk ÷ runs_per_bpc. v1.22: ignored while `use_invention` is on (the computed invention cost replaces it; greyed out in the UI) |
| `invention_source_blueprint_id` | 2026-08-31: the chosen source for a multi-source final — a relic TYPE id (T3 tiers) or a T1 blueprint id (the seven multi-T1 targets). NULL = auto (single-source). An invalid/vanished choice behaves as a stale config (bpc fallback + the reduced Off control) |
| `use_invention`, `decryptor_type_id` | v1.22: the pipeline's invention choice (`decryptor_type_id` NULL = no decryptor). Enabling/changing it materializes the derived values at CONFIG time — runs into `runs_per_bpc`, invented ME/TE into the T2 blueprint's `blueprint_setting` — so the resolver chain and the planning path are untouched; disabling restores the stashed runs and the paste-contract ME/TE defaults. The paste updates only the quantity of an invention pipeline. A stale choice (SDE drift removes the source, or the final has several sources) silently falls back to `bpc_cost_isk`, and the Pipelines page keeps the Off control reachable for it |
| `manual_runs_per_bpc` | v1.22 review: the user's own runs_per_bpc, stashed on the OFF→ON toggle (kept across decryptor changes). The manual-BPC amortization fallback (`costing.bpc_divisor`) divides by THIS while `use_invention` is on, so toggling invention can never reprice pre-invention realized history; turning invention off restores it into `runs_per_bpc` |
| `created_at`, `modified_at` | |

**`index_run_invention`** (v1.22) — the invention economics persisted with
each planned run, one row per invention-enabled pipeline whose final had
runs allocated: the source (`t1_blueprint_id` — a T1 blueprint or, since
2026-08-31, a relic type id), decryptor, skill-applied probability,
invented ME/TE/runs-per-copy, copies_needed, attempts, the datacores JSON
(`[[type_id, qty_per_attempt, landed_price|null], …]` — carries the relic
consumable triple for relic sources), decryptor price,
both per-attempt fees (copy fee 0 for relics), and cost_per_run (schema 5
dropped the never-read `copies_needed`/`attempts` sizing figures). Nothing
on the run pages renders it (user ruling 2026-09-01: no invention
information inside Index Runs); it exists to give lag costing a stable
vintage — the realized `hull_cost` reads THIS row (suppressing the `bpc`
line), never the live pipeline config or today's prices. Written for
every resolving invention pipeline of the run, a slot-starved final
included. Deleted with its run or its pipeline.

> **No stored BOM.** The build chain is re-derived on every planning pass
> (§7 Phase 2). There is deliberately no stage table and no per-item build/buy
> override.

**`pool_character`** — who contributes to the shared pool.

| Column | Notes |
|---|---|
| `character_id` | PK |
| `character_name` | |
| `include_assets` | Count this character's wallet toward buying power (stock is corp-scope since 2026-08-20) |
| `include_job_slots` | Count this character's PERSONAL jobs toward slot occupancy / multi-cycle netting (2026-08-25: corp-feed jobs count under the corp's `count_jobs` toggle instead — the corp feed runs first and claims corp jobs in the dedup) |
| `count_assets` | 2026-08-25: opt this character's PERSONAL hangars into stock on hand (default 0) |

**`esi_corp`** — corporations reachable through the pool (ESI tab); upserted
on every ESI refresh, `count_assets` is user state and survives; rows pruned
when every member has left the pool.

| Column | Notes |
|---|---|
| `corporation_id` | PK |
| `corporation_name` | From the public corp endpoint |
| `count_assets` | Default 1; 0 = this corp's hangars don't feed stock (the assets pull is skipped) |
| `count_wallet` | Default 1; 0 = this corp's ISK leaves the buying-power line (wallets pull skipped) |
| `count_jobs` | Default 1; on = every corp-feed job counts toward in-progress stock AND slot occupancy / multi-cycle netting (corp ESI carries installer + end date, so corp auth alone covers corp-hangar jobs); 0 = the pull is skipped (pool characters' own jobs still arrive via their character feed, then gated by `include_job_slots`) |
| `assets_via` / `jobs_via` / `wallet_via` | character_id that answered the endpoint family (NULL = no role or skipped) |
| `asset_rows` / `job_rows` | Rows returned at the last refresh (pull diagnostics) |
| `refreshed_at` | |

### Settings

**`settings`** — single row.

| Column | Default |
|---|---|
| `stockpile_buffer` | 0.05 — fraction (0.001–0.1); renamed/rescaled from stockpile_buffer_percent |
| `max_run_duration_hours` | 24.0 |
| `ship_batch_multiple` | 8 |
| `composite_reaction_extra_runs` | 1 |
| `price_region_id` | 10000002 (The Forge) — hub quotes come from its hub station, and (since 2026-08-23) raw leaves with no hub-station order take this same region's region-wide best order (the v1.9 `npc_goods_region_id` column was merged into it and dropped) |
| `price_source` | sell |
| `manufacturing_slots` / `reaction_slots` | 10 / 10 — user-entered pools (v1.1) |
| seven industry skill levels (`skill_industry` … `skill_science`, `skill_outpost_construction`) | 5 — user-entered, feed job time only (v1.1; Outpost Construction split out of the science level in v1.9) |
| `count_fitted_stock` | 0 — ESI stock excludes assets fitted to / loaded in ships and structures (module, rig, subsystem, service, fuel, core, drone, fighter slots/bays) and assets deployed in space (singletons located in a solar system) unless on (v1.9) |
| `default_intermediate_me` / `default_intermediate_te` | 10 / 20 — blueprints without an explicit setting |
| `input_purchase_margin` | 0.05 — extra raw materials bought vs. allocated-job consumption |
| `alchemy_enabled` | 0 (v1.4) |
| `alchemy_reprocess_yield` | 0.55 — scrapmetal cap; user-asserted, fold reprocessing tax in |
| `max_alchemy_jobs_per_type` | 4 — per unrefined formula per cycle; 0 disables |
| `skill_accounting` / `skill_broker_relations` | 5 / 5 — sell-side fees (v1.5) |
| `standing_broker_faction` / `standing_broker_corp` | 0.0 — NPC broker fee |
| `freight_in_isk_per_m3` / `freight_out_isk_per_m3` | 0.0 — flat courier rates: "Courier Highsec Market → Industry Hub" (bought materials in) and "Courier Industry Hub → High Sec Market" (finished products out); freight-in is the Jita leg since v1.10 |
| `structure_freight_in_isk_per_m3` | 0.0 — "Courier Null Sec Market → Industry Hub": flat ISK/m³ from the structure market (C-J6) to the industry system (v1.10); on an EXISTING database seeded once as a copy of the Jita rate when the column is added, so a configured Jita rate never makes the structure look freight-free by default |
| `structure_buy_enabled` | 1 — compare inputs against the structure market's sell ladder; off = every input priced from the hub, as before v1.10 |
| `capital_market_mode` / `capital_structure_id` | cj6 / NULL (v1.6) |
| `capital_sales_tax` / `capital_broker_rate` | 0.0337 / 0.01 (v1.6) |
| `capital_movement_cost_isk` | 0.0 — replaces freight-out for capital-priced hulls |
| `capital_scc_surcharge` | 0.015 — SCC on market sales (all sales since 2026-08-20) |
| `industry_scc_surcharge` | 0.04 — SCC on job installation cost (2026-08-21) |
| `esi_client_id` / `esi_client_secret` | Vestigial since v1.21 — the client ID ships in config and the secret is wiped on upgrade |

**`tracked_system`** — `solar_system_id`. Assets outside these do not count
(an empty list means no filter — everything counts).

**`blacklist_category`** / **`blacklist_item`** — the production blacklist
(v1.3): category keys (mapped to product groups in
`config.BLACKLIST_CATEGORIES`) and individual type IDs the user has marked
*buy, don't build*.

**`blueprint_setting`** — per-blueprint assumptions. **Explicit values always
take priority over the global intermediate ME/TE defaults**
(`default_intermediate_me`/`te`), so the user can plan against research levels
not yet achieved. Blueprint ME/TE is user-entered — never read from ESI (the
owned-blueprint step was struck 2026-08-20).

| Column | Notes |
|---|---|
| `blueprint_id` | PK |
| `me_level`, `te_level` | |

**`class_setting`** — global build settings per item class (design change
2026-08-15: replaces the earlier `facility` / `facility_rig` /
`category_facility` model). The user asserts, once per class, where that class
of items is built and what rigs the structure carries — no named facilities,
no per-rig fitting, no applicability filtering at planning time.

Item classes (classification precedence in `industry.classify_item`):
`capital_ships` (Dreadnought, Carrier, Capital Industrial, Force Auxiliary,
Lancer Dreadnought, Command Carrier, Titan, Supercarrier — note Freighters
count as T1 ships and Jump Freighters as T2 ships, per CCP's own size
filters), `t2_ships` (tech level ≥ 2), `t1_ships`, `basic_capital_components`
(group 873), `advanced_components` (groups 334, 913, 964), `structures`
(v1.9: categories 65 Upwell structures and 66 Standup rigs/modules plus
group 536 Structure Components — the Upwell/Standup/component subset of
CCP's "Structures" rig target filter 12, which additionally spans Starbase
23, Infrastructure Upgrades 39, Sovereignty Structures 40, Fuel Blocks 1136
and Skyhooks 4736, all left in `other` by decision; seeded as a copy of the
user's `other` row on existing databases),
`reactions` (classified by blueprint activity), `other` (fallback).

| Column | Notes |
|---|---|
| `item_class` | PK — one of the classes above |
| `structure_type_id` | Null = NPC station (no bonuses, no rigs) |
| `security` | Chosen as a High/Low/Null band (2026-08-20 dropdown), stored as a canonical status; determines the rig band multiplier |
| `me_rig` | none / t1 / t2 / thukker (thukker: component classes and, from v1.9, structures — the XL Thukker rig covers the Structures filter at the standard −2.0 leg) |
| `te_rig` | none / t1 / t2 / thukker |
| `system_cost_index` | |
| `tax_rate` | |

Standard rig magnitudes (verified in live data for both engineering and
reaction rigs): ME −2.0% (T1) / −2.4% (T2), TE −20% (T1) / −24% (T2), scaled
by the rig family's security band. The Thukker tier (component classes and,
since v1.9, the structures class) is ME −3.7% on capital-component groups
873/913 and −2.0% on plain components 334/964 and on structures, TE −20%,
on its own 0.1/1.9/0.1 bands.
Structure bonuses are read from the structure type's own attributes via
`industryModifierSources`.

### Index runs

**`index_run`**

| Column | Notes |
|---|---|
| `index_run_id` | PK |
| `run_number` | Sequential, UNIQUE-indexed (2026-08-20, vs the duplicate-number race) |
| `planned_start`, `actual_start`, `planned_end` | |
| `status` | planned / active / complete |
| `completed_at` | v1.5: stamped on "Mark executed" — lag costing walks completed runs only |
| `wallet_character_isk`, `wallet_corporation_isk` | ISK snapshot at plan time (buying-power check) |

**`index_run_item`** — one row per item, **merged across all active pipelines**.
The core output table.

| Column | Notes |
|---|---|
| `index_run_item_id` | PK |
| `index_run_id` | FK |
| `type_id` | |
| `on_hand_qty` | ESI assets in tracked systems |
| `in_progress_qty` | Output of active jobs — counts as stock |
| `target_stock_qty` | Merged minimum × (1 + buffer) |
| `deficit_qty` | `max(0, target + merged_min − on_hand − in_progress)`; final products: `= target` (see Phase 4) |
| `recommended_action` | buy / build / both |
| `blueprint_id`, `activity_id` | Activity selects the slot pool |
| `time_per_run` | ME/TE/facility adjusted |
| `portion_size` | |
| `max_runs_per_job` | `floor(window / time_per_run)`, capped by the 30-day game ceiling and runs-per-BPC (see Phase 5) |
| `total_runs_needed` | `ceil(deficit / portion_size)` |
| `jobs_needed_unconstrained` | `ceil(total_runs_needed / max_runs_per_job)` |
| `jobs_allocated` | Post-MILP |
| `runs_allocated`, `recommended_build_qty` | |
| `recommended_buy_qty` | Shortfall flipped to purchase (never for finals) |
| `build_savings_per_unit` | MILP objective coefficient |
| `capacity_limited` | Allocation < need |
| `low_stock` | Won't sustain next run |
| `price_snapshot` | Cost basis at this run — the CHOSEN venue's raw best price (v1.10) |
| `price_region_wide` | v1.9: price_snapshot was a region-wide fallback quote (hub-quote provenance only) |
| `buy_venue` | v1.10: `hub` / `structure` / NULL (unpriced; NULL on pre-v1.10 rows = hub) — plan-time venue of price_snapshot |
| `structure_units_cheaper` | v1.10, structure buys: units of the structure's sell ladder landing at or below the hub landed price; the run page flags the buy *shallow* when `recommended_buy_qty` exceeds it |
| `depth`, `item_class`, `merged_min_qty` | Merged chain depth (display), class, one cycle's consumption |
| `unit_install_fee` | v1.5: hypothetical per-unit install fee snapshotted for every buildable |
| `savings_unpriced_inputs` | Unpriced raw leaves in the savings chain (UI badge) |
| `unit_chain_cost` | 2026-08-23: the vertically-integrated chain cost per unit behind `build_savings_per_unit` (savings = landed buy price − this); NULL on older rows, which the run page recovers as price − savings |
| `alchemy_for_type_id` | v1.4, unrefined rows: the composite this route feeds |
| `direct_unit_cost` / `alchemy_unit_cost` | v1.4, composite rows: the route comparison |
| `alchemy_output_qty` | v1.4: composite units expected from this cycle's alchemy jobs |
| `alchemy_credit_qty` | v1.4: units credited from unrefined stock/jobs at the yield |

**`index_run_item_pipeline`** — attributes shared demand back to pipelines.

| Column | Notes |
|---|---|
| `index_run_item_id`, `pipeline_id` | Composite PK |
| `qty_attributable` | This pipeline's share |
| `depth` | 2026-08-20: the item's depth within THIS pipeline's own chain — the depth lag costing prices from; NULL on pre-fix rows falls back to the merged depth |

**`job_link`** — RESERVED, UNIMPLEMENTED: schema only; no code reads or
writes it. The intended plan-vs-reality reconciliation was superseded by the
v1.5 "mark executed" truth model ("the plan is advisory; ESI is the ledger"
survives as a principle, not as this table).

| Column | Notes |
|---|---|
| `job_id` | PK — ESI industry job ID |
| `index_run_id`, `type_id` | |
| `matched_to_recommendation` | False for off-plan jobs |
| `status` | observed / complete / reconciled |

### Cost lots (FIFO vintage costing)

> **Dormant since v1.5** — lag-based costing (`costing.py`) supersedes lot
> genealogy as the realized-cost model; these tables and the Phase-8
> primitives remain but nothing in the app writes them (tests only).

**`cost_lot`**

| Column | Notes |
|---|---|
| `lot_id` | PK |
| `type_id` | |
| `created_index_run_id` | The run this lot entered the pipeline |
| `quantity_original`, `quantity_remaining` | |
| `unit_cost` | Snapshot price, or blended input cost + job fees |
| `source_type` | purchased / manufactured |

**`lot_consumption`** — genealogy edges, FIFO ordered:
`output_lot_id`, `input_lot_id`, `qty_consumed`.

**`finished_batch`**

| Column | Notes |
|---|---|
| `pipeline_id`, `index_run_id` | |
| `output_lot_id` | FK |
| `quantity` | |
| `total_cost_basis` | Walked back through `lot_consumption` |
| `market_value_at_completion`, `profit` | |

### ESI-side state

**`esi_token`** — OAuth refresh/access tokens per character.
**`location_system`** — resolved asset locations (station/structure →
solar system); only definitive answers are cached (2026-08-21), transient
resolve failures retry next refresh.
**`market_price`** — cached prices per (type, region/structure, source),
plus `hub` (v1.9): 1 = the configured quote (hub station where the region
has one), 0 = region-wide fallback for a raw leaf with no hub-station order;
structure-market and adjusted rows are always 1. `index_run_item.price_region_wide`
carries the same flag per planned item (plan-time provenance of
`price_snapshot`).
**`structure_sell_order`** (v1.10) — the structure market's SELL ladder per
wanted type: (structure_id, type_id, price, volume_remain), ascending by
price, replaced wholesale on every structure refresh (same authed pull as the
best-price rows; orders with no remaining volume are dropped from both). The
buy-venue comparison reads it cache-only at plan time.
**`esi_snapshot`** — the persisted ESI pull that decouples planning from the
network: fetched_at, on_hand / in_progress / active_jobs JSON, wallet ISK,
and `job_ends` (2026-08-20: active-job end dates per activity, for
multi-cycle slot netting). Pruned to the most recent five rows.

---

## 7. Run Planning Calculation

### Phase 1 — Snapshot inputs

- ESI **corporation** assets (2026-08-20; per-corp opt-out via
  `esi_corp.count_assets` since 2026-08-25 — an opted-out corp skips the
  assets pull), plus PERSONAL hangars only for characters with
  `pool_character.count_assets` on (default off), filtered to tracked
  systems → `on_hand_qty`. Assets inside
  containers count; each corp endpoint family (assets / jobs / wallets) is
  retried across pool characters until one holds the role. Unless
  `count_fitted_stock` is on (v1.9), assets fitted to or loaded in ships and
  structures (module / rig / subsystem / service / fuel / core / drone /
  fighter slots and bays) and assets deployed in space (singletons whose own
  location is a solar system — anchored Upwell structures, sov hubs, POCOs,
  starbases) are skipped; items stored INSIDE an excluded structure still
  count.
- Active industry jobs (corp AND personal) → `in_progress_qty` per item as
  `Σ(runs × portion_size)`, filtered by DELIVERY location to tracked systems
  (unresolvable locations keep the credit). **In-flight production counts as
  stock**, preventing duplicate recommendations for work already underway.
- Capacity per pool: the user-entered slot totals, minus active jobs whose
  end date lies beyond the next index run (multi-cycle overhang, 2026-08-20).
- Price snapshot per configured source — since v1.10 one quote per type from
  `market.buy_quotes`: the hub cache (with its v1.9 region-wide fallback)
  against the structure market's cached sell ladder, the cheaper LANDED
  venue winning (`costing.choose_buy_venue`: price + that venue's flat
  freight-in × packaged m³; tie → hub; finals excluded → hub). The
  `Snapshot` carries the chosen raw price plus `buy_venue` and
  `structure_units_cheaper` per type; cache-only, no network.

The app does not read these skills — slot totals are user-entered — but as
player guidance for choosing the numbers, each pool gives 1 line plus one
per level of:

| Pool | Skills |
|---|---|
| Manufacturing | Mass Production (3387), Advanced Mass Production (24625) |
| Reaction | Mass Reactions (45748), Advanced Mass Reactions (45749) |

### Phase 2 — Expand demand per pipeline

Expand each active pipeline's final product at `output_qty_per_run`
**topologically** (revised 2026-08-20): demand for an item is merged across
ALL of its consumers before its run count is derived — ceil once per item,
not once per demand edge, matching the single merged job set the engine
installs. At each material, decide whether it is buildable (has a blueprint)
or raw; self-consuming legacy blueprints stay cycle-safe. ME and facility
bonuses are applied at every level, so quantities reflect what a
job actually consumes. Settings resolve as: explicit `blueprint_setting` → global intermediate
default (the ESI owned-blueprint step was never wired and was struck
2026-08-20 along with its scopes).

Cycle-safe: a material that transitively requires itself is treated as raw.

**Production blacklist (v1.3).** The user can mark categories (checkboxes
over product groups: fuel blocks, R.A.M. tools, T1 hulls-as-components, the
component and reaction families) and individual items as *buy, don't build*.
Blacklisted buildables are treated as raw during expansion — bought
just-in-time, their sub-chains never expanded. Never applies to a pipeline's
final product. Tables: `blacklist_category`, `blacklist_item`.

### Phase 3 — Merge into unified demand

Sum each item's requirement across every pipeline that needs it. Populate
`index_run_item_pipeline` with each pipeline's share.

### Phase 4 — Targets and deficits

```
target_stock_qty = merged_min_required × (1 + stockpile_buffer)   [intermediates/raw]
                 = requested output qty                            [final products]

deficit_qty      = target_stock_qty                                [final products]
                 = max(0, target + merged_min − on_hand − in_progress)  [others]
```

> **Corrected 2026-08-16.** The original deficit (`target − stock`) drained
> the pipeline: a fully-stocked stage planned zero jobs, this cycle's
> consumers emptied it, and the line oscillated full/empty one cycle out of
> phase. The deficit now includes one cycle's consumption (`merged_min`),
> so a stage always ends the cycle back at target. Steady state: every
> stage builds exactly one cycle's worth every cycle. From empty stock it
> orders target + one cycle's worth (priming the pipe while feeding this
> cycle's jobs).

Composite reaction **inputs** receive an additional
`composite_reaction_extra_runs` runs' worth of material on top of the target.

**Unrefined credits (v1.4).** With alchemy enabled, unrefined items on hand
or in flight count as their reprocess outputs — composite AND recovered
inputs — at `floor(qty × base_output × yield)`, credited to
`in_progress_qty` (not on-hand: a manual reprocess still stands between
them and usable stock) before deficits are computed. Once the user actually
reprocesses, the next ESI snapshot replaces the credit with real items; if
they never reprocess, the credit persists and nothing double-orders either
way. Planning uses the yield *setting*; Phase 8 records the *actual*
reprocess results.

### Phase 5 — Buy vs. build sizing

> **Revised 2026-08-16 — raw inputs are just-in-time.** Raw materials are no
> longer bought to a stockpile target. After slot allocation (Phase 7), each
> raw input is bought to cover exactly what this cycle's **allocated** jobs
> will consume, × (1 + `input_purchase_margin`, global setting, default
> 0.05), net of on-hand and in-progress. Zero allocated consumers → zero
> purchase. Because reaction jobs saturate their full window, their input
> overshoot is captured exactly — superseding the composite-extra-runs
> approximation for raws. The stockpile buffer now applies to buildable
> intermediates only, and `low_stock` no longer fires for raws (near-empty
> raw stock is by design).

Buildable items → capacity math:

```
max_runs_per_job          = floor(max_run_duration_hours / time_per_run)
                            … capped at runs_per_bpc (pipeline finals) and,
                            for BOTH activities, at the in-game per-job
                            ceiling: ceil(30 days / MODIFIED time_per_run) —
                            the last run may overhang, and a single run over
                            30 days still installs as 1 run (user-verified
                            2026-08-21; see §5). Reaction formulas
                            additionally clamp to their maxProductionLimit
                            where lower.
total_runs_needed         = ceil(deficit_qty / portion_size)
jobs_needed_unconstrained = ceil(total_runs_needed / max_runs_per_job)
```

**One job per slot for the full cycle.** Jobs are installed simultaneously and
cannot be restarted mid-cycle, so a slot is never reused within a run.
`max_runs_per_job` describes how much work packs into one job;
`jobs_needed_unconstrained` describes how many *parallel slots* are required.

Where `time_per_run > max_run_duration_hours`, the job spans multiple cycles. It
occupies its slot until complete and is excluded from new recommendations by the
active-job check in Phase 1.

### Phase 6 — Slot allocation under contention

Grouped by pool (manufacturing, reaction). First, INTERMEDIATES whose build
savings are zero or negative never get slots regardless of contention —
building above the LANDED market price (raw + the venue's courier rate × m³,
2026-08-23) wastes ISK, so Phase 7 buys their deficit instead
(2026-08-20). Pipeline finals are exempt (2026-08-21): they are built to
sell, and their negative paper margin surfaces as a badge, not a buy order.
Among the remaining contenders, if demand fits capacity everyone gets what
they need. Otherwise pipeline FINALS take their slots first, then unpriced
items (neither has a purchase fallback — finals by decision 2026-08-21,
unpriced items by necessity), and the remaining slots are optimized as a
MILP via `scipy.optimize.milp`:

- **Objective:** maximize total build savings, where savings per unit =
  the item's **landed buy price** (its venue's raw price + that venue's
  courier rate × packaged m³ — 2026-08-23; the item's own inbound freight
  was missing before, which biased bulky intermediates toward "buy") − the
  **vertically-integrated chain cost** (2026-08-21): each
  stage costs its install fee plus its inputs, every buildable input priced
  at min(buy at market + inbound freight, build from its own chain);
  bought units carry inbound freight on packaged volume at THEIR venue's
  rate (v1.10: Jita or structure leg); finals add their
  pipeline's per-hull BPC amortization; unpriced raw leaves cost 0 and are
  counted for a UI badge (`savings_unpriced_inputs`). Savings stay GROSS of
  sell-side fees (build-vs-buy avoids a purchase, not a sale).
- **Variables:** each non-saturating item's LAST job is a separate integer
  variable weighted at only the residual runs Phase 7 will grant it (a
  full-window weight favored nearly-empty jobs); saturating reactions keep
  one full-weight variable.
- **Constraint:** slots available per pool. Solver failure raises loudly
  rather than silently flipping the pool to buys.
- **Fallback:** priced INTERMEDIATE losers are not left unfulfilled — their
  shortfall becomes `recommended_buy_qty` (lowest-margin items are the ones
  bought). A final's shortfall is never bought: it stays a
  `capacity_limited` unmet build.

Items with no snapshot price on record (no orders at the hub, or never
fetched) are never flipped to buy — their shortfall remains an unmet deficit
flagged `capacity_limited` rather than producing a fictional purchase order.

### Phase 6.5 — Alchemy substitution (v1.4)

Runs after slot allocation, only when `alchemy_enabled` and the reaction
pool has spare slots — a contended pool disables alchemy entirely (direct
reactions are ~10× more slot-efficient, so alchemy must never displace a
needed direct job).

Routes are derived from data, no hardcoded pairs: a reaction formula whose
product's reprocess outputs contain another reaction formula's product is
an alchemy route for that composite (17 exist). For each routed composite
that won direct jobs:

```
direct_unit  = (Σ inputs @ landed + install) / 200
               (each route costed at its own job scale — runs =
               max_runs_per_job — so once-per-job material rounding
               amortizes as installed, 2026-08-21)
alchemy_unit = (Σ inputs @ landed + install − recovered @ landed × yield)
               / (base_composite_output × yield)
```

Inputs and the recovered credit price LANDED — venue price plus that
venue's courier rate on packaged m³, the same leg every other buy decision
uses (2026-08-24; raw prices before that). Freight does not cancel between
the routes: at 55% yield the unrefined route hauls ~1.8× the volume per
composite unit, so the raw comparison overstated alchemy by ~500–1,250
ISK/unit at 900 ISK/m³ and flipped marginal routes (run 49: Hexite +1,304
raw, −130 landed).

Both recorded on the composite row; alchemy is a candidate only when
strictly cheaper. Then a greedy swap loop: drop one direct job, add however
many alchemy jobs cover the RESIDUAL deficit that job was covering
(`needed − (jobs−1) × out_per_direct_job − alchemy output so far`). The
last direct job of an item is mostly overshoot, so the first swap is cheap
— often one alchemy job at zero net slot cost; wholesale replacement only
happens when spare slots and `max_alchemy_jobs_per_type` genuinely allow
it. Swaps are ranked by ISK saved on the needed units per spare slot
consumed. Coverage never drops below the deficit, and displaced direct
jobs are not a capacity shortfall (Phase 7 credits `alchemy_output_qty`).

Alchemy jobs are ordinary reaction installs of the unrefined formula: they
saturate the cycle window, occupy reaction slots, and their inputs join the
Phase 7 just-in-time purchase pass (inputs the chain doesn't otherwise
demand, e.g. Cadmium, are added as raw plan items). The plan's Alchemy
section lists the installs plus the manual step: "Reprocess N Unrefined X →
~M X (+R goo back)".

### Phase 7 — Finalize recommendations and flags

```
runs_allocated         = min(total_runs_needed, jobs_allocated × max_runs_per_job)
                         … then rounded UP so every job of an INTERMEDIATE
                         runs the SAME count (v1.2: no short last job; the
                         slight overbuild lands in stock and nets off next
                         cycle). Pipeline finals and exact-quantity ship
                         groups are exempt (2026-08-20) — their last job
                         runs short, keeping requested/batch-rounded totals
                         exact (finals ignore stock, so overbuild would
                         never net off).
recommended_build_qty  = runs_allocated × portion_size
```

| Category | Rule |
|---|---|
| Subcapital ships | Round up to whole BPCs (`runs_per_bpc`) when set, else to a multiple of `ship_batch_multiple` (default 8). **Capacity wins:** where rounding exceeds allocated slots, build what fits and set `capacity_limited`. |
| Capitals, Freighters, Jump Freighters | Exact quantities — never batch-rounded (`EXACT_QTY_SHIP_GROUPS`). |
| Reactions | A slot allocated to a reaction runs `max_runs_per_job` — the full cycle window — even if that overshoots the deficit. **Exception (v1.3):** Hybrid Polymers (974) and Molecular-Forged Materials (4096) size to the deficit like manufactured items (`NON_SATURATING_REACTION_GROUPS`). |
| Composite reaction inputs | Carry the extra runs buffer (applied in Phase 4; largely superseded for raws by just-in-time purchasing). |

`low_stock` is set by projecting stock forward — current + allocated
replenishment − consumption by *allocated* downstream builds — and testing
against next run's expected minimum. Buildables only (raw inputs are
just-in-time by design). Suppressed until a pipeline has at least one EXECUTED run behind it
(re-keyed 2026-08-20 — the finished-batch signal died with the v1.5 move to
lag costing), since every stage legitimately reads low while priming.

### Consumption feedback loop (2026-08-21; iterated to convergence 2026-08-28)

Phase 4 estimates each stage's cycle draw as the steady-state `merged_min`,
but the allocation's ACTUAL draw differs: catch-up consumers build more
than one cycle's worth, saturating reactions overshoot, and the game rounds
materials per job. After the draft Phase 7, supplier deficits are re-sized
against the allocation's actual planned consumption and Phases 5–7
(including the alchemy pass) re-run, repeating until the deficits stop
moving. A correction propagates one BOM tier per pass (`bom.expand` layers
depth strictly — every consumer of a material sits shallower than it), so
the loop caps at max buildable depth + 1 passes; the cap is also the guard
for the no-fixed-point case where tiers trade the last contended slots back
and forth forever (the final allocation stands, and its flags tell the
truth). Finals keep their exact-requested rule, and `low_stock` /
`capacity_limited` evaluate the FINAL allocation — with the loop converged,
a surviving flag is a genuinely uncoverable shortfall. (Until 2026-08-28
the pass ran once on the first-order-error-dominates assumption; run 59
showed heavy multi-tier catch-up breaking it — six processed-material
low-stock flags, and capacity-starved buy flips sized off the stale draft
draw.)

### Invention pass (v1.22; vintage-only since v1.23)

After the loop converges (and before persistence), `_invention_pass`
persists each invention-enabled pipeline's invention VINTAGE into
`index_run_invention` — the skill-applied probability, invented stats,
per-attempt input prices and fees, and cost_per_run — for lag costing and
the run's profit view. Since v1.23 it adds NOTHING to the plan items:
sizing, purchasing and copy jobs live on the live Invention tab
(`engine.invention_stockpile`, §5), which targets the BPC stockpile from
CURRENT stock rather than any index run. The invention cost per licensed
run still replaces the `bpc_cost_isk` adder on the final inside
`_chain_coster` (same constant per-unit term, so the MILP objective's
shape is unchanged).

### Phase 8 — Cost lot bookkeeping

> **Dormant since v1.5.** Lag-based costing (§10, v1.5 row) is the
> realized-cost model; these primitives exist (and are exercised by tests)
> but nothing wires ESI or the web app to them. `job_link` was never
> implemented at all.

- Purchases → new `cost_lot` at snapshot price, source `purchased`.
- Jobs observed in ESI → recorded in `job_link`, input lots FIFO-reserved
  (never wired).
- Completed jobs → new `cost_lot` with `unit_cost` blended from consumed input
  lots plus job installation cost, source `manufactured`.
- Finished output → `finished_batch`, with `total_cost_basis` walked back
  through the lot graph to the original purchase runs.

- Alchemy reprocess (v1.4) → `reprocess_unrefined`: unrefined lots consumed
  FIFO become a composite lot plus recovered-input lots. Recovered outputs
  enter at their market credit price; the composite lot carries the residual,
  so total cost is conserved through the genealogy. Quantities are the REAL
  reprocess results, not the planning estimate.

**Per-pipeline profit attribution** (in the live lag model) comes from
`index_run_item_pipeline` shares and per-pipeline depths; the dormant lot
graph would have resolved it by construction had it been wired.

---

## 8. ESI Integration

OAuth2 with PKCE against EVE SSO as a **public client**. Magoo ships one
registered application's client ID (`config.ESI_CLIENT_ID`) and sends no
client secret at all — CCP documents the client ID as public and PKCE as the
flow that lets a native app ship without a secret. Users register nothing;
`MAGOO_ESI_CLIENT_ID` overrides the shipped ID for development.

The redirect URI is the fixed `config.CALLBACK_URL`
(`http://localhost:8765/sso/callback`), which must match the registration
byte for byte — which is why the port cannot float (v1.21).

Login runs in the user's **own browser**, never in the app window: RFC 8252
forbids embedded user-agents for authorization, and the visible address bar
is what lets a player confirm they are on `login.eveonline.com`. Because the
window and the browser are separate cookie jars, the PKCE verifier lives in
`web.LoginBroker` server-side rather than the Flask session, and the window
learns the outcome by polling `/sso/status`.

### Scopes

| Scope | Used for |
|---|---|
| `esi-assets.read_assets.v1` | Personal hangars -> stock when the character's count_assets toggle is on (per-character opt-in, default off — 2026-08-25) |
| `esi-industry.read_character_jobs.v1` | Personal jobs → in-progress stock + slot occupancy |
| `esi-assets.read_corporation_assets.v1` | Corp assets → stock on hand (the only stock source) |
| `esi-industry.read_corporation_jobs.v1` | Corp jobs → in-progress stock + slot occupancy |
| `esi-wallet.read_character_wallet.v1` | Character ISK (buying-power check) |
| `esi-wallet.read_corporation_wallets.v1` | Corp ISK (buying-power check) |
| `esi-universe.read_structures.v1` | Structure → solar-system resolution |
| `esi-markets.structure_markets.v1` | v1.6 capital sell quotes from the structure market |

Tokens are stored in the local SQLite database. Refresh handled
transparently. ETag/Expires response caching is NOT yet implemented — a
confirmed open item (2026-08-20).

---

## 9. Build Status

| Component | State |
|---|---|
> **2026-08-15 — rebuild from spec.** The sandbox-built code was not carried
> over; Magoo is being rebuilt on the user's Windows machine from this spec.
> Status below reflects the rebuild. Unlike the sandbox, this environment has
> full internet: the SDE importer runs against the real CCP export, so the
> development fixture is no longer needed, and ESI is testable locally.

| Component | State |
|---|---|
| Environment | Done — Python 3.13.15, venv with Flask/SciPy/httpx/PyJWT |
| Scaffold, `config.py` constants | Done |
| SDE auto-pull and import | **Done — verified against live CCP data, build 3466501** |
| Reference data read layer | Done (`refdata.py`, cached, real SDE) |
| ME/TE/facility/rig math | Done — Hulk worked example passes; per-class build-settings model |
| Multi-stage BOM expansion | Done — **reproduces prior anchor exactly: Hulk → 78 items, depth 5** |
| Own schema (`store.py`) | Done — full state schema, seeded defaults |
| Engine Phases 2–8 | Done — planning + MILP allocation + FIFO cost lots; Phase 1 snapshot arrives via `Snapshot` from esi/market |
| ESI integration | **Done — live end-to-end incl. corporation data** (SSO login; corp assets→systems for stock — corp-scope since 2026-08-20; char + corp industry jobs deduped with delivery-location filtering; user-entered slot pools net of multi-cycle jobs; char + corp wallets). Live test: 1,020 types on hand, 129 products in progress, 11.06B corp ISK. Full snapshot ~80s (corp asset pagination dominates) |
| Market prices | Done — public ESI adjusted prices + cached regional orders + the structure market's best prices and sell ladders (`market.py`) |
| Web UI | **Done** — dashboard, pipelines (bulk Excel paste incl. per-ship ME/TE), settings (globals + per-class build settings + tracked systems), characters (in-app SSO), index runs with buy/build/reaction lists, Multibuy export, wallet-vs-buy-total check, per-run Profit tab (lagged, on executed runs) + current-prices Profit page (v1.5) |
| ESI guideline compliance | Done — central `esi_request`: descriptive User-Agent, error-limit backoff (X-ESI-Error-Limit / 420 / Retry-After), 5xx retry. Per-endpoint cache-expiry honoring deferred (snapshot volume is one pull per cycle) |
| Test suite | 229 tests passing (industry, classification, BOM, engine, cost lots, blacklist, job ceilings, JIT purchasing, alchemy, price cache + region-wide fallback, lag + current costing, capital pricing, structure pricing/freight exemption, Thukker rigs, chain-cost savings, consumption feedback, ESI refresh scoping + fitted/deployed stock, structure planning, two-venue buying (venue chooser, ladders, buy quotes, venue persistence, per-venue freight), run-tab template renders, SDE import atomicity, schema/settings integrity) |

First live index run (2026-08-15, Hulk ×8 pipeline): 78 items, 68 already
covered by stock + 129 in-progress corp jobs, 0 builds (all slots occupied —
correct), 10 buys totaling 588M ISK, all moon materials.

### v1.1 (2026-08-15, same day)

- **Slot pools are user-entered** (`manufacturing_slots` / `reaction_slots`
  in settings, default 10/10) — no longer derived from ESI skills. Active
  jobs do NOT reduce the pools (revised 2026-08-17): an index run is
  planned for the moment the previous cycle's jobs have all delivered, so
  full capacity is assumed; in-flight output still counts as stock.
- **Skill levels are user-entered** (settings, all default V) and now feed
  job time (they were previously ignored): Industry −4%/lvl and Advanced
  Industry −3%/lvl on all manufacturing; Reactions −4%/lvl on reactions;
  Advanced Ship Construction / racial Starship Engineering / T2 science
  −1%/lvl (per-skill `manufactureTimePerLevel` from dogma) applied only to
  blueprints requiring the skill — requirements imported into the new
  `ref_blueprint_skill` table (9,276 rows). All values verified from dogma.
  Skills endpoint dropped from ESI entirely.
- **ESI refresh decoupled from planning.** "Update from ESI" persists an
  `esi_snapshot` row (assets, in-progress, active-job counts, wallets);
  "Plan Index Run" reads the stored snapshot + cached prices — ~2.5s, so
  settings changes replan instantly. Dashboard shows snapshot age.
- Pipelines page accepts bulk paste from Excel — columns: **ship · qty/run ·
  runs per BPC · ME · TE** (trailing columns optional). Re-pasting an
  existing ship updates it in place; per-row delete and a two-click Clear
  All exist (no JS-confirm dependence — blocked dialogs silently killed the
  original delete). The paste writes the ship's `blueprint_setting`; the
  separate Blueprints tab was removed. Intermediates take their ME/TE from
  the global settings `default_intermediate_me`/`default_intermediate_te`
  (10/20 default), applied to any blueprint without an explicit setting.
- Verified live: refresh 22s, plan 2.5s. Hulk with all-V skills:
  240,000 × 0.8 (TE20) × 0.7 (Sotiyo) × 0.6137 (skills) ≈ 22.9h — now
  inside a 24h cycle window.
- **Stockpile buffer is a fraction, 0.001–0.1** (0.1%–10%), renamed
  `stockpile_buffer`; old percent values clamp to 0.1 on migration.
- **Final ships always build their requested quantities** — no buffer, and
  current stock / in-flight jobs are ignored for finals (displayed only).
  Intermediates and raws still net stock and in-progress against targets.
- **Runs-per-BPC** (pipeline paste column 3) caps runs/job and replaces the
  ship batch multiple as the rounding unit when set.
- **Unpublished blueprints filtered at import** after the live Tungsten
  Carbide incident (see §4) — "Test Reaction Blueprint" had inflated
  reaction-input demand ~500×.

### v1.2 (2026-08-16)

- **Steady-state deficits**: deficit = target + one cycle's consumption −
  stock − in-progress (see corrected Phase 4) — fixes the full/empty
  oscillation the original formula caused.
- **Just-in-time raw purchasing** with `input_purchase_margin` (see revised
  Phase 5); stockpile buffer became a 0.001–0.1 fraction, buildable
  intermediates only.
- **Final ships always build their requested quantities**, ignoring stock
  and in-flight jobs (previous wave is bound for sale).
- **Uniform runs per job** (rounded up, no short last job) and the
  **544-run reaction hard cap** + formula `maxProductionLimit`.
- **SQLite WAL + 30s busy timeout** — fixes "database is locked" from
  threaded requests and OneDrive sync.
- Live index runs surfaced two data bugs fixed in v1.1/v1.2: the
  Test-Reaction-Blueprint import bug (§4) and uninstallable 614-run
  reaction jobs (hence the 544 cap).

### v1.3 (2026-08-16)

- **Production blacklist** (EVE-IPH style): category checkboxes + per-item
  do-not-build list; blacklisted items are bought just-in-time and their
  sub-chains pruned at expansion; finals exempt (see Phase 2).
- **Hybrid Polymers and Molecular-Forged Materials reactions size to the
  deficit** instead of saturating the cycle window.
- **Per-run Plan/Chain tabs**: the Chain view lists the entire chain (Raw
  Inputs / Manufacturing / Reactions with category sub-headers) showing
  cycle need (`merged_min_qty`, now persisted per item), target, stock,
  in-jobs, deficit, and status.
- Plan tables: grouped category sub-headers with T1/T2 Capital and T1 /
  T2-T3 Subcapital ship splits (freighters + JFs under capitals), columns
  reordered to Runs/job · Jobs · Build qty · Time/job (job duration),
  sortable and column-aligned throughout.

### v1.4 (2026-08-17)

- **Alchemy**: spare reaction slots substitute Unrefined reaction routes
  for direct composite reactions when cheaper per unit (§7 Phase 6.5).
  New `typeMaterials` import → `ref_type_material`; routes derived from
  data (17 composite routes; mineral alchemy excluded — randomized
  outputs). Settings: `alchemy_enabled` (default off),
  `alchemy_reprocess_yield` (0.55, scrapmetal cap — verified: unrefined
  items reprocess under scrapmetal rules, rigs don't apply),
  `max_alchemy_jobs_per_type` (4). Unrefined stock/in-flight credits as
  in-progress composite + recovered inputs at the yield; Phase 8
  `reprocess_unrefined` conserves cost through the reprocess. Run detail
  gains an Alchemy section (installs + cost comparison + reprocess
  checklist); chain view marks alchemy rows and credits.
- Verified live (run 29, 2026-08-18 prices): with spare slots, one
  Unrefined Prometium job (277 runs → ~11,121 Prometium + 26,356 Cadmium
  back, 19,268 vs 31,542 ISK/u direct) replaced a mostly-overshoot direct
  job at zero net slot cost; Ferrofluid/Hyperflurite/Hexite routes also
  priced cheaper but were correctly throttled by the per-type cap; the
  contended 540-slot pool correctly produced no alchemy at all. SDE build
  3470007. 77 tests passing (14 new).
- **v1.4.1 (2026-08-18): alchemy jobs hard-cap at 272 runs** — user found
  277-run unrefined jobs uninstallable in game. 272 is exactly half of the
  544 direct-reaction cap, matching the 2× base time (21,600s vs 10,800s):
  the real ceiling is runs × base_time ≤ 544 × 10,800s, encoded as
  `REACTION_MAX_JOB_BASE_SECONDS` and applied to all reaction sizing (the
  flat 544 remains an additional ceiling for shorter formulas). 78 tests.
- **v1.4.2 (2026-08-18): prices decoupled from planning + parallel
  refresh.** Planning had been the only path that refreshed the 1h-TTL
  price cache — a cold cache meant ~336 SERIAL regional-order fetches
  (minutes) inside "Plan Index Run", while planning itself takes 0.04s.
  Now, mirroring the v1.1 ESI-snapshot decoupling: a dashboard "Refresh
  Prices" button does the pull (worker pool of 12; refetches only types
  older than ESI's own 300s server cache; adjusted prices cached under
  source='adjusted' region 0), and planning reads the cache regardless of
  age with the price timestamp shown in the flash and on the dashboard.
  Rate-limit compliance verified against CCP's 2025 token-bucket system:
  live headers show the market-order group at 12,000 tokens/15min, 2 per
  request — a full refresh spends ~7% of the budget; concurrency spends no
  extra tokens. Guards: esi_request's 420/429 Retry-After handling, a
  pool-wide stop when X-Ratelimit-Remaining runs low (skipped types stay
  stale), DB writes only on the request thread. Measured live: cold
  336-type refresh 9s (was minutes); plan instant. 83 tests (5 new).

### v1.5 (2026-08-18)

- **Lag-based costing + profit page** — replaces the planned reconciliation
  flow (see Decision Log). New `costing.py`: cost of a hull delivered at
  completed run N = Σ bought inputs (qty/hull × `price_snapshot` from run
  N−depth) + Σ buildable stages (qty/hull × `unit_install_fee` from run
  N−depth) + inbound freight + BPC amortization, where "run N−k" walks the
  *executed*-run sequence with a `min(depth, history)` clamp. The clamp is
  exact during spin-up (the priming run really did buy the whole chain);
  costs converge to true lagged vintages as history deepens, flagged with a
  spin-up badge until history ≥ chain depth.
- **Per-run snapshots**: `_finalize` now writes `unit_install_fee`
  (hypothetical per-unit install fee, EIV × indices, planned jobs or not)
  for every buildable; `price_snapshot` already covered every chain item.
- **Mark Executed** (reversible) on run detail drives
  `index_run.status`/`completed_at`; only executed runs feed costing —
  abandoned replans are invisible.
- **Two profit views** (split at user request, 2026-08-18):
  - **Per-run Profit tab** (Plan · Chain · Profit subnav on every index
    run): the lagged what-happened costing above; activates when the run
    is marked executed, shows a pointer to the what-if page until then.
    One card per attributable pipeline — cost/hull with the full lagged
    breakdown (kind, depth, lag, clamp per line), current sell quote, net
    proceeds = price × (1 − sales tax − broker fee) − packaged m³ ×
    freight-out, margin ISK/%.
  - **Profit page (nav)**: what-if at TODAY'S cached prices —
    `costing.current_hull_cost` walks the BOM with continuous per-unit
    quantities (no job rounding; that's a planning concern the executed
    view carries) and direct reaction routes only, pricing bought leaves
    at the current cache and charging every stage's install fee at current
    adjusted prices/indices; blacklisted sub-chains are bought, finals
    exempt, mirroring Phase 2. One sortable table row per pipeline
    (sorted by margin, margin/cycle column, price-cache timestamp in the
    header) with a per-row button opening the cost breakdown in a native
    `<dialog>` popup.
- **UI style system "console"** (2026-08-21, `base.html`): cool blue-black
  palette with a teal accent (`--accent #3fc1c9`), Source Sans 3 for UI and
  JetBrains Mono for every figure (Google Fonts with local fallbacks);
  sticky nav with brand mark, pill active state and a right-hand freshness
  cluster (ESI / price-cache age — amber past 24 h — SDE build, corp
  wallet) from the `nav_status()` context processor; tables get zebra
  rows, sticky mono headers, permanent sort chevrons, a faint red tint on
  `tr.neg` and a 2 px left stripe (`tr.top` green / `tr.neg` red); badges
  are outlined labels, with `badge fill bad` reserved for the two act-now
  alerts (exceeds wallets, negative margin). Dense tables show abbreviated
  ISK via the `isk_short` filter (2.81B / 741.3M) with the full figure in
  `title=`; buy lists, Multibuy and breakdown dialogs keep full digits.
  `age` filter renders ISO timestamps as 41m / 2h 14m / 3d 5h. Profit
  pages and the dashboard use the `.pagehead` pattern (title left, actions
  right, one-line caption, long explanations in a `<details>`); the
  per-run Profit tab now uses the same sortable table + breakdown dialog
  as the Profit page instead of stacked cards.
  Run Plan/Chain tabs carry a `.totals` strip (buy total, slots used /
  pool, unmet, low stock; chain status counts) and `layoutAligned()` in
  `base.html` sizes the Item column of every `table.aligned` on a page to
  the longest item name and equalises table widths by flexing the trailing
  badge column. Settings is a two-column page: sticky section side-nav,
  label/input `.fields` grid (descriptions under labels), long prose in
  `details.help`, one sticky save bar — input names unchanged.
  - **Cycle totals strip** (2026-08-21, both views): a header above the
    cards/table rolling every pipeline up to the whole cycle —
    profit/cycle, overall margin % (profit ÷ cost), cost/cycle, net
    proceeds/cycle, hulls/cycle across N pipelines. Per-hull × hulls per
    cycle, priced cards only; pipelines whose final has no sell quote are
    left out of every figure and counted in an "N unquoted" badge
    (`costing.cycle_totals`, `_cycle_totals.html`).
- **Sell-side settings**: Accounting + Broker Relations + station-owner
  faction/corp standings → computed broker fee (NPC venue) or flat
  structure rate; effective rates displayed live for in-client
  verification. Freight in/out ISK/m³ (no collateral term — user decision).
  Per-pipeline `bpc_cost_isk` input on the Pipelines page.
- **`packaged_volume` imported** from the SDE (`packagedVolume`) into
  `ref_type` — `volume` turned out to be assembled volume (Hulk 150,000 m³
  vs 3,750 packaged), which would have inflated hull freight 40×.
  `TypeInfo.freight_volume` prefers packaged. Re-import required (done,
  build 3470007).
- Verified live: run 37 planned (324 items, all 218 buildables carry fee
  snapshots), marked executed, profit rendered real margins (e.g. Hulk
  160.9M cost vs 202M sell → 19.4%; moon materials dominate, 77/78 Hulk
  lines clamped at history 1 as expected), then reopened. Both views
  re-verified against user-executed run 38: lagged vs current costs differ
  by the expected job-rounding overbuild (~7% on capital chains), and the
  current view's net-proceeds math checks exactly against the configured
  rates (Rhea: 7,278M × (1 − 3.37% − 1.19%) − 1.3M m³ × 750 =
  5,970M). 94 tests (11 new).

### v1.6 (2026-08-19): capital pricing

Capital-class hulls (`CAPITAL_PRICING_GROUPS` = capitals incl. Rorqual +
Freighters 513 + Jump Freighters 902 + Industrial Command Ships 941) take
their SELL quote from a **structure market** instead of the Jita region;
subcap hulls and ALL buy-side pricing are unchanged.

- **Market toggle** in the new Capital Pricing settings panel: "C-J6MT"
  preset (the C-J6MT Keepstar, structure_id **1049588174021**, from the
  user's showinfo link) vs custom structure ID. `Settings.capital_structure()`
  resolves it; custom mode without an ID falls back to the preset.
- **Pull**: `market.refresh_structure_prices` — authed paginated
  `GET /markets/structures/{id}` (the endpoint has no per-type filter, so
  the whole book comes down), min sell per capital final, cached in
  `market_price` as region_id=structure_id / source='structure', replaced
  wholesale each refresh (no stale orders linger; "no sell order" caches
  as NULL). Runs automatically at the end of every price refresh when any
  active final is capital-class; failures (no scope, no docking = 403)
  degrade to a flash message without breaking the regional refresh.
- **New scope** `esi-markets.structure_markets.v1` added to
  REQUESTED_SCOPES. It must ALSO be enabled on the ESI application
  registration at developers.eveonline.com, then one docking-access
  character re-logs via Characters → SSO; `esi.character_with_scope`
  picks the puller.
- **Fees/movement**: capital sales use their own user-entered sales tax +
  broker fee pair plus an **SCC surcharge** (flat market surcharge,
  default 1.5%, unaffected by skills/standings — added 2026-08-19 at user
  request; subcaps keep the standings-derived rates), and a fixed
  ISK-per-hull movement cost replaces ISK/m³ freight-out for capital-class
  hulls (`net_proceeds_per_hull(..., capital=True)`). The subcap panel is
  titled "Sub-Capital Pricing" (renamed "Sub-capital & structure pricing"
  in v1.9 when Upwell structures joined it, then "High Sec Trade Hub
  Pricing" on 2026-08-23).
- Profit pages show one quote per hull from whichever market governs its
  class (a "capital mkt" badge marks the routing) — no venue comparison.
  Contracts approach dropped by user decision (ESI can't browse
  alliance-only contracts; design notes in git history).
- Verified live: 8 capital-class finals routed to the structure market
  (badged, quote '—' until first authed pull), subcaps unchanged, refresh
  degrades with guidance when no character has the scope. 100 tests
  passing (6 new).

Candidate next steps: realized sell prices from wallet transactions
(replacing the current-market quote on the profit page); in-client
verification of broker/tax formula constants; honoring ESI cache expiries
to speed refresh; capacity-limited final ships "build next cycle" option
instead of buy-fallback; code signing (SignPath's free OSS tier, so
SmartScreen reputation carries across releases).

ESI notes: the application is used as a PUBLIC client — the client ID ships
in `config.ESI_CLIENT_ID` and no secret is sent or stored (v1.21); requested
scopes are the eight listed in
§8 (incl. `esi-universe.read_structures.v1` for structure→system resolution
and `esi-markets.structure_markets.v1`, v1.6). JWT
validation needs the `cryptography` package and 120s clock-skew leeway.

Import verification against live data (build 3466501, released 2026-08-13):
52,863 types · 4,968 blueprints (4,848 manufacturing, 120 reaction) · 27,196
blueprint materials (0 skill leaks) · 645,752 type attributes · 8,490 solar
systems. Ground truth checks: Hulk 22544 → Exhumer → Ship; Hulk BP 22545 base
time 240,000 s; Construction Blocks base 150; rig ME −2.0/−2.4; security
modifiers 1.0/1.9/2.1; Jita 0.9459 in The Forge.

### v1.7 (2026-08-20/21): review sprint

A 59-agent adversarial code review confirmed 43 findings (16 Major /
24 Minor / 3 Nit, every arithmetic claim script-reproduced); all were fixed,
its 23 open questions were decided with the user (dated §10 rows), and two
game-mechanics assumptions were corrected by in-client verification.

Correctness fixes (highlights): reaction rigs use their own SDE security
bands (1.0/1.1) with the per-class security field now a High/Low/Null
dropdown; the blacklist finals exemption is order-independent; BOM expansion
merges demand before run rounding (topological, ceil-of-sum); lag costing
uses per-pipeline depth (adding a pipeline no longer shifts existing hulls'
realized cost); low_stock re-keyed to executed runs; SDE re-import is one
transaction; ESI reworked (corp-only stock, delivery-filtered job credits,
per-family corp role fallback, definitive-only location caching, corp job
pagination, scope cull); MILP last-job residual weighting, unpriced
pre-allocation, loud solver failure; settings clamps; atomic run numbers;
persistent session secret; Jita 4-4 price venue; throttle pool-stop.

Model changes: the SDE's maxProductionLimit was a misread (it caps COPY
runs) — the real per-job ceiling for both activities is **30 days of
modified time** with last-run overhang, user-verified (the old 544/272
reaction caps were this rule at the user's Tatara); build savings became a
**vertically-integrated chain cost** (min(buy, build) per input, freight-in,
BPC amortization, unpriced-leaf badges); a **consumption feedback pass**
re-sizes suppliers to the draft allocation's actual draw; **finals never
flip to buy** (pre-allocated ahead of the MILP; shortfalls stay flagged) —
closing the v1.6 candidate item "capacity-limited final ships … instead of
buy-fallback". New: Thukker rig tier (component classes, group-split
magnitudes on 0.1/1.9/0.1 bands); industry_scc_surcharge setting (4%);
multi-cycle jobs net from the slot pool; ESI snapshots pruned.

Docs: the project docs audited claim-by-claim against the code (two
rounds, ~70 corrections); unimplemented ideas are now marked as such
(job_link RESERVED, Phase 8 dormant, skills→slots as player guidance).

Test suite 120 → 163 (read-only production-DB fixtures, fair-value price
model, new test_esi.py / test_sdeimport.py, cross-model chain-cost
consistency, migration and cap anchors).

### v1.8 (2026-08-21/22): UI restyle

Cycle totals + "console" visual system, chosen from two mocked directions
(amber vs teal; teal won). Cool blue-black palette with a teal accent,
Source Sans 3 for UI and JetBrains Mono for every figure; sticky nav with
brand mark, pill active state and a freshness cluster (ESI / price-cache
age — amber past 24 h — SDE build, corp ISK) from a cheap `nav_status()`
context processor; tables get zebra rows, sticky mono headers, permanent
sort chevrons, neg/top row stripes; badges are outlined labels with
filled red reserved for the two act-now alerts (exceeds wallets, negative
margin); dense tables show abbreviated ISK (`isk_short`, full on hover).

New: `costing.cycle_totals` whole-cycle roll-up (profit, margin %, cost,
proceeds, hulls across N pipelines; unquoted finals excluded and badged)
rendered as a totals strip on both profit views; the per-run Profit tab
uses the Profit page's sortable table + breakdown dialog instead of 26
stacked cards; column order Ship · Cost · Sell · Hulls · Margin/hull ·
Margin % · Margin/cycle · tags · breakdown (breakdown anchored far right,
tag column absorbs slack). Run Plan/Chain tabs: pagehead with Mark
executed / Reopen as real confirmed buttons, totals strip (buy total,
slots used / pool, unmet, low stock; chain status counts), content-sized
tables — `layoutAligned()` sizes the Item column to the page's longest
item name and equalises all aligned tables by flexing the trailing badge
column. Dashboard: status pills + actions in the header. Settings:
two-column page (sticky section side-nav, label/input field grid with
descriptions under labels, prose folded into details, sticky save bar);
input names and form actions unchanged.

Brand: the nav wordmark reads **M.A.G.O.O.** over the tagline *Mostly
Accurate Guesses On Output?!?*.

Test suite 163 → 166 (cycle_totals).

### v1.9 (2026-08-22): structures scope

Upwell structures, Standup rigs/modules and structure components enter the
planning and profit scope. Scouted first (six read-only agents + a critic
over the code and the live SDE): pasting a structure already worked, the
sub-capital pricing path already applied, and every finals rule is
category-agnostic — so the work was a class, a skill, pricing edges,
stock scoping, sections and wording rather than engine surgery.

- **Item class `structures`** (categories 65/66 + group 536 — the
  Upwell/Standup/component subset of CCP rig filter 12): `classify_item`
  branch, settings row seeded as a copy of the user's `other` row on
  existing databases (never NPC defaults), Thukker tier allowed (XL Thukker
  covers structures at the standard −2.0 leg).
- **Outpost Construction** skill level (`skill_outpost_construction`),
  routed by exact name; proven by an Astrahus test (0.80 × 0.85 × 0.95,
  zero-Outpost → 0.68, zero-Science unchanged).
- **Pricing**: sub-capital model for all three sets (pinned); Keepstar,
  Palatine Keepstar and Sotiyo freight-out exempt via
  `costing.freight_out_exempt` at both net-proceeds call sites; panel
  renamed "Sub-capital & structure pricing".
- **NPC-goods region fallback**: `market._order_prices` returns hub and
  region-wide bests from one pull; `refresh_prices(fallback_type_ids=raw
  leaves, fallback_region_id=npc_goods_region_id)` caches a region-wide
  quote with `market_price.hub = 0` when the hub has no order (one extra
  pull only when the regions differ); the engine persists
  `index_run_item.price_region_wide` at plan time (Snapshot.region_wide) so
  the Buy list badges the price it shows, `region_wide_types` feeds the
  Profit breakdown lines and a "N region-priced" tag per product at today's
  prices; the refresh flash counts them.
- **ESI stock**: `count_fitted_stock` (default off) excludes module / rig /
  subsystem / service / fuel / core / drone / fighter slot and bay assets
  and singletons deployed in space (own location a solar system — anchored
  Upwell structures, sov hubs, POCOs, starbases — plus any category-65
  singleton) inside `_aggregate_by_system`; items stored inside an excluded
  structure still count — same corp-assets pull, no new endpoint.
- **Run tabs**: `_display_category` collapses category 65 to "Upwell
  Structures" and 66 to "Structure Rigs & Modules" (106 EVE groups
  otherwise); structure components are a **Structure Components** category
  group inside Manufacturing on Plan, Chain and the Slot Planner (since
  2026-09-01 — v1.9 gave them their own `<h2>`), with a "Structure
  components bought" sub-table on Plan that mirrors the Buy list, and strip
  stats; manufacturing slot totals sum over every built row; category ranks
  renumbered (ships 0–3, structures 4–5, reactions 10–13, unranked 20).
- **Paste**: omitted ME/TE → intermediate defaults for non-ship finals
  (ships keep 0/0); a pipeline's blueprint_setting pin is removed with the
  pipeline; help text covers structures and the flow-mode caveat.
- **Robustness**: `Refdata.type_id` is case-insensitive (exact case wins
  first) and prefers published types; the paste stores the canonical name.
- **Wording**: profit tables read Product / Cost per unit / Units; totals
  strip "Units / cycle"; settings captions.
- Dependencies worth knowing: XL structures are single-run jobs of
  ~330 h (Sotiyo) to ~500 h (Keepstar) at an NPC station with TE20 and
  all-V skills, ~200 h for a Keepstar at the user's Sotiyo/TE-rig/nullsec
  class settings (multi-cycle overhang netting covers them); a Keepstar's
  2,544 components are 13 h/run at the NPC defaults, so the spec default
  24 h window would need ~4,700 component jobs (~1,100 at the user's
  settings) — the scope leans on the user's long window; Keepstar/Sotiyo
  will usually show "no quote" (no Jita 4-4 sell orders), accepted.

Test suite 166 → 202: classify cases, skill legs, class seeding, settings
round-trips, fallback pricing (same-region, other-region, ineligible,
nothing anywhere, hub order reappearing), fitted/deployed exclusion on and
off for structures and ships, freight exemption math, pricing classification
pins, structure planning (Astrahus 24 / Keepstar 33 anchors, exact runs,
shared components, class flow, rig quantities, persisted price provenance),
region-priced costing lines, run-tab label/partition helpers, and Jinja
renders of the Plan / Chain / Profit templates with the new sections (context
processor stubbed — the suite never writes the production database).

### v1.10 (2026-08-22): two-venue buying — Jita vs the C-J6 structure market

Every purchase is a choice — buy in Jita and jump-freight it, or buy at the
C-J6MT Keepstar (much closer, far cheaper hauling). The plan now prices each
bought input at whichever venue is cheaper **landed** and says so.

- **Comparison** (`costing.choose_buy_venue`, pure): hub landed = hub quote
  + `freight_in_isk_per_m3` × packaged m³; structure landed = the
  structure's BEST sell order + `structure_freight_in_isk_per_m3` × m³. The
  structure wins only when strictly cheaper (tie → the deeper hub). Its
  quote is the best order's raw price — best price plus a depth flag, never
  a fill price (user decision) — and `units_cheaper` counts ladder units
  whose own landed price still beats the hub (the whole book when the hub
  has no order). A structure-only quote makes the type *priced* (it can
  flip to buy); no quote anywhere stays unpriced.
- **Depth flag**: the run page flags a structure buy *shallow* when
  `recommended_buy_qty` > `structure_units_cheaper` ("only N of Q units on
  the structure's sell ladder beat the Jita landed price — buy the rest in
  Jita"); the order is never split across venues (future option).
- **Data**: `structure_sell_order` (per-type ascending sell ladder, replaced
  wholesale with the best-price rows on every structure refresh);
  `index_run_item.buy_venue` / `structure_units_cheaper` (plan-time
  provenance, NULL venue on old rows = hub); settings
  `structure_freight_in_isk_per_m3` (0; seeded once as a copy of the Jita
  rate on an existing database — `store._seed_structure_freight_rate`)
  and `structure_buy_enabled` (1). One structure setting serves both jobs
  (`Settings.structure_market()` = `capital_structure()`;
  `structure_market_label()` names it "C-J6" for the preset, "structure
  <id>" for a custom one — every badge, Multibuy heading, freight line and
  dashboard pill uses the label, never a literal).
- **Pull**: the structure block of `/prices/refresh` now runs whenever the
  comparison is on or a capital final exists, wanting capital finals + every
  non-final demand type even when the comparison is off (the whole book
  came down anyway, and switching the comparison on must work from the
  existing cache); flash reports "N/M inputs quoted at the structure
  market"; scope-missing / 403 degrade as before. One filter over the book:
  the sell ladder (buy orders, unwanted types and empty orders dropped);
  the best price is its first rung, so the v1.6 sell quote and the v1.10
  ladder can never disagree.
- **Engine**: `Snapshot.buy_venue` / `structure_units_cheaper`; `prices`
  holds the CHOSEN raw price so every consumer stays venue-agnostic;
  `_chain_coster.buy_cost` lands each bought unit at its venue's rate;
  one `_stamp_price` stamps price, region-wide bit, venue and depth
  together at all three PlanItem sites (the alchemy rows thereby gain
  `price_region_wide`). `market.buy_quotes` excludes the active finals by
  default (their `price_snapshot` doubles as the run page's sell
  reference) and carries the v1.9 region-wide bit per quote — false once
  the structure undercuts the hub, so the "region price" badge follows the
  venue actually used; `market.quote_maps` unzips quotes for the Snapshot
  and the Profit page. The run flash now stamps both caches ("prices as of
  … UTC; C-J6 as of … UTC"). `snapshot_from_state` tests `is not None` (an
  empty-mapping price model is valid).
- **Costing**: `hull_cost` reads the persisted venue and emits up to two
  freight lines ("Inbound freight (Jita)" / "(C-J6)", same `freight_in`
  kind, m³ derived from the material lines by venue); `current_hull_cost(...,
  venues=)` likewise; `CostLine.venue`, `HullCost.structure_priced`.
- **UI**: Buy list (and the Structure Components "Bought" sub-table) gain a
  Venue column (Jita / **C-J6** badge) and `shallow` badges (shared Jinja
  macros; the tooltip reads the row's own depth figure and quantity and
  covers the no-Jita-order case); totals strip "N via C-J6" / "N shallow";
  Multibuy = one block per market; Profit pages badge "N via C-J6" per card
  and `C-J6` per line; dashboard "C-J6" price pill; Settings panel
  "Structure market & capital pricing" (toggle, C-J6 freight-in; Jita
  freight-in relabeled in the subcap panel). 2026-08-23 follow-up: the two
  pricing panels are headed **"High Sec Trade Hub Pricing"** (Jita: sell
  fees for sub-caps / Upwell products, Jita freight in/out) and **"Null Sec
  Trade Hub Pricing"** (the C-J6 structure market: input buying + capital
  sales) — "structure" had meant both a product class and a venue. Same
  day: the v1.9 NPC-goods region merged into the price region — one "Price
  region" input (with the price source) under High Sec Trade Hub Pricing;
  `settings.npc_goods_region_id` dropped, the region-wide fallback is the
  price region's own pull. Also 2026-08-23: sub-capital sales always list at
  an NPC station — the v1.5 player-structure sell venue and its flat broker
  rate are removed (`sell_venue` / `structure_broker_rate` dropped;
  `costing.broker_fee_rate` is the standings formula only); the three
  courier rates are labelled "Courier Highsec Market → Industry Hub",
  "Courier Industry Hub → High Sec Market" and "Courier Null Sec Market →
  Industry Hub". Profit pages: the per-card "N via C-J6" badge is gone;
  instead **Null Sec Market Share** — the structure market's share of
  materials ISK (`HullCost.structure_material_share_pct`) — sits in each
  product's breakdown header, and the cycle strip shows the hulls-weighted
  total (`CycleTotals.structure_share_pct`).
- **Savings against the landed buy price (2026-08-23)**: the MILP /
  negative-savings figure was raw market price − chain cost while the chain
  cost already landed every bought INPUT — so an item's own courier cost
  never counted on the buy side, and with a 900 ISK/m³ high-sec courier a
  2,000 m³ Capital Corporate Hangar Bay read −652k ISK (buy) when landed it
  is +1.15M (build). `_chain_coster` now returns its `buy_cost` leg as well
  and `_build_savings_per_unit` uses it; `index_run_item.unit_chain_cost`
  is persisted so the finals badge stops recovering chain cost as price −
  savings (older rows still do). The Chain tab's Status badge is the plan's
  DECISION (`web._chain_status`: build / react / buy / unmet / covered /
  alchemy, "+buy" on a partly-bought capacity loser) instead of
  "buildable with a deficit ⇒ build", which had contradicted the Plan tab
  for every intermediate the savings rule bought. The Chain tab's "Unmet"
  count is the Plan tab's definition (deficit with no purchase fallback):
  a partly-built starved final or unpriced intermediate keeps its
  build/react status and is badged "+unmet"; the "Alchemy" stat counts both
  the unrefined route rows and the composites they fully cover (a status
  partition — every row counted once).
- **Verified live** (run 46, 2026-08-23 06:25 UTC, both freight rates 0):
  331 input types wanted at C-J6, 276 quoted, 3,066 ladder rows; 12 of 48
  buys via C-J6, 4 shallow — e.g. Titanium Chromide 2,503 vs 4,785 Jita but
  only 7,800 of 398,400 units at or below the Jita price; Tritanium ties at
  3.63 → Jita. Settings round-trip, executed-run Profit tab (pre-v1.10
  rows → one Jita line), Profit page (28 cards badged). Five-angle code
  review (line-by-line, removed behaviour, cross-file, reuse/simplification,
  efficiency/altitude/conventions): no correctness defects; its cleanups
  landed in the same commit. 229 tests (26 new in `tests/test_buy_venue.py`).

### v1.11 (2026-08-23): settings simplification, Null Sec Market Share, landed-price savings, Chain status, table headers

The day-after follow-ups to v1.10, detailed in the "2026-08-23" bullets of
the v1.10 entry above:

- **Settings**: the two pricing panels are
  "High Sec Trade Hub Pricing" (Jita: price region + source, sell-side
  fees, the two high-sec courier rates) and "Null Sec Trade Hub Pricing"
  (the C-J6 structure market: input buying, its courier rate, capital
  sales); one price region (the v1.9 NPC-goods region merged into it and
  dropped); NPC station is the only sell venue (player-structure venue and
  its broker rate dropped); courier rates labelled "Courier Highsec Market →
  Industry Hub", "Courier Industry Hub → High Sec Market", "Courier Null Sec
  Market → Industry Hub".
- **Profit pages**: the per-card "N via C-J6" badge replaced by
  **Null Sec Market Share** — % of materials ISK from the null-sec market,
  per product in the breakdown header and hulls-weighted in the cycle strip.
- **Engine / run tabs**: build savings = **landed** buy price −
  integrated chain cost (the item's own courier-in was missing on the buy
  side; Capital Corporate Hangar Bay −652k → +1.15M → build);
  `index_run_item.unit_chain_cost` persisted; the Chain tab's Status is the
  plan's decision (build / react / buy / alchemy / unmet / covered, "+buy" /
  "+unmet" on partly-covered capacity losers, Unmet count = the Plan tab's).
- **UI**: sticky table headers inside the overflow-x wrappers had
  been pinned over every table's first data row since the v1.8 restyle —
  fixed (page-sticky at normal widths, scrollable wrapper + static headers
  ≤ 1100px).
- Three-angle review of the engine change: no logic defects; wording,
  comments and test-coverage items landed in the same commit. 234 tests.

### v1.12 (2026-08-24): alchemy comparison priced landed

The follow-up the v1.11 entry queued: the alchemy route comparison
(`_alchemy_pass` → `_unit_build_cost`) priced both routes and the recovered
credit at RAW snapshot prices while every other buy decision was landed.
Freight does not cancel between the routes (at 55% yield the unrefined
route hauls ~1.8× the m³ per composite unit), so alchemy was flattered by
~500–1,250 ISK per composite unit at the user's 900 ISK/m³ — enough to flip
marginal routes (run 49: Hexite read +1,304 ISK/unit savings raw but loses
130 ISK/unit landed). Now both routes' materials and the recovered credit
price at the shared `_landed_price` helper (venue raw price + that venue's
courier rate × packaged m³; `_chain_coster.buy_cost` delegates to it), and
the comparison remains single-stage — both routes' inputs are raw goo, so
there is no chain to integrate. New test pins the exact freight delta on
both recorded unit costs from ref data. 235 tests.

### v1.13 (2026-08-24): Planning tab (Profit + Slot Planner)

New top-level **Planning** tab (between Pipelines and Index Runs): today's
estimates, recomputed on every load, **nothing persisted**. Two views:

- **Profit** (default) — the what-if margins at today's cached prices,
  moved here from the old top-level Profit page (cycle totals strip,
  per-ship margin table, cost breakdowns, the price-refresh button). The
  Profit nav entry is gone; `/profit` remains as a redirect for bookmarks.
  Realized costing stays on each executed run's Profit tab.
- **Slot Planner** (`?view=slots`) — the hypothetical index run at steady
  state: every stockpile at target, each stage installing exactly one
  cycle's replacement — answering "how many slots of each kind does the
  line need, and what does a cycle buy?" ESI is ignored entirely (works
  before any pull); **alchemy is assumed off for planning** (decision
  2026-08-24: substitution is an execution-time opportunity, not part of
  the line's baseline shape — the real /run path still substitutes).

`engine.plan_steady_state(conn, ref, snapshot)` leans on the Phase 4
algebra (deficit = target + cycle draw − stock collapses to the draw when
stock sits at target; buffer and composite-extra terms cancel), calling
`plan_index_run(persist=False, alchemy=False)` throughout — every engine
write site sits behind the persist guard. Two corrections the adversarial
review (4 finder dimensions, 2 refuting verifiers per finding, repros
against the live SDE) forced:

- **Built scale, not requested scale** (critical): ship batch multiples /
  BPC caps round finals up (request 6 Hulks at batch 8 → the line builds
  8 every cycle), and the engine's one-pass consumption feedback corrects
  only one BOM level — depth ≥ 2 stages under-replaced by the rounding
  factor (19% short at qty 6; negative at qty 3). The chain now expands at
  the built quantity via an `output_qty` override on
  `plan_index_run`/`_expand_and_merge` (real /run path never passes it),
  replanned to a fixpoint — rounding is idempotent, one extra pass.
- **Purchase margin carried, not re-bought**: Phase 7 targets raws at
  consumption × (1 + margin); a perpetual cycle buys the excess once and
  carries it. Raws are stocked at that excess, so the buy list is exactly
  one cycle's consumption (was a recurring 12.7B ISK / 8.6% overstatement
  of Materials/cycle on production data).

The Slot Planner page: uncapped slot demand vs pools (red over-pool), job
tables grouped by activity/category (allocated numbers, capacity badges),
a priced replacement-materials list with venue/shallow/region badges — no
multibuy, no wallet checks, no low-stock (stock-dependent flags don't
apply to a synthetic snapshot). `web._steady_rows` adapts the live Plan to
the persisted-column row shape the run templates read;
`_group_by_category` lifted to module level and shared with run_detail.

Tests 235 → 257: steady-state invariants at qty 8 / 6 / BPC-capped
(projected stock ≥ one cycle's need at every stage), buffer cancellation,
margin cancellation, alchemy excluded from the steady plan while the real
plan path still substitutes, caller stock/jobs ignored, zero persistence,
capacity flags and uncapped demand under a starved pool, both views'
subnav, template badges and empty states (the moved profit template's
content tests live on against `planning_profit.html`).

### v1.14 (2026-08-25): ESI tab — authed corporations + count toggles

The Characters tab became **ESI**, restructured as two "authed" tables —
**Authed Corporations** first (new), **Authed Characters** below — each a
row of count toggles over what that entity feeds into planning:

- **`esi_corp`** (new state table, upserted every refresh, toggles survive,
  corps pruned when their last member leaves the pool): per-corp
  `count_assets` / `count_wallet` / `count_jobs` — off skips that pull
  entirely — plus pull provenance for the ESI tab's **Corp auth** column:
  which pool character's token answered each endpoint family (roles:
  Director / Factory Manager / Accountant) and the row counts pulled.
- **`pool_character.count_assets`** (default off) opts a character's
  PERSONAL hangars into stock on hand — the 2026-08-20 corp-assets-only
  scope is now user-adjustable from both sides. Tracked-system and
  fitted-stock filtering apply to both asset sources.
- **Corp-feed-first job counting**: the corp feed now runs before the
  character feeds and claims corp jobs in the `job_id` dedup, and every
  corp-feed job counts toward slot occupancy and multi-cycle netting under
  the corp's `count_jobs` toggle — corp ESI carries installer and end
  date, so corp (CEO / Factory Manager) auth alone covers corp-hangar
  jobs. `include_job_slots` gates only the character's remaining personal
  jobs. This makes the page's recommendation sound: in an
  all-corp-hangar operation, character-level toggles can stay off with no
  loss of slot netting.

New routes (`/corps/<id>/toggle/<flag>`, `count_assets` on the character
toggle whitelist); section headers pared to one-line subheadings. Tests
257 → 262: personal-assets opt-in with tracked-system filtering, per-corp
opt-outs skip their fetches (assets / wallet+jobs), corp-feed slot
counting with the character flag off, provenance recording + toggle
survival across refreshes. Verified live: corp table populates
(9,160 asset rows via one token), active-jobs count restored from the
corp feed with the character flag off.

### v1.15 (2026-08-26): Audit fixes — accessibility, narrow viewports, polish

A full technical audit (15/20 — accessibility and responsive layout the
weak dimensions) followed by a fix-everything pass across all twelve
templates. No planning-math changes; one data-truth fix:

- **Dashboard Pool table told the wrong truth**: its "Assets" column
  rendered `include_assets`, which the ESI feed uses to gate the character
  *wallet* pull — it now shows `count_assets` (the actual personal-hangar
  gate) and a separate **Wallet** column shows `include_assets`.
- **Accessibility**: every Settings control programmatically labeled
  (`for`/`id` on all field panels; `aria-label` on the 48 class-table
  controls, add-rows, paste box, and in-table Pipelines inputs); sortable
  headers join the tab order (Enter/Space sorts, `aria-sort` tracks);
  `fill-bad` badge text `#fff` → `#1a0404` (~6.4:1 on Loss Red, WCAG AA);
  the title-attribute ledger became a focus-reachable tooltip layer
  (badges / totals values / pills / nav readouts get `data-tip` + composed
  `aria-label`; Escape dismisses; follows badges into open dialogs); SSO
  button-in-anchor replaced with an `a.btn` style; stale readouts print
  `!` so staleness isn't color-only; `<thead>` on every table.
- **Narrow viewports** (windowed-EVE scene): `.tablewrap` on the six bare
  tables; nav sheds the wordmark subtitle ≤1100px and wraps ≤900px; the
  collapsed settings grid uses `minmax(0, 1fr)` so the class table can't
  prop the page open (found live at 860px); blacklist checks single-column
  ≤700px.
- **Polish**: `fonts.gstatic.com` preconnect; type drift folded to .75rem
  (`.footnote` class replaces inline font-sizes); `td.end` replaces inline
  `text-align`; pipeline delete now confirms, naming the profit-history
  loss. DESIGN.md badge-literal line updated to match.

Tests unchanged at 262; verified in-browser (desktop + 860px) across
every page. Template-macro dedup (job/profit tables duplicated across
run/planning views) noted as a follow-up, not done here.

### v1.16 (2026-08-27): Runs lifecycle, first-run onboarding, deficit ledger

Two UI passes. First: native number-input spinners hidden
app-wide; every page caption and section sub-header rewritten in plain
second person (fee rates moved into the "How this is computed"
disclosures); the one-off-builds disclosure dropped from Pipelines and its
intro corrected (final products: subcap/capital hulls, Upwell structures,
Standup rigs/modules); a **Corporation pool** table under Character pool
on the dashboard (`esi_corp` count flags).

Second, a critique (30/40) + audit (14/20) driven pass:

- **Runs lifecycle**: a non-complete run with any newer run behind it is
  derived **superseded** (never stored); `/runs` defaults to newest
  planned + completed with a show-all toggle (`?all=1`); per-row
  **discard** for never-executed runs (`/runs/<id>/delete` hard-rejects
  completed runs); superseded badges on the dashboard list and all three
  run pageheads.
- **Plan↔Chain bridge**: item names on the run Plan tab open one shared
  deficit dialog rendering the engine's real per-kind equation —
  intermediates: target + planned draw (recovered from the deficit
  identity, since the second sizing pass re-sizes against actual draw) −
  on hand − in jobs; raws: consumption + margin − stock; finals: always
  the full cycle — plus the plan's answer and a "view in chain →" link to
  the `:target`-highlighted chain row (`id="item-<type_id>"`).
- **First-run onboarding**: a fresh database no longer 500s anywhere —
  `sde_ready()` guards on every route/POST touching `ref_*`, graceful
  `Refdata.sde_build()`, SSO-credentials RuntimeError → flash. Six-step
  dashboard checklist (SDE import command, EVE dev app +
  `esi configure`, SSO, ESI update, pipelines, prices) with live-derived
  states, `aria-current`, text done-badges, dismiss (localStorage) and a
  Settings **First-run setup** reopen button (`/?setup=1`); disabled
  action buttons explain their prerequisites.
- **Hardening**: one staleness threshold (24h) shared by nav and
  dashboard pills via the `|stale` filter (nav readout PX → Prices);
  "Clear all pipelines" confirms; "Mark executed" restates the buy total;
  inline qty/BPC edits save via fetch (scroll/sort preserved, green
  2px-underline flash + aria-live "saved", full-submit fallback).
- **Audit fixes**: nav wraps ≤1100px (no body h-scroll at 901–1010px);
  `layoutAligned()` keeps natural table width ≤1100px so badge columns
  never crush (wrapper scrolls) and batches reads before writes; profit
  breakdowns render into inert `<template>`s cloned into ONE shared
  dialog (planning live DOM −97%: 9,395 → 297 nodes); `sortBy`
  precomputes keys; sortable headers became real buttons (aria-hidden
  chevrons); multibuy textareas named; tooltips hoverable (1.4.13);
  `color-scheme: dark`; yes/no text behind ✓/— cells; affordance strokes
  raised to Slate Dim (≥3:1).
- **Template dedup** (the v1.15 follow-up): `_macros.html`
  (venue/shallow/item-cell/job-table, `plan_time`/`execution` flags —
  fixing the "at plan time" drift on the live Planning view),
  `_run_subnav.html`, `_profit_cards.html` (run Profit gains the missing
  unpriced badge; clamped/capacity/low-stock/no-price badges gain their
  ledger titles). DESIGN.md updated with the nine new components, tip
  shadow, and the sanctioned-literal ledger.

Tests 262 → 281 (`tests/test_onboarding.py`: fresh temp-DB walk of every
page and pre-SDE action). Verified live throughout (desktop + 960/800px);
adversarial verification workflows caught and fixed the fresh-install
crashes and the non-summing deficit equation before ship.

### v1.17 (2026-08-28): Audit-fix pass — the 2026-08-27 review implemented

The full-scope 2026-08-27 review (9 section reviewers + adversarial
verification, 40 confirmed findings, artifact on file) implemented in one
pass. An adversarial review of the fix diff itself then caught 8
regressions in the fixes — notably the steady-state seeding interaction
below — all fixed before commit.

- **Engine math**: `_planned_consumption` divmod-splits runs across jobs
  (the old floor division dropped `runs % jobs` whole runs of material
  demand for finals and exact-quantity ships whose last job runs short —
  the review's one systemic numeric bug, found by five reviewers
  independently); base-qty-1 materials floored at 1 unit/run via a shared
  `industry.unit_quantity` in both chain-cost walkers (a qty-1 input was
  charged ~0.85/run, inflating build savings ≈4.8% on a Neurolink
  Protection Cell); buffered-target ceil guarded against float noise
  (100 × 1.1 → 110, not 111); BOM cycle demotion contained to true SCC
  members (iterative Tarjan — an acyclic descendant under a cycle stays
  buildable); MILP call gained a 60s time limit; `reprocess_unrefined`
  conserves ISK when recovered credit exceeds consumed cost; the
  feedback-pass reset no longer orphans alchemy JIT raw rows;
  `current_hull_cost` exempts ALL active pipelines' finals from the
  blacklist, matching Phase 2.
- **Rulings (2026-08-27, now in the Decision Log)**: dual-role finals net
  their cross-pipeline component share against stock (requested share
  keeps the exact ignore-stock rule; `PlanItem.requested_qty` carries the
  split; steady-state drafts seed finals at ZERO stock so the netting
  stays a no-op there — the diff review caught the seed cancelling the
  component share and understating the whole subtree); the Profit card
  stays at requested scale while the Slot Planner keeps the v1.13 built
  scale, documented at the `hulls_per_cycle` site.
- **ESI/market**: SSO redirect derived from the live host (login no
  longer dead-ends off port 5000 or via 127.0.0.1); denied-structure 403s
  re-probe after 24h instead of caching NULL forever (stock at a
  since-granted structure heals); Asset Safety Wrap contents excluded
  from stock; `X-Pages` parsed via `_int_header`; one bad type no longer
  aborts the whole price refresh; a mid-pull 404 keeps collected pages; a
  revoked refresh token raises a clear re-auth error the refresh route
  flashes without the "try again" tail.
- **Web/Flask**: `before_request` Origin/Host guard (cross-site drive-by
  POSTs and DNS rebinding blocked); `debug` gated behind `MAGOO_DEBUG`
  (the shipped launcher serves plain 500s; NOTE: the dev auto-reloader
  now needs `MAGOO_DEBUG=1`); numeric form fields validated with locale
  commas — the settings POST flashes the offending field, inline saves
  return 422 so a bad value shows a red underline instead of a false
  "saved" tick; run complete AND reopen refuse mid-history splices that
  would silently reprice later runs' lagged costs (reopen newest-first),
  and 404 on unknown ids; "database is locked" during an import flashes
  guidance instead of a raw 500; the WAL is checkpointed at teardown with
  a 100ms budget (OneDrive torn-sync mitigation); `run_detail` split into
  five helpers with `_buy_context` shared into `_planning_context`
  (ending the documented mirroring burden); deficit-dialog raw ledger
  sums on pre-JIT runs 8–11, the finals branch renders a netted ledger
  for dual-role finals, the alchemy panel shows the engine's persisted
  per-job-floored output, and the Chain caption states the post-v1.11
  deficit rule.
- **Store/SDE**: migration failures surface (only known-idempotent
  errors swallowed); `Settings` built by keyword from column names (no
  positional transposition); SDE import streams each dataset (~300MB
  transient peak removed, CRC verification preserved); two unused
  indexes, the banned `canFitShipGroup` constants, and the dead
  `filter_matches`/`attribute_default` machinery deleted with their
  misleading comments corrected.

Tests 281 → 308: request-level run-lifecycle coverage via a
`seeded_client` fixture (production SDE attached read-only; the raw-SQL
completion shim's `actual_start` drift is gone), a base-quantity EIV
golden with non-uniform adjusted prices, buffer monotonicity as a real
all-items property, OAuth token-layer tests, and a regression test
pinning each math fix. Verified live (dashboard, run 58 Plan/Chain,
pipelines inline-save 422 path). One refuted finding recorded to prevent
re-litigation: checked-in SDE parse fixtures stay rejected per the
live-SDE-anchor decision.

### v1.18 (2026-08-28): Sizing feedback loop iterated to convergence

Run 59 broke the 2026-08-21 one-pass assumption: heavy multi-tier
catch-up grew the depth-2 consumers' builds in the correction pass, and
their depth-3/4 suppliers — sized against the pre-correction draw —
ended below one cycle's stock (six truthful-but-closable low-stock
flags), while capacity-starved buy flips were priced off the same stale
draw. `plan_index_run` now repeats the correct-and-re-run cycle until
deficits stop moving, capped at max buildable depth + 1 passes (§7):
strict BOM depth layering propagates a correction one tier per pass, and
the cap doubles as the guard where slot contention has no fixed point
(the final allocation stands; its flags stay truthful). At convergence
the persisted deficit satisfies its defining identity against the final
draw, so the run page's deficit dialog inverts it exactly.

Verified on production state read-only: flags 6 → 0, four allocation
passes, 0.03 s plan; a 20-cycle execution simulation (runs 61–80 on a
database copy, adversarially reviewed harness) showed zero low-stock /
capacity / unmet flags with steady state at ~525–540/540 reaction slots
— the reaction pool is the line's standing bottleneck. Adversarial
review of the diff: engine findings refuted, three minor spec/test
findings fixed pre-commit. Tests 308 → 311 (multi-tier catch-up
regression, capacity-starved buy convergence with a strict pass bound,
forced cap-exit termination).

### v1.19 (2026-08-29): Dashboard game-data download button

First-run setup step 1 traded its CLI instruction for a ↓ Download game
data button: `run_import` gained a progress callback (CLI prints
unchanged), a daemon-thread `ImportJob` runs one import per process,
and the dashboard drives it via `POST /sde/import` plus a deliberately
DB-free `GET /sde/status` (the app's first JSON endpoint) polled at 1s
into a determinate bar + mono readout (the Game-Data Progress
component). Success self-reloads with the milestone announced across
the reload (sessionStorage handoff); an idle status met mid-poll — the
app restarted and lost the in-memory job — reads "download stopped",
never "up to date". `ensure_schema` now runs once per app per DB path
under a lock, so pages keep serving WAL reads while the import
transaction holds the write lock (probed live: ≤0.36 s mid-import) and
the fresh-install first-request "database is locked" race is gone.
Download temps are per-PID with a tolerant rename (the CLI and the
button may race the same build); a `Thread.start` failure reports error
instead of wedging "running". Pre-SDE flashes and reasons all point at
the button; a stored checklist dismissal is ignored until game data
exists. Adversarial review (5 lenses, 3 refuters per finding): 11
confirmed findings fixed pre-commit, including the `[hidden]` idle-row
leak. Verified live end to end on a fresh scratch DB with a real 99 MB
CCP pull of build 3484357. Tests 311 → 327.

### v1.21 (2026-08-30): Packaged as a desktop application

Magoo became something a player downloads and double-clicks: a PyInstaller
onedir build wrapped in an Inno Setup per-user installer (`{autopf}`,
`PrivilegesRequired=lowest`, so no UAC prompt) and a portable zip, both from
one tree via `packaging/build.ps1`. `magoo/desktop.py` starts waitress on a
background thread, waits for a new DB-free `GET /magoo/health`, then opens an
Edge WebView2 window through pywebview; a second launch probes that endpoint
and opens another window rather than racing for the port or running a second
server against the same database. Every failure degrades to "open it in your
browser instead" — WebView2 is checked in the registry AND guarded at window
creation, because a missing runtime crashes rather than degrading.

Four defects that only exist in a frozen build, none visible from a checkout:
a `--windowed` build leaves `sys.stdout` as None, so the SDE worker's
progress `print()` would have killed the v1.19 download button on every
install (now `magoo/logsetup.py`, logging to `<data>/logs/magoo.log`, with
`ensure_std_streams` making stray prints harmless); `_persistent_secret`
wrote unguarded from inside `create_app()`, so an unwritable directory killed
startup before any route existed, with no console to say why; `SDE_CACHE_DIR`
and `DB_PATH` derive from `DATA_DIR` at import, so relocating `DATA_DIR`
alone would still have written the 99 MB SDE zip into a read-only install
directory; and the PKCE verifier lived in the Flask session cookie, which
cannot survive a login that finishes in a different browser. User data now
resolves once in `config`: `MAGOO_DATA_DIR`, else a portable marker beside
the exe, else `%LOCALAPPDATA%\Magoo` when frozen, else the checkout's `data/`
unchanged — the last branch is what keeps the test suite reading the
developer's real database.

SSO became a true public client. The application's client id ships in
`config.ESI_CLIENT_ID` (CCP documents it as public; PKCE exists so a native
app can ship without a secret) and the confidential branch is gone entirely —
`_token_request` always sends `client_id` in the body, `save_credentials` and
the `configure`/`login` CLI commands are retired, and any stored secret is
wiped from existing databases on upgrade. Nobody registers a developer
application any more, so first-run setup lost a step (six to five) and
mentions no terminal command at all — which was mandatory, since the retired
CLI was the only way to enter a client id and a packaged build has no
`python`. Login now opens the user's real browser per RFC 8252, the verifier
lives server-side in `web.LoginBroker` keyed by state, and the window learns
the outcome by polling `GET /sso/status`. `redirect_uri` comes from
`config.CALLBACK_URL` instead of the live request host, and the port is
pinned at 8765 everywhere: EVE SSO exact-matches the registered callback, so
a floating port breaks login while everything else keeps working.

Upgrade safety landed before anyone else has data to lose: `PRAGMA
user_version` is stamped after a successful migration and refuses a database
written by a newer build (previously a `no such column` traceback); an
existing database is snapshotted to `backups/magoo-pre-<version>.sqlite`
through SQLite's backup API before its first migration — not a file copy,
since WAL means committed rows can live only in the `-wal` sidecar; and the
Thukker `class_setting` rebuild is now one transaction with recovery for
databases a pre-v1.21 crash left half-rebuilt, where the old code committed
an empty table and silently reseeded the user's facilities to defaults.

Also: a notify-and-link update check reading GitHub's `releases.atom` rather
than the REST API (60 requests/hour is per IP, so a corp behind one gateway
would throttle itself), silent on every failure, with per-version dismiss and
an off switch; a version chip in the nav; and `pyproject.toml`, `LICENSE`
(MIT) and `README.md`, the repository having had no dependency manifest at
all. `Magoo.exe --selftest` proves the frozen build solves a known MILP,
loads its templates, opens its database and can verify a JWT — SciPy reaches
HiGHS through a dynamic import PyInstaller cannot see, so that failure would
otherwise surface the first time a user planned a run. Verified end to end on
the packaged build, including a real migration of the live 65 MB database.
Tests 327 → 356.

### v1.23 (2026-09-01): T2/T3 invention, the BPC stockpile Invention tab, review fixes

Invention became a first-class cost input (the v1.22 work, folded into this
stamp). Each pipeline whose final is invention-capable picks a decryptor —
or none — and, for the 63 multi-source finals (T3 relic tiers, the seven
targets with several T1 sources), the source. The choice is MATERIALISED at
config time: `pipeline.runs_per_bpc` and the T2 blueprint's
`blueprint_setting` ME/TE are written from the invention math, the user's
own runs stashed in `manual_runs_per_bpc`, so the planning path never
learned what invention is. Every planned run persists an
`index_run_invention` VINTAGE — the skill-applied chance, invented stats,
per-attempt input prices and fees — that lag costing replays at the
continuous expectation `1 / (P × runs × portion)`; both profit views carry
invention lines instead of the BPC amortization. The SDE import gained a
`ref_invention` table (activity 8 kept out of `ref_blueprint`, whose
products are blueprint types) and the copying/invention structure
bonuses; Build settings gained Invention Lab and Copy Lab rows with a
job-cost rig. Relics are consumed one per attempt and ride the datacores
tuple; their fee base is 2% of the invented blueprint's product EIV,
pending in-client verification.

The BPC stockpile is then the v1.23 model: two settings, T1 and T2 BPC
Buffer (default 400%, 100–1000%), drive a live **Invention** tab that
sizes copy and invention jobs — at the blueprint's max runs, grouped like
manufacturing jobs — from CURRENT BPC stock (T1 copies as "stack minus the
BPO"), in-flight lab jobs (ESI activities 5 and 8 now credit
`in_progress`; labs still occupy no slot pool) and cached prices, with a
buy list, per-venue Multibuy blocks and the Raw Material Buffer applied
like any other bought input. Index runs keep only the cost vintage.

The UI followed: collapsible sections (open by default, per-page
persistence in localStorage) on Planning, Index Runs and Invention only —
the user declined them elsewhere; every job-table header carries items,
slots and value; structure components are a Manufacturing group rather
than their own section; the run pages show no invention information at
all; the Pipelines page edits runs/ME/TE inline when invention is off and
keeps its scroll position; Settings gained a "Stockpile Buffer" panel and
Title Case labels; destructive actions confirm through one in-app
`<dialog>` — embedded browsers answer `window.confirm()` false instantly,
which had silently cancelled "Mark executed"; run captions are sentences.

An xhigh recall-mode code review of the whole arc (10 finder angles, one
verifier per candidate, a gap sweep) produced 15 verified findings and 9
cleanups, all applied the same day and listed in the decision log. The
ones that changed numbers: a slot-starved invention final now still gets
its vintage row (its executed profit view had fallen back to the ignored
manual bpc figure); `invention_probability` rounds to 12 places and the
engine rounds away float noise before every ceil/floor (7 copies at 7/16
had planned 17 attempts); one `costing.resolve_invention` rule makes a
vanished decryptor STALE everywhere instead of silently "no decryptor";
materialised runs/ME/TE are re-derived after every SDE import; copy jobs
on T2/T3 blueprint originals are no longer read as invention attempts.
Schema 5 drops the never-read `copies_needed`/`attempts` vintage columns.
Tests 356 → 437.

### Development environment constraints (historical)

The initial build happened in a sandbox with heavily restricted outbound
network access, so the SDE auto-pull and ESI were written blind against their
documented protocols and verified later on the user's machine. Both
have since run live repeatedly (SDE builds 3466501 and 3470007 imported; ESI
end-to-end incl. corp data), and development now happens locally on the
user's Windows machine against the live database.

---

## 10. Decision Log

| Decision | Choice | Rationale |
|---|---|---|
| Delivery | Standalone application, not an EVE-IPH feature | 32k lines of WinForms UI cannot be built or iterated on Linux; avoids permanent fork divergence |
| Language / stack | Python, Flask, SQLite, SciPy | Runs anywhere, testable in development, strong solver and data ecosystem |
| Reference data source | CCP official SDE (JSONL), auto-pulled | First-party, current, no third-party redistributor |
| yamlloader | Rejected | Last commit Nov 2024, no JSONL support; CCP has replaced the YAML format |
| Fuzzwork dumps | Rejected as production source | Adds a third-party hosting and cadence dependency |
| EVE-IPH database | Development fixture only | Convenient and well-formed, but reintroduces the dependency we left |
| Pipeline scope | Unified / shared | Shared intermediates and pooled slots reflect actual practice |
| BOM storage | Fully dynamic, no snapshot, no overrides | Always current; no sync burden |
| Stockpile targets | Derived × single global buffer % | User sets one number, not per-item targets |
| Asset scope | Global multi-system filter | |
| Costing | ~~Full FIFO lot genealogy~~ Superseded by lag-based costing (v1.5 row, 2026-08-18) — Phase 8 lot machinery dormant | Matches "price when it entered the pipeline" precisely |
| Cost basis | Price snapshot at run time | Avoids wallet-transaction matching |
| Max run duration | Sets runs-per-job and parallel slots needed | One job per slot; no mid-cycle restarts |
| Long jobs (> cycle) | Allowed, span multiple cycles | Excluded via active-job check |
| Slot contention | MILP on build savings | Lowest-margin items get bought instead |
| ME/TE ~~and decrypter~~ | Global per-blueprint; explicit beats ~~ESI~~ the global default (revised 2026-08-20) | Enables planning against future research; the ESI owned-blueprint step and decrypters were superseded by the v1.1 paste model — struck 2026-08-20 |
| ~~Default decrypter~~ | ~~Optimized Augmentation~~ Superseded 2026-08-20: decrypter feature struck from spec ~~(never implemented; invention is out of scope)~~ — reinstated v1.22 as a per-pipeline choice with no default (see "T2 invention" rows below) | |
| Facility assignment | ~~Per (category, activity)~~ → global settings per item class (2026-08-15) | User asserts structure, ME/TE rig tier, security, index, and tax once per class; simpler and matches practice |
| Facility math depth | Structure bonuses from data; rig tier asserted per class | Rig-applicability filtering not needed at planning time |
| Ship batching | Multiples of 8 (configurable), capacity wins; **runs-per-BPC, when set, replaces the multiple** — subcap ships build whole blueprint copies | Revised 2026-08-15 |
| Capital vs. non-capital | **Reinstated 2026-08-15**: capitals (incl. supers/titans), Freighters (513), Jump Freighters (902) build in exact quantities — no batch rounding (`EXACT_QTY_SHIP_GROUPS`) | User builds capitals in exact counts |
| Dual-role finals vs. stock (ruled 2026-08-27) | A final consumed as another pipeline's intermediate nets that component share against on-hand/in-flight stock; the requested share keeps the exact ignore-stock rule. Steady-state drafts seed finals at ZERO stock so the netting stays a no-op there (v1.17) | 20 Covetors on hand shouldn't trigger 13 fresh builds when only 5 are for sale; the exact-requested rule exists for the sale share, not the component share |
| Profit card cycle basis (ruled 2026-08-27) | Requested scale (Units = configured qty); the Slot Planner keeps the v1.13 built scale | "Units" answers what was configured, the materials bill answers what the line consumes — the two views deliberately differ, documented at the `hulls_per_cycle` site |
| Runs per BPC | Per-pipeline input (paste column 3) | Caps runs/job (drives parallel-slot math) and is the batch rounding unit |
| Raw input purchasing | Just-in-time: allocated-job consumption × (1 + margin), net of stock (2026-08-16) | User buys per cycle for the jobs actually planned; no raw stockpile targets |
| Reaction jobs | Always run the full cycle window~~, capped at min(maxProductionLimit, 544, 544×10,800/base_time)~~ — per-job ceiling superseded by the unified 30-day rule (2026-08-21 row); Hybrid Polymers + Molecular-Forged size to deficit instead | Keeps bulk reaction slots saturated; expensive low-volume chains don't overbuild (v1.2/v1.3). The old 544/272 observations were the 30-day rule at the user's Tatara |
| Uniform job runs | Every job of an INTERMEDIATE runs the same count, rounded up; finals/exact-qty ships excepted (2026-08-20 row — their last job runs short) | No short last job for intermediates; overbuild nets off next cycle (v1.2) |
| Production blacklist | Per-category + per-item "buy, don't build"; finals exempt | Mirrors EVE-IPH's blacklist; sub-chains pruned at expansion (v1.3) |
| In-progress jobs | Count toward current stock | Prevents duplicate recommendations |
| Blueprint availability | Assume unlimited copies | Not modelled as a constraint |
| T2 invention chain | ~~Out of scope — BPCs assumed on hand~~ In scope since v1.22 (per-pipeline decryptor choice, computed cost, datacores on the buy list) — but lab slots stay unmodeled: invention/copy jobs contend for no pool and ESI keeps dropping them from slot accounting | Lab slots drop out; only two pools contend |
| Plan drift | ESI is the ledger, plan is advisory | Reconcile after the fact |
| Aggregate ISK/hr reporting | Deferred | Data model supports it when wanted |
| Rig applicability source | `industryModifierSources` + `industryTargetFilters` (2026-08-15) | canFitShipGroup lists structures a rig fits, not products it bonuses; CCP now ships applicability as data |
| Alchemy semantics (v1.4) | Substitute, never add: spare reaction slots only, residual-coverage swaps, gate = direct build cost | Alchemy is a price play (~10× less slot-efficient); output stays sized to the deficit; buy-vs-build remains the MILP's job |
| Alchemy scope (v1.4) | 17 composite routes, data-derived; mineral alchemy excluded | Mineral routes have randomized reprocess outputs (unplannable) and no direct reaction to compare against |
| Alchemy yield (v1.4) | Single user-asserted fraction, default 0.55 | Scrapmetal rules: flat, capped at 55%, rigs never apply; fold reprocessing tax in |
| Unrefined stock (v1.4) | Credits as in-progress (composite + recovered) at the yield | A manual reprocess stands between unrefined items and usable stock; ESI replaces the credit with reality |
| Price refresh (v1.4.2) | Decoupled button + parallel pool; planning reads cache at any age | Planning is 0.04s — the wait was ~336 serial ESI fetches; tokens, not concurrency, are CCP's limit (12k/15min bucket, ~7% used per refresh) |
| Structure bonus source | Read from type attributes via modifier sources | Values verified live (Raitaru 0.99/0.85); ~~hardcoded table in config demoted to sanity checks~~ tables deleted 2026-08-20 — tests pin the SDE-derived values |
| Realized cost model (v1.5) | **Lag-based costing supersedes FIFO lot genealogy**: an input at depth k of a hull delivered at completed run N is priced from the snapshot of the k-th previous *executed* run, clamped to available history | The pipeline advances one stage per executed run, so the lag *is* the vintage; steady state converges to true cost with no job matching, no wallet parsing. Phase 8 lot machinery stays dormant |
| Run execution truth (v1.5) | One user-asserted bit per run: "Mark executed" (reversible) | Planned prices stand in for receipts; abandoned replans stay `planned` and are invisible to costing |
| Sell-side fees (v1.5) | Broker fee from Broker Relations + station-owner standings (NPC venue) or flat structure rate; sales tax from Accounting | Formula constants pending in-client verification, like all industry math |
| Freight (v1.5) | Flat ISK/m³ in (bought materials, packaged volume) and out (finished hull, packaged volume); **no collateral term by design**. v1.6: capital-priced hulls replace freight-out with a fixed per-hull movement cost | User decision 2026-08-18 |
| BPC cost (v1.5) | Per-pipeline all-in ISK per copy, amortized ÷ runs_per_bpc | ~~Invention math stays out of scope; user enters the number~~ v1.22: kept ONLY for non-invention pipelines (bought copies) and as the fallback for a stale invention config; an invention-enabled pipeline computes the figure instead |
| Per-class security (2026-08-20) | High/Low/Null **dropdown** replaces the numeric field; stored capital_ships 2.1 migrates to nullsec; reaction classes offer Low/Null only | Live data showed the field used as a band multiplier; out-of-range statuses silently read as highsec |
| Subcap SCC surcharge (2026-08-20) | The SCC market surcharge applies to sub-capital net proceeds too | The game levies ~1.5% on all sell orders since Apr 2023, not just capitals |
| Price snapshot venue (2026-08-20) | Jita 4-4 station only (location 60003760), not region-wide min sell — the HUB leg; the structure leg (v1.10) deliberately takes its best order plus a depth flag, see "Structure depth rule (2026-08-22)" | A 1-unit scam/stale order in a backwater Forge station must not set cost basis or MILP savings |
| Stock scope (2026-08-20) | on_hand counts **corporation assets only** (tracked systems); personal hangars excluded. Amended 2026-08-25: both ends became toggles on the ESI tab — per-character `count_assets` opts personal hangars IN (default off), per-corp `esi_corp.count_assets` / `count_wallet` / `count_jobs` opt a corp's hangars / ISK / jobs OUT (defaults on; off skips that pull) | Corp hangars are the production stock; personal assets (parked ships, fittings, cargo) are noise — but the user wants the exception to be theirs to make |
| In-progress scope (2026-08-20) | Corp AND personal jobs still credit in-progress output | Personal job output is delivered into corp hangars |
| Multi-cycle slot netting (2026-08-20) | Active jobs whose end date lies beyond the next index run are netted from the slot pool; single-cycle jobs stay un-netted per v1.1. Amended 2026-08-25: the corp feed runs first and claims corp jobs — they count toward slots under the corp's `count_jobs` toggle; `include_job_slots` gates only the character's remaining personal jobs | The v1.1 "pools as entered" premise assumes all jobs deliver before planning; multi-cycle capital jobs violate it. Corp ESI carries installer + end date, so corp auth alone covers corp-hangar jobs |
| Finals never overbuild (2026-08-20) | Uniform round-up is skipped for pipeline finals incl. EXACT_QTY groups — the last job runs short | Finals ignore stock, so overbuild never nets off; also keeps batch/BPC-rounded totals exact (batch multiple is a hard contract) |
| ~~Component run caps (2026-08-20)~~ | ~~maxProductionLimit caps manufacturing runs/job~~ Superseded 2026-08-21: maxProductionLimit is the max licensed runs per blueprint COPY and does not cap manufacturing | In-client verification showed the game accepts far more runs |
| Per-job run ceiling (2026-08-21) | ONE rule for manufacturing AND reactions: runs keep being added while the job's total **modified** time is under 30 days, so the last run may overhang — `ceil(30d / time_per_run)`; a single run over 30 days installs as 1 run. Reaction formulas' maxProductionLimit kept as an extra ceiling where lower (unverified). The earlier verified reaction caps (544, alchemy 272) were this same rule at the user's Tatara (543 runs = 29d 23:21:59, the 544th allowed) | User-verified in client 2026-08-21; supersedes the flat-544/base-time-scaled reaction machinery and the (misread) maxProductionLimit manufacturing cap |
| Negative build savings (2026-08-20) | Buy in BOTH branches (contended and idle slots) | Building above market price — the LANDED price since 2026-08-23 — wastes ISK regardless of slot pressure; removes the one-slot build/buy flip-flop |
| Unpriced inputs in savings (2026-08-20) | Keep MILP weighting; badge the savings figure as incomplete ("N inputs unpriced") in the UI | Ranking-last could starve good builds on a stale price cache; flagging is honest and cheap |
| Alchemy 1-job minimum (2026-08-20) | Kept — a zero-residual swap still installs one alchemy job | Confirmed intended: the token job's output is cheap extra buffer |
| Lag depth semantics (2026-08-20) | Per-pipeline max depth (once the cross-pipeline depth bug is fixed); multi-depth usage within a hull prices at the deepest occurrence | Exact per-depth attribution needs schema the approximation doesn't warrant |
| ESI blueprint ME/TE (2026-08-20) | Resolver chain is explicit → global default; dead fetch_owned_blueprints and the read_blueprints + read_skills scopes are dropped | The ESI step was never wired; scope hygiene at SSO |
| NPC broker floor (2026-08-20) | broker_fee_rate encodes the in-game 1% minimum | Unreachable today but future-proofs the constants |
| ETag caching (2026-08-20) | Confirmed open item, not yet implemented; §8 wording corrected 2026-08-21 | Candidate next step from v1.6 stands |
| Refresh-route errors (2026-08-20) | /esi/refresh and /prices/refresh catch network failures and flash instead of bare 500s | Matches app-wide error surfacing |
| Snapshot pruning (2026-08-20) | Keep every index run (costing history); prune esi_snapshot to the recent few | Old snapshots are superseded the moment a newer one exists |
| SDE test tripwires (2026-08-20) | Pinned counts (78 plan items / 17 alchemy routes) kept; re-baseline on SDE import | Red-after-import is a drift alarm, not brittleness |
| Test reference data (2026-08-20) | Live SDE DB, opened read-only via a shared conftest fixture | Pragmatic; a checked-in fixture subset would drift from the real SDE |
| T3 classification (2026-08-20) | Strategic Cruisers stay in t2_ships (comment the >= so it isn't "fixed" later) | They share the T2-ship facility setup in practice |
| Hardcoded structure tables (2026-08-20) | Deleted — SDE-derived values are the single source of truth | The "demoted to sanity checks" role was never implemented; dead code |
| EIV non-consumed materials (2026-08-20) | On the pending in-client fee-verification checklist | Moot today (zero non-consumed rows in the imported SDE) |
| Thukker rigs (2026-08-21) | Fourth rig tier `thukker`, offered only on the component classes; ME magnitude splits by product group (−3.7% capital groups 873/913, −2.0% plain components) on the Thukker bands 0.1/1.9/0.1 | Lowsec capital-component specialist rigs; product group threads through build_multiplier so one class row prices both legs correctly |
| Build savings model (2026-08-21) | Vertically-integrated chain cost: each stage = install fee + inputs at min(market + inbound freight, own chain cost), raw leaves at market (unpriced ones cost 0 and are counted for a UI badge), finals add BPC amortization; savings stays GROSS of sell fees for the MILP; the finals badge shows net proceeds − chain cost; alchemy comparison stays single-stage. **2026-08-23:** the buy side of the savings figure is the item's LANDED price (raw + its venue's courier rate × m³), matching how its inputs were already landed; `unit_chain_cost` is persisted | The old single-stage figure priced the whole chain below at market and read negative while the Profit page showed real profit (run 41 told the user to buy 19 of 26 finals) |
| Finals never market-bought (2026-08-21) | Pipeline finals are exempt from the negative-savings buy rule; their negative margin surfaces as a badge, not a buy order | Finals are built to SELL — "buy your own product" is never actionable advice |
| Consumption feedback pass (2026-08-21) | After the draft allocation, supplier deficits are re-sized against the draft's ACTUAL planned draw (catch-up consumers, saturating-reaction overshoot, per-job rounding) and Phases 5–7 re-run once; low_stock evaluates the final allocation. **2026-08-28:** iterated to convergence — the correct-and-re-run cycle repeats until deficits stop moving, capped at max buildable depth + 1 passes (a correction propagates one strictly-layered BOM tier per pass; the cap also guards the no-fixed-point case under slot contention, where the final allocation stands and its flags tell the truth) | Phase 4's steady-state merged_min underestimated real draw, leaving truthful-but-unactioned low-stock flags; one pass captured the first-order gap, but run 59's heavy multi-tier catch-up left six deep-chain low-stock flags and stale-draw buy flips the loop now closes — finals keep the exact-requested rule, steady state still converges after one correction |
| Finals never buy (2026-08-21) | Pipeline finals pre-allocate slots ahead of the MILP and are exempt from the Phase-7 buy flip; a starved final stays a flagged unmet build | Buying your own product is never actionable advice — completes the run-41 finals exemption under contention |
| Structures item class (2026-08-22) | One class `structures` = categories 65 + 66 + group 536 — the Upwell/Standup/component subset of CCP rig filter 12; the filter's other members (Starbase 23, Infrastructure Upgrades 39, Sovereignty Structures 40, Fuel Blocks 1136, Skyhooks 4736) stay `other`; Thukker tier allowed (standard leg) | The Structure ME/TE rig family bonuses all three sets identically; a single row lets the user assert that facility separately from Everything Else |
| Structure pricing (2026-08-22) | Sub-capital sell model (hub quote, standings fees, ISK/m³ freight-out on packaged volume); Keepstar, Palatine Keepstar and Sotiyo (800,000 m³) are freight-out exempt; no structure-market routing | The user chose the subcap model; per-m³ hauling of an XL hull is not a real cost, everything smaller is comparable to hulls already accepted |
| Structure components: no force-build (2026-08-22) | Components stay ordinary intermediates under the savings rule; no per-item build/buy override, no new blacklist category | Same rules as everything else; per-item blacklist already exists for the opposite case |
| Structure pipelines stay in flow mode (2026-08-22) | No one-shot semantic; an active structure pipeline re-demands its quantity every cycle and primes feeders on the first run — documented on the Pipelines page; deactivate after the executed run | Keeps one planning model; the cost of the simplification is a user step, not a wrong number |
| Fitted/deployed stock toggle (2026-08-22) | `count_fitted_stock` (default off): ESI assets in module/rig/subsystem/service/fuel/core/drone/fighter slots and bays, and singletons deployed in space (own location a solar system — anchored Upwell structures, sov hubs, POCOs, starbases — plus any category-65 singleton) are excluded from on-hand; items stored inside them still count; no new ESI call (corp assets already carry location_flag / is_singleton / location_type) | Fitted rigs and the station you live in are not stock; fuel in bays was inflating fuel-block stock |
| NPC-goods region fallback (2026-08-22) | A raw leaf (no blueprint) with no hub-station order takes the region-wide best order from the price region (originally a separate `npc_goods_region_id`, merged into `price_region_id` and dropped 2026-08-23 — one "Price region" input under High Sec Trade Hub Pricing; same pull, no extra call); rows carry `market_price.hub = 0`; the engine persists `index_run_item.price_region_wide` at plan time so the run page badges the price it shows ("region price"), while the Profit what-if page badges today's cache | The SDE has no NPC-seeded flag; "unpriced raw leaf" is the observable; keyed off the user's price-region hub filter so Marines/Janitor/moon-drill parts get a real cost |
| Outpost Construction level (2026-08-22) | Own settings column routed by exact name in `_per_bp_skill_level`; Tech I structure rigs and structure components need only Industry, Tech II Standup rigs add T2 science skills (science level) | Was silently riding on the Tech 2 Science level for the 118 blueprints that require it (13 Upwell structures, 63 Standup modules, 41 Standup fighters, the Orbital Skyhook) |
| Non-ship final ME/TE default (2026-08-22) | Omitted ME/TE in the paste: ships 0/0 (unchanged contract), any other product takes `default_intermediate_me/te`; a pipeline's blueprint_setting pin is deleted with the pipeline | A component pasted as a final without ME/TE pinned its blueprint at ME0 inside every structure chain, and the pin outlived the pipeline |
| Type lookup by name (2026-08-22) | `Refdata.type_id` is case-insensitive (exact-case match first), then prefers the published type, then lowest id; the paste stores the canonical name | 'astrahus' should be the Astrahus; 'Azbel' names both the Engineering Complex and an unpublished celestial; rowid order was an accident |
| Two-venue buying (v1.10, 2026-08-22) | Inputs priced at the cheaper LANDED of the Jita hub quote and the structure market's best sell order (price + that venue's flat ISK/m³ freight-in on packaged volume); `price_snapshot` stays the venue's RAW price with freight a separate line; tie → hub; finals excluded (always hub) | Every purchase is a Jita-vs-C-J6 decision; raw price keeps buy totals, Multibuy and lag costing as market prices; finals' price_snapshot is the run page's sell reference |
| Structure depth rule (2026-08-22) | Best price + flag, never a fill price: shallow = units of the structure's sell ladder landing ≤ the Jita landed price < quantity to buy; no order splitting across venues | User choice — a thin cheap order still sets the quote; the flag tells the user to buy the remainder in Jita |
| One structure market (2026-08-22) | The v1.6 capital-pricing structure (C-J6MT preset / custom ID) is also the buy venue; a second flat freight-in rate for its leg | One authed order-book pull serves both; no second structure setting |
| Alchemy comparison landed (2026-08-24) | Both routes' materials and the recovered credit price LANDED (venue raw + that venue's courier rate × packaged m³, the same `_landed_price` leg as build savings); the comparison stays single-stage | Freight does not cancel between the routes — at 55% yield the unrefined route hauls ~1.8× the m³ per composite unit, so raw prices overstated alchemy by ~500–1,250 ISK/unit at 900 ISK/m³ and flipped marginal routes (run 49: Hexite +1,304 raw → −130 landed) |
| T2 invention: materialize at config time (v1.22) | Choosing a decryptor writes the derived values THEN — runs into `pipeline.runs_per_bpc` (the user's own value stashed in `manual_runs_per_bpc` on the OFF→ON edge), invented ME/TE into the T2 blueprint's `blueprint_setting`; disabling restores the stashed runs and the paste-default ME/TE; the paste updates only qty while invention is on | The resolver chain, `_size_jobs` batch rounding and the `bpc_runs_limit` flow stay untouched; the only plan-time addition is the cost math and the buy rows. The stash exists because the manual-BPC fallback reads the live `runs_per_bpc` — without it the toggle silently repriced every pre-invention executed run's realized BPC line (review find, 2026-08-31) |
| T2 invention: stale-config escape (v1.22) | A `use_invention=1` pipeline whose source no longer resolves keeps a reduced control on the Pipelines page (current-stale + Off); the BPC-cost input re-enables since costing falls back to it | The Off POST deliberately runs before the capability check; without the control the materialized runs/ME/TE were unfixable short of deleting the pipeline (and its profit history) |
| T2 invention: capability rule (v1.22) | ~~A final is invention-capable when its manufacturing blueprint has EXACTLY ONE `ref_invention` source; T3 deferred~~ 2026-08-31: capable = AT LEAST one source; multi-source finals (T3 relic tiers, and the seven multi-T1-source T2 targets) store a chosen source (`pipeline.invention_source_blueprint_id`) picked via a source select beside the decryptor; single-source stays auto (column NULL, heals multi→single drift) | Every T2 target has one source in live data (verified, build 3484357); the source picker generalizes to all 63 multi-source targets with one mechanism |
| T3 relic fee base (2026-08-31) | A relic attempt's invention fee base = 2% of the INVENTED blueprint's product manufacturing EIV; copy fee 0 (a relic is consumed outright — no copy job) | Relics have no manufacturing activity, so the T2 base (source's act-1 EIV) is undefined; user decision, pending in-client verification like the other fee constants |
| Relic as consumable-in-JSON (2026-08-31) | The relic rides the `datacores` tuple/JSON as one extra per-attempt triple — no schema change, and the buy-row demand loop, stock netting (wormhole loot), hull-cost replay and run-page Input table all work untouched | One representation for every per-attempt consumable beats a parallel relic column set |
| Subsystem classification (2026-08-31) | Category 32 (T3 subsystems) classifies `t2_ships`, joining the techLevel-3 hulls already there | CCP's "Medium T2 Ships" rig target filter (8) spans category 32 — the user's T2-ship rigs bonus subsystem jobs in game, so pricing them under `other` understated ME/TE bonuses |
| BPC stockpile overbuild (v1.23) | Two settings, `t1_bpc_overbuild` / `t2_bpc_overbuild` (default 400%, clamp 100–1000%): the Invention tab targets ceil(one cycle's copies × mult), netted against BPC stock and in-flight lab jobs | User request 2026-09-01: BPCs stocked like any other input material; the flat-multiplier alternative was rejected for netting |
| Invention tab is live; runs keep cost only (v1.23) | All invention/copy PRODUCTION and purchasing moved to the top-level "Invention" tab — computed from the latest ESI snapshot + price cache on every GET, never persisted (the Planning-page pattern). Index runs persist only the cost vintage (`index_run_invention`; copies/attempts informational) and inject no buy rows. Same day the tab was restructured to the Planning / Index Run shape: totals strip, then sortable tables in the order the work happens — Buy + Multibuy · T1 copy jobs · Invention jobs — the install checklist panel, and a "How this is computed" disclosure; no per-pipeline prose | User decision 2026-09-01: "keep invention/copy costs as part of each index run, but pull out the actual stockpiles and material purchases to an entire new tab… live and based off current stock, not index runs" |
| Lab jobs credit in-progress (v1.23) | ESI activity-8 jobs credit raw attempts under the invented blueprint type; activity-5 jobs credit copies under `blueprint_type_id` (`product_type_id` is optional in ESI); portion forced 1; research 3/4 still dropped; labs still contend for NO slot pool | Amends "T2 invention chain": the in-progress-counts-as-stock invariant now covers BPCs; the engine converts attempts to expected copies at the configured chance |
| T1 BPC stock = stack minus one (v1.23) | `max(0, on_hand[T1 blueprint] − 1)` copies, each assumed to hold the blueprint's `maxProductionLimit` runs: the BPO is assumed to sit with its copy stack in a tracked hangar; assets cannot tell BPO from BPC or runs remaining (only the dropped read_blueprints scopes can) | User decision 2026-09-01 over "whole stack is copies" and re-adding the ESI scopes; stakes are copy fees + checklist workload |
| Copies at max runs (2026-09-01) | Copy jobs are planned at the T1 blueprint's `maxProductionLimit` (`Refdata.max_runs`); the T1 side of the Invention tab is denominated in licensed RUNS (one per attempt), stocked copies count max runs each, and activity-5 jobs credit copies × `licensed_runs`. Invention jobs group the same way — `ceil(attempts / max_runs)` jobs of up to max runs each; relic sources (no T1 blueprint) show plain attempts, their job grouping unmodeled | User request: "when making copies assume the max runs per copy the blueprint allows" and "invention should be the same as copies" — copy fees are per run, so the cost per attempt is unchanged |
| Shared-pool netting (v1.23) | Pipelines sharing a T1 source (Arazu + Lachesis ← Celestis) or an invented blueprint draw stock/in-flight from ONE pool in pipeline order; inputs net once across pipelines | Mirrors the v1.22 shared-datacore rule; double-crediting a copy stack would under-produce |
| Collapsible sections (2026-09-01) | Every h2 section and h3 category group on the data tabs only (Planning, Index Runs Plan/Chain, Invention) is a native `details.section`, open by default, heading-as-summary with a ▾/▸ marker; closed state persists per page family + `data-key` in localStorage (run ids normalised, `?view=` part of the key so Plan and Chain remember separately). Pagehead, totals, alerts, checklist panels, dialogs and methodology disclosures are never sections; Dashboard, Pipelines, Settings and ESI stay static by user ruling | User request: collapse header and sub-header tables on Planning, Index Runs and Invention, default open — the Ops Console's existing `<details>` idiom, no JS framework |
| Section-header stats; structure components nested (2026-09-01) | Every job-table h2/h3 on Plan, Chain and the Slot Planner reads "N items, S slots, X" — X = Σ build qty × unit price (plan-time snapshot on the run tabs, today's on Planning; unpriced rows count 0 and are called out); Raw inputs and the bought sub-table show the buy twin (`_macros` `job_stats` / `buy_stats`, filters `slots` / `build_value` / `buy_value` / `unpriced`). Structure components render as a Manufacturing category group instead of their own `<h2>`; the bought rows keep a sub-table under Manufacturing on Plan; the totals-strip "Structure comps" stat is folded into the Mfg slots stat's sub-line ("· 7 structure comps", built/bought in the tooltip) on Plan and the Slot Planner | User request; the unit price is the snapshot the Buy list prices from, so the section figures reconcile with the Buy total and the strip |
| No invention information inside Index Runs (2026-09-01) | The run Plan tab's "Invention — amortized cost" vintage table is gone (`_invention_context` deleted); `index_run_invention` still persists per run and feeds only the Profit tab's invention cost line; everything else about invention is the Invention tab's | User ruling: index runs are the buy/install worksheet; invention is planned and tracked on its own tab |
| Settings "Stockpile Buffer" panel (2026-09-01) | A new panel between Global and Alchemy (side-nav `#stockpile`) holds the five sizing buffers, relabelled the same day — Intermediate Buffer (`stockpile_buffer`), Raw Material Buffer (`input_purchase_margin`), Composite Reaction Buffer (`composite_reaction_extra_runs`), T1 / T2 BPC Buffer (the overbuilds) — moved out of Global; same form, same field names, no storage change | User request: the buffers are one decision family and were scattered through Global |
| Confirmations via the in-app dialog (2026-09-01) | Every destructive submit (Mark executed, Reopen run, Discard run, Delete pipeline, Clear all pipelines, Remove character) declares `data-confirm` on its form; one shared `<dialog id="confirm">` in base.html intercepts the submit (capture phase), asks, and re-submits via `requestSubmit` on yes. `window.confirm()` is gone from the templates (JS keeps it only as a fallback where `showModal` is missing) | Embedded browsers — the Claude Code Browser pane, any WebView with dialogs suppressed — answer `confirm()` false instantly, silently cancelling the submit: "Mark executed" did nothing there, the same failure that killed the original pipeline delete (§ Pipelines) |
| Review fixes, invention arc (2026-09-01) | 15 confirmed findings + 9 cleanups from the xhigh code review, all applied: the vintage row persists for a slot-starved final too (else hull_cost fell back to the manual bpc line); `invention_probability` rounds to 12 places and the engine's `_ceil`/`_floor` round away float noise (7/0.4375 planned 17 attempts); the Invention tab's buys carry the Raw Material Buffer and the shallow flag; one `costing.resolve_invention` rule makes a vanished decryptor STALE (bpc fallback + Off control) everywhere; `engine.rematerialize_invention` re-derives runs/ME/TE after every SDE import (`ImportJob(on_imported=…)`); ESI skips copy jobs on invented (T2/T3) blueprint originals (they would read as attempts); profit cards label by line kind; `/invention` prices only `invention_type_ids` after its empty-state checks (inactive / stale named as such) and guards an empty price cache; the Chain strip folds structure comps under Manufactured; the enable flash's batch wording respects `EXACT_QTY_SHIP_GROUPS`; pipelines selects `requestSubmit()` (scroll restore) with guarded sessionStorage; strip sub-line reads "N in structure comps"; `prices_refresh` pulls `market_type_ids` (EIV bases go to the adjusted store only); schema 5 drops the never-read `copies_needed`/`attempts`; shared helpers `store.set_blueprint_setting`, `_paste_default_me_te`, `costing.landed_price`, `_bpc_line`, `_price_maps`; `Refdata.max_runs` memoised; `_split_structure_components` returns a pair; decryptor cost line now tested | Recall-mode review; each finding independently verified before fixing |
| Pipelines inline editing (2026-09-01) | Runs/BPC, ME and TE are inline-editable (fetch saves, 422 refusals) whenever invention is OFF — the paste remains the bulk path and writes the same `blueprint_setting` pin; while invention is ON the three cells are read-only with one non-wrapping `inv` badge each, and the edit routes refuse with 422. Full-form submits on the page (invention selects, activate/delete, paste) restore the scroll position after their redirect. Same day: the table became `table.pipelines` — content-packed columns (the `profit` rule), 4rem figure inputs, date-only Created with the timestamp on hover, `mini` row actions in a slack-absorbing column, and a multi-source row's selects stacked so the table fits the page | User feedback: the values were paste-only display cells; the badge wrapped differently per column width; every submit jumped to the top; the 100%-width table with 6.5rem inputs sprawled |
| T2 invention: lab facility (v1.22) | ~~One `invention` class row drives BOTH the invention and copy fees~~ Split 2026-08-31 (user request): separate `invention` and `copying` class rows, each fee with its own activity's structure cost bonus. Amended same day (user request): the lab rows carry a security band and a cost-rig tier like every other class — the tier (in `me_rig`) is the lab's Standup Invention/Copy/Laboratory Optimization rig, a universal −10%/−12% JOB-COST bonus on the engineering bands (verified across M/L/XL); the TE column stays empty (lab time unmodeled). On an existing database `copying` seeds from the `invention` row it used to share (new classes otherwise seed from `other`); costing falls back to the invention row if the copying row is missing | The split exists because copying has its own per-system cost index in game; the cost-rig tier reuses the one-rig-per-class assertion model — no applicability filtering, same as ME/TE rigs |
| T2 invention: expected-value sizing (v1.22) | `attempts = ceil(copies / P)` buys datacores/decryptors at expected quantities, rounded up; per-run cost amortizes at the continuous expectation | The plan is advisory (ESI is the ledger); variance of a binomial across a cycle doesn't warrant a safety-stock model |
| T2 invention: landed invention lines (v1.22) | Datacore/decryptor prices inside the invention cost are landed; the buy-list rows keep venue raw prices like every other buy | Same freight stance as build savings and the alchemy comparison; `_freight_in_lines` only aggregates `material` lines so nothing double-counts |
| Compare-decryptors table removed (2026-08-31) | The Pipelines page keeps only the decryptor dropdown — the per-row nine-option economics table was struck as clutter (user request). The refresh list still covers datacores + all decryptors for every capable pipeline, so switching choices prices immediately; per-option numbers live on the run and profit pages | The comparison was the page's heaviest element (9 costings + ~30 cache reads per capable row per GET) for a decision made once per pipeline |
| T2 invention: lag semantics (v1.22) | The realized view reads the per-run `index_run_invention` snapshot at lag 0; the what-if view computes live. **v1.23:** the replay prices the CONTINUOUS expected consumption (1/(P × runs × portion)) from the vintage row, never `attempts` — production volume belongs to the Invention tab's stockpile, so a 4× overbuild cycle and a stock-covered cycle cost the same per hull | The invention spend happens AT the run that licenses the copies — there is no deeper vintage to walk back to; per-hull cost stays amortized (user decision) |
| T2 invention: skills (v1.22) | One new setting (`skill_encryption`, /40 term); the datacore sciences reuse `skill_starship_engineering` / `skill_science` through the existing `_per_bp_skill_level` name families | The families already route every science skill correctly; only Encryption Methods had no home (and its formula weight differs) |

---

## 11. Open Items

- ~~Capital ship handling~~ — resolved: exact-quantity builds, structure-market
  sell quotes, and a dedicated fee pair all exist (v1.5/v1.6 + 2026-08-15 row).
- ~~Market price source~~ — resolved: ESI throughout (adjusted prices, Jita
  4-4-filtered regional orders, v1.6 structure market).
- **Multi-activity items** — items producible via both manufacturing and
  reaction are not yet disambiguated.
- **System cost indices** — currently user-asserted per item class (the
  v1.22 `invention` and `copying` lab classes included); pulling live
  values from ESI `/industry/systems/` remains unimplemented.
- ~~T3 / relic invention~~ — resolved 2026-08-31: multi-source targets get
  a source picker; relics are consumed per attempt, priced landed, bought
  on the plan.
- ~~In-flight invention jobs~~ — resolved v1.23: activity-5/8 jobs credit
  their output blueprint into in-progress, and the Invention tab nets BPC
  stock and in-flight attempts before sizing; lab jobs still contend for
  no slot pool.
- **Invention fee constants** — the 2% EIV fee base
  (`JOB_FEE_EIV_FRACTION`), the relic fee base (2% of the invented
  blueprint's product manufacturing EIV — decision 2026-08-31), and the
  lab cost indices await in-client verification, like the SCC surcharge.
- **ETag/Expires response caching** — confirmed open (2026-08-20); would
  speed refreshes and cut the error-limit budget draw.
- **Deployment** — how the user runs it day to day (local script, service,
  packaged executable).
- **Aggregate profit reporting** (ISK/hour across pipelines over time) —
  deferred from v1.
