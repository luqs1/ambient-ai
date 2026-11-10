"""Generate faceted low-poly SVG landscape layers and assemble the landscape variant page.

usage: python3 gen.py <template.html> <out index.html>
"""
import math, random, sys

def r(v):  # compact coords
    return str(int(round(v)))

def path_tri(t):
    (a, b, c) = t
    return f"M{r(a[0])} {r(a[1])}L{r(b[0])} {r(b[1])}L{r(c[0])} {r(c[1])}Z"

def zipper(A, B):
    tris, i, j = [], 0, 0
    while i < len(A) - 1 or j < len(B) - 1:
        if j == len(B) - 1 or (i < len(A) - 1 and A[i + 1][0] < B[j + 1][0]):
            tris.append((A[i], A[i + 1], B[j])); i += 1
        else:
            tris.append((A[i], B[j], B[j + 1])); j += 1
    return tris

def interp(line, x):
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        if x0 <= x <= x1:
            t = 0 if x1 == x0 else (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0), (y1 - y0) / max(1e-6, x1 - x0)
    return line[-1][1], 0.0

def ridge_line(rng, W, ymin, ymax, n, smooth=False):
    pts, step = [], (W + 160) / n
    for k in range(n + 1):
        x = -80 + k * step + (rng.uniform(-.3, .3) * step if 0 < k < n else 0)
        if smooth:
            y = rng.uniform(ymin, ymax)
        else:
            span = ymax - ymin
            y = ymin + rng.uniform(0, .42) * span if k % 2 == 0 else ymin + rng.uniform(.5, 1) * span
        pts.append((x, y))
        # occasional shoulder point for more angular silhouettes
        if not smooth and k < n and rng.random() < .55:
            x2 = x + step * rng.uniform(.3, .6)
            pts.append((x2, y + rng.uniform(-.1, .35) * (ymax - ymin)))
    pts.sort()
    return pts

def facet_layer(rng, W, ymin, ymax, n, rows, bottom, cls, alt=0.0, smooth=False, shade_k=1.0, flat=False):
    top = ridge_line(rng, W, ymin, ymax, n, smooth)
    rows_pts = [top]
    for k in range(1, rows + 1):
        m = max(3, int(len(top) * (1 - k / (rows + 2))) + 1)
        row, step = [], (W + 160) / (m - 1)
        for q in range(m):
            x = -80 + q * step + (rng.uniform(-.35, .35) * step if 0 < q < m - 1 else 0)
            ty, _ = interp(top, x)
            f = k / (rows + 1)
            f = f ** 1.25
            y = ty + (bottom - ty) * f + rng.uniform(-.12, .12) * (bottom - ty) / (rows + 1)
            row.append((x, y))
        row.sort()
        rows_pts.append(row)
    rows_pts.append([(-80, bottom + 400), (W + 80, bottom + 400)])
    tris = []
    for A, B in zip(rows_pts, rows_pts[1:]):
        tris += zipper(A, B)
    sil = "M-80 " + r(bottom + 400) + "".join(f"L{r(x)} {r(y)}" for x, y in top) + f"L{W+80} {r(bottom+400)}Z"
    out = [f'<path class="b" d="{sil}"/>']
    if flat:
        return out
    vals = []
    for t in tris:
        cx = sum(p[0] for p in t) / 3
        cy = sum(p[1] for p in t) / 3
        _, slope = interp(top, cx)
        # rising-to-the-right faces (negative dy/dx) look toward the light on the left
        v = -math.atan(slope * 1.6) + rng.uniform(-.55, .55)
        ty, _ = interp(top, cx)
        depth = (cy - ty) / max(1, bottom - ty)
        v -= depth * .35
        vals.append(v)
    srt = sorted(vals)
    lo, hi = srt[int(len(srt) * .38)], srt[int(len(srt) * .62)]
    buckets = {}
    for t, v in zip(tris, vals):
        if alt and rng.random() < alt:
            buckets.setdefault(("a", 1), []).append(t)
        if v >= hi:
            lvl = min(4, int((v - hi) / max(1e-6, srt[-1] - hi) * 5))
            buckets.setdefault(("g", lvl), []).append(t)
        elif v <= lo:
            lvl = min(4, int((lo - v) / max(1e-6, lo - srt[0]) * 5))
            buckets.setdefault(("s", lvl), []).append(t)
    for (kind, lvl), ts in sorted(buckets.items(), key=lambda kv: (kv[0][0] != "a", kv[0])):
        d = "".join(path_tri(t) for t in ts)
        if kind == "a":
            out.append(f'<path class="a" d="{d}"/>')
        else:
            op = (.10 + lvl * .055) * (shade_k if kind == "s" else 1)
            out.append(f'<path class="{kind}" fill-opacity="{op:.2f}" d="{d}"/>')
    return out

def pine(x, base, h, w):
    return (f'<path class="pl" d="M{r(x-w/2)} {r(base)}L{r(x)} {r(base-h)}L{r(x+w*.06)} {r(base)}Z"/>'
            f'<path class="pd" d="M{r(x)} {r(base-h)}L{r(x+w/2)} {r(base)}L{r(x+w*.06)} {r(base)}Z"/>')

def pines(rng, xs, base_fn, hmin, hmax, ratio=.32):
    out = []
    for x in xs:
        h = rng.uniform(hmin, hmax)
        out.append(pine(x, base_fn(x) + 6, h, h * ratio))
    return "".join(out)

def faceted_sun(cx, cy, rad, n=10, cls_a="sun1", cls_b="sun2"):
    out = []
    for k in range(n):
        a0, a1 = 2 * math.pi * k / n - math.pi / 2, 2 * math.pi * (k + 1) / n - math.pi / 2
        p0 = (cx + rad * math.cos(a0), cy + rad * math.sin(a0))
        p1 = (cx + rad * math.cos(a1), cy + rad * math.sin(a1))
        out.append(f'<path class="{cls_a if k % 2 else cls_b}" d="{path_tri(((cx, cy), p0, p1))}"/>')
    return "".join(out)

def cloud(rng, x, y, w, h, cls="cl"):
    # flat angular streak, like a cut-paper cloud
    pts = [(x, y)]
    n = 6
    for k in range(1, n):
        pts.append((x + w * k / n + rng.uniform(-10, 10), y - h * rng.uniform(.3, 1)))
    pts.append((x + w, y))
    pts.append((x + w * .55, y + h * .25))
    d = "M" + "L".join(f"{r(a)} {r(b)}" for a, b in pts) + "Z"
    return f'<path class="{cls}" d="{d}"/>'

def river(pts, w0, w1):
    left, right = [], []
    n = len(pts)
    for k, (x, y) in enumerate(pts):
        w = w0 + (w1 - w0) * (k / (n - 1)) ** 1.6
        if k == 0: dx, dy = pts[1][0] - x, pts[1][1] - y
        elif k == n - 1: dx, dy = x - pts[k-1][0], y - pts[k-1][1]
        else: dx, dy = pts[k+1][0] - pts[k-1][0], pts[k+1][1] - pts[k-1][1]
        L = math.hypot(dx, dy); nx, ny = -dy / L, dx / L
        left.append((x + nx * w / 2, y + ny * w / 2)); right.append((x - nx * w / 2, y - ny * w / 2))
    poly = left + right[::-1]
    d = "M" + "L".join(f"{r(a)} {r(b)}" for a, b in poly) + "Z"
    # highlight facets: triangles along the left bank
    hi = []
    for k in range(0, n - 1, 1):
        a, b, c = left[k], left[k + 1], ((left[k][0] + right[k][0]) / 2, (left[k][1] + right[k][1]) / 2)
        if k % 2 == 0:
            hi.append(path_tri((a, b, c)))
    sh = []
    for k in range(1, n - 1, 2):
        a, b, c = right[k], right[k + 1], ((left[k+1][0] + right[k+1][0]) / 2, (left[k+1][1] + right[k+1][1]) / 2)
        sh.append(path_tri((a, b, c)))
    return f'<path class="rv" d="{d}"/><path class="rvh" d="{"".join(hi)}"/><path class="s" fill-opacity=".14" d="{"".join(sh)}"/>'

def sparkle(x, y, s, delay):
    d = (f"M{x} {y-s}L{x+s*.28:.1f} {y-s*.28:.1f}L{x+s} {y}L{x+s*.28:.1f} {y+s*.28:.1f}"
         f"L{x} {y+s}L{x-s*.28:.1f} {y+s*.28:.1f}L{x-s} {y}L{x-s*.28:.1f} {y-s*.28:.1f}Z")
    return f'<path class="tw" style="animation-delay:{delay}s" d="{d}"/>'

def layer(name, k, body):
    return f'<g class="ly {name}" style="--k:{k}">{body}</g>'

# ---------------- hero scene ----------------
def hero():
    rng = random.Random(7)
    W, B = 1440, 760
    parts = []
    sky = [faceted_sun(1010, 404, 60, 14)]
    for (x, y, w, h) in [(80, 300, 340, 26), (560, 250, 260, 18), (980, 214, 380, 30), (1220, 312, 260, 20)]:
        sky.append(cloud(rng, x, y, w, h))
    stars = "".join(sparkle(x, y, s, d) for x, y, s, d in
                    [(220, 120, 7, 0), (430, 60, 5, 1.3), (700, 150, 6, 2.1), (900, 80, 4, .7), (1180, 140, 7, 1.7), (1330, 60, 5, 2.6), (120, 220, 4, 3.1)])
    parts.append(layer("Lsky", .3, "".join(sky) + f'<g class="stars">{stars}</g>'))
    far = facet_layer(rng, W, 318, 440, 11, 2, 520, "far", alt=.18)
    parts.append(layer("Lfar", .22, "".join(far)))
    mid = facet_layer(rng, W, 380, 500, 8, 3, 600, "mid", alt=.22)
    parts.append(layer("Lmid", .15, "".join(mid)))
    rng2 = random.Random(19)
    near_top_rng = random.Random(23)
    near = facet_layer(near_top_rng, W, 452, 570, 7, 3, 660, "near", alt=.2)
    parts.append(layer("Lnear", .09, "".join(near)))
    # meadow with river and glints
    mrng = random.Random(41)
    meadow = facet_layer(mrng, W, 556, 610, 16, 4, 720, "meadow", alt=.32, smooth=True, shade_k=.8)
    rv = river([(812, 584), (760, 596), (842, 612), (690, 634), (604, 650), (720, 678), (520, 712), (430, 760), (380, 820)], 6, 190)
    glints = "".join(sparkle(x, y, s, d) for x, y, s, d in [(300, 610, 9, .4), (1010, 640, 10, 1.6), (1180, 600, 7, 2.8), (560, 600, 7, 3.6)])
    smallp = pines(rng2, [470, 492, 508, 1130, 1150, 1166, 1190, 330, 352], lambda x: 590, 26, 52)
    parts.append(layer("Lmeadow", .04, "".join(meadow) + smallp + rv + f'<g class="glints">{glints}</g>'))
    # foreground pines + ground
    fg = []
    prng = random.Random(5)
    for x, h in [(1300, 470), (1372, 560), (1440, 640), (1250, 300), (1340, 360), (40, 330), (-20, 420), (110, 220)]:
        fg.append(pine(x, 770, h, h * .30))
    grng = random.Random(77)
    ground = facet_layer(grng, W, 708, 744, 18, 1, 760, "ground", smooth=True, flat=True)
    parts.append(layer("Lfg", 0, "".join(fg) + "".join(ground)))
    return (f'<svg class="scene" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMax slice" aria-hidden="true" focusable="false">'
            + "".join(parts) + "</svg>")

def cta():
    rng = random.Random(101)
    W, B = 1440, 460
    parts = [faceted_sun(1080, 190, 52, 10, "moon1", "moon2")]
    stars = "".join(sparkle(x, y, s, d) for x, y, s, d in
                    [(180, 70, 6, 0), (380, 140, 4, 1.1), (640, 50, 5, 2.2), (820, 120, 4, .5), (1260, 90, 6, 1.8), (1380, 170, 4, 2.9)])
    parts.append(f'<g class="stars">{stars}</g>')
    parts.append(layer("Lfar", 0, "".join(facet_layer(rng, W, 190, 290, 10, 2, 340, "far", alt=.15))))
    parts.append(layer("Lmid", 0, "".join(facet_layer(rng, W, 250, 340, 8, 2, 400, "mid", alt=.2))))
    near = facet_layer(rng, W, 318, 392, 9, 2, 440, "near", alt=.15)
    pr = random.Random(3)
    pn = pines(pr, [60, 96, 120, 150, 1290, 1320, 1350, 1384, 1410], lambda x: 420, 70, 150)
    parts.append(layer("Lnear", 0, "".join(near) + pn))
    ground = facet_layer(random.Random(9), W, 430, 452, 20, 1, 460, "ground", smooth=True, flat=True)
    parts.append(layer("Lfoot", 0, "".join(ground)))
    return (f'<svg class="scene" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMax slice" aria-hidden="true" focusable="false">'
            + "".join(parts) + "</svg>")

def edge_top():
    rng = random.Random(55)
    W, B = 1440, 120
    body = facet_layer(rng, W, 14, 84, 12, 1, 120, "band", alt=.0)
    return (f'<svg class="edge top" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMax slice" aria-hidden="true" focusable="false">'
            + layer("Lband", 0, "".join(body)) + "</svg>")

def edge_bottom():
    rng = random.Random(66)
    W, B = 1440, 90
    body = facet_layer(rng, W, 10, 64, 14, 1, 90, "band", smooth=True, flat=True)
    return (f'<svg class="edge bottom" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMin slice" aria-hidden="true" focusable="false">'
            f'<g transform="translate(0 {B}) scale(1 -1)">' + layer("Lband", 0, "".join(body)) + "</g></svg>")

def edge_paper():
    rng = random.Random(88)
    W, B = 1440, 80
    body = facet_layer(rng, W, 10, 60, 16, 1, 80, "paper", smooth=True, flat=True)
    return (f'<svg class="edge paper" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMin slice" aria-hidden="true" focusable="false">'
            f'<g transform="translate(0 {B}) scale(1 -1)">' + layer("Lfg", 0, "".join(body)) + "</g></svg>")

def grove():
    rng = random.Random(12)
    W, B = 520, 500
    out = []
    # a floating island: meadow top, faceted rock underside, pines on top
    rim = [(14, 318), (60, 300), (120, 290), (190, 284), (260, 281), (330, 284), (400, 292), (460, 304), (506, 320)]
    under = [(506, 320), (470, 352), (420, 372), (360, 402), (300, 448), (262, 486), (226, 440), (170, 400), (110, 372), (54, 350), (14, 318)]
    tip = (262, 486)
    rock = []
    edge = [(506, 320), (470, 352), (420, 372), (360, 402), (300, 448)]
    left = [(226, 440), (170, 400), (110, 372), (54, 350), (14, 318)]
    lip = [(x, y + 26 + rng.uniform(-6, 6)) for x, y in rim]
    # underside: lip -> contracted ring -> tip, faceted and lit from the left
    ring = []
    for x, y in lip[1:-1:2]:
        t = .5 + rng.uniform(-.06, .06)
        ring.append((x + (tip[0] - x) * t + rng.uniform(-8, 8), y + (tip[1] - y) * t + rng.uniform(-10, 10)))
    ring = [lip[0]] + ring + [lip[-1]]
    tris = zipper(lip, ring) + [(p, q, tip) for p, q in zip(ring, ring[1:])]
    for t in tris:
        cx = sum(p[0] for p in t) / 3
        cls = "rk1" if cx < tip[0] - 20 else "rk2"
        rock.append(f'<path class="{cls}" d="{path_tri(t)}"/>')
        v = rng.random()
        if v < .45:
            rock.append(f'<path class="s" fill-opacity="{.06 + v * .3:.2f}" d="{path_tri(t)}"/>')
        elif v > .8:
            rock.append(f'<path class="g" fill-opacity="{.12 + (v - .8) * .8:.2f}" d="{path_tri(t)}"/>')
    # grass cap
    cap = "M" + "L".join(f"{r(x)} {r(y)}" for x, y in rim) + "L" + "L".join(f"{r(x)} {r(y)}" for x, y in reversed(lip)) + "Z"
    capf = []
    for k, (a, b) in enumerate(zip(rim, rim[1:])):
        la, lb = lip[k], lip[k + 1]
        capf.append(f'<path class="{"g" if k % 2 else "a"}" fill-opacity=".9" d="{path_tri((a, b, la))}"/>')
        if k % 3 == 0:
            capf.append(f'<path class="s" fill-opacity=".12" d="{path_tri((b, lb, la))}"/>')
    out.append(f'<g class="Lhill">{"".join(rock)}<path class="b" d="{cap}"/>{"".join(capf)}</g>')
    trees = []
    for x, h in [(70, 120), (112, 190), (156, 150), (206, 250), (258, 180), (318, 220), (366, 150), (412, 186), (456, 118)]:
        _, _ = 0, 0
        base = 300 + rng.uniform(0, 8) - (8 if 150 < x < 380 else 0)
        trees.append(pine(x, base, h, h * .32))
    out.append(f'<g class="Lgrove">{"".join(trees)}</g>')
    out.append(f'<g class="glints">{sparkle(300, 70, 8, .3)}{sparkle(130, 60, 6, 1.9)}{sparkle(470, 200, 5, 3.1)}</g>')
    return (f'<svg class="grove" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMax meet" aria-hidden="true" focusable="false">'
            + "".join(out) + "</svg>")

def mini():
    rng = random.Random(31)
    W, B = 200, 120
    out = [faceted_sun(150, 46, 14, 8)]
    out.append(layer("Lfar", 0, "".join(facet_layer(rng, W, 52, 78, 5, 1, 92, "far", alt=.2))))
    out.append(layer("Lnear", 0, "".join(facet_layer(rng, W, 74, 96, 5, 1, 110, "near", alt=.2))))
    out.append(layer("Lmeadow", 0, "".join(facet_layer(rng, W, 98, 106, 6, 1, 120, "meadow", smooth=True, alt=.3))))
    return (f'<svg class="mini" viewBox="0 0 {W} {B}" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">'
            + "".join(out) + "</svg>")

if __name__ == "__main__":
    tpl = open(sys.argv[1]).read()
    for key, fn in {"HERO": hero, "CTA": cta, "EDGE_TOP": edge_top, "EDGE_BOTTOM": edge_bottom, "GROVE": grove, "EDGE_PAPER": edge_paper, "MINI": mini}.items():
        tpl = tpl.replace("{{" + key + "}}", fn())
    open(sys.argv[2], "w").write(tpl)
    print("wrote", sys.argv[2], len(tpl), "bytes")
