"""W301 AI 店長 Phase1 S5 (2026-07-02): scripts/reclassify_existing_competitors_w301.py.

背景: 既存 active 採用ライバル (competitor_products、S1 backfill 後は
  pricing_eligible=1) を裏で AI 一括再判定し、「疑い分だけ」を markdown で
  user 提示する one-shot script。pricing_eligible / is_active 等
  competitor_products は一切変更しない (S1 承認済み条件 1)。

カバレッジ:
  - 対象抽出が S3 (task_rival_pricing._get_listings_with_active_competitors)
    のゲート (is_active=1 AND pricing_eligible=1) と同一集合であること
  - dry-run が AI 呼出ゼロ・DB 書込ゼロであること
  - apply で rival_classifications にのみ記録され、competitor_products が
    全列不変であること
  - competitor_title データなし (competitor_products / listing_rival_discoveries
    いずれにも無し) → AI を呼ばず route='no_title_data' / classification='review'
  - --limit / --max-ai-calls の動作
  - 疑いリスト markdown の生成内容 (real は載らない、noise/review が載る)
"""
from __future__ import annotations

import sqlite3

import pytest

from monitor.database import get_conn
from monitor.rival_classifier import AIJudgeResult


def _ensure_w301_schema(conn: sqlite3.Connection) -> None:
    """S1 (migration v86) が追加するはずの competitor_products.pricing_eligible /
    rival_classifications を、このテストの中で idempotent に補う。

    ⚠️ 2026-07-02 発見 (K0/Q0 に基づき明記): 本タスク着手時点で
    `monitor/database.py` の v86 migration ブロックを実際に Read tool で確認
    できた (pricing_eligible ALTER / rival_classifications CREATE TABLE を
    含む詳細な実装) にもかかわらず、その直後の grep では同ブロックが
    disk 上から消えており (`git status` は database.py を clean=無変更と
    報告、つまり HEAD の commit f98b2a0 相当に巻き戻っている)、本番 DB
    (`data/monitor.db`) も `PRAGMA user_version` が 85 のままで v86 未適用
    だった。`git stash list` には別ワークストリーム (W300 rival_pricing 安全弁)
    が database.py を含む WIP を stash 済で発見したが、その stash diff にも
    pricing_eligible/rival_classifications は含まれておらず、本件の原因とは
    別 (=このリポジトリを複数 agent が並行編集しており、S1 の migration 編集が
    何らかの理由で disk 上から失われた可能性が高い)。

    本 script (S5) は S1 の migration 完了を前提として書かれており、
    database.py 自体を修正するのは S5 のスコープ外 (K2 Surgical、かつ
    並行編集中の可能性がある共有ファイルを不用意に触らない)。そのため
    テストはこの補助関数で必要最小限のスキーマを自己完結的に用意し、
    S1 の状態に依存せず S5 スクリプトのロジックを検証する。
    本件は生成報告で main に明示的にエスカレーションする。
    """
    try:
        conn.execute(
            "ALTER TABLE competitor_products ADD COLUMN pricing_eligible INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # 列が既に存在 = 冪等 (S1 migration が復旧していれば no-op)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rival_classifications (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            discovery_id       INTEGER,
            ebay_item_id       TEXT NOT NULL,
            competitor_item_id TEXT NOT NULL,
            classification     TEXT NOT NULL
                               CHECK(classification IN ('real','noise','review')),
            route              TEXT,
            exclude_reason     TEXT,
            title_similarity   REAL,
            price_ratio        REAL,
            same_product       INTEGER,
            variant_risk       TEXT,
            ai_condition       TEXT,
            confidence         REAL,
            reason             TEXT,
            ai_model           TEXT,
            shadow_mode        INTEGER NOT NULL DEFAULT 1,
            would_be_eligible  INTEGER NOT NULL DEFAULT 0,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ddu_sellers (seller_id TEXT PRIMARY KEY, reason TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS warning_brand_watchlist (brand TEXT PRIMARY KEY)"
    )


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    import monitor.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    with db_mod.get_conn() as conn:
        _ensure_w301_schema(conn)
    yield db_path


def _seed_listing(
    ebay_item_id: str,
    title: str = "Sony WH-1000XM5 Wireless Headphones Black",
    price: float = 200.0,
    condition_id: str = "3000",
    condition_rank: str = "B",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_listings
               (ebay_item_id, sku, title, current_price, ebay_condition_id, condition_rank)
               VALUES (?, 'stock01', ?, ?, ?, ?)""",
            (ebay_item_id, title, price, condition_id, condition_rank),
        )


def _seed_competitor(
    our_item_id: str,
    competitor_item_id: str,
    *,
    is_active: int = 1,
    pricing_eligible: "int | None" = 1,
    competitor_seller: "str | None" = None,
    competitor_price_usd: "float | None" = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO competitor_products
               (our_item_id, competitor_item_id, competitor_seller, is_active,
                pricing_eligible, competitor_price_usd)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (our_item_id, competitor_item_id, competitor_seller, is_active,
             pricing_eligible, competitor_price_usd),
        )
        return cur.lastrowid


def _seed_discovery(
    ebay_item_id: str,
    competitor_item_id: str,
    competitor_title: str,
    competitor_seller: str = "jp_seller_1",
    competitor_price_usd: "float | None" = 190.0,
    status: str = "monitoring_added",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword, status)
               VALUES (?, ?, ?, ?, ?, 'sony headphones', ?)""",
            (ebay_item_id, competitor_seller, competitor_item_id,
             competitor_title, competitor_price_usd, status),
        )


def _dump_competitor_products() -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM competitor_products ORDER BY id"
        ).fetchall()
    return [tuple(r) for r in rows]


# ────────────────────────────────────────────────────────────────
# 対象抽出 = S3 ゲートと同一集合
# ────────────────────────────────────────────────────────────────

def test_target_matches_s3_gate(tmp_db):
    _seed_listing("OUR_A")
    _seed_listing("OUR_B")
    _seed_listing("OUR_C")
    _seed_listing("OUR_D")

    _seed_competitor("OUR_A", "COMP_A", is_active=1, pricing_eligible=1)   # 対象
    _seed_competitor("OUR_B", "COMP_B", is_active=1, pricing_eligible=0)   # Shadow 未backfill=除外
    _seed_competitor("OUR_C", "COMP_C", is_active=0, pricing_eligible=1)   # 非採用=除外
    _seed_competitor("OUR_D", "COMP_D", is_active=1, pricing_eligible=None)  # NULL=除外

    from scripts.reclassify_existing_competitors_w301 import _fetch_targets
    targets = _fetch_targets()
    comp_ids = {t["competitor_item_id"] for t in targets}
    assert comp_ids == {"COMP_A"}
    assert {t["our_item_id"] for t in targets} == {"OUR_A"}

    # 注: 本来は tasks.task_rival_pricing._get_listings_with_active_competitors()
    # (S3 の実クエリ) とも突合する設計だったが、2026-07-02 実装時点で
    # tasks/task_rival_pricing.py が (別ワークストリームによる同ファイルへの
    # 並行編集で) is_active=1 のみのゲート (pricing_eligible 未導入版) と
    # is_active=1 AND pricing_eligible=1 版との間で断続的に切り替わる不安定な
    # 状態を観測した (S5 のスコープ外、report で main にエスカレーション済)。
    # そのため本テストは S5 自身のクエリが「設計書が定める S3 ゲート」
    # (is_active=1 AND COALESCE(pricing_eligible,0)=1) を正しく実装している
    # ことの検証に留め、他ファイルの現在の live 状態への依存を排除する。


# ────────────────────────────────────────────────────────────────
# dry-run: AI 呼出ゼロ・DB 書込ゼロ
# ────────────────────────────────────────────────────────────────

def test_dry_run_no_ai_calls_no_writes(tmp_db, monkeypatch):
    _seed_listing("OUR1", title="Sony WH-1000XM5 Wireless Headphones Black Bluetooth")
    _seed_competitor("OUR1", "COMP1", competitor_price_usd=150.0)
    _seed_discovery(
        "OUR1", "COMP1",
        competitor_title="WH-1000XM5 ソニー ワイヤレス ヘッドホン",  # グレー相当
    )

    import scripts.reclassify_existing_competitors_w301 as mod

    def _boom(*a, **kw):
        raise AssertionError("dry-run で AI が呼ばれてはいけない")

    monkeypatch.setattr(mod, "judge_rival", _boom)

    summary = mod._run(apply=False, limit=0, max_ai_calls=200, output_dir=str(tmp_db.parent))
    assert summary["apply"] is False
    assert summary["target_count"] == 1

    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM rival_classifications").fetchone()[0]
    assert n == 0, "dry-run は rival_classifications に一切書き込まないはず"


# ────────────────────────────────────────────────────────────────
# apply: rival_classifications にのみ記録、competitor_products 完全不変
# ────────────────────────────────────────────────────────────────

def test_apply_persists_only_rival_classifications_and_leaves_competitor_products_untouched(
    tmp_db, monkeypatch
):
    # noise (スコア足切り、AI 不要)
    _seed_listing("OUR1", title="Sony WH-1000XM5 Wireless Headphones")
    _seed_competitor("OUR1", "COMP1", competitor_price_usd=190.0)
    _seed_discovery("OUR1", "COMP1", competitor_title="全く関係ない掃除機のパーツセット")

    # real (グレー→AI mock)
    _seed_listing("OUR2", title="Panasonic Rice Cooker SR-HB105")
    _seed_competitor("OUR2", "COMP2", competitor_price_usd=95.0)
    _seed_discovery("OUR2", "COMP2", competitor_title="パナソニック 炊飯器 SR-HB105 美品")

    import scripts.reclassify_existing_competitors_w301 as mod

    def _fake_judge_rival(signals, model="unused"):
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=0.95, reason="同一商品", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    monkeypatch.setattr(mod, "judge_rival", _fake_judge_rival)

    before = _dump_competitor_products()
    summary = mod._run(apply=True, limit=0, max_ai_calls=200, output_dir=str(tmp_db.parent))
    after = _dump_competitor_products()

    assert before == after, "competitor_products は 1 バイトも変更されてはいけない"

    assert summary["counts"]["noise"] == 1
    assert summary["counts"]["real"] == 1

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT competitor_item_id, classification, route, shadow_mode, "
            "would_be_eligible, discovery_id FROM rival_classifications ORDER BY competitor_item_id"
        ).fetchall()
    assert len(rows) == 2
    by_comp = {r[0]: r for r in rows}
    assert by_comp["COMP1"][1] == "noise"
    assert by_comp["COMP1"][2] == "score"
    assert by_comp["COMP2"][1] == "real"
    assert by_comp["COMP2"][3] == 1  # shadow_mode 固定
    assert by_comp["COMP2"][4] == 1  # would_be_eligible
    # S5 は discoveries 経由ではないため discovery_id は常に NULL
    assert by_comp["COMP1"][5] is None
    assert by_comp["COMP2"][5] is None


# ────────────────────────────────────────────────────────────────
# competitor_title データなし → AI 不使用、no_title_data で review
# ────────────────────────────────────────────────────────────────

def test_no_title_data_route_skips_ai(tmp_db, monkeypatch):
    _seed_listing("OUR1")
    # listing_rival_discoveries に一致行なし、competitor_price_usd も未設定
    _seed_competitor("OUR1", "COMP1", competitor_price_usd=None)

    import scripts.reclassify_existing_competitors_w301 as mod

    def _boom(*a, **kw):
        raise AssertionError("competitor_title 不明なのに AI を呼んではいけない")

    monkeypatch.setattr(mod, "judge_rival", _boom)

    summary = mod._run(apply=True, limit=0, max_ai_calls=200, output_dir=str(tmp_db.parent))
    assert summary["counts"]["review"] == 1
    assert summary["ai_calls_used"] == 0

    with get_conn() as conn:
        row = conn.execute(
            "SELECT classification, route, reason FROM rival_classifications "
            "WHERE competitor_item_id='COMP1'"
        ).fetchone()
    assert row[0] == "review"
    assert row[1] == "no_title_data"
    assert "AI 判定不能" in row[2]


# ────────────────────────────────────────────────────────────────
# --limit
# ────────────────────────────────────────────────────────────────

def test_limit_option_restricts_processed_rows(tmp_db):
    for i in range(5):
        our_id = f"OUR{i}"
        comp_id = f"COMP{i}"
        _seed_listing(our_id, title=f"全く関係ない掃除機パーツ {i}")
        _seed_competitor(our_id, comp_id, competitor_price_usd=1.0)
        _seed_discovery(our_id, comp_id, competitor_title=f"別カテゴリ商品 {i}")

    from scripts.reclassify_existing_competitors_w301 import _run
    summary = _run(apply=True, limit=2, max_ai_calls=200, output_dir=str(tmp_db.parent))
    assert summary["target_count"] == 2

    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM rival_classifications").fetchone()[0]
    assert n == 2


# ────────────────────────────────────────────────────────────────
# --max-ai-calls cap
# ────────────────────────────────────────────────────────────────

def test_max_ai_calls_cap_limits_ai_invocations(tmp_db, monkeypatch):
    for i in range(3):
        our_id = f"OUR{i}"
        comp_id = f"COMP{i}"
        _seed_listing(
            our_id,
            title=f"Sony WH-1000XM5 Wireless Headphones Black Bluetooth {i}",
        )
        _seed_competitor(our_id, comp_id, competitor_price_usd=150.0)
        _seed_discovery(
            our_id, comp_id,
            competitor_title=f"WH-1000XM5 ソニー ワイヤレス ヘッドホン {i}",
        )

    call_count = {"n": 0}

    import scripts.reclassify_existing_competitors_w301 as mod

    def _fake_judge_rival(signals, model="unused"):
        call_count["n"] += 1
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=0.95, reason="同一商品", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    monkeypatch.setattr(mod, "judge_rival", _fake_judge_rival)

    summary = mod._run(apply=True, limit=0, max_ai_calls=1, output_dir=str(tmp_db.parent))
    assert call_count["n"] == 1
    assert summary["ai_calls_used"] == 1
    assert summary["counts"]["real"] == 1
    assert summary["counts"]["review"] == 2

    with get_conn() as conn:
        capped = conn.execute(
            "SELECT COUNT(*) FROM rival_classifications WHERE route='ai_cap_exceeded'"
        ).fetchone()[0]
    assert capped == 2


# ────────────────────────────────────────────────────────────────
# 疑いリスト markdown
# ────────────────────────────────────────────────────────────────

def test_suspicious_markdown_contains_only_non_real(tmp_db, monkeypatch):
    _seed_listing("OUR1", title="Sony WH-1000XM5 Wireless Headphones")
    _seed_competitor("OUR1", "COMP1", competitor_price_usd=190.0)
    _seed_discovery("OUR1", "COMP1", competitor_title="全く関係ない掃除機のパーツセット")

    _seed_listing("OUR2", title="Panasonic Rice Cooker SR-HB105")
    _seed_competitor("OUR2", "COMP2", competitor_price_usd=95.0)
    _seed_discovery("OUR2", "COMP2", competitor_title="パナソニック 炊飯器 SR-HB105 美品")

    import scripts.reclassify_existing_competitors_w301 as mod

    def _fake_judge_rival(signals, model="unused"):
        return AIJudgeResult(
            same_product=True, variant_risk="none", condition="USED",
            confidence=0.95, reason="同一商品", ai_model="claude-haiku-4-5-20251001",
            route="ai",
        )

    monkeypatch.setattr(mod, "judge_rival", _fake_judge_rival)

    out_dir = tmp_db.parent / "out_md"
    summary = mod._run(apply=True, limit=0, max_ai_calls=200, output_dir=str(out_dir))

    md_path = summary["suspicious_markdown_path"]
    from pathlib import Path
    text = Path(md_path).read_text(encoding="utf-8")

    assert "COMP1" in text  # noise → 疑いリストに載る
    assert "COMP2" not in text  # real → 疑いリストに載らない
    assert "Sony WH-1000XM5 Wireless Headphones" in text
    assert "OUR1"[-4:] in text  # ebay_item_id 末尾4桁相当の断片
