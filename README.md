# HW//INDEX

A live hardware price index for consumer GPUs and RAM. The owner runs a local Flask scraper to refresh data; the public sees a static, read-only dashboard published to GitHub Pages.

https://jonny-lloyd.github.io/hardware-index/

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
