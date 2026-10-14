"""koukou-model.js（線上展示用）必須跟 Python 版給出一模一樣的答案。需要 node；沒有就跳過。"""

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from koukou.ai import local_model
from koukou.ai.categories import normalize
from koukou.ai.evaluate import load_evalset

JS = Path(local_model.__file__).parents[1] / "web" / "static" / "koukou-model.js"
RUNNER = """
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[2]).href);
let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const data = JSON.parse(raw);
const cfg = data.export.config;
const base = data.export.trainset.map(([text, store, category]) => ({ text, store, category, weight: 1 }));
const out = [];
for (const scenario of data.scenarios) {
  const user = scenario.user.map(([text, category]) => ({ text, store: "", category, weight: cfg.user_weight }));
  const model = new mod.NaiveBayes([...base, ...user], cfg);
  out.push(scenario.items.map((it) => mod.guess(model, it, cfg)));
}
out.push(data.normalize.map(mod.normalize));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_js_model_matches_python(tmp_path):
    from koukou.ai.categorizer import ABSTAIN, USER_WEIGHT

    rng = random.Random(7)
    items = [{"description": r["item"], "store": r["store"], "amount": rng.choice([None, 50, -10])}
             for r in load_evalset()]
    odd = ("ＳＷＥＤＩＳＨ　ＭＥＡＴＢＡＬＬＳ", "95無鉛", "1~2杯袋", "")  # 全形、純數字、空字串
    items += [{"description": d, "store": "", "amount": 30} for d in odd]
    scenarios = [
        {"user": [], "items": items},
        {"user": [["神秘禮盒 A款", "娛樂"], ["SWEDISH MEATBALLS 12P", "餐飲"], ["咖波 聯名 杯套", "其他"]],
         "items": items},
    ]
    payload = {"export": local_model.export_for_browser(), "scenarios": scenarios,
               "normalize": ["  Ｃｏｋｅ   Zero ", "ＣＩＴＹ　ＣＡＦＥ", "Café　拿鐵"]}
    runner = tmp_path / "run.mjs"
    runner.write_text(RUNNER)
    proc = subprocess.run(["node", str(runner), str(JS)], input=json.dumps(payload), capture_output=True,
                          text=True, check=True)
    *js_scenarios, js_norm = json.loads(proc.stdout)

    assert js_norm == [normalize(t) for t in payload["normalize"]]
    for scenario, js_out in zip(scenarios, js_scenarios, strict=True):
        extra = [local_model.Example(t, "", c, USER_WEIGHT) for t, c in scenario["user"]]
        model = local_model.build(extra)
        for it, js in zip(scenario["items"], js_out, strict=True):
            exclude = frozenset({"折扣"}) if it["amount"] is not None and it["amount"] > 0 else frozenset()
            cat, conf = model.predict(it["description"], it["store"], exclude)
            if conf < ABSTAIN:
                cat = "未分類"
            assert js["category"] == cat, it
            assert abs(js["confidence"] - conf) < 1e-9, it
