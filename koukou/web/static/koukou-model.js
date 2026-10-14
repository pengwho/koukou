// 阿財內建小模型的 JavaScript 版，跟 koukou/ai/local_model.py 一行對一行。
// 線上展示沒有伺服器，所以在瀏覽器裡訓練、預測、吃進你的修正。
// tests/test_js_model.py 會拿兩邊的結果逐筆比對，確保答案完全一樣。

/** 跟 koukou.ai.categories.normalize 相同：NFKC、壓縮空白、去頭尾、轉小寫。 */
export function normalize(text) {
  return (text ?? "").normalize("NFKC").replace(/\s+/gu, " ").trim().toLowerCase();
}

const stem = (w) => (w.length > 4 && w.endsWith("s") ? w.slice(0, -1) : w);

/** 回傳 Map<特徵, 次數>，插入順序跟 Python 的 Counter 一樣。 */
export function features(text, store, cfg) {
  const norm = normalize(text);
  const t = "^" + norm.replace(/\p{Nd}+/gu, "0").replace(/[a-z]+/g, "").replaceAll(" ", "") + "$";
  const chars = Array.from(t);
  const feats = new Map();
  const add = (f, n) => feats.set(f, (feats.get(f) ?? 0) + n);
  for (const n of [1, 2, 3]) {
    for (let i = 0; i + n <= chars.length; i++) {
      const gram = chars.slice(i, i + n).join("");
      if (/[^$^0]/u.test(gram)) add(gram, 1); // 只有數字或邊界的片段不算
    }
  }
  for (const word of norm.match(/[a-z]{3,}/g) ?? []) add("字:" + stem(word), cfg.word_weight);
  if (store) {
    const name = normalize(store);
    add("店:" + name, cfg.store_weight);
    const nc = Array.from(name);
    for (let i = 0; i + 1 < nc.length; i++) add("店2:" + nc[i] + nc[i + 1], cfg.store_gram_weight);
  }
  return feats;
}

export class NaiveBayes {
  /** examples: [{text, store, category, weight}] */
  constructor(examples, cfg) {
    this.cfg = cfg;
    const counts = new Map();
    const docs = new Map();
    for (const ex of examples) {
      if (!counts.has(ex.category)) counts.set(ex.category, new Map());
      const c = counts.get(ex.category);
      const w = ex.weight ?? 1;
      for (const [f, n] of features(ex.text, ex.store, cfg)) c.set(f, (c.get(f) ?? 0) + n * w);
      docs.set(ex.category, (docs.get(ex.category) ?? 0) + w);
    }
    this.classes = cfg.categories.filter((c) => (docs.get(c) ?? 0) > 0);
    this.vocab = new Set();
    for (const c of counts.values()) for (const f of c.keys()) this.vocab.add(f);
    const v = this.vocab.size;
    let totalDocs = 0;
    for (const n of docs.values()) totalDocs += n;
    this.logPrior = new Map();
    this.logLik = new Map();
    this.logUnseen = new Map();
    for (const c of this.classes) {
      this.logPrior.set(c, 0.5 * Math.log(docs.get(c) / totalDocs));
      const cc = counts.get(c);
      let sum = 0;
      for (const n of cc.values()) sum += n;
      const denom = sum + cfg.alpha * v;
      const lik = new Map();
      for (const [f, n] of cc) lik.set(f, Math.log((n + cfg.alpha) / denom));
      this.logLik.set(c, lik);
      this.logUnseen.set(c, Math.log(cfg.alpha / denom));
    }
  }

  /** 回傳 [分類, 信心]；完全沒有認得的特徵時回傳 ["其他", 0]。 */
  predict(text, store = "", exclude = new Set()) {
    const all = features(text, store, this.cfg);
    const feats = [...all].filter(([f]) => this.vocab.has(f));
    if (!feats.length) return ["其他", 0];
    const scores = [];
    for (const c of this.classes) {
      if (exclude.has(c)) continue;
      const lik = this.logLik.get(c);
      const unseen = this.logUnseen.get(c);
      let s = this.logPrior.get(c);
      let acc = 0;
      for (const [f, n] of feats) acc += n * (lik.get(f) ?? unseen);
      scores.push([c, s + acc]);
    }
    let top = scores[0];
    for (const sc of scores) if (sc[1] > top[1]) top = sc;
    let z = 0;
    for (const [, v] of scores) z += Math.exp((v - top[1]) / this.cfg.temperature);
    return [top[0], 1 / z];
  }
}

/** 跟 categorizer.classify 的小模型那一段相同：正數品項排除「折扣」、信心太低就不猜。 */
export function guess(model, item, cfg) {
  const exclude = item.amount != null && item.amount > 0 ? new Set(["折扣"]) : new Set();
  const [category, confidence] = model.predict(item.description, item.store, exclude);
  return { category: confidence >= cfg.abstain ? category : "未分類", confidence };
}
