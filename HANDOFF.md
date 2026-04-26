# HW//INDEX

A live hardware price index for consumer GPUs and RAM. The owner runs a local Flask scraper to refresh data; the public sees a static, read-only dashboard published to GitHub Pages.

## How it works

There are two views:

**The owner's view** — `python hardware_index.py` runs a Flask app at `localhost:5000`. Hitting **Refresh Prices** scrapes eBay (UK) for current pricing on 26 GPUs and 12 RAM kits across new and used markets. Hitting **Publish** writes a static mirror of the dashboard to `docs/` and pushes it to GitHub.

**The public view** — whatever was last published, served by GitHub Pages. Same UI, same data, no refresh button. Anyone can view the latest snapshot; only the owner can update it.

This split exists because eBay's bot detection is real, and a free static-only deploy target like Pages can't run scrapers anyway. The owner does the work locally on their own IP and trust score, and Pages just serves the result.

## What's tracked

- **26 GPUs** spanning current-gen flagships (RTX 5090, RX 7900 XTX) down to budget tier (Intel Arc, RTX 3050), weighted toward consumer-friendly mid-range.
- **12 RAM kits** across DDR4 and DDR5, capacities from 16GB to 64GB, speeds from 3200 to 6400 MT/s.
- **Both new and used pricing** — new from active listings sorted price+shipping ascending, used from sold/completed listings (i.e. real clearing price, not asking price).
- **Performance per pound** for GPUs (using approximate 3DMark Time Spy Graphics scores) and **cost per gigabyte** for RAM.
- **Index tab** showing a stock-market-style time series of how the average price-vs-MSRP ratio is moving across each category.

## Run it (owner)

```powershell
pip install -r requirements.txt
python hardware_index.py
```

Open http://localhost:5000, click **Refresh Prices**. First refresh takes 3–4 minutes (rate-limited to avoid eBay's bot detection). When the data looks good, click **Publish** to push the static export to GitHub.

## View it (public)

Visit the GitHub Pages URL configured for this repo. The dashboard shows whatever was last published.

## Configuration

Knobs at the top of `hardware_index.py`:

| Constant | Default | Purpose |
|---|---|---|
| `EBAY_DOMAIN` | `"ebay.co.uk"` | `"ebay.com"` for US listings |
| `WORKERS` | `2` | Concurrent fetchers |
| `DELAY_MIN`, `DELAY_MAX` | `1.5`, `3.0` | Seconds between requests within a worker |
| `RETRY_ON_CHALLENGE` | `True` | Retry once after waiting if eBay returns a captcha |
| `ABORT_AFTER_N_CHALLENGES` | `6` | Bail out of the run if rate-limited this many times consecutively |

## Methodology notes

- **Used median = sold listings only.** eBay's `LH_Sold=1&LH_Complete=1` filter returns completed transactions, so this is real clearing price, not wishful asking price.
- **Outlier trimming.** When n ≥ 8 listings match, the outer 10% are clipped before computing the median — long-tail outliers (broken cards, scam listings, mismatched bundles) are common and pull arithmetic means around badly. Median, not mean.
- **Token matching.** Search terms are normalized (whitespace and hyphens stripped, lowercased) and **all** must appear in the listing title. Exclude keywords filter wrong tiers (Ti, Super), wrong form factors (laptop, mobile, SODIMM), and bundles.
- **Index ratio.** Each item's market median is divided by its MSRP. The "GPU New Index" is the average of these ratios across all GPUs with valid data — a value of 1.07 means new GPUs are trading 7% above MSRP on average.
- **Benchmarks are approximate.** 3DMark Time Spy Graphics scores hardcoded in the catalogue; treat as relative ranking, not absolute precision.
- **Snapshot history is committed to the repo.** Every published refresh becomes a git commit, so the repo log is a complete audit trail of the market over time.

## Not financial advice

Hardware prices are noisy. The signal is in the trend, not any single snapshot. A few refreshes over days/weeks before drawing conclusions about market direction.

---

## First-time deployment (one-off setup)

The pipeline expects a git repo with a remote and GitHub Pages enabled. Once these are in place, the loop is just **Refresh → inspect → Publish** forever.

### 1. Initialise the repo and commit the current code

```powershell
cd C:\Users\Jonny\OneDrive\Desktop\hardware-index
git init -b main
git add .
git commit -m "initial commit"
```

### 2. Create the GitHub repo and push

Pick one — both work, both leave you with the same end state.

**Option A — `gh` CLI (faster):**
```powershell
gh auth login                      # only the first time on this machine
gh repo create hardware-index --public --source=. --remote=origin --push
```

**Option B — web UI:**
1. Go to https://github.com/new, create a repo called `hardware-index` (public — Pages on free accounts requires this), do NOT tick "add README" (the repo is already initialised).
2. Copy the `git remote add origin …` + `git push -u origin main` block GitHub shows you and run it locally.

### 3. Enable GitHub Pages

In the repo on github.com:
- **Settings → Pages**
- **Source:** `Deploy from a branch`
- **Branch:** `main`, **folder:** `/docs`
- Save. The first build kicks off immediately.

The public URL appears at the top of the Pages settings page once the build finishes (~1 min). It's `https://<your-username>.github.io/hardware-index/`.

> The repo includes `docs/.nojekyll` so Pages skips the Jekyll build and serves files verbatim — instant deploys, no Ruby gems involved.

### 4. First Refresh + Publish

```powershell
python hardware_index.py
```

Open http://localhost:5000, click **Refresh Prices**, wait ~3–4 minutes for the catalogue to populate, eyeball the data on the Index tab, then click **Publish**. The Publish button runs `git add docs/ price_history.json && git commit && git push` and surfaces git stderr in the UI on failure.

Wait ~1 minute after Publish for Pages to rebuild, then visit your public URL. You should see the dashboard in read-only mode — Refresh and Publish buttons hidden, "READ-ONLY · UPDATED Xh AGO" pill in the header.

### Troubleshooting

| Symptom in the Publish dialog | Fix |
|---|---|
| `git is not on PATH` | Install Git for Windows from git-scm.com, restart the terminal, restart `python hardware_index.py`. |
| `This directory is not a git repository` | You skipped `git init`. Run section 1 above. |
| `src refspec main does not match any` on first push | You committed but pushing to a remote with no `main`. Run `git push -u origin main` once manually, then Publish works. |
| `Authentication failed` | Re-run `gh auth login`, or set up a credential helper / PAT for HTTPS. |
| Public URL 404s after Publish | Pages takes ~1 min to build. Check the Pages tab in repo settings — there's a build log. |

---

## Pending changes for next session

*No pending tasks.* The previous session's queued items (clean history, MSRP corrections, Index tab restructure, new visualisations, static export pipeline, publish endpoint, GitHub Pages walkthrough) are all complete. Add new items below as they come up.

---

<details>
<summary>Archived task spec (for reference — implemented)</summary>

### Task 1 — Delete `price_history.json` and start clean

The very first scrape run was partially broken (incomplete data, incorrect fields, malformed entries). That poisoned snapshot is now part of the history file and will skew trend lines and index calculations for as long as it sits there. Given we are only a couple of hours into data collection, the cleanest fix is to simply delete `price_history.json` entirely and let the next successful refresh create a fresh, fully-correct file from scratch. Do not attempt to surgically remove the bad snapshot — start over.

**Action:** Delete `price_history.json`. No code changes required. The app will recreate it on the next refresh.

---

### Task 2 — Correct all MSRP values to verified GBP launch prices

The MSRPs currently hardcoded in `hardware_index.py` are incorrect or unverified. Before the next data collection run, Claude Code must web-search and verify the official UK launch price (or closest major-retailer launch RRP) for every item in the catalogue, then update the hardcoded values accordingly. These are the reference baseline for every ratio calculation in the dashboard, so accuracy here matters.

Use the table below as the corrected reference. All figures are GBP at UK launch / official RRP. Items marked `†` should be double-checked as launch pricing was regional or staggered.

#### GPU MSRP reference (GBP)

| GPU | Verified GBP MSRP |
|---|---|
| RTX 5090 | £1,939 |
| RTX 5080 | £1,079 |
| RTX 5070 Ti | £749 |
| RTX 5070 | £589 |
| RTX 4090 | £1,599 |
| RTX 4080 Super | £999 |
| RTX 4070 Ti Super | £799 |
| RTX 4070 Super | £599 |
| RTX 4070 | £589 |
| RTX 4060 Ti 16GB | £439 |
| RTX 4060 Ti | £399 |
| RTX 4060 | £299 |
| RTX 3090 | £1,399 † |
| RTX 3080 Ti | £1,099 † |
| RTX 3080 (10GB) | £649 † |
| RTX 3070 Ti | £529 † |
| RTX 3070 | £469 † |
| RTX 3060 Ti | £369 † |
| RTX 3060 | £299 † |
| RTX 3050 | £229 † |
| RX 7900 XTX | £999 |
| RX 7900 XT | £849 |
| RX 7800 XT | £479 |
| RX 7700 XT | £349 |
| RX 7600 | £259 |
| Intel Arc B580 | £249 |

> `†` — 30-series launch prices are a few years old. Verify against the Nvidia UK press releases or archived Scan/Overclockers launch-day listings if the code currently has something different. The figures above are best-effort from UK launch data.

#### RAM MSRP reference (GBP)

RAM MSRPs are significantly harder to pin down as there is no single official launch RRP and kit pricing has moved materially since release. **Claude Code: for each RAM SKU in the catalogue, web-search the kit name + "launch price UK" or check CamelCamelCamel / archived Overclockers/Scan listings to find the closest verifiable GBP reference price at time of product launch. Do not use current street price as the MSRP baseline — this defeats the purpose of the ratio.**

---

### Task 3 — Restructure the Index dashboard layout

The current Index tab has a layout problem. The top section (the four-card summary dashboard showing GPU new vs MSRP, GPU used, RAM new, RAM used, followed by the Index vs MSRP over time line chart) renders correctly and should not be touched.

Below that, the remaining charts — GPU Tier Index vs MSRP, Top Performance per Pound, and everything down through RAM Lowest Pounds per Gigabyte — are rendering as a weird, partially-sideways stacked table layout that looks broken and is hard to read. These need to be restructured so that the entire page is a single clean vertical scroll, each chart taking full-width (or a sensible two-column grid where pairs of charts are genuinely complementary), matching the visual style of the top section.

**What to preserve:** all the existing charts. Do not remove any of them. Just fix how they're laid out.

**Structural target:**
- Full-width single column, or a clean 2-column CSS grid where it makes sense (e.g. two related bar charts side by side)
- Consistent card/panel styling throughout — same border-radius, shadow, padding as the top summary cards
- Responsive: at narrow viewports everything stacks to single column
- No horizontal overflow, no sideways text, no clipped axes

---

### Task 4 — Append new data visualisations to the Index tab

Once the existing charts are displaying correctly, append the following new charts beneath them. These are derived from data already being collected — no new scraping required. Be creative with presentation but keep the visual language consistent with the rest of the dashboard.

Suggested additions (implement as many as are meaningful given the data available):

1. **New vs Used Price Spread per GPU** — for each GPU, show the gap between new median and used median as a bar or range chart. Highlights which cards are holding value vs depreciating sharply.

2. **Market Premium / Discount Heatmap** — a colour-coded grid (GPU on one axis, latest vs 7-day vs 30-day on the other) showing how far above or below MSRP each item is trading. Red = above, green = below, white = at MSRP. Good at-a-glance view of the whole market.

3. **Price Momentum** — for each GPU, compute the direction of travel: is the new-market median rising, falling, or flat over the last N snapshots? Display as a sorted horizontal bar (positive = rising, negative = falling). Shows where pressure is building.

4. **GPU Generation Comparison** — average price-to-MSRP ratio grouped by generation (30-series, 40-series, 50-series, AMD RDNA3, Intel Arc). Shows whether older cards are above or below launch pricing as a cohort, useful for timing a purchase.

5. **Best Value GPU at Each Budget Tier** — given a set of price bands (e.g. sub-£200, £200–£350, £350–£500, £500–£800, £800+), show the GPU with the highest performance-per-pound currently available in each bracket. Simple ranked list or small bar chart per tier.

6. **RAM DDR4 vs DDR5 Cost per GB** — track the average cost per gigabyte across all DDR4 kits vs all DDR5 kits over time. Shows whether the DDR5 premium is narrowing.

7. **Listing Count Confidence Indicator** — for each item, display how many listings underpinned the latest median. Items with very few listings (< 5) are less reliable. A small bar or dot chart sorted by confidence.

8. **Price Distribution Histogram** — for the most-tracked GPUs (top 5–6 by listing volume), show the distribution of current used asking prices as a histogram. Gives a sense of how tight or wide the market is for that card.

These are suggestions — use judgement on which ones are cleanest to implement given the data structure. Aim for at least 4–5 new charts. Append them below the fixed existing charts; do not interleave.

---

### Task 5 — Full work process after implementation

Once Tasks 1–4 above are complete and verified working at `localhost:5000`, here is the complete process to get back to a clean, running state:

**Step 1 — Delete stale data**
```
del price_history.json
```
Confirm the file is gone. The app will create a fresh one on first successful refresh.

**Step 2 — Verify the MSRP corrections**
Open `hardware_index.py`, locate the hardware catalogue / MSRP dictionary, and confirm all GPU values match the GBP reference table in Task 2 above. For RAM, verify each kit's MSRP against a quick web search for UK launch pricing. Commit this change before running any refresh.

**Step 3 — Test the dashboard layout locally**
```
python hardware_index.py
```
Open `http://localhost:5000` and navigate to the Index tab. Confirm:
- Top summary cards and the main Index vs MSRP line chart render correctly (unchanged)
- GPU Tier Index vs MSRP and all charts below it display in clean vertical/grid layout, no sideways text, no overflow
- All new appended charts render with data (or graceful empty-state if history is too short)

**Step 4 — Run the first clean refresh**
Click **Refresh Prices** in the local app. Wait 3–4 minutes. Check:
- No repeated CAPTCHA challenges (if more than 6, the run will abort — try again later)
- All 26 GPUs and 12 RAM kits return valid data (non-null medians)
- `price_history.json` has been created with exactly one clean snapshot

**Step 5 — Spot-check a few MSRP ratios**
Eyeball a handful of ratios in the dashboard. RTX 4070 new should be somewhere around 0.9–1.1× MSRP in a normal market. Anything wildly off (e.g. 0.2× or 5×) suggests a scraper or MSRP value issue — investigate before publishing.

**Step 6 — Publish**
Once the data looks good, click **Publish**. This writes the static export to `docs/` and pushes to GitHub. Confirm the GitHub Pages site updates within a minute or two.

**Step 7 — Establish the refresh cadence**
From here on, aim to hit Refresh + Publish once or twice a day for the first week to seed the trend charts with enough data to be meaningful. After that, daily is fine. The Index tab's time-series charts need at least 5–7 snapshots before they tell a useful story.

---

*End of archived tasks.*

</details>
