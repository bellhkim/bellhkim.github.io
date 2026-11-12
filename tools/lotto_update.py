"""Weekly lotto data and statistics for the 고스톱 lucky card.

Runs on GitHub Actions every Saturday night after the draw (see lotto.yml):
  1. adds any new 로또 6/45 draws to lotto/draws.json (backfills about 10 years on the first run)
  2. analyses the last 10, 5, 3 and 1 years of draws
  3. writes this week's number sets to lotto/predict.json, which the game reads

Honest note: every draw is independent and every combination has the same 1 in 8,145,060
chance. These sets summarise past draws for fun; they cannot raise the odds of winning.

Usage: python3 lotto_update.py <site_dir>        (fetch new draws + rebuild predictions)
       python3 lotto_update.py <site_dir> --offline   (rebuild predictions from saved draws only)
Standard library only.
"""
import datetime as dt
import json
import math
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

FIRST_DRAW = dt.date(2002, 12, 7)  # 제1회 추첨일, every Saturday since
WINDOWS = [("10y", "최근 10년", 10), ("5y", "최근 5년", 5), ("3y", "최근 3년", 3), ("1y", "최근 1년", 1)]
UA = "Mozilla/5.0 (compatible; bellhkim-lotto-stats/1.0; +https://bellhkim.github.io)"
BALLS = range(1, 46)


# ---------- fetching ----------
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def fetch_json(no):
    """동행복권 JSON endpoint."""
    body = http_get(f"https://www.dhlottery.co.kr/common.do?method=getLottoNumber&drwNo={no}")
    try:
        d = json.loads(body)
    except ValueError:
        raise ValueError("not JSON: " + " ".join(body[:160].split()))
    if d.get("returnValue") != "success":
        return None
    return {"no": d["drwNo"], "date": d["drwNoDate"], "nums": sorted(d[f"drwtNo{i}"] for i in range(1, 7)), "bonus": d["bnusNo"]}


def fetch_html(no):
    """Fallback: the result page."""
    body = http_get(f"https://www.dhlottery.co.kr/gameResult.do?method=byWin&drwNo={no}")
    balls = [int(x) for x in re.findall(r'class="ball_645[^"]*">\s*(\d+)\s*<', body)]
    date = re.search(r"\((\d{4})년 (\d{2})월 (\d{2})일 추첨\)", body)
    if len(balls) < 7 or not date:
        return None
    return {"no": no, "date": "-".join(date.groups()), "nums": sorted(balls[:6]), "bonus": balls[6]}


def fetch(no):
    for f in (fetch_json, fetch_html):
        try:
            d = f(no)
            if d and d["no"] == no and len(set(d["nums"])) == 6 and all(1 <= n <= 45 for n in d["nums"] + [d["bonus"]]):
                return d
        except Exception as e:  # network or format trouble: try the next source
            print(f"  draw {no}: {f.__name__} failed: {e}", flush=True)
    return None


# Public mirror of the official results (a GitHub Pages project that republishes 동행복권 data as JSON).
# The official site often blocks or times out for overseas servers such as GitHub Actions.
MIRROR_ALL = "https://smok95.github.io/lotto/results/all.json"


def pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def normalise(d):
    """Accept the field names different sources use for one draw."""
    no = pick(d, "draw_no", "drwNo", "drw_no", "round", "no")
    nums = pick(d, "numbers", "nums", "winning_numbers")
    if nums is None and "drwtNo1" in d:
        nums = [d[f"drwtNo{i}"] for i in range(1, 7)]
    bonus = pick(d, "bonus_no", "bnusNo", "bonus", "bonus_number")
    date = str(pick(d, "date", "drwNoDate", "draw_date") or "")[:10]
    try:
        out = {"no": int(no), "date": date, "nums": sorted(int(n) for n in nums), "bonus": int(bonus)}
    except (TypeError, ValueError):
        return None
    ok = len(set(out["nums"])) == 6 and all(1 <= n <= 45 for n in out["nums"] + [out["bonus"]]) and len(date) == 10
    return out if ok else None


def fetch_mirror():
    try:
        data = json.loads(http_get(MIRROR_ALL))
    except Exception as e:
        print(f"mirror failed: {e}", flush=True)
        return []
    items = data if isinstance(data, list) else pick(data, "results", "data", "draws") or []
    out = [d for d in (normalise(x) for x in items if isinstance(x, dict)) if d]
    print(f"mirror: {len(out)} draws" + ("" if out else f" (could not read; first item: {str(items[:1])[:300]})"), flush=True)
    return out


def expected_latest(today):
    """The newest draw number that should exist by today (draws are Saturday evening KST)."""
    return (today - FIRST_DRAW).days // 7 + 1


def update_draws(draws, today):
    have = {d["no"] for d in draws}
    latest = expected_latest(today)
    floor = max(1, latest - 10 * 53 - 5)  # keep a bit over 10 years
    mirrored = 0
    for d in fetch_mirror():
        if d["no"] >= floor and d["no"] not in have:
            draws.append(d); have.add(d["no"]); mirrored += 1
    if mirrored:
        print(f"added {mirrored} draws from the mirror", flush=True)
    start = (max(have) + 1) if have else floor
    added, misses = mirrored, 0
    print(f"fetching draws {start}..{latest}", flush=True)
    for no in range(start, latest + 1):
        if no in have:
            continue
        d = fetch(no)
        if not d:
            print(f"  draw {no}: not available", flush=True)
            misses += 1
            if no == latest or misses >= 3:  # tonight's draw may not be out yet; 3 misses in a row = site unreachable
                break
            continue
        misses = 0
        draws.append(d)
        added += 1
        if added % 50 == 0:
            print(f"  ...{added} draws (up to {no})", flush=True)
        time.sleep(0.25)
    draws.sort(key=lambda d: d["no"])
    return added


# ---------- statistics ----------
def window_draws(draws, years, latest_date):
    since = latest_date - dt.timedelta(days=round(365.25 * years))
    return [d for d in draws if dt.date.fromisoformat(d["date"]) > since]


def frequencies(ds):
    f = {n: 0 for n in BALLS}
    for d in ds:
        for n in d["nums"]:
            f[n] += 1
    return f


def gaps(ds):
    """Draws since each number last appeared (larger = longer absent)."""
    g = {}
    for n in BALLS:
        g[n] = len(ds)
        for i, d in enumerate(reversed(ds)):
            if n in d["nums"]:
                g[n] = i
                break
    return g


def chi_square(f, n_draws):
    exp = n_draws * 6 / 45
    return sum((v - exp) ** 2 / exp for v in f.values()) if exp else 0.0


def shape_stats(ds):
    sums = [sum(d["nums"]) for d in ds]
    mean = sum(sums) / len(sums)
    sd = math.sqrt(sum((s - mean) ** 2 for s in sums) / len(sums))
    return mean, sd


def balanced(nums, mean, sd):
    odd = sum(n % 2 for n in nums)
    low = sum(n <= 22 for n in nums)
    runs = sum(1 for a, b in zip(nums, nums[1:]) if b == a + 1)
    return abs(sum(nums) - mean) <= sd and 2 <= odd <= 4 and 2 <= low <= 4 and runs <= 1


def top(score, k=6):
    return sorted(sorted(BALLS, key=lambda n: (-score[n], n))[:k])


def weighted_pick(rng, weights, k=6):
    pool, out = dict(weights), []
    for _ in range(k):
        total = sum(pool.values())
        r, acc = rng.random() * total, 0.0
        for n, w in pool.items():
            acc += w
            if acc >= r:
                out.append(n)
                del pool[n]
                break
    return sorted(out)


def zscores(v):
    vals = list(v.values())
    m = sum(vals) / len(vals)
    s = math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)) or 1.0
    return {n: (x - m) / s for n, x in v.items()}


def predict(draws):
    latest = draws[-1]
    latest_date = dt.date.fromisoformat(latest["date"])
    target_no, target_date = latest["no"] + 1, latest_date + dt.timedelta(days=7)
    rng = random.Random(target_no)  # same numbers all week for everyone
    windows, sets = {}, []
    for key, label, years in WINDOWS:
        ds = window_draws(draws, years, latest_date)
        f = frequencies(ds)
        chi = chi_square(f, len(ds))
        mean, sd = shape_stats(ds)
        windows[key] = {
            "label": label, "draws": len(ds), "from": ds[0]["date"], "to": ds[-1]["date"],
            "expected": round(len(ds) * 6 / 45, 1),
            "hot": top(f), "cold": sorted(sorted(BALLS, key=lambda n: (f[n], n))[:6]),
            "chi2": round(chi, 1),  # about 44 on average for a fair machine (44 degrees of freedom)
            "sumMean": round(mean, 1), "sumSd": round(sd, 1),
        }
        sets.append({"id": "hot-" + key, "name": label + " 최다 출현", "method": "대수의 법칙: " + str(len(ds)) + "회 추첨에서 가장 많이 나온 6개", "nums": top(f),
                     "name_en": "Most drawn, last " + str(years) + (" year" if years == 1 else " years"), "method_en": "Law of large numbers: the 6 drawn most often in " + str(len(ds)) + " draws"})
    # 미출현 회귀: numbers absent the longest over the last 3 years, measured against the expected gap
    d3 = window_draws(draws, 3, latest_date)
    g = gaps(d3)
    sets.append({"id": "overdue", "name": "오래 쉰 번호", "method": "평균 회귀: 최근 3년 중 가장 오래 안 나온 6개 (평균 간격 7.5회)", "nums": top(g),
                 "name_en": "Longest absent", "method_en": "Regression to the mean: the 6 absent longest in the last 3 years (average gap 7.5 draws)"})
    # 종합 점수: 10년·1년 빈도와 공백을 표준점수로 합친 순위
    f10, f1 = frequencies(window_draws(draws, 10, latest_date)), frequencies(window_draws(draws, 1, latest_date))
    z10, z1, zg = zscores(f10), zscores(f1), zscores(g)
    combo = {n: z10[n] + 0.7 * z1[n] + 0.5 * zg[n] for n in BALLS}
    sets.append({"id": "zscore", "name": "종합 점수", "method": "10년 빈도 + 1년 빈도 + 공백을 표준점수로 더한 상위 6개", "nums": top(combo),
                 "name_en": "Combined score", "method_en": "Top 6 by z-scores of 10-year frequency, 1-year frequency and absence"})
    # 대표 번호: 최근 가중 빈도로 뽑고, 과거 당첨 조합의 모양(합계·홀짝·고저·연번)에 맞는 것만 남기는 몬테카를로
    d10 = window_draws(draws, 10, latest_date)
    weights = {n: 1.0 for n in BALLS}
    for i, d in enumerate(reversed(d10)):
        for n in d["nums"]:
            weights[n] += math.exp(-i / 104)  # half-life about 1.4 years
    mean, sd = shape_stats(d10)
    best = None
    for _ in range(5000):
        nums = weighted_pick(rng, weights)
        if balanced(nums, mean, sd):
            best = nums
            break
    best = best or weighted_pick(rng, weights)
    bonus = weighted_pick(rng, {n: w for n, w in weights.items() if n not in best}, 1)[0]
    sets.append({"id": "weighted", "name": "가중 몬테카를로", "method": "최근일수록 무게를 더 준 10년 빈도로 뽑은 뒤, 과거 당첨 조합의 합계·홀짝·고저 분포에 맞춘 조합", "nums": best, "bonus": bonus,
                 "name_en": "Weighted Monte Carlo", "method_en": "Drawn by 10-year frequency weighted toward recent draws, kept only if its sum, odd/even and high/low mix look like past winners"})
    # 대표 번호: 최근 10년 동안 기대 횟수보다 가장 적게 나온 6개, 보너스는 7번째로 적게 나온 번호
    least = sorted(BALLS, key=lambda n: (f10[n], n))
    exp10 = len(d10) * 6 / 45
    main = {"id": "main", "name": "이번 주 추천 · 가장 안 나온 수", "method": "최근 10년 " + str(len(d10)) + "회 추첨에서 가장 적게 나온 6개 (번호당 기대 " + str(round(exp10)) + "회 · 실제 " + str(f10[least[0]]) + "~" + str(f10[least[5]]) + "회)", "nums": sorted(least[:6]), "bonus": least[6],
            "name_en": "This week's pick · least drawn", "method_en": "The 6 drawn least in the last 10 years (" + str(len(d10)) + " draws; about " + str(round(exp10)) + " expected each, actually " + str(f10[least[0]]) + "-" + str(f10[least[5]]) + ")"}
    return {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "target": {"no": target_no, "date": target_date.isoformat()},
        "basedOn": {"no": latest["no"], "date": latest["date"], "nums": latest["nums"], "bonus": latest["bonus"], "totalDraws": len(draws)},
        "main": main, "sets": sets, "windows": windows,
        "note": "추첨은 매번 독립적이라 어떤 조합이든 당첨 확률은 8,145,060분의 1로 같아요. 지난 기록을 재미로 요약한 번호예요.",
        "note_en": "Every draw is independent, so every combination has the same 1 in 8,145,060 chance. These numbers just summarise past draws for fun.",
    }


def main():
    site = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out = site / "lotto"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "draws.json"
    draws = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if "--offline" not in sys.argv:
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
        added = update_draws(draws, today)
        print(f"added {added} draws, {len(draws)} in total")
    if not draws:
        sys.exit("no draws available: the lottery site could not be reached")
    path.write_text(json.dumps(draws, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    pred = predict(draws)
    (out / "predict.json").write_text(json.dumps(pred, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"predict.json for draw {pred['target']['no']} ({pred['target']['date']}): {pred['main']['nums']} + {pred['main']['bonus']}")


if __name__ == "__main__":
    main()
