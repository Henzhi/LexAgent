"""知识库版本新鲜度体检：同一部法律多版本共存的**只读**判定脚本。

背景（见 docs/知识库版本新鲜度体检报告-2026-09-07.md）：
  历史上多次 ingest / rechunk 叠加，导致同一部法律在库中存在多个版本：
    - 真版本冲突——如《刑法》(2023修正) 与不含修正案（十二）的无后缀旧版，
      两者 status 同为 'active'，检索会召回已过时条文（P0 可用性缺陷）；
    - 合法并存——如《宪法修正案》1988/1993/1999/2004/2018 各自独立有效，**不得误删**；
    - 纯重复——文件名尾部空格（"XX法(2023修订) "）导致同一份文件进了两次。

  三者处置完全不同，因此**判定必须用内容证据（版本时间戳 + 条文覆盖 + 内容指纹），
  不能用文件名**——无后缀文件未必是旧版，且多组被不同批次 rechunk 过。

判定流程（一组 = 同一 base 法名的多个 documents 行）：
  1. 版本时间戳 = max(标题年份, 沿革文本中最大的「YYYY年M月D日」)；
  2. 内容指纹（去空白 md5）+ Jaccard 相似度 → 识别重复入库；
  3. 条文覆盖 = 旧版条文号被最高版本覆盖的比例 → 判断是否为替代文本；
  4. 扎口角色 role ∈ {current, superseded, duplicate, keep, ambiguous}。

默认 **dry-run**：只打印并落盘 CSV/JSON。确认判定表无误后加 `--apply` 才写回
`documents.status` / `documents.superseded_by`（写回前自动导出备份 JSON）。
ambiguity 一律不自动处置，交人工。

用法:
  uv run python evaluation/scripts/check_law_version_conflicts.py
  uv run python evaluation/scripts/check_law_version_conflicts.py --base 中华人民共和国刑法
  uv run python evaluation/scripts/check_law_version_conflicts.py --apply
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "evaluation" / "data"

# 相似度达到该阈值且文本长度接近 → 判为同一份文件的重复入库
DUPLICATE_JACCARD = 0.98
DUPLICATE_LEN_RATIO = 0.02
# 旧版条文被新版覆盖到该比例 → 判为已被替代
SUPERSEDED_COVERAGE = 0.9

_TITLE_VERSION_RE = re.compile(r"[（(]\s*(\d{4})")
_FULL_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_YEAR_ONLY_RE = re.compile(r"(\d{4})\s*年")
_ARTICLE_RE = re.compile(r"第\s*([〇零一二三四五六七八九十百千0-9]+)\s*条(?:\s*之\s*([一二三四五六七八九十0-9]+))?")


def load_conn_str() -> str:
    """连接串：优先环境变量 PG_CONN，回落到 src.config（会自动加载项目 .env）。"""
    import os

    if os.getenv("PG_CONN"):
        return os.environ["PG_CONN"]
    from src.config import PG_CONN

    return PG_CONN


class LawDoc:
    """一个 documents 行的判定视图（构造不依赖 DB，便于单测）。"""

    def __init__(self, doc_id: str, title: str, chunks: list[str]):
        self.doc_id = doc_id
        self.title = title
        self.chunks = chunks

    @property
    def content(self) -> str:
        return "\n".join(self.chunks)

    @property
    def prologue(self) -> str:
        """沿革文本：前两篇 chunk 通常含「…通过 …修订 根据…修正」。"""
        return "\n".join(self.chunks[:2])[:4000]

    @property
    def article_nums(self) -> set[str]:
        return extract_article_numbers(self.content)

    @property
    def norm_articles(self) -> dict[str, str]:
        """条文号 → 正文（已剥【罪名】标题），跨爬虫来源可比。"""
        return _article_map_impl(self.content, strip_labels=True)

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.content)

    @property
    def plain_len(self) -> int:
        return len(re.sub(r"\s+", "", self.content))


def base_law_name(title: str) -> str:
    """去扩展名 + 去尾部版本括号 + 空白压缩 → 基准法名。"""
    name = title.rsplit(".", 1)[0] if title.lower().endswith(".txt") else title
    name = re.sub(r"[（(]\s*\d{4}[^）)]*[）)]\s*$", "", name)
    return " ".join(name.split())


def title_year(title: str) -> int | None:
    """标题显式标注的版本年份，如「刑法(2023修正)」→ 2023。"""
    m = _TITLE_VERSION_RE.search(title)
    return int(m.group(1)) if m else None


def max_document_date(text: str) -> tuple[int | None, str]:
    """文本中最大的制定/修订日期 → (YYYYMMDD 数值, 命中原文)。

    优先完整日期；没有完整日期时退化为「YYYY年」，避免漏判。
    """
    full = _FULL_DATE_RE.findall(text)
    if full:
        y, mo, d = max(full, key=lambda t: (int(t[0]), int(t[1]), int(t[2])))
        return int(f"{int(y):04d}{int(mo):02d}{int(d):02d}"), f"{y}年{mo}月{d}日"
    years = _YEAR_ONLY_RE.findall(text)
    if years:
        y = max(int(x) for x in years)
        return y * 10000, f"{y}年"
    return None, ""


_CN_DIGITS = "零一二三四五六七八九"


def _int_to_cn(n: int) -> str:
    """阿拉伯数字 → 中文数字，用于把「第165条」与「第一百六十五条」归一到同一 key。"""
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n - 10] if n > 10 else "")
    if n < 100:
        tens, rest = divmod(n, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[rest] if rest else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = _CN_DIGITS[hundreds] + "百"
        if rest == 0:
            return head
        if rest < 10:
            return head + "零" + _CN_DIGITS[rest]
        return head + _int_to_cn(rest)
    thousands, rest = divmod(n, 1000)
    head = _CN_DIGITS[thousands] + "千"
    if rest == 0:
        return head
    if rest < 100:
        return head + "零" + _int_to_cn(rest)
    return head + _int_to_cn(rest)


def normalize_article_token(token: str) -> str:
    """条文号归一：阿拉伯数字与中文数字统一为中文，避免同一条文被算成两条。"""
    token = token.strip()
    return _int_to_cn(int(token)) if token.isdigit() else token


def _article_map_impl(text: str, strip_labels: bool) -> dict[str, str]:
    parts = re.split(r"(?=第[〇零一二三四五六七八九十百千0-9]+条)", text)
    out: dict[str, str] = {}
    for part in parts:
        m = _ARTICLE_RE.match(part)
        if not m:
            continue
        key = f"第{normalize_article_token(m.group(1))}条"
        if m.group(2):
            key = f"{key}之{normalize_article_token(m.group(2))}"
        body = re.sub(r"\s+", "", part)
        # 不同爬取来源会给条文加【罪名】标题（如「第三百四十七条【走私…罪】」），
        # 这是排版噪声不是版本差异，比对前必须剥掉（见 E-01 教训：先跑一遍再下结论）。
        if strip_labels:
            body = strip_offense_labels(body)
        out[key] = body
    return out


def extract_article_numbers(text: str) -> set[str]:
    """抽取条文号集合（「第X条」「第X条之Y」），用于判断新旧版包含关系。

    数字形态统一为中文（第165条 ≡ 第一百六十五条），否则跨版本覆盖率会被系统性低估。
    """
    out = set()
    for num, sub in _ARTICLE_RE.findall(text):
        key = f"第{normalize_article_token(num)}条"
        if sub:
            sub_cn = normalize_article_token(sub)
            if sub_cn and not sub_cn.startswith("第"):
                key = f"{key}之{sub_cn}"
        out.add(key)
    return out


def article_map(text: str) -> dict[str, str]:
    """条文号 → 去空白正文（保留【罪名】标题），用于对外展示。"""
    return _article_map_impl(text, strip_labels=False)


def strip_offense_labels(text: str) -> str:
    """剥掉条文里的【罪名】标题，消除爬取来源差异造成的伪差分。"""
    return re.sub(r"【[^】]*】", "", text)


def compare_articles(top_map: dict[str, str], other_map: dict[str, str]) -> tuple[int, int]:
    """逐条正文比对，返回 (实质差异条数, 共同条文数)。

    这是 duplicate 与 superseded 的分水岭：
      - 0 条差异 → 同一份文件的重复入库（切分批次不同而已）；
      - 有差异 → 真版本差异，差异条数即为影响面（如刑法两版差 7 条）。
    """
    common = set(top_map) & set(other_map)
    diff = sum(1 for k in common if top_map[k] != other_map[k])
    return diff, len(common)


def content_fingerprint(text: str) -> str:
    """去空白后的内容指纹：切分批次不同但最终文本一致时也能命中。"""
    return hashlib.md5(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()[:12]  # noqa: S324


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def coverage(newer: set[str], older: set[str]) -> float:
    """older 中条文被 newer 覆盖的比例。"""
    return len(older & newer) / len(older) if older else 0.0


def version_score(doc: LawDoc) -> tuple[int, str]:
    """版本先后标量：标题年份优先（YYYY0000），缺失时用沿革最大日期兜底。

    标题年份相同时再比具体日期，避免同年修正 vs 修订无法区分。
    """
    ty = title_year(doc.title)
    if ty is not None:
        dy, _ = max_document_date(doc.prologue)
        return ty * 10000 + (dy % 10000 if dy else 0), f"title={ty}"
    dy, raw = max_document_date(doc.prologue)
    if dy is not None:
        return dy, f"doc_date={raw}"
    return 0, "unknown"


def is_amendment_series(base_name: str) -> bool:
    """修正案/两高解释类：各年份文件本身独立有效，属合法并存，不参与替代判定。"""
    return base_name.endswith("修正案") or base_name.startswith(("最高人民法院", "最高人民检察院"))


def looks_duplicate(top: LawDoc, other: LawDoc) -> tuple[bool, str]:
    """是否同一份文件的重复入库：**逐条正文比对**，零差异才算重复。

    ⚠️ 早期版本用「正文长度差 + 条文号 Jaccard」做判据，被真实数据证伪：
       刑法两版条文集合相同（452 条）、长度接近，仅 3 条实质差异，
       却被误判为 duplicate。版本冲突与重复入库必须看**条文正文**，不能看编号与长度。
    """
    if top.fingerprint == other.fingerprint:
        return True, "去空白正文 md5 完全一致"
    diff, common = compare_articles(top.norm_articles, other.norm_articles)
    if common and diff == 0 and len(top.norm_articles) == len(other.norm_articles):
        return True, f"逐条正文比对：{common} 条全部一致（差异仅在【罪名】标题或切分粒度）"
    return False, ""


def classify_group(base_name: str, docs: list[LawDoc]) -> list[dict]:
    """判定一组文档内每个文档的角色。返回判定行（dict）列表。"""
    scored = []
    for d in docs:
        score, source = version_score(d)
        scored.append((score, source, d))
    # 版本降序；同分时 chunk 多者在前（更完整的切分通常才是我们要保留的）
    scored.sort(key=lambda t: (t[0], len(t[2].chunks)), reverse=True)
    top_score, _, top = scored[0]

    rows: list[dict] = []
    for score, source, d in scored:
        row = {
            "base_name": base_name,
            "doc_id": d.doc_id,
            "title": d.title,
            "chunks": len(d.chunks),
            "articles": len(d.article_nums),
            "title_year": title_year(d.title),
            "version_score": score,
            "version_source": source,
            "fingerprint": d.fingerprint,
            "role": "",
            "evidence": "",
        }
        rows.append(row)

        if is_amendment_series(base_name):
            row["role"] = "keep"
            row["evidence"] = "修正案/司法解释系列：各年份文件独立有效（白名单，不处置）"
            continue

        if d is top:
            row["role"] = "current"
            row["evidence"] = f"组内最高版本（{source}）"
            continue

        dup, why = looks_duplicate(top, d)
        if dup:
            row["role"] = "duplicate"
            row["evidence"] = f"与 {top.title} 重复入库：{why}"
            continue

        diff, common = compare_articles(top.norm_articles, d.norm_articles)
        missing = set(top.norm_articles) - set(d.norm_articles)
        if common and diff == 0 and missing and set(d.norm_articles) <= set(top.norm_articles):
            row["role"] = "partial"
            row["evidence"] = (
                f"残缺副本：{common} 条正文与保留版逐条一致，但少了 {len(missing)} 条"
                f"（保留版共 {len(top.norm_articles)} 条）"
            )
            continue

        cov = coverage(top.article_nums, d.article_nums)
        impact = f"逐条比对 {common} 条中有 {diff} 条实质差异"
        if cov >= SUPERSEDED_COVERAGE and score < top_score:
            row["role"] = "superseded"
            row["evidence"] = f"新版覆盖其 {cov:.0%} 条文且版本更旧（{score} < {top_score}）；{impact}"
        else:
            row["role"] = "ambiguous"
            row["evidence"] = f"无法自动定序（版本 {score} vs {top_score}，条文覆盖 {cov:.0%}）；{impact}，需人工确认"
    return rows


ROLE_STATUS = {
    "current": ("active", None),
    "keep": ("active", None),
    "superseded": ("superseded", "__top__"),
    # 重复入库与残缺副本：都不应该再参与检索，处置同为 duplicate（可逆，不物理删除）
    "duplicate": ("duplicate", None),
    "partial": ("duplicate", None),
}


def fetch_groups(conn, only_base: str | None = None) -> dict[str, list[LawDoc]]:
    """从 DB 取 all documents 的 id/title，按 base 法名分组，只回填 len>=2 的组。"""
    with conn.cursor() as cur:
        cur.execute("SELECT id, title FROM documents ORDER BY title")
        rows = cur.fetchall()

    grouped: dict[str, list[tuple[str, str]]] = {}
    for doc_id, title in rows:
        grouped.setdefault(base_law_name(title), []).append((doc_id, title))

    targets = {
        base: items for base, items in grouped.items() if len(items) >= 2 and (only_base is None or base == only_base)
    }
    if not targets:
        return {}

    doc_ids = [i for items in targets.values() for i, _ in items]
    with conn.cursor() as cur:
        cur.execute(
            # ⚠️ 必须按 metadata.paragraph_index 还原原文顺序：chunk 的 id 是 uuid、
            # 无语义顺序，按 id 拼接会把条文拆碎、让逐条比对得到伪差异。
            "SELECT doc_id, content FROM document_chunks "
            "WHERE doc_id = ANY(%s::uuid[]) "
            "ORDER BY doc_id, COALESCE((metadata->>'paragraph_index')::int, 999999)",
            (doc_ids,),
        )
        chunks: dict[str, list[str]] = {}
        for doc_id, content in cur.fetchall():
            chunks.setdefault(doc_id, []).append(content or "")

    return {
        base: [LawDoc(doc_id, title, chunks.get(doc_id, [])) for doc_id, title in items]
        for base, items in targets.items()
    }


def backup_rows(conn, doc_ids: list[str], out_path: Path) -> None:
    """写回前导出受影响行的原值，保证可回滚。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, status, superseded_by, version, effective_date FROM documents WHERE id = ANY(%s::uuid[])",
            (doc_ids,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def apply_verdicts(conn, rows: list[dict]) -> int:
    """按判定表写回 status / superseded_by。返回实际更新行数。"""
    top_ids = {r["base_name"]: r["doc_id"] for r in rows if r["role"] == "current"}
    targets = [r for r in rows if r["role"] in ROLE_STATUS and r["role"] != "current"]
    updated = 0
    with conn.cursor() as cur:
        for r in targets:
            status, fb = ROLE_STATUS[r["role"]]
            fb_id = top_ids.get(r["base_name"]) if fb == "__top__" else None
            cur.execute(
                "UPDATE documents SET status = %s, superseded_by = %s WHERE id = %s",
                (status, fb_id, r["doc_id"]),
            )
            updated += cur.rowcount
    conn.commit()
    return updated


FIELDS = [
    "base_name",
    "title",
    "role",
    "chunks",
    "articles",
    "title_year",
    "version_score",
    "version_source",
    "evidence",
    "fingerprint",
    "doc_id",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库版本冲突判定（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="写回 documents.status / superseded_by")
    parser.add_argument("--base", help="只检查指定基准法名")
    parser.add_argument("--csv", default=str(OUT_DIR / "law_version_verdicts.csv"))
    parser.add_argument("--json", default=str(OUT_DIR / "law_version_verdicts.json"))
    args = parser.parse_args()

    import psycopg2

    conn = psycopg2.connect(load_conn_str())
    try:
        groups = fetch_groups(conn, args.base)
    finally:
        if not args.apply:
            conn.close()

    if not groups:
        print("没有发现同法名多版本共存的文档组。")
        return 0

    rows: list[dict] = []
    for base, docs in sorted(groups.items(), key=lambda kv: -sum(len(d.chunks) for d in kv[1])):
        rows.extend(classify_group(base, docs))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["role"]] = counts.get(r["role"], 0) + 1

    print(f"\n共 {len(groups)} 组 / {len(rows)} 个文档待判定：")
    for base, docs in sorted(groups.items(), key=lambda kv: -sum(len(d.chunks) for d in kv[1])):
        group_rows = [r for r in rows if r["base_name"] == base]
        print(f"\n· {base}（{len(docs)} 份）")
        for r in group_rows:
            print(
                f"    [{r['role']:>10}] {r['title']} — {r['chunks']} chunks / {r['articles']} 条"
                f"（{r['version_source']}）"
            )
            print(f"                {r['evidence']}")
    print("\n汇总：" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n判定表已落盘：{csv_path} / {args.json}")

    ambiguous = [r for r in rows if r["role"] == "ambiguous"]
    if ambiguous:
        print("\n以下需人工确认（脚本不处置）：")
        for r in ambiguous:
            print(f"  - {r['title']}：{r['evidence']}")

    if not args.apply:
        print("\n[dry-run] 未改动数据库。确认判定表后加 --apply 写回 status/superseded_by。")
        conn.close()
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = OUT_DIR / f"documents_status_backup_{ts}.json"
    backup_rows(conn, [r["doc_id"] for r in rows], backup_path)
    print(f"已备份受影响行 → {backup_path}")
    n = apply_verdicts(conn, rows)
    conn.close()
    print(
        f"已写回 {n} 行（current/keep 保持 active；superseded → status='superseded'；"
        f"duplicate → status='duplicate'）。回滚请对照备份 JSON。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
