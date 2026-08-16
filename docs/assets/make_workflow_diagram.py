"""Generates docs/assets/workflow_diagram.svg — the AI build workflow diagram.
Edit labels/colors below and re-run: .venv/bin/python docs/assets/make_workflow_diagram.py
"""
E = []
INK = "#1f2937"; MUT = "#475569"
def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def text(cx, cy, lines, fs=12.5, fill=INK, bold=False, anchor="middle"):
    w = ' font-weight="600"' if bold else ''
    lh = fs * 1.35
    y0 = cy - lh*(len(lines)-1)/2
    for i, ln in enumerate(lines):
        E.append(f'<text x="{cx}" y="{y0+i*lh:.1f}" font-size="{fs}" fill="{fill}" text-anchor="{anchor}" dominant-baseline="middle"{w}>{esc(ln)}</text>')
def rbox(x, y, w, h, fill, stroke, lines, fs=12, bold=False, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    E.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{d}/>')
    text(x+w/2, y+h/2, lines, fs, bold=bold)
def arrow(x1, y1, x2, y2, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    E.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{MUT}" stroke-width="1.6" marker-end="url(#arr)"{d}/>')
def harrow(x1, x2, y): arrow(x1, y, x2, y)

W = 880; CX = W/2
PX, PW = 70, 640
BL, VF = ("#e8effc", "#2f6bd8"), ("#fdf0d5", "#c77d10")
PH, PS = "#f8fafc", "#c3cdd9"
y = 24
text(CX, y+8, ["How this repo was built — AI-assisted development with adversarial gates"], 16, bold=True)
y += 40

def phase(y, title, nodes, h=118):
    E.append(f'<rect x="{PX}" y="{y}" width="{PW}" height="{h}" rx="10" fill="{PH}" stroke="{PS}" stroke-width="1.4"/>')
    text(PX+14, y+20, [title], 13, "#334155", bold=True, anchor="start")
    n = len(nodes); gap = 26
    nw = (PW - 28 - gap*(n-1)) / n
    nx = PX + 14; ny = y + 36; nh = h - 50
    centers = []
    for lines, style in nodes:
        rbox(nx, ny, nw, nh, style[0], style[1], lines, fs=11.3)
        centers.append((nx, nx+nw, ny+nh/2))
        nx += nw + gap
    for i in range(n-1):
        harrow(centers[i][1], centers[i+1][0], centers[i][2])
    return y + h

def gate(y, label):
    gy = y + 16; s = 21
    E.append(f'<path d="M {CX} {gy} l {s} {s} l {-s} {s} l {-s} {-s} Z" fill="#e2f6e9" stroke="#189a4a" stroke-width="1.5"/>')
    text(CX + s + 14, gy + s, [label], 12, "#166534", bold=True, anchor="start")
    arrow(CX, y, CX, gy - 2)
    return gy + 2*s

def down(y, n=22):
    arrow(CX, y, CX, y+n); return y+n

y = phase(y, "Phase 0 — Scaffold", [ (["Builder", "repo layout, config,", "metric registry, dependencies"], BL) ], h=104)
y = down(y)
y = phase(y, "Phase 1 — Data Validation & Check", [
    (["Checks for Each CSV +", "Data Correction Proposals", "(Deterministic) [pandas]"], BL),
    (["Adversarial verifier", "recomputes every count from", "raw CSVs with its own code"], VF)])
y = gate(y, "GATE: zero diffs")
y = down(y, 18)
y = phase(y, "Phase 2 — Signal engine", [
    (["KPI spike / drift risk /", "gap scores = SCORING.md", "(Deterministic)"], BL),
    (["Test Writer", "pytest suite written", "from SCORING.md"], BL),
    (["Adversarial verifier", "doc-vs-code consistency", "+ runs the tests"], VF)])
y = gate(y, "GATE: tests green + doc matches code")
y = down(y, 18)
AI = ("#f5f0fe", "#7c3aed")
y3 = y
outerH = 168
E.append(f'<rect x="{PX-16}" y="{y3}" width="{PW+32}" height="{outerH}" rx="14" fill="#f5f0fe" stroke="#7c3aed" stroke-width="1.6" stroke-dasharray="7 5"/>')
text(PX+2, y3+22, ["AI / LLM used here — verdict language only; all numbers deterministic"], 12.5, "#6d28d9", bold=True, anchor="start")
y = phase(y3+38, "Phase 3 — Claimed vs. Verified + harness + ledger", [
    (["Plan Facts +", "Harness Rules", "(Deterministic)"], BL),
    (["Verdict Narrative", "(AI / LLM)", "harness-checked", "[selected tier]"], AI),
    (["Ledger +", "Orchestrator", "(Deterministic)"], BL),
    (["Adversarial verifier", "attacks harness w/", "fabricated numbers"], VF)])
y = y3 + outerH
y = gate(y, "GATE: harness rejects every inaccurate number")
y = down(y, 18)
y = phase(y, "Phase 4 — Monday Digest", [
    (["API Layer", "serves digest data", "to the frontend (Node.js)"], BL),
    (["Monday Digest", "Web Application (React)", "Dr. Priya's 30-min weekly scan"], BL)])
y = gate(y, "GATE: screen numbers = pipeline numbers")
y = down(y, 18)
y = phase(y, "Phase 5 — Integration", [
    (["Clean-run rehearsal", "fresh environment,", "timed end to end"], BL),
    (["Doc-consistency pass", "every doc checked", "against the code"], VF)])
y = down(y)
rbox(CX-170, y, 340, 54, "#efeafc", "#6d3fd1", ["Evidence report → human review"], fs=12.5, bold=True)
y += 54 + 26

sh = 96
E.append(f'<rect x="{PX}" y="{y}" width="{PW}" height="{sh}" rx="10" fill="#fdeaea" stroke="#d03c3c" stroke-width="1.4" stroke-dasharray="6 4"/>')
text(PX+14, y+20, ["At every gate — the fix loop"], 13, "#991b1b", bold=True, anchor="start")
mini = ["verifier says FAIL", "fixer agent repairs", "fresh re-verification", "pass — or build aborts"]
n = len(mini); gap = 24; nw = (PW-28-gap*(n-1))/n; nx = PX+14; ny = y+36
for i, lbl in enumerate(mini):
    rbox(nx, ny, nw, 42, "#ffffff", "#d03c3c", [lbl], fs=11.3)
    if i < n-1: harrow(nx+nw, nx+nw+gap, ny+21)
    nx += nw + gap
y += sh + 24

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{y:.0f}" viewBox="0 0 {W} {y:.0f}" '
       f'font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif">'
       f'<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
       f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{MUT}"/></marker></defs>'
       f'<rect width="{W}" height="{y:.0f}" fill="#ffffff"/>' + "".join(E) + '</svg>')
import os
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_diagram.svg")
open(out, "w").write(svg)
print(f"wrote {out} ({W}x{y:.0f})")
