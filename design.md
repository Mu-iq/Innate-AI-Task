# Design note — storefront capture & visualisation

A prospecting engine that finds independent London venues with bare frontages, photographs each one's **real** entrance, composites the client's **actual** planters onto it at the correct scale, and decides on its own whether the result is good enough to send to the owner.

The engineering claim of this submission is not that the images are beautiful. It is that **nothing on the output page was chosen by a human**, and that every accept and reject is a named constant in [`backend/app/config.py`](backend/app/config.py) rather than a judgement someone made by eye. At 5,000 venues a week, that distinction is the whole product.

> **Status of the numbers in this document.** The run-dependent tables (§2.4, §3) are generated straight from the database by `python -m scripts.design_tables`, not typed by hand, so they cannot drift from what the pipeline actually did. They quote run `20260716-230923-0e7fad`; every run is kept and browsable in the live demo.

---

## 1. System overview

Six stages. Each is a separate module exposing a pure, typed function. No stage imports another; [`services/pipeline.py`](backend/app/services/pipeline.py) is the only thing that knows the order exists. Every stage returns **either** its result **or** a structured `Rejection` carrying reasons — never `None`, never a bare `False`.

```
                    ┌─────────────────────────────────────────────┐
                    │  config.py — every threshold, one file       │
                    └─────────────────────────────────────────────┘
                                       │ (read by all)
                                       ▼
  ┌──────────┐   VenueCandidate   ┌──────────┐   Capture    ┌──────────┐
  │    [1]   │───────────────────▶│    [2]   │─────────────▶│    [3]   │
  │ discover │                    │ capture  │              │  assess  │
  │  Places  │                    │ SV meta  │◀── nudge ────│  vision  │
  │  (New)   │                    │ →bearing │   ±25° once  │  0–10    │
  └──────────┘                    │ →static  │              └──────────┘
       │                          │ ↓fallback│                   │
       │ Rejection                │  Places  │                   │ Assessment
       │                          │  Photos  │                   │ (+product choice)
       ▼                          └──────────┘                   ▼
  ┌────────────────────────────────────────────────┐        ┌──────────┐
  │      Supabase: runs · venues · run_results      │        │    [4]   │
  │      + storage bucket (before/after/prompt)     │        │ measure  │
  │      ↳ read back as run history in the UI       │        │ door→px  │
  └────────────────────────────────────────────────┘        └──────────┘
       ▲                                                          │
       │ VenueResult                                              │ Measurement
       │                                                          │ px_per_metre
  ┌──────────┐                    ┌──────────┐                    │
  │    [6]   │◀───── Composite ───│    [5]   │◀───────────────────┘
  │  verify  │                    │composite │
  │ before/  │───── reject ──────▶│ Nano     │  + 3 real product refs
  │  after   │   reasons appended │ Banana 2 │
  │  diff    │   (max 2 attempts) │          │
  └──────────┘                    └──────────┘
```

### What flows between the stages

| Stage | In | Out | Key derived value |
|---|---|---|---|
| [1] discover | areas × categories | `VenueCandidate` | — |
| [2] capture | `VenueCandidate` | `Capture` | `heading_used` (bearing, computed) |
| [3] assess | `Capture` | `Assessment` | `accepted`, `product_slug` |
| [4] measure | `Capture`, product | `Measurement` | `px_per_metre`, `expected_planter_px` |
| [5] composite | all of the above + 3 product refs | `Composite` | the prompt (persisted) |
| [6] verify | `Capture` + `Composite` + product ref | `Verification` | `scale_ratio`, final `verdict` |

### The rule that shapes every stage

**The model reports; the code decides.**

Gemini is never asked "should we accept this venue?" or "is the scale right?" as the operative question. It is asked what it *observes* — is the entrance visible, how bare is the frontage, how many pixels tall is the door, how tall is the rendered vessel. Every accept/reject is then recomputed in Python against a named constant.

This matters for a reason beyond tidiness: a threshold in `config.py` can be quoted, argued with, and tuned. A judgement living inside a model's opinion can only be re-prompted and hoped about. It also means the decision boundary is identical on every run, because all vision calls are made at `temperature=0`.

---

## 2. Venue selection

### 2.1 Source and filters

Google **Places API (New)**, `places:searchText`. Five London areas × three category templates = 15 queries, deduplicated by place ID.

```python
LONDON_AREAS = ("Shoreditch", "Soho", "Islington", "Hackney", "Clapham")
DISCOVERY_QUERY_TEMPLATES = (
    "independent cafe in {area}, London",
    "independent restaurant in {area}, London",
    "hair salon in {area}, London",
)
```

> **Why Places API (New) and not the legacy endpoint.** The legacy `maps/api/place/*` surface is not enabled on keys issued to new customers. Building against it would produce a pipeline that runs on nobody's key but a grandfathered one — a failure that looks like broken code and is actually a dead API.

Four filters, each recording a `Rejection` with its reason:

| Filter | Rule | Why |
|---|---|---|
| Chain blocklist | Name substring against `CHAIN_BLOCKLIST` | Outreach needs an owner who can say yes to a planter. A branch manager cannot. This is a **category** rule, not a curated list of venues we happen to dislike. |
| Indoor context | Name/address contains `INDOOR_CONTEXT_TERMS` (food court, shopping centre, arcade…) | A unit inside a container has no street frontage to dress. Nothing for the product to improve. |
| Business status | `business_status == "OPERATIONAL"` | Don't pitch a closed business. |
| Review floor | `user_ratings_total >= MIN_USER_RATINGS` (5) | Near-zero reviews usually means closed-but-unmarked, relocated, or a ghost kitchen with no frontage. A low bar: it excludes noise, not small shops. |

Survivors are sorted by review count descending. **This is an ordering, not a selection** — it decides who meets the expensive stages first under `MAX_VENUES`; every venue still has to pass the vision gates on its own merit.

### 2.2 The assessment prompt

The full text is in [`services/assess.py`](backend/app/services/assess.py) as `ASSESS_PROMPT`; this is its substance. One call answers both "is this photo usable?" and "is this frontage worth pitching?", because both are read from the same pixels.

```
- "entrance_visible" (boolean): Is the venue's main pedestrian entrance clearly
  visible? False if the image shows an interior, a close-up of food, a logo, a
  sign, a menu, the road, or a building with no identifiable door.

- "framing_usable" (boolean): Is the doorway AND the ground directly in front of
  it both in frame, sharp enough, and unobstructed enough to composite onto?
  False if the entrance is cut off at the frame edge, severely oblique, heavily
  occluded, too dark to read, or the pavement in front of the door isn't visible.

- "frontage_bare_score" (integer 0-10): How BARE is the entrance area?
    10 = completely bare: blank pavement, nothing but hard surface at the door.
    7-9 = essentially bare, perhaps a doormat, a sign, or a bin.
    4-6 = partially dressed: some greenery, a hanging basket, one small pot.
    1-3 = well dressed already: multiple planters, established greenery.
  Score what is at the ENTRANCE, not the wider street. Street trees belonging to
  the council are not the venue's dressing; ignore them.

- "frontage_palette": "dark" | "light" | "warm_brick" | "mixed"
- "people_prominence" (0-10), "obstructions" (string[]), "reject_reasons" (string[])
```

Two details worth defending:

- **"Street trees are not the venue's dressing."** Without that line the model scores a bare shopfront on a leafy street as already-dressed, and the pipeline rejects the best leads in London. The score has to describe what the *venue* controls.
- **`people_prominence` exists for a legal reason, not an aesthetic one.** See §7.

### 2.3 The threshold, and why that number

```python
FRONTAGE_BARE_THRESHOLD = 6   # accept only if bareness >= 6
```

Accept iff `entrance_visible AND framing_usable AND frontage_bare_score >= 6 AND people_prominence < 6`.

**Why 6 and not 5 or 8.** The scale's own definition puts the break at 6/7: at 7–9 the entrance is essentially bare, at 4–6 it is partially dressed. Six is the lowest score that still means "visibly under-dressed" rather than "already has something." Below it the sales pitch collapses — you are telling an owner who already put a bay tree by the door that their entrance looks bare — and the composite has nowhere clean to stand. Above it (8+) we would throw away perfectly good leads whose only sin is a doormat. Six is the boundary of the claim the outreach email actually makes.

The cost of the threshold is asymmetric and that asymmetry sets its direction: a rejected good venue costs one lead from a pool of thousands; an accepted bad venue costs a real business a strange email about their own shopfront. When in doubt, reject.

### 2.4 The funnel

Run `20260716-235319-f88e2c`. Generated from the database by
`python -m scripts.design_tables`, not typed by hand, so these cannot drift from what the pipeline actually did.

| Stage | Remaining | Gate |
|---|---:|---|
| Pulled from Google Places | **285** | Text Search across 5 areas × 3 categories |
| Survived chain / indoor filters | 279 | Name blocklist + container terms |
| Survived status / review filters | 279 | OPERATIONAL, ≥ 5 reviews |
| Entered the paid stages | 12 | Capped by `MAX_VENUES` |
| Frontage photographed | 10 | Street View, or Places Photos fallback |
| Passed vision assessment | 4 | Entrance visible, framing usable, bare enough |
| Scale measured | 4 | Door found and within sanity bounds |
| Composite generated | 3 | Nano Banana returned an image |
| **Accepted** | **3** | **Passed verification — safe to send** |

Every rejection, with the reason the pipeline recorded:

| Venue | Stage | Why it was rejected |
|---|---|---|
| Small square cafe | discover | Not street-facing — address indicates a mall |
| Headmasters Soho / Clapham High St / Clapham Junction | discover | Chain brand — no owner to pitch to |
| Pretty Earth | discover | Chain brand (name matched "pret") |
| Black Sheep Coffee | discover | Chain brand |
| KOZZEE | assess | No entrance or doorway visible; pavement in front of the entrance not visible |
| The Blues Kitchen Shoreditch | assess | The image shows a bar interior — the entrance is not visible |
| Blacklock Soho · Amalfi Ristorante · Fallow · Grasso | assess | Frontage unusable, or already dressed below the bareness threshold |
| **Gloria** | **verify** | **A composite was generated and then refused — see below** |

**The two interesting rows are `Gloria` and `Pretty Earth`.**

**Gloria** is the verification stage earning its place. A planter was generated, and the verifier — given the original and the composite side by side — refused to let it through:

> *"The planters lack realistic contact shadows and appear to float, particularly the left planter which hovers over the window base. The right planter blocks the entrance to the shop. Planter rendered at 143% of the expected size (observed 220 vs 154 expected on a 0-1000 scale, tolerance ±40%)."*

Three independent faults — floating, blocking the door, and 43% oversized — caught on a generation that a human skimming thumbnails would likely have passed. That composite is exactly the one that must never reach a venue owner, and no part of the system except stage 6 could have stopped it.

**Pretty Earth is a false positive — found by reading this table, and since fixed.** It was binned because "Pretty" contains the substring "pret". Reviewing the same table turned up a second of the identical kind: **Small square cafe** was filtered as being inside a mall because "s**mall**" contains "mall". One bug, two victims: a brand name is a *word*, not a character sequence.

`_is_chain` and `_is_indoor_context` now match on word boundaries, with an optional possessive/plural so the list can say "mcdonald" while the shopfront says "McDonald's". Both false positives are pinned by `tests/test_filters.py`, alongside the cases that must still be caught — the risk of tightening a filter is that you loosen it too far, and "Pret A Manger" must still go.

The table above is from the run *before* that fix, and is left as it was: **the rejection log is only evidence if it is allowed to show its own mistakes.** This one was found precisely because the log was written down and read.

The rest read: *no entrance in frame*, *a bar interior*, *a parked van*, *already dressed*. That is a real London street defeating naive capture, and the pipeline noticing unaided. **A hand-curated demo has nothing to put in this table.**

---

## 3. The chosen venues

| Venue | Address | Postcode | Bareness | Source | Planter |
|---|---|---|---:|---|---|
| **L'ETO Soho** | 155 Wardour St, London | **W1F 8WG** | **8/10** | Places photo (fallback) | charcoal drum |
| **Scarlett Green** | 4 Noel St, London | **W1F 8GB** | **7/10** | Street View @ 318° | white tapered |
| **Megan's Clapham Old Town** | 55–57 The Pavement, London | **SW4 0JQ** | **8/10** | Street View @ 80° | charcoal drum |

All three selected with no human involvement: discovered by an area text search, filtered on chain and status, photographed at a heading computed from the nearest panorama's own position, scored for bareness against the threshold of 6, and composited with the planter the palette rule in §5.3 chose. Scarlett Green's shopfront read **dark** → white tapered, for contrast. Megan's and L'ETO read **light**/**mixed** → charcoal drum.

**L'ETO Soho is the fallback chain earning its place.** An earlier run rejected it at assess: *"car in foreground blocks pavement; pedestrians block the entrance area."* No heading fixes that — the obstruction is in the street, not the framing. Step [3] of §4.3 tried the venue's own Google Business photograph instead, and it cleared the same bar at 8/10. **One of the three accepted venues exists only because the pipeline changed source rather than giving up.**

---

## 4. Frontage capture

This is the stage where a prototype quietly fakes it. The naive implementation requests a Street View image at the venue's lat/lng with a guessed heading — and gets a confident, well-exposed photograph of the road, the sky, or the shop opposite. **Nothing downstream can detect this**, because the wrong shopfront is still a shopfront. It will be assessed, measured, composited and verified, and the client will send a stranger's building to a venue owner.

### 4.1 How the heading is derived

Three calls, in this order:

**1. Street View Metadata API** — free and unmetered. It answers two things: whether coverage exists at all, and **where the panorama camera actually stands**. That position is never the venue's own coordinates; it is wherever the survey car was on the road. Calling this first means we never pay for a venue with no coverage, and it is the only way to learn the origin the bearing must be measured *from*.

**2. Compute the bearing** — the standard great-circle initial bearing, **from the panorama camera to the venue**:

```
θ = atan2( sin Δλ · cos φ₂,
           cos φ₁ · sin φ₂ − sin φ₁ · cos φ₂ · cos Δλ )
```

where `φ₁, λ₁` is the panorama camera, `φ₂, λ₂` is the venue, and the result is normalised to `[0, 360)` — Street View rejects a negative heading, and `atan2` returns them natively.

Direction is the entire point. Swap the arguments and you compute the bearing *from the venue to the camera*, which points the camera 180° away — at the shop across the street. [`tests/test_geo.py`](backend/tests/test_geo.py) asserts that reversing the arguments flips the bearing by 180°, and that a nudge across north wraps to 15° rather than 375°.

This and the discovery filters ([`tests/test_filters.py`](backend/tests/test_filters.py)) are the only tested code, on purpose: they are the two places where a wrong answer arrives looking like a correct one. A bad bearing photographs the wrong building and every later stage agrees; a bad filter silently deletes a venue and states a false reason for it. Everything else in this pipeline is a network call or a model judgement, and a unit test on either tells you nothing true.

Earth is treated as a sphere. At ≤ 30m the error against WGS-84 is sub-centimetre — irrelevant next to a 640px frame at 75° fov.

**3. Street View Static**, requested **by panorama ID, not by location.** The heading was computed from *that specific panorama's* position; requesting by location lets Google serve a different, possibly nearer panorama, and a correctly-calculated bearing would then be measured from the wrong origin. This is a subtle way to be wrong and it is worth the extra parameter to be certain.

### 4.2 fov, pitch, size

| Parameter | Value | Why |
|---|---|---|
| `fov` | 75° | Narrow enough that the venue fills the frame; wide enough to keep both door jambs and the pavement in shot on a narrow London street. |
| `pitch` | 8° | Slight upward tilt keeps the fascia and signage in frame from a kerbside camera without cropping away the pavement we composite onto. |
| `size` | 640×640 | The free-tier maximum; above this needs a premium/signed plan. Square keeps door and pavement together. **This is the pipeline's main quality ceiling** — see §8. |
| `source` | `outdoor` | Rejects business-interior panoramas, which are useless here. |

### 4.3 The fallback chain, and what happens when imagery faces the wrong way

Two different failures need two different answers, which is why the chain has two escalations rather than one.

```
[1] Street View metadata
      ├─ ZERO_RESULTS / no pano ..................... ┐
      ├─ pano further than 30 m ..................... ├─→ straight to Places Photos
      ├─ static request fails ....................... │   (no usable panorama exists)
      └─ blank "no imagery" tile .................... ┘
      │  otherwise: bearing → static image
      ▼
    assess
      ├─ accepted ................................... → measure
      ├─ frontage not bare enough ................... → Rejection (no source fixes this)
      └─ framing / entrance unusable
            │
[2]         ├─→ re-shoot the SAME panorama at +25° → assess
            │      accepted? → measure
            │
[3]         └─→ still unusable? → the venue's own Google Business photos → assess
                   accepted? → measure
                   else      → Rejection, persisted with reasons
```

**Why a nudge is not enough, and step [3] exists.** A nudge fixes a door sitting at the edge of the frame. It cannot fix a parked van across the pavement, a lamppost through the doorway, or a survey car that drove past at a raking angle — those obstructions are in the world, not in the framing, and no heading moves them. That is the difference between the two escalations, and in central London it is the difference between one accepted venue and several. The venue's own photographs are usually the frontage, shot deliberately, at eye level, on a clear day.

**Why 30 m.** Beyond roughly a London street's width plus a couple of shopfronts, the frontage occupies too few pixels to composite onto and the viewing angle is too oblique for believable scale. Past that point the honest move is a different source, not a better crop.

**Why 25°, and only once.** About a third of the 75° fov: enough to recentre a door at the frame edge, not so much that we photograph the neighbouring shop. Street View only — a Places photo has no heading to nudge.

**Escalation only ever fires for framing failures.** A frontage that is simply not bare is a fact about the venue, not the photograph. Another angle or another source will not change it, and trying would spend money to reach the same conclusion.

**The blank-tile trap.** The Static API answers `200 OK` with a flat grey "no imagery" tile rather than an error. We test greyscale standard deviation against `BLANK_IMAGE_STDDEV_THRESHOLD` and treat a featureless frame as no coverage — otherwise it sails into assess and burns a vision call to be told there is no door.

### 4.4 The accept/reject bar for a framing

A framing is usable iff the vision model reports `framing_usable AND entrance_visible` — defined in the prompt as: the doorway **and the pavement in front of it** both in frame, not cut off at the edge, not severely oblique, not heavily occluded, not too dark. The pavement clause is not decoration: it is the surface the planter has to stand on, and a frame without it cannot be composited at any quality.

Places Photos are treated as **candidates, not answers**. They are frequently interiors, food shots or logos. They go through exactly the same assess gate and are thrown out by it if they don't show a door. Capture supplies pixels; assess decides usability. No fallback is ever assumed to be good.

---

## 5. Compositing

### 5.1 Why reference-conditioned generation, not cutout-and-paste

Cutout-and-paste is the tempting answer, and it is geometrically honest: the product stays pixel-identical to the reference, which is the single thing we most need to preserve. We rejected it anyway, and the reason is that it looks pasted **every time**:

- The product photos are lit from their own direction; the frontage has its own light. The shadow falls the wrong way.
- They are shot at their own focal length and camera height. The perspective of the vessel's rim disagrees with the perspective of the pavement it is standing on.
- Grain, white balance and colour temperature don't match a Street View frame.

Fixing all of that *is* a relighting-and-perspective problem — which is the problem an image model already solves. So we condition on the real photos and let the model re-render the product into the scene's own light and geometry.

That trade has an obvious cost: a generative model can quietly redesign the product. **This design is only defensible because stage 6 exists.** Generation is cheap and unreliable; the verifier is the control that makes it safe. Without the verifier this would be the wrong choice.

**Which models, and how we know: listing is not permission.**

The Nano Banana 2 identifier appears as both `gemini-3.1-flash-image` and `gemini-3.1-flash-image-preview` depending on whether you are on Vertex AI or the AI Studio developer API, and availability differs per key. Guessing produces 404s that look like broken code.

The first version of this resolver asked `models.list()` and took the first candidate the key reported. That was wrong, and the first live run proved it: **`gemini-2.5-flash` — the original hardcoded vision model — is still returned by `models.list()` on a newly-issued key and answers 404 "This model is no longer available to new users" when called.** Every vision call in the run failed against a model the API had just told us it had.

So both resolvers now probe with a **real request** and take the first candidate that actually answers, caching the result to disk. The listing is still used, but only to *skip* probing models the key cannot see — never to conclude that one works. The vision probe costs a fraction of a penny; the image probe costs one small generation, once, and is worth paying at setup rather than discovering at venue one.

Resolved against the project key:

| Purpose | Resolved to | Notes |
|---|---|---|
| Vision (assess / measure / verify) | **`gemini-3.5-flash`** | `gemini-3-flash-preview` and `gemini-3.1-flash-lite` also answer; `gemini-flash-latest` returned 503; **`gemini-2.5-flash` is retired (404)** |
| Compositing | **`gemini-3.1-flash-image`** (Nano Banana 2) | the preferred model, not the fallback |

Imagen models are visible on the key and deliberately **not** used: deprecated, shut down 2026-08-17.

**Rate limits are a design input, not an error.** The AI Studio free tier allows ~5 requests/minute/model, and this pipeline makes three vision calls per venue back to back — so on a free key a `429` is routine, not exceptional. The client treats it that way: it retries using **the delay the API itself states** (~50s) rather than a guessed backoff, and after the first 429 it **paces every subsequent call** to one per 13s so it stops walking into the same wall. `GEMINI_MIN_INTERVAL_S` sets that floor up front and skips the first forced wait. On a paid key the pacing never engages.

**Errors are not rejections.** A venue skipped because of a quota 429 is not a venue the pipeline rejected — it never formed a view about it. `Rejection.kind` separates `"decision"` from `"error"` at the column level, and the UI renders them in separate sections. Counting infrastructure failures as rejections would credit the system with judgements it never made, which is precisely the dishonesty the rejection log exists to prevent.

### 5.2 The products, and preparing them

Extracted from the brief PDF into [`backend/data/products/`](backend/data/products/):

| Slug | What it actually is | Vessel | Width | Planted |
|---|---|---:|---:|---:|
| `charcoal_drum` | Matte charcoal-black cylindrical drum, smooth seamless finish, no rim lip. Hosta, variegated *Fatsia japonica*, ornamental grasses, trailing eucalyptus. | 0.70 m | 0.75 m | ~1.20 m |
| `corten_column` | Corten weathering-steel square column, rust-orange patina, mitred edges, brushed-metal maker's plaque. Paired with a matching lower cube. | 1.00 m | 0.50 m | ~1.90 m |
| `white_tapered` | Gloss-white tapered square (inverted truncated pyramid), narrow base, fine line-art graphic on the face. Supplied as a matched pair. | 0.65 m | 0.45 m | ~1.05 m |

> **Assumption, stated plainly.** The client supplied no spec sheet. These dimensions are **estimated from the reference photography** against the pavement slabs, doorways and furniture in each shot. They all land inside the brief's stated ~0.8–1.2 m range, but they are the first thing to replace with real figures — they propagate directly into `expected_planter_px` and therefore into both the composite prompt and the verifier's tolerance check.

**Preparation.** The three supplied images are *lifestyle shots, not product plates*. Each contains an entire storefront behind the planter, and `planter_3` has motion-blurred pedestrians walking across the front of it. Handed to an image model as "this is the product", the background reads as part of the instruction — the composite comes back with a Dutch shopfront pasted onto a Shoreditch cafe, and the verifier correctly rejects it as "building altered."

So [`services/products.py`](backend/app/services/products.py) crops each reference to its hero product with one vision call, cached forever after three calls. It is the same automatic operation applied to whatever photography the client supplies — product prep, not venue curation. If the model isn't confident, or proposes a crop under `MIN_PRODUCT_CROP_AREA_FRAC` of the frame, **we keep the full photo**: a wrong crop is worse than no crop, because a planter cut off at the rim teaches the model the product is a bowl.

### 5.3 Which planter goes on which frontage

A real decision the brief leaves open, and picking by hand would be exactly the curation it forbids. The assess call is already looking at the frontage, so it returns `frontage_palette` and a table in config decides:

```python
PRODUCT_MATCH_RULES = {
    "dark":       "white_tapered",   # black/navy shopfront → white reads at distance
    "light":      "charcoal_drum",   # white/pale render → charcoal anchors the entrance
    "warm_brick": "corten_column",   # brick/terracotta → Corten shares the palette
    "mixed":      "charcoal_drum",
}
```

**Rule: contrast against the facade**, because a planter that disappears into the shopfront sells nothing. Zero extra API calls, deterministic, and defensible in one sentence on a call. All three references are still passed to the model on every generation — the tier accepts up to 14 — with the primary first and named explicitly in the prompt.

### 5.4 How scale is derived

A photograph has no scale. The only object in a shopfront photo whose real size we can assume with a straight face is the door.

```python
STANDARD_DOOR_HEIGHT_M = 2.03
px_per_metre        = door_height_px / STANDARD_DOOR_HEIGHT_M
expected_planter_px = px_per_metre * product.body_height_m
```

**Where 2.03 m comes from, and its error bars.** A UK internal door leaf is 1981 mm (BS 4787, the old 6′6″). Add the frame and threshold and a typical doorway opening is ~2.03 m. **The honest error bar is around ±10%**: shopfront doors are not standardised, and glazed commercial entrances run 2.0–2.3 m. A Victorian shopfront in Islington and a new-build unit in Hackney genuinely differ.

That error is *why* `SCALE_TOLERANCE` is 40% and not 10% — see §6.2. We are not pretending to a precision we don't have; we are choosing an anchor whose error is bounded and known, and then setting the downstream tolerance wide enough to absorb it.

**Gemini reports coordinates on a 0-1000 grid, not in pixels.**

This is the model's trained convention and it holds regardless of what the prompt asks for — told to report pixels against a 640×640 frame, it still answers on the normalised grid, and the `1000` ceiling in a `placement_zones` array is the tell.

Reading those numbers as pixels is silently catastrophic. A door reported as `475` is not 475 px of a 640 px frame — it is 475/1000, i.e. **304 px**. Taken literally it yields 234 px/m instead of 150, and every planter renders **56% too large**, with the verifier comparing against the same inflated expectation and agreeing. Nothing downstream could catch it.

So the pipeline adopts the model's convention rather than fighting it: the prompt asks for 0-1000 explicitly, `measure.py` converts to real pixels, and — the part that matters — **the sanity bounds run in the grid space the value was reported in**, not against a pixel dimension it was never measured against. `MeasurementRaw` is normalised, `Measurement` is pixels, and only `measure.py` sits between them.

The same convention governs two other places: the product-plate cropper converts before cropping, and the verifier compares scale as a **fraction of image height** rather than absolute pixels — which also makes it immune to the image model returning a composite at a different resolution than the frontage it was given.

**Two deliberate choices:**

- **The model is asked only for positions, never for metres.** Where the door is, how tall it is on the grid, where the ground line is. Every metre-denominated number is computed in Python. Asking a vision model "how tall is this in metres?" invites a hallucinated figure we would then have to defend on a call; asking "where is the door in this image?" is a question about what it can actually see.
- **We scale on the vessel, never the planting.** The palm in `planter_2` is ~2 m and the drum's foliage is seasonal, but the vessel is a manufactured constant. It gives the verifier one unambiguous number to check.

**The measurement is sanity-checked before it is trusted.** A bad door height corrupts everything silently: the prompt asks for the wrong size, and the verifier then checks against that *same wrong size* and happily agrees. So `MIN_DOOR_HEIGHT_FRAC` (0.15) and `MAX_DOOR_HEIGHT_FRAC` (0.85) reject a "door" occupying 5% or 95% of the frame — that is a hallucination or the whole shopfront, not a door. Caught here or not at all.

### 5.5 How faithfulness is enforced

Three mechanisms, in increasing order of how much I trust them:

1. **Conditioning** — all three real product images are passed on every call, primary first.
2. **Instruction** — the prompt names the product from `PRODUCT_SPECS[].description`, states the references are the exact physical products and not inspiration, and explicitly permits re-lighting and re-angling while forbidding redesign.
3. **Verification** — stage 6 receives the product reference *alongside* the before/after pair and must affirm `product_faithful_to_reference`. This is the one that actually holds.

### 5.6 The full composite prompt

Assembled by `build_prompt()` in [`services/composite.py`](backend/app/services/composite.py) — pure and deterministic, which is what makes the cache key meaningful. Values in `{}` are substituted per venue.

```
You are editing a real photograph of a real business frontage to show the owner
how the entrance would look with professionally installed outdoor planters.

IMAGE 1 is the frontage photograph. This is the image you are editing.
IMAGES 2-4 are photographs of the EXACT physical products to be installed. They
are a real manufacturer's actual products, not inspiration.

THE PRODUCT TO PLACE — this is IMAGE 2:
{product.description}

Reproduce that product faithfully. Its shape, proportions, colour, finish,
material and planting must match IMAGE 2. You may re-light it to match the scene
and you may show it from the angle the scene requires — but you must not redesign
it. Do not substitute a generic planter. Do not change its colour or material. Do
not invent a different plant scheme. If IMAGE 2 shows a matched pair, place a
matched pair.

SCALE — this is the most important instruction:
The doorway in IMAGE 1 is {door_height_px} pixels tall and is a standard 2.03m
commercial door, which sets the scale of the scene at approximately
{px_per_metre} pixels per metre.
The planter's vessel is {body_height_m}m tall in real life.
Therefore the planter's vessel — the container alone, from its base to its rim,
NOT including the plants growing out of it — must be rendered approximately
{expected_planter_px} pixels tall in the output image. The foliage will extend
above that.
Getting this wrong is the most common failure. Measure it against the door: the
vessel should stand roughly {ratio}% of the door's height.

PLACEMENT:
The ground plane at the entrance is at y = {ground_line_y} pixels from the top of
the image. The planter's base must sit ON that ground line, in contact with the
pavement, not floating and not sunk into it.
Place the planter on the pavement beside the entrance, against or near the facade.
Do NOT block the doorway. Do NOT stand it in the door opening. Leave a clear,
walkable path through the entrance — this is a real business that real customers,
including wheelchair users, must be able to walk into.
Suitable clear pavement areas, best first: {placement_zones}

LIGHT AND SHADOW:
The scene's light comes {light_direction}.
Give the planter a natural contact shadow consistent with that light and with the
other shadows already in IMAGE 1. The shadow must fall in the same direction as
the existing shadows in the scene, be soft or hard to the same degree, and anchor
the planter to the ground. A planter with no contact shadow reads as pasted on
and is unusable.

DO NOT ALTER ANYTHING ELSE — this is a photograph of a real business:
- Do not change the building, its brickwork, render, paint, or architecture.
- Do not change, move, redraw, translate or "improve" any signage, lettering,
  logo or house number. The business's name must read exactly as it does in IMAGE 1.
- Do not change the windows, the door, the door furniture, or what is visible
  through the glass.
- Do not add, remove, move or alter people, vehicles, bicycles, bins, or street
  furniture.
- Do not change the road, the pavement surface, the kerb, the sky, the weather,
  or the time of day.
- Do not restyle, colour-grade, sharpen, or beautify the photograph.
- Do not add any text, watermark, logo or caption.

The ONLY difference between IMAGE 1 and your output must be that the planter is
now standing there, with its shadow. Everything else must be pixel-for-pixel the
original photograph.

Output the edited photograph.
```

On a retry, the verifier's own reject reasons are appended verbatim:

```
CORRECTING A PREVIOUS FAILED ATTEMPT:
Your previous attempt at this exact edit was rejected by an automated quality
check for these specific reasons:
  - {reason}
  - {reason}
Fix every one of those faults. Keep everything else about the brief above identical.
```

The prompt is saved next to every generated image in `outputs/`. A composite whose prompt we cannot reproduce is not defensible.

---

## 6. Rejection criteria

### 6.1 The conditions under which a generation never reaches a venue owner

Stage 6 receives **three** images — the original frontage, the composite, and the product reference — and diffs them. A generation is rejected if **any** of these is true:

| Condition | Why it is fatal |
|---|---|
| `building_unaltered == false` | The model changed the building, the signage, the windows, the street, the people, or the sky. We are sending someone a doctored photograph of their own business. Image models rewrite shop signage constantly, which is why the prompt makes the verifier read the sign letter by letter. |
| `product_faithful_to_reference == false` | The planter drifted from the client's actual product. The entire pitch is "here is *our* planter on *your* shop." A generic planter makes the email a lie. |
| `planter_blocks_entrance == true` | A planter in the doorway is unusable regardless of how good it looks — and it is an accessibility problem being proposed to a business with a legal duty. |
| `grounded_with_shadow == false` | No contact shadow, floating, sunk into the pavement, or a shadow contradicting the scene's light. Reads as pasted on. |
| `scale_plausible == false` | Doesn't read as a real object of that size in that scene. |
| scale outside `SCALE_TOLERANCE` | Our arithmetic, not the model's opinion. See below. |
| **verification could not be completed** | We cannot confirm the image is safe to send, therefore it is not safe to send. Ambiguity rejects. |

**This is not theoretical.** In the run quoted in §2.4, `Gloria` produced a composite and the verifier refused it on three of those conditions at once — floating with no contact shadow, blocking the entrance, and rendered at 143% of the expected size (220 vs 154 on the 0-1000 scale, against a ±40% tolerance). Two of the three are judgements; the third is arithmetic we did ourselves. The image was never published, and the reasons are in the rejection log.

### 6.2 The scale check is arithmetic, not opinion

The model is asked for `observed_planter_height_px` — a pixel observation. We compute the verdict:

```python
scale_ratio = observed_planter_height_px / expected_planter_px
scale_within_tolerance = abs(scale_ratio - 1.0) <= SCALE_TOLERANCE   # 0.40
```

**Why 40%, which sounds far too loose.** It is deliberately loose, and the looseness is load-bearing. It has to absorb two stacked errors that are already in the system before the generator does anything: the ~10% uncertainty in the 2.03 m door assumption (§5.4), and vision-model bounding-box noise on top of it. A 10% tolerance would reject *correct* composites for errors we introduced ourselves, and the pipeline would fail closed on everything.

Meanwhile it still catches the failure that actually matters. The real generative failure mode is not "8% too small" — it is a planter rendered at doll size or skip size, which is off by 2× or more. A 40% band catches all of those while surviving our own measurement error. **The tolerance is set by the precision we honestly have, not by the precision we would like.**

### 6.3 The verification loop

```
composite(attempt=1)
    └─▶ verify ──accept──▶ VenueResult ✓
            │
          reject
            │  reasons appended verbatim to the prompt
            ▼
composite(attempt=2)
    └─▶ verify ──accept──▶ VenueResult ✓
            │
          reject
            ▼
        Rejection persisted, venue abandoned, move on
```

`MAX_COMPOSITE_ATTEMPTS = 2`. **Why not three.** A third attempt is a billed generation for a model that has already failed the same frontage twice, with the same references and corrections. That money buys more expected leads spent on the next venue. One or two rejected attempts before falling back is a good outcome; the pipeline is graded on the decision being automated, not on it being flawless.

Both retry loops live in the orchestrator rather than inside a stage, and neither can loop: capture cannot own the framing retry because capture doesn't know the photo was bad, and assess cannot own it because assess doesn't take photographs. Same shape for composite/verify.

### 6.4 A note on the verifier grading its own homework

The verifier and the composer are both Gemini, and the verifier is judging a generation from the same family of models. That is a real weakness and worth naming rather than hiding.

Three things blunt it: the verifier is a **different model** (`gemini-2.5-flash`, not the image model) doing a **different task** (comparison, not generation); it is given the original as ground truth rather than asked to judge the composite alone; and its verdict is **not what we act on** — we recompute the decision from constants, and when the model says "accept" and the rules say "reject", the rules win and the disagreement is logged. In practice that disagreement is almost always the model being lenient about its own output, which is exactly the bias you would predict.

The genuinely robust version is a non-generative check — a structural diff (SSIM/edge-map) over the region *outside* the planter's bounding box to catch building alteration mechanically. That is the first thing I would add with more time; see §8.

---

## 7. Imagery rights

They asked for a position, so here is one.

**The photograph is not the problem. The redistribution is.** Photographing a building's exterior from the public highway is generally lawful in the UK — there is no general right against being photographed from a public place, and no architectural copyright issue for a building's exterior (CDPA s.62 permits it). If the client sent a junior out with a phone to shoot 5,000 shopfronts from the pavement, the legal position would be substantially cleaner than what this prototype does. That fact is worth sitting with, because it locates the actual risk precisely.

**The actual risk is Google's terms, not the building owner's rights.** Street View imagery is licensed for display *through Google's APIs*, under the Google Maps Platform Terms of Service. Those terms restrict caching, restrict redistribution outside the API surface, and require attribution. This pipeline does three things they were not written for: it **caches tiles to disk** (§ deliberately, for cost), it **modifies them** (that is the entire product), and it **redistributes the modified result outside a Google map** as commercial outreach material. Using Street View as the base layer for marketing collateral at scale sits outside standard usage. Not a grey area worth arguing on a call — outside.

**UK GDPR is engaged, but is not the binding constraint.** Faces and number plates are personal data. Google blurs them, which handles most of it. The pipeline still rejects frames where people are prominent (`PEOPLE_PROMINENCE_THRESHOLD = 6`) — partly because blurring is imperfect, mostly because a stranger in the shot makes the outreach image worse anyway. Belt and braces on a risk that is real but secondary.

**The separate one nobody asks about: depicting an identifiable business in marketing material without consent.** The composite shows a named, findable business, with its signage legible, altered to show products it has not bought. That is a commercial depiction of an identifiable third party. It is fine as a one-to-one pitch to that business's own owner — the depiction is *of* the recipient, which is the point. It is not fine in a portfolio, a case study, an ad, or a deck, and the boundary between those uses is one careless marketing hire wide. That is a policy control, not a technical one, and it should be written down before the first send.

### The position

**For a prototype demonstrating the pipeline, Street View is the right call and I would defend it.** For production at 5,000 venues/week, it is not, and shipping it would be knowingly building on a licence that does not cover the use.

The production path, in order of preference:

1. **A Google Maps Platform commercial agreement** negotiated for this use case. Fastest route to compliant, keeps the coverage advantage, costs money. Talk to Google before scaling, not after.
2. **First-party capture.** A contractor with a phone and a route. Lawful from the highway, no licence question, current imagery (Street View can be years stale — a frontage we assess as bare may have had planters for two years), and higher resolution than the 640×640 free-tier ceiling. Slower and costs more per venue, but it is the only option that is unambiguously clean *and* fixes the quality ceiling at the same time.
3. **The venue's own published photos** — Google Business, their website, their Instagram. Already in the fallback path. Carries its own licensing question (the venue rarely owns its own photography; a hired photographer usually does), and is arguably the *worst* of the three legally despite feeling like the most polite.

**Ranking them honestly: (2) is the correct long-term answer, (1) is the correct next-quarter answer, (3) is a trap that looks like a solution.**

### The architecture already anticipates this

This is not a promise, it is a property of the code you can check. Every downstream stage consumes a `Capture` and knows nothing about where the pixels came from:

```python
def capture_frontage(venue, out_dir, heading_nudge=0.0, attempt=1) -> Capture | Rejection
```

[`services/capture.py`](backend/app/services/capture.py) is the **only** module that imports the Street View client. Swapping to licensed or first-party imagery means reimplementing that one function against the same signature. Assess, measure, composite and verify do not change, do not know, and do not care — they already handle two sources (`streetview` and `places_photo`) through the identical path today, which is the proof that the seam is real and not aspirational. The `image_source` field is recorded per venue precisely so that a future audit can ask "which of these were built on imagery we had the rights to?" and get an answer.

---

## 8. Production notes

### What breaks at 5,000 venues/week

| Breaks | Why | What I'd do |
|---|---|---|
| **The licence** | §7. This is the one that stops the business, not the one that slows it. | Commercial agreement, then first-party capture. |
| **Sequential execution** | The orchestrator is a `for` loop. ~30–60 s/venue × 5,000 = ~50 hours/week of wall clock. | Fan out per venue — the stages are already pure functions with no shared state. A work queue and 20 workers; the code barely changes. This is the easiest big win. |
| **Quota** | 5,000 × (1 Places + 1–2 Street View + 3–4 Gemini + 1–2 image gen) ≈ 40k calls/week. Image generation quota is the binding one. | Negotiate quota; batch discovery weekly and cache it hard (venues don't move). |
| **In-memory run state** | `_RUNS` is a dict. Gone on restart, wrong with >1 worker. Status polling already falls back to the database, so this only affects a run in flight. | Move run state fully into the `runs` table it already writes to. |
| **The open run endpoint** | `POST /api/run` is unauthenticated and spends money. Bounded today (1 concurrent, 3/hr, capped venues) but bounded is not secured, and the rate limiter is per-instance memory. | A signed webhook or a queue with a service token. It is open *because* a reviewer must be able to press it without a credential — that reason expires the moment this stops being a demo. |
| **The 640×640 ceiling** | The free-tier Street View cap is the pipeline's main quality ceiling. Composites are generated from a small, compressed source. | Falls out of first-party capture for free — the licensing fix and the quality fix are the same fix. |
| **Prompt drift on model updates** | The entire quality bar is a prompt against a preview-tier image model. A silent model update changes behaviour with no code change. | Pin model versions; keep a golden set of ~20 frontages and re-run on every model change. The cache makes this nearly free. |
| **Models retiring underneath us** | Not theoretical — `gemini-2.5-flash` was retired for new keys mid-build, while still being listed by the API. | Keep the fallback list current; alert when the primary stops answering rather than silently degrading. |
| **Verifier self-agreement** | §6.4. A model grading another model's output. | Add a non-generative structural diff (SSIM/edge-map outside the planter bbox) as a hard gate before the vision verifier runs. |
| **Stale imagery** | Street View can be years old. We may assess a frontage as bare that has had planters since 2023. | Use the panorama `date` from metadata (already captured) as a freshness filter — a config threshold, ~10 lines. Or first-party capture. |
| **Chain matching is name-based** | Now word-boundary matched (§2.4), so the two known false positives are gone — but it is still a name blocklist, and a chain that isn't on it walks straight through. | A Places `types` / chain signal, or a brand dataset. The list is a stopgap that will not scale to every chain in London. |
| **Product dimensions are estimates** | `PRODUCT_SPECS` heights were read off the client's photos, not a spec sheet. They set the expected planter size, so a wrong figure biases every composite *and* the verifier that checks it. | Ask the client for real dimensions. This is the highest-value correction available and the cheapest. |
| **Rate limits shape the run** | On a free Gemini key the pipeline paces itself to ~1 call/13s, making a run minutes long. | Billing. The pacing floor drops to 0 and the run is bounded by generation latency instead. |

### Cost per venue

**The pipeline measures this itself.** Every run counts the billable calls it actually made — cache hits are free and are not counted — and captures exact token usage from each Gemini response. That is multiplied by the rates in `config.py`, saved to the `runs` table, and shown on the run in the UI. So the numbers below are the model, not a guess, and the real figure is on screen after every run.

Unit prices (mid-2026):

| Item | Unit price |
|---|---|
| Places Text Search (New) | $0.032 / call |
| Street View **metadata** | free — not counted |
| Street View Static | $0.007 / call |
| `gemini-3.5-flash` vision | $1.50 / 1M in · $9.00 / 1M out (images are tokenised into the input) |
| `gemini-3.1-flash-image` | $0.25 / 1M in · **$0.067 per generated image** at the default 1K (~1MP) output |

A representative run — 8 venues attempted, 3 accepted:

| Line | Cost |
|---|---|
| Places (15 queries, cached after the first run) | $0.48 |
| Street View Static (8 + 1 re-shoot) | $0.06 |
| Vision (18 calls: plates, assess, measure, verify) | $0.09 |
| Image generation (3 composites) | $0.20 |
| **Total** | **≈ $0.84 → ~$0.28 per accepted venue** |

**Image generation is the dominant marginal cost, and discovery is a fixed cost that caches to zero.** A re-run of the same venues costs ~$0.30 — Places and Street View are served from disk. That is why the funnel is ordered as it is: every cheap filter (chain name, business status, review count, blank-tile check, bareness score, door sanity check) runs *before* the one expensive call. Rejecting at discovery is free; rejecting at verify costs a generated image. **The funnel's ordering is a cost-control decision as much as a quality one.**

At 5,000 venues/week, discovery amortises to near-nothing and the bill is essentially vision (~$0.03/venue) plus a generated image for each venue that clears assess. Expect **roughly $300–500/week**, dominated by generation. Against a planter rental contract that is negligible per closed deal — but note the pipeline spends most of it on venues it then rejects, which is correct behaviour and an uncomfortable line item.

### What I would change first, in order

1. **Structural diff before the vision verifier** (§6.4). Cheapest fix for the biggest correctness weakness.
2. **Parallelise the orchestrator.** ~50 hours → ~3.
3. **Replace the estimated `PRODUCT_SPECS` dimensions with real ones** from the client. They propagate into every scale decision and they are currently my estimates off a photograph.
4. **Panorama freshness gate** using the `date` already captured.
5. **First-party capture behind the existing `capture_frontage` seam** — fixes the licence and the resolution ceiling together.

### One deliberate deviation: a database

The brief says "do not add … a database." This adds one — Supabase (Postgres + storage) — and the deviation is considered rather than careless.

The reason is durability. Without it, a run triggered on the deployed backend writes to a container filesystem that resets on restart: the "live" path proves the pipeline runs but keeps no record. For a prototype whose whole point is *automated, defensible, repeatable* decisions, throwing away every run's decision trail is a real loss. Durable history — every run's funnel, every venue's trail, every generated image, browsable and comparable side by side — is a genuine improvement, not gold-plating. It is also what lets §2.4 and §3 of this document be generated from the record rather than typed.

It is built so the deviation stays contained:

- **Isolated.** All SQL lives in `repository.py`; all bucket layout in `storage.py`. No stage imports either — the orchestrator persists, the same way it orchestrates. Swapping Postgres for anything else is one module.
- **Fail-soft.** A run costs money in image generation. Every write logs-and-continues rather than raising, so a database outage degrades the run to "not recorded" instead of losing it.
- **Correct trust boundary.** The backend writes with the `service_role` key; row-level security grants the public `anon` key SELECT and nothing else. Shipping the anon key to a browser is safe by construction, not by convention.

The schema keeps `venues` (facts about London) separate from `run_results` (judgements made at a point in time), so re-running adds history rather than overwriting it — you can ask whether a venue's bareness score changed after a Street View refresh. Rejections carry `kind` (`decision` vs `error`) at the column level, so a quota 429 is never aggregated as a judgement the pipeline never made.

### What I deliberately did *not* build

No auth, no CI, and tests only on the geo maths and the discovery filters. All correct calls for a prototype: those two are where a silent error produces a confident wrong answer no downstream stage can catch — a wrong bearing photographs the wrong building convincingly, and a wrong filter deletes a venue while stating a plausible reason. Everything else here is a network call or a model judgement, and a unit test on either tells you nothing true.

I also did not build a hand-curation step anywhere, which was the point.
