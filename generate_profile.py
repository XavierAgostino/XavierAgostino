#!/usr/bin/env python3
"""Generate dark_mode.svg / light_mode.svg for the profile README.

Usage: python3 generate_profile.py [path/to/avatar.png]

If an ACCESS_TOKEN / GITHUB_TOKEN env var is set, live stats are fetched
from the GitHub GraphQL API; otherwise the STATS fallback below is used.
A token that can see private repos (a classic PAT with `repo` scope) is
needed for private repo/commit counts to be included.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import date
from html import escape

from PIL import Image, ImageOps

# ---------------------------------------------------------------- config

LOGIN = "XavierAgostino"

STATS = {  # fallback when no API token is available (snapshot 2026-06-12)
    "repos": 16,
    "contributed": 3,
    "stars": 4,
    "commits": 1335,
    "followers": 6,
    "loc_add": 562036,
    "loc_del": 84625,
}

BIRTHDATE = date(2003, 7, 14)        # uptime = age
ACCOUNT_CREATED = date(2019, 6, 19)  # first year to count commits from

ART_WIDTH = 48          # ASCII art columns
INFO_WIDTH = 62         # right-column width in characters
GAP = "    "            # space between art and info column

FONT_SIZE = 16
CHAR_W = 9.65           # monospace advance at 16px
LINE_H = 20
PAD_X, PAD_Y = 28, 26
SCALE = 1.18            # display scale (SVG is vector, so this stays crisp)

THEMES = {
    "dark": {
        "bg": "#161b22", "border": "#30363d",
        "fg": "#c9d1d9", "key": "#ffa657", "val": "#79c0ff",
        "dots": "#8b949e", "art": "#c9d1d9",
        "green": "#56d364", "red": "#f85149",
    },
    "light": {
        "bg": "#fffefe", "border": "#d0d7de",
        "fg": "#24292f", "key": "#953800", "val": "#0969da",
        "dots": "#6e7781", "art": "#24292f",
        "green": "#1a7f37", "red": "#cf222e",
    },
}

# ---------------------------------------------------------------- ascii art

RAMP = " .'-,:;!|j({[%kM%@g"  # sparse -> dense (bright pixels get dense glyphs)
DARK_FLOOR = 46  # pixels darker than this become empty space (kills bg noise)
GAMMA = 0.62     # < 1 brightens midtones so the face renders dense

# Crop to the head before converting so the face gets the resolution.
CROP = (0.20, 0.0, 0.80, 0.70)  # left, top, right, bottom (fractions)

# Elliptical vignette: keep the centered portrait, fade out background bokeh.
ELLIPSE_CX, ELLIPSE_CY = 0.49, 0.50   # center (fraction of image)
ELLIPSE_RX, ELLIPSE_RY = 0.41, 0.58   # radii where fade begins
FADE = 0.16                            # fade-out band width beyond the radii


def ascii_art(path: str) -> list[str]:
    img = Image.open(path).convert("L")
    img = img.crop((
        int(CROP[0] * img.width), int(CROP[1] * img.height),
        int(CROP[2] * img.width), int(CROP[3] * img.height),
    ))
    img = ImageOps.autocontrast(img, cutoff=1)
    h = int(ART_WIDTH * img.height / img.width * 0.5)
    img = img.resize((ART_WIDTH, h))
    px = img.load()
    rows = []
    for y in range(h):
        row = ""
        for x in range(ART_WIDTH):
            dx = (x / ART_WIDTH - ELLIPSE_CX) / ELLIPSE_RX
            dy = (y / h - ELLIPSE_CY) / ELLIPSE_RY
            r = (dx * dx + dy * dy) ** 0.5
            mask = max(0.0, min(1.0, (1 + FADE - r) / FADE))
            v = px[x, y] * mask
            if v < DARK_FLOOR:
                row += " "
                continue
            lum = ((v - DARK_FLOOR) / (255 - DARK_FLOOR)) ** GAMMA
            row += RAMP[min(int(lum * len(RAMP)), len(RAMP) - 1)]
        rows.append(row.rstrip())
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    return rows


# ---------------------------------------------------------------- stats


def fetch_stats() -> bool:
    """Refresh STATS from the GitHub GraphQL API. Returns True on success."""
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return False

    today = date.today()
    year_fields = ""
    for y in range(ACCOUNT_CREATED.year, today.year + 1):
        to = f"{y + 1}-01-01T00:00:00Z" if y < today.year else f"{today}T00:00:00Z"
        year_fields += (
            f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{to}") '
            "{ totalCommitContributions restrictedContributionsCount } "
        )
    query = f"""query {{
      user(login: "{LOGIN}") {{
        followers {{ totalCount }}
        repositories(first: 100, ownerAffiliations: OWNER) {{
          totalCount nodes {{ name stargazerCount }}
        }}
        repositoriesContributedTo(
          contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
        ) {{ totalCount }}
        {year_fields}
      }}
    }}"""

    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            user = json.load(resp)["data"]["user"]
    except Exception as e:  # noqa: BLE001 - fall back to the snapshot
        print(f"stats fetch failed ({e}), using fallback STATS", file=sys.stderr)
        return False

    commits = sum(
        v["totalCommitContributions"] + v["restrictedContributionsCount"]
        for k, v in user.items() if k.startswith("y")
    )
    STATS.update(
        repos=user["repositories"]["totalCount"],
        contributed=user["repositoriesContributedTo"]["totalCount"],
        stars=sum(n["stargazerCount"] for n in user["repositories"]["nodes"]),
        commits=commits,
        followers=user["followers"]["totalCount"],
    )
    fetch_loc(token, [n["name"] for n in user["repositories"]["nodes"]])
    return True


def fetch_loc(token: str, repo_names: list[str]) -> None:
    """Sum lines added/deleted by LOGIN across repos via the REST stats API.

    GitHub computes these stats lazily and answers 202 until ready, so each
    repo is retried a few times. On total failure the fallback STATS stand.
    """
    total_add = total_del = 0
    ok = False
    for name in repo_names:
        url = f"https://api.github.com/repos/{LOGIN}/{name}/stats/contributors"
        data = None
        for _ in range(8):
            req = urllib.request.Request(
                url, headers={"Authorization": f"bearer {token}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    if resp.status == 202 or not body.strip():
                        time.sleep(3)
                        continue
                    data = json.loads(body)
                    break
            except Exception:
                time.sleep(2)
        if not isinstance(data, list):
            continue
        ok = True
        for c in data:
            if c.get("author") and c["author"].get("login") == LOGIN:
                for w in c["weeks"]:
                    total_add += w["a"]
                    total_del += w["d"]
    if ok:
        STATS.update(loc_add=total_add, loc_del=total_del)


# ---------------------------------------------------------------- info lines
# Each line is a list of (text, class) segments.


def uptime() -> str:
    today = date.today()
    y = today.year - BIRTHDATE.year
    m = today.month - BIRTHDATE.month
    d = today.day - BIRTHDATE.day
    if d < 0:
        m -= 1
        prev = date(today.year, today.month, 1)
        d += (prev - date(prev.year - (prev.month == 1), prev.month - 1 or 12, 1)).days
    if m < 0:
        y -= 1
        m += 12
    return f"{y} years, {m} months, {d} days"


def kv(key, value, key_cls="key", val_cls="val"):
    used = 2 + len(key) + 1 + 1 + 1 + len(value)
    dots = "." * max(INFO_WIDTH - used, 2)
    return [
        (". ", "dots"), (f"{key}:", key_cls), (" ", "fg"),
        (dots, "dots"), (" ", "fg"), (value, val_cls),
    ]


def rule(label=""):
    if label:
        head = f"─ {label} "
        return [(head + "─" * (INFO_WIDTH - len(head)), "fg")]
    return [("", "fg")]


def header():
    name = "xavier@agostino"
    return [(name, "bold"), (" " + "─" * (INFO_WIDTH - len(name) - 1), "fg")]


def stat_pair(k1, v1, k2, v2, split):
    left_used = 2 + len(k1) + 1 + 1 + 1 + len(v1)
    d1 = "." * max(split - left_used, 2)
    right_used = 3 + len(k2) + 1 + 1 + 1 + len(v2)
    d2 = "." * max(INFO_WIDTH - split - right_used, 2)
    return [
        (". ", "dots"), (f"{k1}:", "key"), (" ", "fg"), (d1, "dots"),
        (" ", "fg"), (v1, "val"), (" | ", "fg"), (f"{k2}:", "key"),
        (" ", "fg"), (d2, "dots"), (" ", "fg"), (v2, "val"),
    ]


def loc_line():
    s = STATS
    net = f"{s['loc_add'] - s['loc_del']:,}"
    add = f"{s['loc_add']:,}++"
    dele = f"{s['loc_del']:,}--"
    key = "Lines of Code on GitHub:"
    used = 2 + len(key) + 1 + len(net) + 3 + len(add) + 2 + len(dele) + 2
    dots = "." * max(INFO_WIDTH - used, 1)
    return [
        (". ", "dots"), (key, "key"), (dots, "dots"), (" ", "fg"),
        (net, "val"), (" ( ", "fg"), (add, "green"), (", ", "fg"),
        (dele, "red"), (" )", "fg"),
    ]


def info_lines():
    s = STATS
    return [
        header(),
        kv("OS", "macOS, iOS"),
        kv("Uptime", uptime()),
        kv("Host", "Harvard University, CS"),
        kv("Kernel", "Student-Athlete (D1 Football, retired)"),
        kv("IDE", "VS Code, Cursor"),
        [(".", "dots")],
        kv("Languages.Programming", "TypeScript, Python, JavaScript"),
        kv("Languages.Computer", "HTML, CSS, SQL, JSON, YAML"),
        kv("Languages.Real", "English"),
        [(".", "dots")],
        kv("Hobbies.Software", "Building tools I wish existed"),
        kv("Hobbies.Offline", "Football, Chess, Catan"),
        [("", "fg")],
        rule("Contact"),
        kv("LinkedIn", "in/xavieragostino"),
        kv("GitHub", "XavierAgostino"),
        [("", "fg")],
        rule("GitHub Stats"),
        stat_pair("Repos", f"{s['repos']} {{Contributed: {s['contributed']}}}",
                  "Stars", str(s["stars"]), split=40),
        stat_pair("Commits", f"{s['commits']:,}",
                  "Followers", str(s["followers"]), split=40),
        loc_line(),
    ]


# ---------------------------------------------------------------- svg


def build_svg(theme: dict, art: list[str]) -> str:
    info = info_lines()
    n = max(len(art), len(info))
    offset = (n - len(info)) // 2  # vertically center the info column
    rows = []
    for i in range(n):
        segs = []
        art_row = art[i] if i < len(art) else ""
        segs.append((art_row.ljust(ART_WIDTH) + GAP, "art"))
        j = i - offset
        segs.extend(info[j] if 0 <= j < len(info) else [("", "fg")])
        rows.append(segs)

    cols = ART_WIDTH + len(GAP) + INFO_WIDTH
    width = round(cols * CHAR_W + 2 * PAD_X)
    height = n * LINE_H + 2 * PAD_Y

    t = theme
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{round(width * SCALE)}" height="{round(height * SCALE)}" '
        f'viewBox="0 0 {width} {height}" font-family="Menlo, Consolas, \'DejaVu Sans Mono\', monospace" '
        f'font-size="{FONT_SIZE}px">',
        "<style>",
        f".fg{{fill:{t['fg']}}} .key{{fill:{t['key']}}} .val{{fill:{t['val']}}}",
        f".dots{{fill:{t['dots']}}} .art{{fill:{t['art']}}}",
        f".bold{{fill:{t['fg']};font-weight:600}}",
        "</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" '
        f'fill="{t["bg"]}" stroke="{t["border"]}"/>',
    ]
    for i, segs in enumerate(rows):
        y = PAD_Y + (i + 1) * LINE_H - 5
        spans = "".join(
            f'<tspan class="{cls}">{escape(txt)}</tspan>' for txt, cls in segs if txt
        )
        out.append(f'<text x="{PAD_X}" y="{y}" xml:space="preserve">{spans}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main():
    avatar = sys.argv[1] if len(sys.argv) > 1 else "avatar.png"
    print("stats:", "live" if fetch_stats() else "fallback snapshot")
    art = ascii_art(avatar)
    for name, theme in THEMES.items():
        path = f"{name}_mode.svg"
        with open(path, "w") as f:
            f.write(build_svg(theme, art))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
