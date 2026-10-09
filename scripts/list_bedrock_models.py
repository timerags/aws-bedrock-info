#!/usr/bin/env python3
"""
List Amazon Bedrock foundation models for one or more regions, with their
lifecycle status (ACTIVE / LEGACY) and, for LEGACY models, the EOL date.

The Bedrock API (list-foundation-models / get-foundation-model) only returns
`modelLifecycle.status`. It does NOT populate the EOL date even though the
API schema defines an `endOfLifeTime` field (confirmed empty as of this
writing). The only place the EOL date is published is the model card page
on the AWS docs site, so this script scrapes that page for LEGACY models.
This is best-effort: if AWS changes the docs page structure, EOL lookup may
break (the status/ACTIVE/LEGACY columns, which come from the real API, will
keep working regardless).

Usage:
    ./scripts/list_bedrock_models.py [--region REGION ...] [--no-eol] [--csv FILE] [--html FILE]

Examples:
    ./scripts/list_bedrock_models.py
    ./scripts/list_bedrock_models.py --region ap-northeast-1 --region us-east-1
    ./scripts/list_bedrock_models.py --region eu-west-1 --csv models.csv
    ./scripts/list_bedrock_models.py --html site/index.html
    ./scripts/list_bedrock_models.py --no-eol   # skip scraping, faster

Requires: aws cli (configured credentials). No third-party Python packages.
"""

import argparse
import csv
import html as html_module
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), name="JST")

MODEL_CARDS_INDEX_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html"
MODEL_CARD_BASE_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; bedrock-model-lister/1.0)"}

LINK_RE = re.compile(r'href="\./(model-card-[a-z0-9-]+\.html)">([^<]+)</a>')
EOL_DATE_RE = re.compile(r"<b>Model EOL date:</b>\s*([^<]+?)\s*</p>")
LEGACY_PERIOD_RE = re.compile(r"<b>Legacy period:</b>\s*([^<]+?)\s*</p>")
LAUNCH_DATE_RE = re.compile(r"<b>Model launch date:</b>\s*([^<]+?)\s*</p>")


def fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def list_foundation_models(region: str) -> list[dict]:
    result = subprocess.run(
        ["aws", "bedrock", "list-foundation-models", "--region", region, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return data.get("modelSummaries", [])


def build_model_name_to_slug_map() -> dict[str, str]:
    html = fetch_url(MODEL_CARDS_INDEX_URL)
    mapping = {}
    for slug, name in LINK_RE.findall(html):
        mapping.setdefault(name.strip(), slug)
    return mapping


NORMALIZE_RE = re.compile(r"[^a-z0-9.]+")
PAREN_SUFFIX_RE = re.compile(r"\([^)]*\)")


def normalize_model_name(name: str) -> str:
    name = PAREN_SUFFIX_RE.sub("", name.lower())
    return NORMALIZE_RE.sub(" ", name).strip()


def build_normalized_name_to_slug_map(name_to_slug: dict[str, str]) -> dict[str, str]:
    mapping = {}
    for name, slug in name_to_slug.items():
        mapping.setdefault(normalize_model_name(name), slug)
    return mapping


def resolve_slug(
    model_name: str,
    provider_name: str,
    name_to_slug: dict[str, str],
    normalized_to_slug: dict[str, str],
) -> str | None:
    """Match a Bedrock API model name to a docs model-card slug.

    The API's modelName and the docs index page's link text sometimes disagree
    on formatting (hyphen vs. space, parenthetical version suffixes, a missing
    or extra provider prefix, trailing variant suffixes). Try progressively
    looser matching strategies before giving up.
    """
    if model_name in name_to_slug:
        return name_to_slug[model_name]

    normalized = normalize_model_name(model_name)
    if normalized in normalized_to_slug:
        return normalized_to_slug[normalized]

    provider_prefix = normalize_model_name(provider_name) + " "
    if normalized.startswith(provider_prefix):
        stripped = normalized[len(provider_prefix):]
        if stripped in normalized_to_slug:
            return normalized_to_slug[stripped]

    candidates = {
        slug for key, slug in normalized_to_slug.items()
        if normalized == key or normalized.startswith(key + " ")
    }
    if len(candidates) == 1:
        return next(iter(candidates))

    return None


def fetch_model_card_info(slug: str) -> tuple[str | None, str | None, str | None]:
    html = fetch_url(MODEL_CARD_BASE_URL + slug)
    eol_match = EOL_DATE_RE.search(html)
    legacy_match = LEGACY_PERIOD_RE.search(html)
    launch_match = LAUNCH_DATE_RE.search(html)
    eol_date = eol_match.group(1).strip() if eol_match else None
    legacy_period = legacy_match.group(1).strip() if legacy_match else None
    launch_date = launch_match.group(1).strip() if launch_match else None
    return eol_date, legacy_period, launch_date


WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# "18th Sept 2026" / "18 September 2026"
DAY_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})$")
# "September 28, 2026" / "Dec 02, 2025" / "Dec 2 2025"
MONTH_DAY_YEAR_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")
# "Jun 2025" / "September 2026"
MONTH_YEAR_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{4})$")


def parse_scraped_date(text: str) -> tuple[datetime | None, bool]:
    """Parse an AWS docs date string. Returns (datetime, has_day)."""
    if not text:
        return None, False
    text = text.strip()

    m = DAY_MONTH_YEAR_RE.match(text)
    if m:
        day, month_name, year = m.groups()
        month = MONTH_NAMES.get(month_name.lower())
        if month:
            return datetime(int(year), month, int(day), tzinfo=timezone.utc), True

    m = MONTH_DAY_YEAR_RE.match(text)
    if m:
        month_name, day, year = m.groups()
        month = MONTH_NAMES.get(month_name.lower())
        if month:
            return datetime(int(year), month, int(day), tzinfo=timezone.utc), True

    m = MONTH_YEAR_RE.match(text)
    if m:
        month_name, year = m.groups()
        month = MONTH_NAMES.get(month_name.lower())
        if month:
            return datetime(int(year), month, 1, tzinfo=timezone.utc), False

    return None, False


def parse_eol_date(text: str) -> datetime | None:
    dt, _ = parse_scraped_date(text)
    return dt


def format_date_ja(text: str) -> str:
    dt, has_day = parse_scraped_date(text)
    if dt is None:
        return text or ""
    if has_day:
        return f"{dt.year}年{dt.month}月{dt.day}日({WEEKDAY_JA[dt.weekday()]})"
    return f"{dt.year}年{dt.month}月"


def model_name_link(r: dict) -> str:
    name = html_module.escape(r["model_name"])
    url = r.get("model_card_url")
    if url:
        return f'<a href="{html_module.escape(url)}" target="_blank" rel="noopener noreferrer">{name}</a>'
    index_url = html_module.escape(MODEL_CARDS_INDEX_URL)
    mark = (
        f'<a href="{index_url}" target="_blank" rel="noopener noreferrer" '
        f'class="no-card-mark" title="モデルカードが自動では見つかりませんでした。一覧から検索してください">?</a>'
    )
    return f"{name} {mark}"


def render_html(rows: list[dict], regions: list[str]) -> str:
    generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    now = datetime.now(timezone.utc)

    total = len(rows)
    legacy_rows = [r for r in rows if r["status"] == "LEGACY"]

    def eol_class(r: dict) -> str:
        if r["status"] != "LEGACY":
            return ""
        dt = parse_eol_date(r["eol_date"])
        if dt is None:
            return "eol-unknown"
        days = (dt - now).days
        if days < 0:
            return "eol-passed"
        if days <= 30:
            return "eol-soon"
        return "eol-later"

    def eol_days_label(r: dict) -> str:
        if r["status"] != "LEGACY":
            return ""
        dt = parse_eol_date(r["eol_date"])
        if dt is None:
            return ""
        days = (dt - now).days
        if days < 0:
            return f"({abs(days)}日前に終了)"
        if days == 0:
            return "(本日)"
        return f"(あと{days}日)"

    body_rows = []
    for r in rows:
        status_class = "status-active" if r["status"] == "ACTIVE" else "status-legacy"
        eol_cls = eol_class(r)
        eol_text = html_module.escape(format_date_ja(r["eol_date"])) if r["eol_date"] else "&ndash;"
        launch_text = html_module.escape(format_date_ja(r["launch_date"])) if r["launch_date"] else "&ndash;"
        days_label = eol_days_label(r)
        body_rows.append(f"""
        <tr class="{eol_cls}" data-region="{html_module.escape(r['region'])}" data-status="{html_module.escape(r['status'])}">
          <td>{html_module.escape(r['region'])}</td>
          <td>{html_module.escape(r['provider'])}</td>
          <td><code>{html_module.escape(r['model_id'])}</code></td>
          <td>{model_name_link(r)}</td>
          <td><span class="badge {status_class}">{html_module.escape(r['status'])}</span></td>
          <td>{launch_text}</td>
          <td>{eol_text} <span class="days-label">{html_module.escape(days_label)}</span></td>
        </tr>""")

    legacy_summary_rows = []
    for r in sorted(legacy_rows, key=lambda r: parse_eol_date(r["eol_date"]) or datetime.max.replace(tzinfo=timezone.utc)):
        eol_cls = eol_class(r)
        launch_text = html_module.escape(format_date_ja(r["launch_date"])) if r["launch_date"] else "&ndash;"
        eol_text = html_module.escape(format_date_ja(r["eol_date"])) if r["eol_date"] else "&ndash;"
        legacy_summary_rows.append(f"""
        <tr class="{eol_cls}">
          <td>{model_name_link(r)}</td>
          <td><code>{html_module.escape(r['model_id'])}</code></td>
          <td>{html_module.escape(r['region'])}</td>
          <td>{launch_text}</td>
          <td>{eol_text} <span class="days-label">{html_module.escape(eol_days_label(r))}</span></td>
        </tr>""")

    regions_label = ", ".join(regions)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bedrock Model Status</title>
<style>
  :root {{
    --bg: #f7f8fa;
    --surface: #ffffff;
    --text: #1a1d23;
    --text-muted: #5b6270;
    --border: #e3e6ea;
    --accent: #2563eb;
    --active-bg: #e7f6ec;
    --active-text: #1a7f37;
    --legacy-bg: #fff3e0;
    --legacy-text: #b45309;
    --soon-bg: #fde2e1;
    --soon-text: #b91c1c;
    --later-bg: #fff8e1;
    --passed-bg: #f1f1f1;
    --passed-text: #6b7280;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #0f1115;
      --surface: #171a21;
      --text: #e6e8eb;
      --text-muted: #9aa2af;
      --border: #2a2e37;
      --accent: #5b9dff;
      --active-bg: #10301c;
      --active-text: #4ade80;
      --legacy-bg: #3a2a12;
      --legacy-text: #fbbf24;
      --soon-bg: #3a1414;
      --soon-text: #f87171;
      --later-bg: #332c10;
      --passed-bg: #23262d;
      --passed-text: #9aa2af;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #0f1115;
    --surface: #171a21;
    --text: #e6e8eb;
    --text-muted: #9aa2af;
    --border: #2a2e37;
    --accent: #5b9dff;
    --active-bg: #10301c;
    --active-text: #4ade80;
    --legacy-bg: #3a2a12;
    --legacy-text: #fbbf24;
    --soon-bg: #3a1414;
    --soon-text: #f87171;
    --later-bg: #332c10;
    --passed-bg: #23262d;
    --passed-text: #9aa2af;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.5;
  }}
  main {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 2rem 1.25rem 4rem;
  }}
  h1 {{
    font-size: 1.5rem;
    margin-bottom: 0.25rem;
  }}
  .subtitle {{
    color: var(--text-muted);
    font-size: 0.9rem;
    margin-bottom: 2rem;
  }}
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 0.75rem;
    margin-bottom: 2rem;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
  }}
  .card .num {{
    font-size: 1.6rem;
    font-weight: 600;
  }}
  .card .label {{
    color: var(--text-muted);
    font-size: 0.8rem;
  }}
  section {{
    margin-bottom: 2.5rem;
  }}
  h2 {{
    font-size: 1.1rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
    margin-bottom: 1rem;
  }}
  .table-wrap {{
    overflow-x: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
    white-space: nowrap;
  }}
  th, td {{
    text-align: left;
    padding: 0.55rem 0.75rem;
    border-bottom: 1px solid var(--border);
  }}
  th {{
    color: var(--text-muted);
    font-weight: 600;
    position: sticky;
    top: 0;
    background: var(--surface);
  }}
  tr:last-child td {{ border-bottom: none; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.8rem;
  }}
  .badge {{
    display: inline-block;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
  }}
  .status-active {{ background: var(--active-bg); color: var(--active-text); }}
  .status-legacy {{ background: var(--legacy-bg); color: var(--legacy-text); }}
  .days-label {{
    color: var(--text-muted);
    font-size: 0.78rem;
  }}
  tr.eol-soon {{ background: var(--soon-bg); }}
  tr.eol-soon .days-label {{ color: var(--soon-text); font-weight: 600; }}
  tr.eol-later {{ background: var(--later-bg); }}
  tr.eol-passed {{ background: var(--passed-bg); }}
  tr.eol-passed .days-label {{ color: var(--passed-text); }}
  .controls {{
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }}
  select, input {{
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font-size: 0.85rem;
  }}
  footer {{
    color: var(--text-muted);
    font-size: 0.78rem;
    margin-top: 2rem;
  }}
  footer a {{ color: var(--accent); }}
  td a {{ color: var(--accent); text-decoration: none; }}
  td a:hover {{ text-decoration: underline; }}
  .no-card-mark {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.1rem;
    height: 1.1rem;
    border-radius: 999px;
    background: var(--border);
    color: var(--text-muted) !important;
    font-size: 0.7rem;
    font-weight: 700;
    text-decoration: none !important;
  }}
  .no-card-mark:hover {{ background: var(--accent); color: var(--surface) !important; }}
</style>
</head>
<body>
<main>
  <h1>Amazon Bedrock Model Status</h1>
  <p class="subtitle">対象リージョン: {html_module.escape(regions_label)} &middot; 最終更新: {generated_at}</p>

  <div class="cards">
    <div class="card"><div class="num">{total}</div><div class="label">全モデル数</div></div>
    <div class="card"><div class="num">{total - len(legacy_rows)}</div><div class="label">ACTIVE</div></div>
    <div class="card"><div class="num">{len(legacy_rows)}</div><div class="label">LEGACY</div></div>
    <div class="card"><div class="num">{len(regions)}</div><div class="label">対象リージョン数</div></div>
  </div>

  <section>
    <h2>⚠ LEGACY モデル一覧（EOL日付順）</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>モデル名</th><th>Model ID</th><th>リージョン</th><th>提供開始日</th><th>EOL日</th></tr></thead>
        <tbody>{"".join(legacy_summary_rows) if legacy_summary_rows else '<tr><td colspan="5">現在LEGACYのモデルはありません</td></tr>'}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>全モデル一覧</h2>
    <div class="controls">
      <input type="search" id="filter" placeholder="モデル名・IDで検索…" oninput="filterRows()">
      <select id="statusFilter" onchange="filterRows()">
        <option value="">すべてのステータス</option>
        <option value="ACTIVE">ACTIVE</option>
        <option value="LEGACY">LEGACY</option>
      </select>
      <select id="regionFilter" onchange="filterRows()">
        <option value="">すべてのリージョン</option>
        {"".join(f'<option value="{html_module.escape(reg)}">{html_module.escape(reg)}</option>' for reg in regions)}
      </select>
    </div>
    <div class="table-wrap">
      <table id="modelsTable">
        <thead><tr><th>リージョン</th><th>プロバイダー</th><th>Model ID</th><th>モデル名</th><th>ステータス</th><th>提供開始日</th><th>EOL日</th></tr></thead>
        <tbody>{"".join(body_rows)}</tbody>
      </table>
    </div>
  </section>

  <footer>
    提供開始日・EOL日はAWS公式APIでは提供されないため、<a href="https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html">Bedrockモデルカードのドキュメント</a>をスクレイピングして補完しています（取得失敗時は空欄になります）。ステータス（ACTIVE/LEGACY）は <code>aws bedrock list-foundation-models</code> の公式APIレスポンスです。<br>
    Generated by <code>scripts/list_bedrock_models.py</code>
  </footer>
</main>
<script>
function filterRows() {{
  const q = document.getElementById('filter').value.toLowerCase();
  const status = document.getElementById('statusFilter').value;
  const region = document.getElementById('regionFilter').value;
  document.querySelectorAll('#modelsTable tbody tr').forEach(tr => {{
    const text = tr.textContent.toLowerCase();
    const matchesQ = !q || text.includes(q);
    const matchesStatus = !status || tr.dataset.status === status;
    const matchesRegion = !region || tr.dataset.region === region;
    tr.style.display = (matchesQ && matchesStatus && matchesRegion) ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--region", action="append", dest="regions",
        help="AWS region to query. Repeatable. Default: ap-northeast-1",
    )
    parser.add_argument("--no-eol", action="store_true", help="Skip launch/EOL date lookup (faster, no doc scraping)")
    parser.add_argument("--csv", metavar="FILE", help="Write results to a CSV file")
    parser.add_argument("--html", metavar="FILE", help="Write results to a self-contained HTML report")
    args = parser.parse_args()

    regions = args.regions or ["ap-northeast-1"]

    rows = []
    for region in regions:
        print(f"Fetching models for {region}...", file=sys.stderr)
        for m in list_foundation_models(region):
            rows.append({
                "region": region,
                "provider": m.get("providerName", ""),
                "model_id": m.get("modelId", ""),
                "model_name": m.get("modelName", ""),
                "status": m.get("modelLifecycle", {}).get("status", ""),
                "eol_date": "",
                "legacy_period": "",
                "launch_date": "",
                "model_card_url": "",
            })

    if not args.no_eol:
        name_to_provider = {r["model_name"]: r["provider"] for r in rows}
        all_names = sorted(name_to_provider)
        if all_names:
            print(f"Looking up model card info (launch/EOL dates) for {len(all_names)} model(s)...", file=sys.stderr)
            try:
                name_to_slug = build_model_name_to_slug_map()
            except Exception as e:
                print(f"Warning: could not fetch model card index ({e}); skipping launch/EOL lookup", file=sys.stderr)
                name_to_slug = {}
            normalized_to_slug = build_normalized_name_to_slug_map(name_to_slug)

            card_cache: dict[str, tuple[str | None, str | None, str | None]] = {}
            card_url_cache: dict[str, str | None] = {}
            for name in all_names:
                slug = resolve_slug(name, name_to_provider[name], name_to_slug, normalized_to_slug)
                card_url_cache[name] = (MODEL_CARD_BASE_URL + slug) if slug else None
                if not slug:
                    card_cache[name] = (None, None, None)
                    continue
                try:
                    card_cache[name] = fetch_model_card_info(slug)
                except Exception as e:
                    print(f"Warning: failed to fetch model card info for '{name}' ({e})", file=sys.stderr)
                    card_cache[name] = (None, None, None)

            for r in rows:
                eol_date, legacy_period, launch_date = card_cache.get(r["model_name"], (None, None, None))
                r["launch_date"] = launch_date or ""
                r["model_card_url"] = card_url_cache.get(r["model_name"]) or ""
                if r["status"] == "LEGACY":
                    r["eol_date"] = eol_date or "unknown (check model card)"
                    r["legacy_period"] = legacy_period or ""

    rows.sort(key=lambda r: (r["region"], r["provider"], r["model_id"]))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["region", "provider", "model_id", "model_name", "status", "launch_date", "eol_date", "legacy_period", "model_card_url"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}", file=sys.stderr)

    if args.html:
        import os
        os.makedirs(os.path.dirname(args.html) or ".", exist_ok=True)
        with open(args.html, "w") as f:
            f.write(render_html(rows, regions))
        print(f"Wrote HTML report to {args.html}", file=sys.stderr)

    headers = ["REGION", "PROVIDER", "MODEL_ID", "MODEL_NAME", "STATUS", "LAUNCH_DATE", "EOL_DATE"]
    col_widths = [len(h) for h in headers]
    table_rows = []
    for r in rows:
        row = [r["region"], r["provider"], r["model_id"], r["model_name"], r["status"], r["launch_date"], r["eol_date"]]
        table_rows.append(row)
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(row):
        return "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row))

    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in col_widths]))
    for row in table_rows:
        print(fmt_row(row))

    legacy_count = sum(1 for r in rows if r["status"] == "LEGACY")
    print(f"\nTotal: {len(rows)} models ({legacy_count} legacy)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
