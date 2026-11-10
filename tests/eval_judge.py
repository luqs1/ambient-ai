"""Compare local zero-shot judges on free-form trigger sentences.
    uv run python tests/eval_judge.py [repo:onnxfile ...]
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from engine import decide

CASES = {
 "someone asks me for a file or document": ("in",
   ["can you send me the deck from yesterday?", "could you share the Q3 spreadsheet pls", "do you have the pdf of the contract?", "send over the slides when you can", "where's the invoice template?"],
   ["haha nice", "are we still on for friday?", "thanks!", "the build is broken again", "I sent the deck yesterday", "what time is the meeting?", "love the new slides"]),
 "someone wants to schedule a meeting with me": ("in",
   ["are you free for a call tomorrow at 3?", "can we find 30 mins this week to chat?", "let's set up a meeting to discuss the roadmap", "when works for a quick sync?"],
   ["can you send me the deck?", "lol", "the server is down", "great work on the launch", "the meeting went well"]),
 "I promise to do something": ("out",
   ["I'll send you the deck by Friday", "will do, I'll book the room", "sure, I can review your PR tomorrow", "on it, will get back to you after lunch", "let me check and get back to you"],
   ["wassup", "testing ai things", "did you see the game?", "you said you'd do it", "haha that's hilarious", "what time is it?"]),
 "someone reports a bug or something broken": ("in",
   ["the login page is throwing a 500 error", "checkout is broken on mobile again", "getting a crash when I open settings", "prod is down"],
   ["can we meet tomorrow?", "nice work!", "what's for lunch", "I'll fix it tonight", "shipped the new feature"]),
 "it is about pricing or a quote": ("in",
   ["how much would 40 seats cost?", "can you send pricing for the enterprise plan?", "what's your rate for a 3 month contract"],
   ["see you at 5", "the deck looks great", "who's joining the call?", "I'm running late"]),
 "I open a GitHub pull request page": ("visit",
   ["Fix flaky test by luqs1 · Pull Request #12 · luqs1/ambient-ai", "Add Windows build · Pull Request #40 · acme/api · GitHub"],
   ["luqs1/ambient-ai: Proactive agent triggers", "Issues · luqs1/ambient-ai", "weather tomorrow - Google Search", "Inbox (3) - Gmail"]),
 "someone asks me to review something": ("in",
   ["can you take a look at my PR when you get a sec?", "would love your eyes on this doc", "mind reviewing the proposal before friday?"],
   ["merged!", "thanks for the review", "lunch?", "the doc is done"]),
}

def run(judge):
    def P(prem, hyp): return judge.probs(prem, [hyp])[0]
    tot = n = 0; lines = []; t0 = time.time(); calls = 0
    for hyp, (d, pos, neg) in CASES.items():
        ctx = {"in": "Message received", "out": "Message I sent", "visit": "Page opened"}[d]
        def score(t):
            nonlocal calls; calls += 2
            return (P(f'{ctx}: "{t}"', f"In this case, {hyp}.") + P(t, f"This example is: {hyp}.")) / 2
        ps = [score(t) for t in pos]; ns = [score(t) for t in neg]
        ok = sum(p >= .5 for p in ps) + sum(x < .5 for x in ns); tot += ok; n += len(ps) + len(ns)
        lines.append(f"   {hyp[:40]:40} pos {[round(p,2) for p in ps]} neg {[round(x,2) for x in ns]}")
    ms = (time.time() - t0) / calls * 1000
    return tot, n, ms, lines

specs = sys.argv[1:] or [f"{decide.JUDGE_REPO}:onnx/model_quantized.onnx"]
for spec in specs:
    repo, _, f = spec.partition(":")
    class J(decide.Judge):
        def __init__(self):
            decide._Model.__init__(self, repo, f or "onnx/model_quantized.onnx")
            labels = {v.lower(): int(k) for k, v in self.config["id2label"].items()}
            self.entail = labels.get("entailment", 0)
    try:
        t0 = time.time(); j = J(); load = time.time() - t0
        tot, n, ms, lines = run(j)
        print(f"{repo}: {tot}/{n}  {ms:.0f} ms/check  load {load:.1f}s  labels {j.config['id2label']}")
        print("\n".join(lines))
    except Exception as e:
        print(f"{repo}: FAILED {e!r}")
