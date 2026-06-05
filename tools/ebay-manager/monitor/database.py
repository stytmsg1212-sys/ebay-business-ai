"""
監視アイテムのSQLiteデータベース管理
"""
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "monitor.db"
logger = logging.getLogger(__name__)

DEFAULT_SITE_CONFIGS = [
    # ---- フリマ・オークション ----
    {
        "site_name": "メルカリ",
        "url_keyword": "mercari",
        "in_stock_text1": "購入手続きへ",
        "in_stock_text2": "",
        "sold_out_text": "売り切れました",
        "no_page_text": "ページが見つかりません",
        "common_url": "https://jp.mercari.com/item/",
        "convert_url": "ebayme_",
    },
    {
        "site_name": "メルカリショップ",
        "url_keyword": "jp.mercari.com/shops",
        "in_stock_text1": "購入手続きへ",
        "in_stock_text2": "",
        "sold_out_text": "売り切れ",
        "no_page_text": "ページが見つかりません",
        "common_url": "",
        "convert_url": "ebayMS_",
    },
    {
        "site_name": "ラクマ",
        "url_keyword": "item.fril",
        "in_stock_text1": "購入に進む",
        "in_stock_text2": "",
        "sold_out_text": "SOLD OUT",
        "no_page_text": "",
        "common_url": "https://fril.jp/item/",
        "convert_url": "ebayrm_",
    },
    {
        "site_name": "Paypayフリマ",
        "url_keyword": "paypayflexmarket",
        "in_stock_text1": "購入手続きへ",
        "in_stock_text2": "",
        "sold_out_text": "関連商品をアプリで探す",
        "no_page_text": "この商品は存在しません",
        "common_url": "https://paypayfleamarket.yahoo.co.jp/item/",
        "convert_url": "ebayPF_",
    },
    {
        "site_name": "ヤフオク",
        "url_keyword": "yahoo.co.jp/auction",
        "in_stock_text1": "入札する",
        "in_stock_text2": "今すぐ落札",
        "sold_out_text": "このオークションは終了",
        "no_page_text": "このオークションは存在しません",
        "common_url": "https://page.auctions.yahoo.co.jp/jp/auction/",
        "convert_url": "ebayh_",
    },
    # ---- ECモール ----
    {
        "site_name": "楽天市場",
        "url_keyword": "item.rakuten",
        # W183 (2026-05-28): schema.org microdata で在庫判定 (migration v55 と同値に統一).
        # 旧 'かごに追加'/'売り切れ' は売切ページにも disabled で残り誤判定 (Codex 実機調査).
        # 詳細: .company/engineering/migration/codex-ec-direct-url-design.md
        "in_stock_text1": 'itemprop="availability" content="http://schema.org/InStock"',
        "in_stock_text2": "",
        "sold_out_text": 'itemprop="availability" content="http://schema.org/OutOfStock"',
        "no_page_text": "ご指定のページは見つかりません",
        "common_url": "https://x.gd/",
        "convert_url": "ebayRT_",
    },
    {
        "site_name": "楽天ブックス",
        "url_keyword": "books.rakuten",
        "in_stock_text1": "在庫あり",
        "in_stock_text2": "買い物かごに入れる",
        "sold_out_text": "再入荷",
        "no_page_text": "お探しのページが見つか",
        "common_url": "https://books.rakuten.co.jp/rb/",
        "convert_url": "ebayRB_",
    },
    {
        "site_name": "Yahoo!ショッピング",
        "url_keyword": "shopping.yahoo.co.jp",
        # W192 (2026-05-30): 実 HTML 検証で「在庫有」だけの clean な marker が無い
        # (「カートに入れる」「数量」は売切ページにも残り誤判定 = strict で unknown 化)。
        # よって在庫有 signal は空にして、売切 (「在庫がありません」) と消滅 (HTTP 404) のみ
        # 確実に拾う。在庫有は unknown 扱い (false-OOS を出さない安全方向)。価格は別経路で取得。
        "in_stock_text1": "",
        "in_stock_text2": "",
        "sold_out_text": "在庫がありません",
        "no_page_text": "",
        "common_url": "https://store.shopping.yahoo.co.jp/",
        "convert_url": "ebayYS_",
    },
    {
        "site_name": "Amazon",
        "url_keyword": "www.amazon.co.jp",
        # W183 (2026-05-28): add-to-cart-button で主ボタン特定 (migration v55 と同値に統一).
        # 旧 'カートに入れる' は nav / 関連商品にも出て誤判定 (Codex 実機調査).
        "in_stock_text1": 'id="add-to-cart-button"',
        "in_stock_text2": 'name="submit.add-to-cart"',
        "sold_out_text": "現在在庫切れ",
        "no_page_text": "この商品は現在お取り扱いできません",
        "common_url": "https://www.amazon.co.jp/dp/",
        "convert_url": "ebayAM_",
    },
    # ---- 中古・リユース ----
    {
        "site_name": "OFFモール（ハードオフ）",
        "url_keyword": "netmall.hardoff",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "この商品は売り切れな",
        "no_page_text": "",
        "common_url": "https://netmall.hardoff.co.jp/product/",
        "convert_url": "ebayOFF_",
    },
    {
        "site_name": "駿河屋",
        "url_keyword": "suruga-ya",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "品切れ中です。",
        "no_page_text": "The requested page c",
        "common_url": "https://www.suruga-ya.jp/product/detail/",
        "convert_url": "ebaySU_",
    },
    {
        "site_name": "アナログレコード（otaiweb）",
        "url_keyword": "otaiweb.com",
        "in_stock_text1": "注文数",
        "in_stock_text2": "",
        "sold_out_text": "SOLD OUT",
        "no_page_text": "",
        "common_url": "https://x.gd/",
        "convert_url": "ebayOR_",
    },
    # ---- 家電・AV ----
    {
        "site_name": "ヨドバシカメラ",
        "url_keyword": "yodobashi",
        "in_stock_text1": "ショッピングカートに",
        "in_stock_text2": "",
        "sold_out_text": "販売を終了しました",
        "no_page_text": "",
        "common_url": "https://www.yodobashi.com/product/",
        "convert_url": "ebayYD_",
    },
    {
        "site_name": "ソフトマップ",
        "url_keyword": "sofmap.com",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "完売御礼",
        "no_page_text": "",
        "common_url": "https://www.sofmap.com/product_detail.aspx?sku=",
        "convert_url": "ebaySF_",
    },
    {
        "site_name": "フジヤカメラ",
        "url_keyword": "fujiya-camera",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "在庫がありません",
        "no_page_text": "ご指定の商品は販売終了",
        "common_url": "https://www.fujiya-camera.co.jp/shop/g/",
        "convert_url": "ebayFC_",
    },
    {
        "site_name": "e-イヤホン",
        "url_keyword": "e-earphone",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "ただいま売り切れ中です",
        "no_page_text": "",
        "common_url": "https://www.e-earphone.jp/products/",
        "convert_url": "ebayEE_",
    },
    {
        "site_name": "e-ナビ屋",
        "url_keyword": "e-naviya",
        "in_stock_text1": "ショッピングカートへ",
        "in_stock_text2": "在庫あり",
        "sold_out_text": "品切れ",
        "no_page_text": "ご訪問、誠にありがとう",
        "common_url": "https://e-naviya.com/view/item/",
        "convert_url": "ebayEN_",
    },
    {
        "site_name": "オーディオ逸品館",
        "url_keyword": "e.ippinkan",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "品切れ",
        "no_page_text": "",
        "common_url": "https://e.ippinkan.com/shopdetail/",
        "convert_url": "ebayAD_",
    },
    # ---- 音楽・ゲーム ----
    {
        "site_name": "diskunion",
        "url_keyword": "diskunion",
        "in_stock_text1": "新品をカートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "ご注文できません",
        "no_page_text": "",
        "common_url": "https://diskunion.net/jp/ct/detail/",
        "convert_url": "ebayDU_",
    },
    {
        "site_name": "TSUKUMO",
        "url_keyword": "fa-tsukumo",
        "in_stock_text1": "残りあと",
        "in_stock_text2": "",
        "sold_out_text": "売り切れ",
        "no_page_text": "",
        "common_url": "https://fa-tsukumo.100-1.co.jp/shopdetail/",
        "convert_url": "ebayTK_",
    },
    # ---- 専門店 ----
    {
        "site_name": "FavoriteStyle",
        "url_keyword": "favoritestyle",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "ただいま品切れ",
        "no_page_text": "ページが見つかりません",
        "common_url": "https://www.favoritestyle.jp/products/detail/",
        "convert_url": "ebayFS_",
    },
    {
        "site_name": "KINBON WEB SHOP（盆栽）",
        "url_keyword": "bonsai.co.jp",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "ただいま品切れ中です",
        "no_page_text": "ページが見つかりません。",
        "common_url": "https://www.bonsai.co.jp/products/detail/",
        "convert_url": "ebayBS_",
    },
    {
        "site_name": "現場市場",
        "url_keyword": "genbaichiba",
        "in_stock_text1": "買い物かごへ入れる",
        "in_stock_text2": "",
        "sold_out_text": "",
        "no_page_text": "",
        "common_url": "",
        "convert_url": "ebayGC_",
    },
    {
        "site_name": "gute gouter",
        "url_keyword": "gutegoutier-japan",
        "in_stock_text1": "カートに追加する",
        "in_stock_text2": "",
        "sold_out_text": "売り切れ",
        "no_page_text": "ページが見つかりません。",
        "common_url": "https://x.gd/",
        "convert_url": "ebayGG_",
    },
    {
        "site_name": "計測器ランド",
        "url_keyword": "keisokuki-land",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "在庫あり",
        "sold_out_text": "在庫無し",
        "no_page_text": "ご指定のページは見つか",
        "common_url": "",
        "convert_url": "ebayKL_",
    },
    {
        "site_name": "Caster House",
        "url_keyword": "casterhouse.co.jp",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "在庫あり",
        "sold_out_text": "申し訳ございません",
        "no_page_text": "指定のページ",
        "common_url": "https://www.casterhouse.co.jp/shop/products/detail/",
        "convert_url": "ebayCH_",
    },
    {
        "site_name": "COMPONENTS 76",
        "url_keyword": "components76",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "この商品は、ただいま在",
        "no_page_text": "この商品は存在しません。",
        "common_url": "https://components76.com/product/detail/",
        "convert_url": "ebayCP_",
    },
    {
        "site_name": "モノタロウ",
        "url_keyword": "monotaro",
        "in_stock_text1": "バスケットに入れる",
        "in_stock_text2": "",
        "sold_out_text": "取扱い終了",
        "no_page_text": "取扱い終了",
        "common_url": "https://x.gd/",
        "convert_url": "ebayMT_",
    },
    {
        "site_name": "ATAGO",
        "url_keyword": "atago.net",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "",
        "no_page_text": "お探しの製品が見つかり",
        "common_url": "https://www.atago.net/japanese/new/atagoshop-index.php?key=",
        "convert_url": "ebayAT_",
    },
    {
        "site_name": "FA機器",
        "url_keyword": "fakiki.com",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "売り切れ",
        "no_page_text": "",
        "common_url": "https://x.gd/",
        "convert_url": "ebayFA_",
    },
    {
        "site_name": "HILTI",
        "url_keyword": "hilti.co.jp",
        "in_stock_text1": "カートに追加する",
        "in_stock_text2": "",
        "sold_out_text": "",
        "no_page_text": "",
        "common_url": "",
        "convert_url": "ebayHI_",
    },
    {
        "site_name": "保守部品",
        "url_keyword": "hoshubuhin",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "",
        "no_page_text": "指定されたURLでは現在",
        "common_url": "https://x.gd/",
        "convert_url": "ebayHB_",
    },
    {
        "site_name": "トンカタストア",
        "url_keyword": "shop.tonkachi",
        "in_stock_text1": "カートに入れる",
        "in_stock_text2": "",
        "sold_out_text": "SOLD OUT",
        "no_page_text": "お探しのページが見つか",
        "common_url": "https://shop.tonkachi.co.jp/products/",
        "convert_url": "ebayTS_",
    },
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS site_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_name TEXT NOT NULL,
                url_keyword TEXT,
                in_stock_text1 TEXT,
                in_stock_text2 TEXT,
                sold_out_text TEXT,
                no_page_text TEXT,
                common_url TEXT,
                convert_url TEXT UNIQUE NOT NULL,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS monitored_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebay_item_id TEXT,
                title TEXT,
                sku TEXT NOT NULL,
                source_url TEXT,
                site_config_id INTEGER,
                is_active INTEGER DEFAULT 1,
                last_status TEXT DEFAULT 'unknown',
                last_check TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (site_config_id) REFERENCES site_configs(id)
            );

            CREATE TABLE IF NOT EXISTS check_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL,
                discord_sent INTEGER DEFAULT 0,
                FOREIGN KEY (item_id) REFERENCES monitored_items(id)
            );

            CREATE INDEX IF NOT EXISTS idx_check_log_item_time
                ON check_log(item_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_monitored_active
                ON monitored_items(is_active);
            CREATE INDEX IF NOT EXISTS idx_monitored_ebay_id
                ON monitored_items(ebay_item_id)
                WHERE ebay_item_id IS NOT NULL AND ebay_item_id != '';
            CREATE INDEX IF NOT EXISTS idx_monitored_source_url
                ON monitored_items(source_url)
                WHERE source_url IS NOT NULL AND source_url != '';

            CREATE TABLE IF NOT EXISTS ebay_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebay_item_id TEXT NOT NULL UNIQUE,
                sku TEXT NOT NULL,
                title TEXT,
                current_price REAL,
                quantity_ebay INTEGER DEFAULT 0,
                last_synced_at TIMESTAMP,
                source_status TEXT DEFAULT 'unknown',
                rank TEXT DEFAULT 'C',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                -- FK(sku)->monitored_items(sku) 撤廃 (2026-05-29 Opus 4.8 総チェック H2):
                -- sku は listing 識別子ではない (stock**/ebay** を多数 listing が共有) ため
                -- FK は意味論的に誤り. v28 で参照先 monitored_items.UNIQUE(sku) も撤廃済
                -- = 既に無効FK. FK は未強制 (get_conn が PRAGMA foreign_keys=ON しない) ため
                -- 本撤廃は機能変化なし. listing 識別は ebay_item_id (上記 UNIQUE). sku-rules.md 準拠.
            );

            CREATE INDEX IF NOT EXISTS idx_ebay_listings_sku
                ON ebay_listings(sku);
            CREATE INDEX IF NOT EXISTS idx_ebay_listings_item_id
                ON ebay_listings(ebay_item_id);

            CREATE TABLE IF NOT EXISTS competitor_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                our_item_id TEXT NOT NULL,
                our_sku TEXT,
                competitor_item_id TEXT NOT NULL UNIQUE,
                competitor_seller TEXT,
                seller_location TEXT,
                price_rule TEXT,
                min_price REAL,
                max_discount REAL,
                is_active INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS new_competitor_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                our_item_id TEXT NOT NULL,
                keyword TEXT,
                found_item_id TEXT NOT NULL UNIQUE,
                found_seller TEXT,
                found_location TEXT,
                found_price REAL,
                is_japan_seller INTEGER,
                found_at TIMESTAMP,
                notified INTEGER DEFAULT 0,
                action TEXT DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_competitor_our_item
                ON competitor_products(our_item_id);
            CREATE INDEX IF NOT EXISTS idx_competitor_location
                ON competitor_products(seller_location);
            CREATE INDEX IF NOT EXISTS idx_alerts_japan
                ON new_competitor_alerts(is_japan_seller);
            CREATE INDEX IF NOT EXISTS idx_alerts_action
                ON new_competitor_alerts(action);
        """)

    # デフォルトサイト設定を投入（存在しない場合のみ）
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM site_configs").fetchone()[0]
        if count == 0:
            for cfg in DEFAULT_SITE_CONFIGS:
                conn.execute(
                    """INSERT OR IGNORE INTO site_configs
                       (site_name, url_keyword, in_stock_text1, in_stock_text2,
                        sold_out_text, no_page_text, common_url, convert_url)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        cfg["site_name"], cfg["url_keyword"],
                        cfg["in_stock_text1"], cfg["in_stock_text2"],
                        cfg["sold_out_text"], cfg["no_page_text"],
                        cfg["common_url"], cfg["convert_url"],
                    ),
                )

    # マイグレーション: ebay_listings に rank カラムを追加
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE ebay_listings ADD COLUMN rank TEXT DEFAULT 'C'")
        except sqlite3.OperationalError:
            # rank カラムが既に存在する場合はスキップ
            pass

    # マイグレーション: メトリクス・ランク計算用カラムを追加
    migration_columns = [
        ("watch_count", "INTEGER DEFAULT 0"),
        ("view_count", "INTEGER DEFAULT 0"),
        ("sales_count_30d", "INTEGER DEFAULT 0"),
        ("last_watch_count", "INTEGER DEFAULT 0"),
        ("last_view_count", "INTEGER DEFAULT 0"),
        ("last_sales_count_30d", "INTEGER DEFAULT 0"),
        ("watch_growth_rate", "REAL DEFAULT 0.0"),
        ("view_growth_rate", "REAL DEFAULT 0.0"),
        ("sales_growth_rate", "REAL DEFAULT 0.0"),
        ("metrics_score", "REAL DEFAULT 0.0"),
        ("last_metrics_updated_at", "TIMESTAMP"),
        ("shipping_cost", "REAL DEFAULT 0.0"),
    ]
    with get_conn() as conn:
        for col_name, col_type in migration_columns:
            try:
                conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

    # マイグレーション v2: SKU変換データ統合 + 売上トラッキング
    migration_v2_columns = [
        # SKU変換データ統合（sku_conversion_results.json から）
        ("source", "TEXT"),                  # 仕入先名（メルカリ, Yahoo Auctions 等）
        ("source_url", "TEXT"),              # 仕入先URL
        ("classification", "TEXT"),          # sourced / self_stock / unknown
        # 商品物理データ（task_enrich_ebay_data から）
        ("weight_g", "REAL DEFAULT 0"),
        ("length_cm", "REAL DEFAULT 0"),
        ("width_cm", "REAL DEFAULT 0"),
        ("height_cm", "REAL DEFAULT 0"),
        ("includes", "TEXT"),                # 付属品情報
        ("warranty", "TEXT"),                # 保証情報
        # 売上トラッキング
        ("total_sold_count", "INTEGER DEFAULT 0"),    # 累計販売数
        ("total_revenue_usd", "REAL DEFAULT 0"),      # 累計売上(USD)
        ("last_sold_at", "TIMESTAMP"),                # 最終売上日時
        ("avg_days_to_sell", "REAL DEFAULT 0"),        # 平均販売日数
        # 在庫ステータス（inventory_check から）
        ("source_last_checked", "TIMESTAMP"),          # 仕入先最終チェック日
        ("source_out_of_stock_since", "TIMESTAMP"),    # 在庫切れ開始日
        # 価格最適化
        ("competitor_min_price", "REAL"),               # 競合最低価格
        ("competitor_count", "INTEGER DEFAULT 0"),      # 競合セラー数
        ("price_suggestion", "REAL"),                   # 推奨価格
        ("price_suggestion_reason", "TEXT"),             # 推奨理由
        # 仕入先在庫リスク確認フラグ
        ("risk_confirmed", "INTEGER DEFAULT 0"),        # ユーザーが確認済みか
    ]
    with get_conn() as conn:
        for col_name, col_type in migration_v2_columns:
            try:
                conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

    # マイグレーション v2: 売上履歴テーブル
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sales_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebay_item_id TEXT NOT NULL,
                sku TEXT,
                title TEXT,
                sold_price_usd REAL NOT NULL,
                sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                buyer_country TEXT,
                shipping_cost_usd REAL DEFAULT 0,
                ebay_fee_usd REAL DEFAULT 0,
                source_cost_jpy REAL DEFAULT 0,
                profit_jpy REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sales_history_date
            ON sales_history (sold_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sales_history_sku
            ON sales_history (sku)
        """)

    # マイグレーション v3: メール管理テーブル
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                gmail_id TEXT PRIMARY KEY,
                subject TEXT,
                sender TEXT,
                date TEXT,
                body_text TEXT,
                body_ja TEXT,
                category TEXT,
                confirmed INTEGER DEFAULT 0,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_emails_fetched
            ON emails (fetched_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_emails_category
            ON emails (category)
        """)

    # マイグレーション v4: 仕入先候補自動探索機能（#9）
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE ebay_listings ADD COLUMN yahoo_grace_until TIMESTAMP")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS supplier_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                ebay_item_id TEXT NOT NULL,
                source_platform TEXT,
                candidate_url TEXT NOT NULL,
                candidate_price_jpy INTEGER,
                candidate_title TEXT,
                match_score INTEGER,
                match_reasoning TEXT,
                profit_jpy REAL,
                profitable INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                user_action_at TIMESTAMP,
                discovered_via TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- UNIQUE(ebay_item_id, candidate_url): listing 識別は ebay_item_id (sku-rules.md).
                -- W185 (2026-05-29 Opus 4.8 総チェック H3) で UNIQUE(sku, candidate_url) から変更.
                -- sku は有/無在庫 prefix 判定 + 仕入先 URL 変換用に残す (集約/一意キーには使わない).
                -- ebay_item_id NOT NULL: NULL は SQLite で UNIQUE 上 distinct 扱い = dedup 無効化のため.
                -- 旧 DB は scripts/migrate_supplier_candidates_v56.py one-shot で RECREATE (init_db 内 DROP 禁止 Q2).
                UNIQUE(ebay_item_id, candidate_url)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_supplier_candidates_sku
            ON supplier_candidates (sku)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_supplier_candidates_status
            ON supplier_candidates (status, match_score DESC)
        """)

    # マイグレーション v5: 仕入先候補に別SKU出品機会フラグを追加（#9 拡張）
    v5_columns = [
        ("junk_likely_untested", "INTEGER DEFAULT 0"),
        ("alt_listing_possible", "INTEGER DEFAULT 0"),
        ("alt_listing_note", "TEXT"),
    ]
    with get_conn() as conn:
        for col_name, col_type in v5_columns:
            try:
                conn.execute(f"ALTER TABLE supplier_candidates ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

    # マイグレーション v6: eBay退役検出（ActiveListから消えたlistingに印を付ける）
    v6_columns = [
        ("is_ended", "INTEGER DEFAULT 0"),
        ("ended_at", "TIMESTAMP"),
        ("ended_reason", "TEXT"),      # 'not_in_active_list' / 'manual' など
    ]
    with get_conn() as conn:
        for col_name, col_type in v6_columns:
            try:
                conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ebay_listings_is_ended ON ebay_listings (is_ended)"
            )
        except sqlite3.OperationalError:
            pass

    # マイグレーション v7: weight データのソース識別（ebay実測 / claude推定 / default / manual）
    v7_columns = [
        ("weight_source", "TEXT"),          # 'ebay' | 'claude' | 'default_500g' | 'manual' | NULL
        ("weight_estimated_at", "TIMESTAMP"),
        ("weight_confidence", "TEXT"),      # 'high' | 'medium' | 'low' | NULL (claude推定の自信度)
    ]
    with get_conn() as conn:
        for col_name, col_type in v7_columns:
            try:
                conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

    # マイグレーション v8: Claude によるメール要約
    v8_email_columns = [
        ("summary_ja", "TEXT"),           # 1〜2文の日本語要約
        ("action_ja", "TEXT"),            # 次にやるべきアクション
        ("buyer_message_ja", "TEXT"),     # バイヤーからの問い合わせ本文（日本語で抽出）
        ("priority_ai", "TEXT"),          # 'urgent' | 'high' | 'normal' | 'low'
        ("category_ai", "TEXT"),          # Claude判定のカテゴリ
        ("summarized_at", "TIMESTAMP"),
    ]
    with get_conn() as conn:
        for col_name, col_type in v8_email_columns:
            try:
                conn.execute(f"ALTER TABLE emails ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

    # マイグレーション v10: 動画に日付・関税時代区分を追加
    v10_video_columns = [
        ("published_date", "TEXT"),          # YYYY-MM-DD (動画公開日 or タイトルから抽出)
        ("tariff_era", "TEXT"),              # 'pre_tariff' | 'transition' | 'post_tariff' | 'evergreen'
        ("time_sensitive_topics", "TEXT"),   # JSON: 時代依存トピック (shipping/tariff/pricing等)
    ]
    with get_conn() as conn:
        # テーブルが既に存在する場合のみ ALTER（v9 で作成済）
        try:
            for col_name, col_type in v10_video_columns:
                try:
                    conn.execute(f"ALTER TABLE videos_learned ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_tariff_era ON videos_learned (tariff_era)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_published_date ON videos_learned (published_date DESC)"
            )
        except sqlite3.OperationalError:
            pass

    # マイグレーション v9: 動画学習（Gemini 2.5 Pro による動画→構造化知識）
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos_learned (
                video_id TEXT PRIMARY KEY,        -- YouTube video ID (dQw4w9WgXcQ 等)
                url TEXT NOT NULL,
                title TEXT,
                channel TEXT,
                duration_sec INTEGER,
                status TEXT DEFAULT 'pending',    -- pending | processing | done | failed
                status_message TEXT,
                summary_ja TEXT,
                key_insights TEXT,                -- JSON array[str]
                products_mentioned TEXT,          -- JSON array[{name, category, price_range}]
                platforms_mentioned TEXT,         -- JSON array[str]
                actionable_steps TEXT,            -- JSON array[str]
                pricing_hints TEXT,               -- JSON array[{product, range, reasoning}]
                topics TEXT,                      -- カンマ区切り
                gemini_response_raw TEXT,         -- デバッグ用
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                error_detail TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos_learned (status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_added ON videos_learned (added_at DESC)"
        )

        # knowledge_index: 検索用キーワード→video_id マッピング
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,             -- "OMRON" / "値下げ交渉" / "Yahoo Auctions" 等
                video_id TEXT NOT NULL,
                weight REAL DEFAULT 1.0,           -- 重要度 (将来利用)
                context TEXT,                      -- そのキーワードがどういう文脈で登場するか
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (video_id) REFERENCES videos_learned(video_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_keyword ON knowledge_index (keyword)"
        )

    # マイグレーション v8: ニューステーブル（ファイル→DB化、Claude要約用）
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                title TEXT,
                url TEXT,
                summary_ja TEXT,          -- 日本語要約（1〜2文）
                impact_ja TEXT,           -- eBay物販への影響説明
                impact_level TEXT,        -- 'high' | 'medium' | 'low' | 'none'
                categories TEXT,          -- カンマ区切り（api_change / new_feature / research 等）
                published_at TEXT,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, title)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_checked ON news_items (checked_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_impact ON news_items (impact_level)"
        )

    # マイグレーション v12: 日次 End→Relist SEO ブースト機能
    v12_columns = [
        ("time_left_seconds", "INTEGER"),   # eBay の TimeLeft を秒化して格納
        ("start_time", "TIMESTAMP"),        # 出品開始時刻 (listing の古さ判定用)
    ]
    with get_conn() as conn:
        for col_name, col_type in v12_columns:
            try:
                conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass
        # relist 履歴テーブル（cooldown 30日管理用）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS relist_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                old_item_id TEXT NOT NULL,
                new_item_id TEXT,
                sku TEXT,
                title TEXT,
                end_reason TEXT,                  -- 'Incorrect' / 'NotAvailable' 等
                success INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relist_old ON relist_history (old_item_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relist_new ON relist_history (new_item_id)"
        )

    # マイグレーション v13: ユーザー判断と自動処理を区別（Phase 1 学習の精度向上）
    with get_conn() as conn:
        try:
            conn.execute(
                "ALTER TABLE supplier_candidates ADD COLUMN auto_rejected INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass

    # マイグレーション v14: W9 個別新規出品 - description テンプレート
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS description_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_desc_templates_default "
            "ON description_templates (is_default DESC)"
        )

    # マイグレーション v15: W9 個別新規出品 - listing drafts
    #
    # 画像カラムの意味論 (W10 で正式確定, 2026-04-23):
    #   supplier_image_urls    — 仕入先 scraper が取得した生 URL 群 (Mercari 等)
    #   selected_image_urls    — ユーザーが UI で「出品に使う」とチェックした URL 群
    #                            (supplier_image_urls の部分集合, 最大 24 枚)
    #   processed_image_urls   — W10 画像加工パイプライン出力の eBay EPS URL 群
    #                            (selected_image_urls を加工した後のホスト URL)
    #
    # 優先順位 (ebay_lister.py が参照):
    #   processed_image_urls > selected_image_urls > supplier_image_urls[:24]
    #
    # UI (tab_individual_listing.py) の _resolve_listing_image_urls() helper で
    # 上記優先順位で resolve し、ebay_lister.py には image_urls 1 本で渡す契約。
    #
    # Phase 状況 (2026-04-23):
    #   - helper 関数は実装済 (processed > selected > supplier の順で解決する)
    #   - ただし Phase A 時点では W10 画像加工未実装のため processed は常に空、
    #     実質 selected のみが返る。Phase D で加工パイプラインが稼働すると
    #     processed が優先されるようになる。
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listing_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT,
                supplier_url TEXT,
                supplier_platform TEXT,
                supplier_title_ja TEXT,
                supplier_price_jpy INTEGER,
                supplier_condition_ja TEXT,
                supplier_includes_ja TEXT,
                supplier_image_urls TEXT,
                selected_image_urls TEXT,
                reference_ebay_url TEXT,
                reference_ebay_item_id TEXT,
                reference_category_id TEXT,
                reference_item_specifics_keys TEXT,
                reference_condition_id TEXT,
                rank_code TEXT,
                rank_label TEXT,
                quick_notes TEXT,
                ebay_title TEXT,
                ebay_description TEXT,
                ebay_category_id TEXT,
                ebay_category_name TEXT,
                ebay_condition_id TEXT,
                item_specifics TEXT,
                listing_price_usd REAL,
                weight_g INTEGER,
                in_stock INTEGER DEFAULT 1,
                shipping_policy_id TEXT,
                template_id INTEGER,
                scheduled_time TIMESTAMP,
                ebay_item_id TEXT,
                status TEXT DEFAULT 'draft',
                api_error_message TEXT,
                processed_image_urls TEXT,
                primary_market TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (template_id) REFERENCES description_templates(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listing_drafts_status "
            "ON listing_drafts (status, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listing_drafts_sku "
            "ON listing_drafts (sku)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listing_drafts_ebay_item "
            "ON listing_drafts (ebay_item_id)"
        )
        # W191 (2026-05-30): 個別出品 UI で出品時に選んだ出品区分 (primary_market) を
        # 下書きにも永続化し、再編集で復元できるようにする。公開済 listing の継続的な
        # 区分判定は従来どおり market-analysis (ebay_listings 側) が担う。
        try:
            conn.execute("ALTER TABLE listing_drafts ADD COLUMN primary_market TEXT")
        except sqlite3.OperationalError:
            pass  # カラム既存 (冪等性)

    # マイグレーション v11: API呼出ログ (モデル稼働状況・コスト追跡)
    # 全マイグレーションの末尾に配置（他テーブルに依存しない独立テーブル）
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,         -- 'anthropic' | 'google'
                model TEXT NOT NULL,
                operation TEXT,                 -- 'email_summary' | 'weight_estimate' | 'candidate_evaluate' 等
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                duration_ms INTEGER,
                success INTEGER DEFAULT 1,
                error_message TEXT,
                cost_usd REAL,                  -- 推定USDコスト
                called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_log_called ON api_call_log (called_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_log_model ON api_call_log (model, called_at DESC)"
        )
        # W108 (2026-05-09): Anthropic Message Batches API は 50% 割引。is_batch フラグで永続化
        # して、retroactive audit + Console との daily 突合を可能にする (db-migration-rules.md 準拠).
        try:
            conn.execute(
                "ALTER TABLE api_call_log ADD COLUMN is_batch INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # カラム既存 (冪等性)

    # マイグレーション v16 (2026-04-23): Phase D EPS アップロードキャッシュ
    # ローカル画像の SHA256 hash → eBay EPS publicly-accessible URL のマップ.
    # 同じ画像を 2 回以上アップロードしないようにする重複排除キャッシュ.
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eps_upload_cache (
                file_hash TEXT PRIMARY KEY,          -- ローカル画像の SHA256
                local_path TEXT NOT NULL,            -- 最後にアップロードした時のパス (informational)
                eps_url TEXT NOT NULL,               -- eBay EPS が返した publicly-accessible URL
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eps_uploaded ON eps_upload_cache (uploaded_at DESC)"
        )

    # マイグレーション v17 (2026-04-24): W13 X ベース AI ニュース取得機能
    # 既存 news_items テーブルを X/Reddit/HN 対応に拡張 + api_budget_log 新設
    # code-reviewer 指摘 C-1: x_news_v2 新設せず news_items を拡張 (UI 統合要件)
    # code-reviewer 指摘 H-1: budget を DB atomic 化で race condition 解消
    with get_conn() as conn:
        # news_items に source 識別 + engagement + raw_content を追加
        for col, typ, default in [
            ("source_type", "TEXT", "'web'"),        # 'x' / 'reddit' / 'hn' / 'web'
            ("source_handle", "TEXT", "NULL"),       # @AnthropicAI / r/ClaudeAI / 'hn' / blog URL
            ("engagement_count", "INTEGER", "0"),    # likes / upvotes / points
            ("raw_content", "TEXT", "NULL"),         # 原文 (tweet/post 本体)
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE news_items ADD COLUMN {col} {typ} DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # カラム既存 (冪等性保証)

        # URL 一意インデックス (L1 dedupe 用、nullable/empty は除外)
        # H-W13-1 対応: 既存重複 URL があると IntegrityError で migration 失敗するため
        # 事前に engagement_count 最大 (NULL の場合は最新 id) を残して他を削除.
        try:
            conn.execute("""
                DELETE FROM news_items
                WHERE url IS NOT NULL AND url != ''
                  AND id NOT IN (
                    SELECT MAX(id) FROM news_items
                    WHERE url IS NOT NULL AND url != ''
                    GROUP BY url
                  )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url ON news_items(url) "
                "WHERE url IS NOT NULL AND url != ''"
            )
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
            # 部分 UNIQUE INDEX は SQLite 3.8.0+ で利用可. 古い SQLite or
            # 何らかの理由で作成失敗しても migration 全体は止めない.
            import logging as _lg
            _lg.getLogger(__name__).warning(
                f"idx_news_url creation skipped (non-fatal): {e}"
            )
        # source_type 絞り込み用インデックス
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_news_source_type "
                "ON news_items(source_type, checked_at DESC)"
            )
        except sqlite3.OperationalError:
            pass

        # api_budget_log: API コスト atomic 累計 (xAI/Claude 等)
        # atomic increment で race 解消: cumulative_cost を RETURNING で取得
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_budget_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,                  -- YYYY-MM-DD (JST)
                provider TEXT NOT NULL,              -- 'xai' / 'anthropic'
                cost_usd REAL NOT NULL,              -- このコール単体の USD コスト
                cumulative_cost REAL NOT NULL,       -- 当日累計 (insert 時点スナップショット)
                context TEXT,                        -- task/component 識別子
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_budget_date_provider "
            "ON api_budget_log(date, provider)"
        )

    # マイグレーション v18 (2026-04-24): W14 通関対応自動化
    # code-reviewer HIGH-4/5 反映:
    #   - status に 'sending' 追加 (atomic 遷移)
    #   - gmail_id NOT NULL, deadline CHECK (LIKE '____-__-__'), gmail_sent_id UNIQUE index
    #   - tracking_number index (同 TRK 複数 request 対応)
    #   - customs_send_audit 独立テーブル (immutable, forensic 用)
    #   - customs_kb_pending (Tier 3 自動蓄積の承認待ち、H-8 対応)
    # 注: SQLite GLOB は `_` を literal 扱い、`?` が single-char wildcard.
    # LIKE は `_` が single-char wildcard. ISO date 形式強制には LIKE '____-__-__' を使用.
    with get_conn() as conn:
        # v18 初期実装に GLOB vs LIKE バグがあったが、修正用の DROP/RECREATE は撤廃
        # (Q2 規定: init_db 内 DROP TABLE 禁止. 旧コードは schema 文字列に "deadline LIKE"
        #  を含むかの脆弱判定で、文字列が変わると本番 DROP = v18 95 件消失クラス再発リスク).
        # 旧 GLOB 制約の DB は必ず v19 status set ('drafted_in_gmail') を欠くため、後続 v19
        # migration (L1196-) のデータ保持 RENAME+INSERT SELECT が LIKE 制約へ自動再構築する.
        # 新規環境は下記 CREATE TABLE IF NOT EXISTS が LIKE 制約で作成.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customs_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id TEXT NOT NULL UNIQUE,
                gmail_sent_id TEXT,                  -- 送信後の Gmail message id (audit)
                carrier TEXT NOT NULL CHECK(carrier IN ('fedex','dhl','ups')),
                tracking_number TEXT,
                recipient TEXT,                      -- 米国宛先等
                ship_date TEXT,
                deadline TEXT CHECK(
                    deadline IS NULL OR deadline LIKE '____-__-__'
                ),                                   -- ISO 8601 強制 (LIKE は _ が wildcard)
                request_items TEXT,                  -- JSON array (要求情報項目)
                ebay_item_id TEXT,
                sku TEXT,
                product_title TEXT,
                draft_subject TEXT,
                draft_body TEXT,
                draft_recipients TEXT,               -- JSON (TO/CC)
                attached_photos TEXT,                -- JSON paths
                attached_attachments TEXT,           -- JSON (受信添付保管)
                template_used TEXT,                  -- テンプレファイル名
                template_hash TEXT,                  -- テンプレ内容の SHA256 (再現性)
                kb_hits TEXT,                        -- JSON (使用 KB)
                status TEXT NOT NULL
                    CHECK(status IN (
                        'detected','drafted','drafted_no_photo',
                        'sending','sent','failed','manual'
                    ))
                    DEFAULT 'detected',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                drafted_at TIMESTAMP,
                sent_at TIMESTAMP,
                error_msg TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customs_status "
            "ON customs_requests(status, detected_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customs_deadline "
            "ON customs_requests(deadline, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customs_tracking "
            "ON customs_requests(tracking_number)"
        )
        # 送信の二重化検知 (gmail_sent_id の UNIQUE、NULL は除外)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_customs_sent_msg "
                "ON customs_requests(gmail_sent_id) "
                "WHERE gmail_sent_id IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass

        # audit log (immutable, INSERT only, forensic 用)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customs_send_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customs_request_id INTEGER NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                gmail_sent_id TEXT,
                recipients_hash TEXT NOT NULL,       -- SHA256(sorted TO+CC)
                body_hash TEXT NOT NULL,             -- SHA256(body)
                attachments_hash TEXT,               -- SHA256(sorted paths)
                result TEXT NOT NULL CHECK(result IN ('success','failed')),
                error_msg TEXT,
                FOREIGN KEY (customs_request_id) REFERENCES customs_requests(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_customs_audit_req "
            "ON customs_send_audit(customs_request_id, sent_at DESC)"
        )

        # KB Tier 3 承認待ち (H-8 対応): Web 検索結果を無検証で KB に書かない
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customs_kb_pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('manufacturer','hts')),
                brand_or_category TEXT NOT NULL,
                proposed_json TEXT NOT NULL,         -- JSON (distributor addr 等)
                source_url TEXT,                     -- 根拠 URL
                status TEXT NOT NULL
                    CHECK(status IN ('proposed','approved','rejected'))
                    DEFAULT 'proposed',
                detected_from_customs_request_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_pending_status "
            "ON customs_kb_pending(status, created_at DESC)"
        )

    # マイグレーション v19 (2026-04-25): W14 Gmail 下書き保存 + reply スレッド対応
    # 追加内容:
    #   - draft_gmail_id: Gmail drafts.create() の戻り draft.id (送信時に drafts.send で使用)
    #   - gmail_thread_id: 元メールのスレッド ID (Gmail で「会話」表示まとめ)
    #   - original_message_id: 元メールの RFC822 Message-ID (In-Reply-To/References ヘッダー用)
    #   - status に 'drafted_in_gmail' を追加 (UI フローの 2 段階確認のため)
    #
    # 冪等性 (feedback_db_migration_idempotency.md ルール遵守):
    #   - ALTER TABLE ADD COLUMN は OperationalError catch (既に追加済なら no-op)
    #   - status CHECK 制約変更は SQLite では ALTER 不可なので、新 status を CHECK に
    #     含めるためテーブル recreate が必要だが、毎回 DROP すると本番データ消失
    #     (W14 v18 事故の繰り返し). schema 検査で 'drafted_in_gmail' が CHECK に
    #     既に含まれていれば skip、含まれていなければ ALTER TABLE RENAME +
    #     INSERT SELECT で データ保持しつつ再構築する.
    with get_conn() as conn:
        # Step 1: ADD COLUMN (冪等)
        # draft_lock_at は create_customs_draft の atomic claim 専用 (drafted_at とは別)
        # H-X1 修正 (2026-04-25): drafted_at 流用は本番 INSERT パスと衝突するため分離
        for col, typ, default in [
            ("draft_gmail_id", "TEXT", "NULL"),
            ("gmail_thread_id", "TEXT", "NULL"),
            ("original_message_id", "TEXT", "NULL"),
            ("draft_lock_at", "TIMESTAMP", "NULL"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE customs_requests ADD COLUMN {col} {typ} DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # カラム既存

        # Step 2: status CHECK 制約に 'drafted_in_gmail' を追加
        # 既に含まれているか schema 検査
        _row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='customs_requests'"
        ).fetchone()
        _need_status_migration = (
            _row and _row[0] and "'drafted_in_gmail'" not in _row[0]
        )
        if _need_status_migration:
            # データ保持しつつテーブル再構築
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "ALTER TABLE customs_requests RENAME TO customs_requests_old"
                )
                conn.execute("""
                    CREATE TABLE customs_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        gmail_id TEXT NOT NULL UNIQUE,
                        gmail_sent_id TEXT,
                        carrier TEXT NOT NULL CHECK(carrier IN ('fedex','dhl','ups')),
                        tracking_number TEXT,
                        recipient TEXT,
                        ship_date TEXT,
                        deadline TEXT CHECK(
                            deadline IS NULL OR deadline LIKE '____-__-__'
                        ),
                        request_items TEXT,
                        ebay_item_id TEXT,
                        sku TEXT,
                        product_title TEXT,
                        draft_subject TEXT,
                        draft_body TEXT,
                        draft_recipients TEXT,
                        attached_photos TEXT,
                        attached_attachments TEXT,
                        template_used TEXT,
                        template_hash TEXT,
                        kb_hits TEXT,
                        status TEXT NOT NULL
                            CHECK(status IN (
                                'detected','drafted','drafted_no_photo',
                                'drafted_in_gmail','sending','sent','failed','manual'
                            ))
                            DEFAULT 'detected',
                        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        drafted_at TIMESTAMP,
                        sent_at TIMESTAMP,
                        error_msg TEXT,
                        draft_gmail_id TEXT,
                        gmail_thread_id TEXT,
                        original_message_id TEXT,
                        draft_lock_at TIMESTAMP
                    )
                """)
                # 旧テーブルから列順序を考慮して INSERT SELECT
                # H-Y1 対応 (2026-04-25): draft_lock_at も移行
                conn.execute("""
                    INSERT INTO customs_requests
                      (id, gmail_id, gmail_sent_id, carrier, tracking_number,
                       recipient, ship_date, deadline, request_items,
                       ebay_item_id, sku, product_title, draft_subject, draft_body,
                       draft_recipients, attached_photos, attached_attachments,
                       template_used, template_hash, kb_hits, status,
                       detected_at, drafted_at, sent_at, error_msg,
                       draft_gmail_id, gmail_thread_id, original_message_id,
                       draft_lock_at)
                    SELECT
                       id, gmail_id, gmail_sent_id, carrier, tracking_number,
                       recipient, ship_date, deadline, request_items,
                       ebay_item_id, sku, product_title, draft_subject, draft_body,
                       draft_recipients, attached_photos, attached_attachments,
                       template_used, template_hash, kb_hits, status,
                       detected_at, drafted_at, sent_at, error_msg,
                       draft_gmail_id, gmail_thread_id, original_message_id,
                       draft_lock_at
                    FROM customs_requests_old
                """)
                conn.execute("DROP TABLE customs_requests_old")
                conn.execute("COMMIT")
                # インデックス再作成
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customs_status "
                    "ON customs_requests(status, detected_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customs_deadline "
                    "ON customs_requests(deadline, status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customs_tracking "
                    "ON customs_requests(tracking_number)"
                )
                try:
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_customs_sent_msg "
                        "ON customs_requests(gmail_sent_id) "
                        "WHERE gmail_sent_id IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    pass
            except sqlite3.Error as _e:
                conn.execute("ROLLBACK")
                import logging as _lg
                _lg.getLogger(__name__).error(
                    f"v19 status migration failed: {_e}"
                )
                raise

        # Step 3: 新規 column の index (gmail thread)
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_customs_thread "
                "ON customs_requests(gmail_thread_id) "
                "WHERE gmail_thread_id IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass

    # マイグレーション v24 (2026-04-26): W23 Research 脳 中核
    # Opus 4.7 ベースの相談役エンドポイント. Q&A 履歴 + 日次予算追跡用テーブル.
    # 詳細: feedback_anthropic_video_cal_rueb_takeaways.md, feedback_model_selection_policy.md
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_qa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL,
                query TEXT NOT NULL,
                context_keys TEXT,
                model TEXT NOT NULL,
                answer_md TEXT,
                citations TEXT,
                thinking_md TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                duration_ms INTEGER DEFAULT 0,
                user_rating INTEGER,
                user_action_at TIMESTAMP,
                via TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_qa_source_asked "
            "ON research_qa(source, asked_at DESC)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_brain_quota (
                date TEXT PRIMARY KEY,
                opus_calls INTEGER DEFAULT 0,
                opus_cost_usd REAL DEFAULT 0.0,
                haiku_calls INTEGER DEFAULT 0,
                haiku_cost_usd REAL DEFAULT 0.0,
                over_budget_at TIMESTAMP
            )
        """)

    # マイグレーション v23 (2026-04-26): W19 video_learning 自動リトライ + permanent_failed
    # 失敗動画を 3 回まで自動リトライし、4 回目以降は permanent_failed として
    # キューから除外する. Gemini quota / yt-dlp cookies 等の transient 失敗を救う一方、
    # 動画削除等の永続失敗は明示的にマーク.
    with get_conn() as conn:
        for col, typ, default in [
            ('retry_count', 'INTEGER', '0'),
            ('last_retry_at', 'TIMESTAMP', 'NULL'),
            ('permanent_failed_at', 'TIMESTAMP', 'NULL'),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE videos_learned ADD COLUMN {col} {typ} DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass

    # マイグレーション v22 (2026-04-26): videos_learned に Opus 4.7 深掘り結果カラム追加
    # Gemini 抽出 (gemini_response_raw) を Opus 4.7 が読み eBay/開発業務目線で深く解釈し、
    # MonoHonpo 適用案・cross-link・red flags・enriched keywords を生成して上書き保存する.
    # ハイブリッド方式 (W22 ROADMAP): Gemini = 視覚抽出 / Opus = 業務適用思考.
    with get_conn() as conn:
        for col, typ in [
            ("opus_enriched_at", "TIMESTAMP"),
            ("opus_model", "TEXT"),
            ("opus_cost_usd", "REAL"),
            ("core_lesson", "TEXT"),
            ("applicable_to_us", "TEXT"),       # JSON array
            ("cross_video_links", "TEXT"),      # JSON array
            ("red_flags", "TEXT"),              # JSON array
            ("enriched_keywords", "TEXT"),      # JSON array
            ("opus_raw_response", "TEXT"),      # debug
        ]:
            try:
                conn.execute(f"ALTER TABLE videos_learned ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass

    # マイグレーション v21 (2026-04-25): supplier_candidates に評価 model を記録
    # AI 評価の精度比較 (Haiku vs Sonnet vs Opus 4.7) のため、各候補を
    # どのモデルで評価したか追跡できるようにする.
    # 2026-04-25 Opus 4.7 切替後の候補は eval_model='claude-opus-4-7' で記録され、
    # UI で「Opus」バッジ表示される.
    with get_conn() as conn:
        try:
            conn.execute(
                "ALTER TABLE supplier_candidates ADD COLUMN eval_model TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column 既存

    # マイグレーション v20 (2026-04-25): 定時実行可観測化
    # 全タスクの実行/skip/失敗を記録し、サイレントスキップを検知可能にする.
    # 5日間 daily_relist が hour ドリフトでサイレントスキップされていた事故への恒久対策.
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL,
                display_name TEXT,
                batch_id TEXT NOT NULL,
                batch_hour INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('started','completed','failed','skip_disabled',
                               'skip_time','skip_weekday','skip_other')
                ),
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                duration_sec REAL,
                success INTEGER,
                message TEXT,
                expected_today INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tel_batch ON task_execution_log(batch_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tel_task_started "
            "ON task_execution_log(task_key, started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tel_started "
            "ON task_execution_log(started_at DESC)"
        )
        # 健康チェック Discord 通知の dedupe 用 (H-5 対応).
        # 同一 (task_key, expected_hour) 組み合わせは 1 日 1 回のみ通知する.
        # PRIMARY KEY による UNIQUE 制約で誤多重通知を物理的に防ぐ.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS health_alert_log (
                alert_date TEXT NOT NULL,
                task_key TEXT NOT NULL,
                expected_hour INTEGER NOT NULL,
                first_alerted_at TIMESTAMP NOT NULL,
                last_alerted_at TIMESTAMP NOT NULL,
                alert_count INTEGER DEFAULT 1,
                PRIMARY KEY (alert_date, task_key, expected_hour)
            )
        """)

    # マイグレーション v25 (2026-04-27): W7-A 市場戦略 (Buyer Location 別運用)
    # Terapeak Research Products から市場全体の Buyer Location 別 sold データを取得し、
    # primary_market = US_only / mixed_global / global_only / unknown を listing 毎に判定する.
    # 動画 [60JJUZaMdpo] の 70% 閾値ベース. 2026-05-01 候補 C で global_only 追加 4 区分化
    # (詳細: reference_shipping_tariff_logic.md v1.0 § 4).
    with get_conn() as conn:
        # ebay_listings に primary_market 関連カラム追加 (冪等)
        for col, typ in [
            ("primary_market", "TEXT"),           # 'US_only' / 'mixed_global' / 'global_only' / 'unknown'
            ("us_buyer_ratio", "REAL"),           # 0.0-1.0
            ("market_analysis_at", "TIMESTAMP"),
            ("market_sample_size", "INTEGER"),    # 90日 sold 件数 (β=5 判定基準)
        ]:
            try:
                conn.execute(f"ALTER TABLE ebay_listings ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass

        # 市場分析の生データを保存 (SKU 毎の Terapeak スナップショット)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                ebay_item_id TEXT,
                keyword TEXT,
                day_range INTEGER DEFAULT 90,
                total_sold INTEGER,
                us_count INTEGER,
                non_us_count INTEGER,
                countries_breakdown TEXT,
                avg_sold_price_usd REAL,
                avg_shipping_usd REAL,
                sell_through_pct REAL,
                total_sellers INTEGER,
                primary_market TEXT,
                primary_market_reason TEXT,
                scraped_at TIMESTAMP NOT NULL,
                source TEXT DEFAULT 'terapeak_cdp'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ma_sku_scraped "
            "ON market_analysis(sku, scraped_at DESC)"
        )

        # 区分跨ぎ移行の提案 (mixed_global → US_only 等). user 承認待ち.
        # W7-A migration v26 完走後の正規 schema (listing 粒度, ebay_item_id PK).
        # 既存 DB (user_version >= 26) では IF NOT EXISTS により skip され影響なし.
        # 旧 sku PK スキーマ DB から起動するケースは v26 migration block で _new 経由移行.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_market_changes (
                ebay_item_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                current_market TEXT,
                proposed_market TEXT NOT NULL,
                proposed_at TIMESTAMP NOT NULL,
                market_analysis_id INTEGER NOT NULL,
                reason TEXT,
                FOREIGN KEY (market_analysis_id) REFERENCES market_analysis(id)
            )
        """)

        # user 承認/却下の履歴. 自動化昇格判断 + ライバル抽出材料.
        # W7-A migration v26 完走後の正規 schema (ebay_item_id NOT NULL).
        # 既存 DB (user_version >= 26) では IF NOT EXISTS により skip され影響なし.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_strategy_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                ebay_item_id TEXT NOT NULL,
                previous_market TEXT,
                proposed_market TEXT,
                final_market TEXT,
                action TEXT CHECK(action IN ('approved','rejected','expired')),
                decided_at TIMESTAMP NOT NULL,
                reason TEXT,
                reviewer TEXT DEFAULT 'user'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msd_sku "
            "ON market_strategy_decisions(sku, decided_at DESC)"
        )

        # Override #2 改: $1500+ DE/IT/FR/KZ 注文時の関税通知履歴
        conn.execute("""
            CREATE TABLE IF NOT EXISTS high_value_eu_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                sku TEXT,
                ebay_item_id TEXT,
                buyer_country TEXT,
                item_price_usd REAL,
                shipping_usd REAL,
                total_usd REAL,
                detected_at TIMESTAMP NOT NULL,
                discord_sent INTEGER DEFAULT 0,
                template_text TEXT,
                user_marked_sent_at TIMESTAMP,
                shipped_at TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hvea_detected "
            "ON high_value_eu_alerts(detected_at DESC)"
        )

        # DDP-B 発送 invoice アラート (US_only SKU の注文発生時)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ddpb_dispatch_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                sku TEXT,
                ebay_item_id TEXT,
                buyer_country TEXT,
                sale_price_usd REAL,
                tariff_buffer_usd REAL,
                invoice_declared_usd REAL,
                detected_at TIMESTAMP NOT NULL,
                pre_ship_alerted_at TIMESTAMP,
                shipped_at TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dda_detected "
            "ON ddpb_dispatch_alerts(detected_at DESC)"
        )

    # ===== マイグレーション v26 (2026-04-29): W7-A listing 単位化 =====
    # 旧 pending_market_changes (PK=sku) と market_strategy_decisions (sku 集約) を
    # listing 粒度 (PK or NOT NULL = ebay_item_id) に変更.
    # 事故再発防止: 1 SKU 承認 → 40 listing 巻添え cascade を物理的に防ぐ.
    # Q2 ルール準拠で DROP + RENAME は本 migration 内では実行せず、
    # 別 one-shot script (scripts/migrate_pending_to_listing_v26.py) に分離.
    # user_version = 26 で冪等性ガード.
    with get_conn() as conn:
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver < 26:
            # canonical PMC が既に新スキーマ (ebay_item_id PK) なら _new 作成 + 旧→新 INSERT は
            # no-op. 新規環境 (init_db で canonical を新スキーマで直接 CREATE) では
            # 本 gate により _new 孤児テーブル発生を防ぐ. W68 Step 1 / sku-rules.md.
            pmc_pk = [
                r[1] for r in conn.execute(
                    "PRAGMA table_info(pending_market_changes)"
                ).fetchall() if r[5] == 1
            ]
            if pmc_pk != ["ebay_item_id"]:
                # (a) pending_market_changes_new: listing 粒度 (PK = ebay_item_id NOT NULL)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pending_market_changes_new (
                        ebay_item_id TEXT PRIMARY KEY,
                        sku TEXT NOT NULL,
                        current_market TEXT,
                        proposed_market TEXT NOT NULL,
                        proposed_at TIMESTAMP NOT NULL,
                        market_analysis_id INTEGER NOT NULL,
                        reason TEXT,
                        FOREIGN KEY (market_analysis_id) REFERENCES market_analysis(id)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pmc_new_sku "
                    "ON pending_market_changes_new(sku)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pmc_new_proposed_at "
                    "ON pending_market_changes_new(proposed_at DESC)"
                )

                # 旧 → 新 引越し (現状 0 件だが冪等性のため処理は書く).
                # 旧 sku PK 1 行 → ebay_listings JOIN で N 件 listing 行に展開.
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO pending_market_changes_new
                            (ebay_item_id, sku, current_market, proposed_market,
                             proposed_at, market_analysis_id, reason)
                        SELECT el.ebay_item_id, pmc.sku,
                               COALESCE(el.primary_market, pmc.current_market),
                               pmc.proposed_market, pmc.proposed_at,
                               pmc.market_analysis_id, pmc.reason
                        FROM pending_market_changes pmc
                        JOIN ebay_listings el ON el.sku = pmc.sku
                        WHERE pmc.market_analysis_id IS NOT NULL
                    """)
                except sqlite3.OperationalError:
                    # 旧テーブル既に script で消えている場合 (再実行シナリオ)
                    pass

            # canonical MSD の ebay_item_id 列存在 + NOT NULL 制約を厳密チェック.
            # 列はあるが NULLABLE な中間状態でも _new 作成 path を実走 (review M-2 対応).
            msd_eii = next(
                (
                    r for r in conn.execute(
                        "PRAGMA table_info(market_strategy_decisions)"
                    ).fetchall() if r[1] == "ebay_item_id"
                ),
                None,
            )
            if msd_eii is None or msd_eii[3] != 1:
                # (b) market_strategy_decisions_new: listing 粒度 (ebay_item_id NOT NULL)
                # ALTER で NOT NULL 後付け不可なので新テーブル作成 (B option, user 指示).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS market_strategy_decisions_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sku TEXT NOT NULL,
                        ebay_item_id TEXT NOT NULL,
                        previous_market TEXT,
                        proposed_market TEXT,
                        final_market TEXT,
                        action TEXT CHECK(action IN ('approved','rejected','expired')),
                        decided_at TIMESTAMP NOT NULL,
                        reason TEXT,
                        reviewer TEXT DEFAULT 'user'
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_msd_new_ebay_item "
                    "ON market_strategy_decisions_new(ebay_item_id, decided_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_msd_new_sku_v26 "
                    "ON market_strategy_decisions_new(sku, decided_at DESC)"
                )
                # 旧 decisions は Phase 1 で全削除済 (空) → 引越し SQL 不要

            conn.execute("PRAGMA user_version = 26")

        # v27 (W50 / 2026-04-30): Yahoo Auctions の ebayyh_ prefix を site_configs に seed.
        # 旧経路 (CSV) は ebayh_、新経路 (DB monitored_items) は ebayyh_ で
        # convert_url 不一致だったため prepare_batch_items が全件除外する問題への根本対応.
        # convert_url UNIQUE 制約 + INSERT OR IGNORE = 冪等 (再実行で重複しない).
        if schema_ver < 27:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO site_configs
                       (site_name, url_keyword, in_stock_text1, in_stock_text2,
                        sold_out_text, no_page_text, common_url, convert_url, is_active)
                       SELECT site_name, url_keyword, in_stock_text1, in_stock_text2,
                              sold_out_text, no_page_text, common_url, 'ebayyh_', is_active
                       FROM site_configs WHERE convert_url='ebayh_' LIMIT 1"""
                )
                conn.execute("PRAGMA user_version = 27")
            except sqlite3.OperationalError:
                pass

        # v28 (W72 / 2026-05-01): monitored_items.UNIQUE(sku) 撤廃.
        # 旧 DB は scripts/migrate_monitored_items_v28.py one-shot で RECREATE.
        # 新規環境は L381 が既に新スキーマ (UNIQUE なし) → init_db で完結.
        # Q2 規定 (init_db 内 DROP TABLE 禁止) のため自動 RECREATE しない.
        if schema_ver < 28:
            sku_unique = any(
                r[1].startswith("sqlite_autoindex")
                for r in conn.execute(
                    "PRAGMA index_list(monitored_items)"
                ).fetchall()
            )
            if not sku_unique:
                conn.execute("PRAGMA user_version = 28")
            else:
                # 既存 v27 DB で UNIQUE(sku) 残存 = one-shot script 未実行.
                # init_db 内 RECREATE は Q2 規定で禁止のため、user 通知のみ.
                import logging as _lg
                _lg.getLogger(__name__).warning(
                    "v28 migration pending: monitored_items に UNIQUE(sku) 残存. "
                    "scripts/migrate_monitored_items_v28.py を実行してください."
                )

        # v29 (W94 / 2026-05-02): supplier_eval_pending DLQ table.
        # Anthropic Batch API + Cache stack 統合 (id=181) の Tier 3 fallback 受け皿.
        # batch hard_timeout / errored 後の通常 API fallback も失敗した item を保存.
        # CREATE TABLE IF NOT EXISTS で冪等. Q2: DROP/ALTER 不使用.
        if schema_ver < 29:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS supplier_eval_pending (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        custom_id TEXT NOT NULL,
                        batch_id TEXT,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        UNIQUE(custom_id, batch_id)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_supplier_eval_pending_unresolved
                    ON supplier_eval_pending (created_at)
                    WHERE resolved_at IS NULL
                """)
                conn.execute("PRAGMA user_version = 29")
            except sqlite3.OperationalError as e:
                # Q2 silent skip 防止: 半成立 (CREATE 通って PRAGMA 失敗 等) を surface.
                import logging as _lg29
                _lg29.getLogger(__name__).warning(
                    f"v29 migration (supplier_eval_pending DLQ) skipped: {e}"
                )

        # v30 (W98 / 2026-05-05): 最安値チェック (lowest price check) 用カラム追加.
        # ebay_listings に 仕入価格 / 最低価格(下限) / 最低利益価格キャッシュ を追加.
        # ALTER TABLE ADD COLUMN は重複適用で「duplicate column name」エラー → 個別 try/except.
        if schema_ver < 30:
            for _col_sql in (
                "ALTER TABLE ebay_listings ADD COLUMN purchase_yen REAL",
                "ALTER TABLE ebay_listings ADD COLUMN lp_min_price REAL",
                "ALTER TABLE ebay_listings ADD COLUMN lp_breakeven_usd REAL",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass  # 既に列存在 = OK
            conn.execute("PRAGMA user_version = 30")

        # v31 (W98 / 2026-05-05): 新規発見ライバルの送料カラム追加.
        # eBay GetItem API で取得した送料を都度キャッシュ.
        if schema_ver < 31:
            try:
                conn.execute(
                    "ALTER TABLE new_competitor_alerts ADD COLUMN found_shipping REAL"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 31")

        # v32 (W98 / 2026-05-05): ライバル価格・送料の DB キャッシュ + 自動取得日時.
        # UI で id/価格/送料/合計 の 4 情報表示するため、Browse API 取得値を保持.
        if schema_ver < 32:
            for _col_sql in (
                "ALTER TABLE competitor_products ADD COLUMN competitor_price_usd REAL",
                "ALTER TABLE competitor_products ADD COLUMN competitor_shipping_usd REAL",
                "ALTER TABLE competitor_products ADD COLUMN last_priced_at TIMESTAMP",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("PRAGMA user_version = 32")

        # v33 (W183 / 2026-05-10): 値下げ履歴テーブル.
        # 6h scheduler / 手動 button による ReviseFixedPriceItem 実行履歴を全件記録.
        # L2 (1 日 4 回上限) の判定 + UI 履歴表示 + retrospective audit に使う.
        if schema_ver < 33:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS price_change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ebay_item_id TEXT NOT NULL,
                        old_price_usd REAL,
                        new_price_usd REAL,
                        competitor_item_id TEXT,
                        competitor_total_usd REAL,
                        rule_applied TEXT,
                        triggered_by TEXT NOT NULL,
                        success INTEGER NOT NULL DEFAULT 0,
                        error_message TEXT,
                        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_price_change_log_listing "
                    "ON price_change_log(ebay_item_id, changed_at DESC)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_price_change_log_date "
                    "ON price_change_log(changed_at)"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 33")

        # v34 (W119 / 2026-05-10): ebay_listings に検索キーワード 3 列を追加.
        # search_keyword: Opus 4.7 batch で title から抽出した eBay 検索ワード
        # search_keyword_generated_at: 生成日時 (再生成判定用)
        # search_keyword_source: 'opus_batch' | 'manual_edit' (出所識別)
        # Browse API 競合検索 + eBay リンクボタン URL の両方で使用.
        if schema_ver < 34:
            for _col_sql in (
                "ALTER TABLE ebay_listings ADD COLUMN search_keyword TEXT",
                "ALTER TABLE ebay_listings ADD COLUMN search_keyword_generated_at TIMESTAMP",
                "ALTER TABLE ebay_listings ADD COLUMN search_keyword_source TEXT",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("PRAGMA user_version = 34")

        # v35 (W119 / 2026-05-11): competitor_products に handling/delivery 列を追加.
        # Browse API は handling_time を直接返さないため、
        # min_estimated_delivery_date / max_estimated_delivery_date を保存し、
        # UI 側で「発送目安日数」を計算表示する.
        # 商品管理タブのライバル dataframe で表示.
        if schema_ver < 35:
            for _col_sql in (
                "ALTER TABLE competitor_products ADD COLUMN min_delivery_date TEXT",
                "ALTER TABLE competitor_products ADD COLUMN max_delivery_date TEXT",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("PRAGMA user_version = 35")

        # v36 (W119 / 2026-05-12): ebay_listings.inventory_count 追加.
        # 商品管理タブで user が 有在庫 (SKU が "stock" prefix) の物理在庫数を入力.
        # 売れたら task_order_alert.py が GetOrders API で検知 → 自動減算.
        # quantity_ebay (= eBay 出品数) とは別管理 = user の物理在庫管理用.
        if schema_ver < 36:
            try:
                conn.execute(
                    "ALTER TABLE ebay_listings ADD COLUMN inventory_count INTEGER"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 36")

        # v37 (W119 / 2026-05-12 Wave A): inventory_decrement_log を init_db に集約.
        # task_order_alert._decrement_inventory_for_stock_sku 内 CREATE TABLE を解消 (Q2).
        # UNIQUE(order_id, ebay_item_id) で同 order 二重 polling を冪等化.
        if schema_ver < 37:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS inventory_decrement_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT NOT NULL,
                        ebay_item_id TEXT NOT NULL,
                        sku TEXT,
                        quantity_decremented INTEGER NOT NULL,
                        new_inventory_count INTEGER,
                        decremented_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(order_id, ebay_item_id)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 37")

        # v38 (W120+W121 / 2026-05-12): 仕入先 価格変動検知 + 楽天 text 補完.
        # 1) monitored_items に 4 列 ALTER (baseline / current / baseline_at / alert_state)
        # 2) 楽天市場 site_config の sold_out_text / no_page_text を UPDATE (DEFAULT_SITE_CONFIGS は空文字、追加が必要)
        if schema_ver < 38:
            for _col_sql in (
                "ALTER TABLE monitored_items ADD COLUMN baseline_price_jpy INTEGER",
                "ALTER TABLE monitored_items ADD COLUMN current_price_jpy INTEGER",
                "ALTER TABLE monitored_items ADD COLUMN baseline_at TIMESTAMP",
                "ALTER TABLE monitored_items ADD COLUMN price_alert_state TEXT",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            # 楽天市場の sold_out / no_page text を補完 (DEFAULT は空文字、判定不能だった).
            # 「売り切れ」「ご指定のページは見つかりません」は楽天市場で頻出する標準表記.
            try:
                conn.execute(
                    """UPDATE site_configs
                       SET sold_out_text = ?, no_page_text = ?
                       WHERE convert_url = 'ebayRT_'
                         AND (sold_out_text IS NULL OR sold_out_text = '')""",
                    ("売り切れ", "ご指定のページは見つかりません"),
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 38")

        # v39 (W122 / 2026-05-13): morning_discovery_candidates 新規テーブル.
        # 朝 07:00 に Opus 4.7 が発掘した新商品候補 3 件を保存し、
        # クリック型評価 (buy/skip/hold/listed) + 自由記述 (user_comment) で
        # 翌日以降の Few-shot プロンプトに学習反映する.
        # 親 = research_qa (source='morning_discovery') / 1 QA = 3 候補.
        if schema_ver < 39:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS morning_discovery_candidates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        qa_id INTEGER NOT NULL,
                        candidate_rank INTEGER NOT NULL,
                        product_name TEXT NOT NULL,
                        rationale TEXT,
                        supplier_price_jpy INTEGER,
                        ebay_estimated_price_usd REAL,
                        estimated_profit_usd REAL,
                        similar_sold_count_30d INTEGER,
                        competitor_jp_count INTEGER,
                        vero_risk_level TEXT,
                        star_rating INTEGER,
                        next_action TEXT,
                        source_urls TEXT,
                        layer_origin TEXT,
                        user_decision TEXT,
                        user_comment TEXT,
                        user_decided_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (qa_id) REFERENCES research_qa(id)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mdc_qa "
                    "ON morning_discovery_candidates(qa_id)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mdc_decision "
                    "ON morning_discovery_candidates(user_decision, created_at)"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 39")

        # v40 (W133 / 2026-05-16): 有在庫管理 — eBay 在庫数 sync 痕跡 + 仕入確認ログ.
        # 1) ebay_listings に 3 列 ALTER:
        #    last_qty_sync_at      : 最終 ReviseInventoryStatus 成功時刻
        #    last_synced_quantity  : その時 eBay へ送った数量 (= inventory_count)
        #    qty_sync_error        : sync 失敗 / 数量0 revise 抑止 の理由 (NULL=正常)
        #    → Q0 silent skip 防止: sync 失敗が DB 列に必ず残る (UI + Discord と併用).
        # 2) purchase_confirmation_log 新規:
        #    仕入入荷メール → user が listing と仕入個数を確定した履歴.
        #    dedupe = UNIQUE(gmail_id, ebay_item_id). SKU は dedupe キーに **含めない**
        #    (SKU は在庫種別フラグであって listing 識別キーではない / sku-rules.md).
        #    listing 識別は ebay_item_id 単位 (migration v26 / W7-A 単位化準拠).
        if schema_ver < 40:
            for _col_sql in (
                "ALTER TABLE ebay_listings ADD COLUMN last_qty_sync_at TIMESTAMP",
                "ALTER TABLE ebay_listings ADD COLUMN last_synced_quantity INTEGER",
                "ALTER TABLE ebay_listings ADD COLUMN qty_sync_error TEXT",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS purchase_confirmation_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        gmail_id TEXT NOT NULL,
                        ebay_item_id TEXT NOT NULL,
                        sku TEXT,
                        quantity_added INTEGER NOT NULL,
                        old_inventory_count INTEGER,
                        new_inventory_count INTEGER,
                        ebay_qty_sync_ok INTEGER NOT NULL DEFAULT 0,
                        confirmed_by TEXT DEFAULT 'user',
                        confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(gmail_id, ebay_item_id)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pcl_item "
                    "ON purchase_confirmation_log(ebay_item_id, confirmed_at DESC)"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 40")

        # v41 (W138-A / 2026-05-17): shipping policy (BP) を DB 列化し
        # 商品管理 hero に価格同様「最初から自動表示」。
        #    shipping_profile_id        : eBay Business Policy (fulfillment) ID。
        #                                 NULL/'' は文脈依存 (下記 fetched_at と併せ 3 分岐):
        #    shipping_profile_fetched_at : 当該 BP を実 eBay GetItem から最後に取得した
        #                                 時刻 (UTC、sqlite-timezone.md 準拠)。
        #    判定 (HIGH-2 NULL 多義性解消): fetched_at IS NULL=未取得 (Inline と断定不可) /
        #      fetched_at NOT NULL & id NULL/''=確定 Inline (BP なし) /
        #      fetched_at NOT NULL & id あり=BP あり.
        #    鮮度: GetMyeBaySelling は BP を返さないため定期 task_ebay_sync に
        #      相乗り不可。📤eBay反映時 _sync_db_to_actual / 単発 ↻ 再取得 /
        #      初回 backfill でのみ更新 (= 価格に劣る鮮度を fetched_at 併記で正直開示).
        if schema_ver < 41:
            for _col_sql in (
                "ALTER TABLE ebay_listings ADD COLUMN shipping_profile_id TEXT",
                "ALTER TABLE ebay_listings "
                "ADD COLUMN shipping_profile_fetched_at TIMESTAMP",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("PRAGMA user_version = 41")

        # v42 (W7/W183 H4 race 堅牢化 / 2026-05-17): price_change_log に
        # claim_status を追加。値下げ 1 回分の「予約 (reservation)」を eBay API
        # 呼出の前に確保し、scheduler プロセスと Streamlit プロセスが同じ
        # listing を同時処理して 1 日 4 回上限を超える race (H4) を防ぐ。
        #   claim_status: NULL      = legacy / 直接記録行 (success で結果判定)
        #                 'pending' = 予約確保済・API 実行中 (枠を一時消費)
        #                 'final'   = 確定 (success=1 で枠消費継続 /
        #                             success=0 で枠解放 = 失敗は本日 4 回に
        #                             カウントしない / user 確定 2026-05-17)
        if schema_ver < 42:
            try:
                conn.execute(
                    "ALTER TABLE price_change_log ADD COLUMN claim_status TEXT"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 42")

        # v43 (W142 / 2026-05-19): 送料 +each (ShippingServiceAdditionalCost)
        # を DB 列化。根本原因#5(b): ebay_listings に +each 保存列が無いため
        # 商品管理タブの「送料 +each」入力が常時 value=None (表示 source 皆無)。
        # shipping_cost (v1) と対称だが DEFAULT を付けない (= NULL)。理由:
        #   shipping_cost は v1 で全 listing 0.0 初期化済の既存事実があり今は
        #   変えない (K2) が、+each は新規列なので「未取得 NULL」と「明示
        #   $0.00」を最初から区別でき、_sync_db_to_actual の None-skip 慣習
        #   (snap の値が None の項目は触らない) と整合する (HIGH-2 NULL
        #   多義性の踏襲)。
        #   shipping_additional_cost        : Domestic ShippingServiceAdditional
        #                                     Cost (2 個目以降の追加送料 USD)。
        #   shipping_additional_fetched_at  : 実 eBay GetItem から最後に取得
        #                                     した時刻 (UTC、sqlite-timezone.md)。
        #     更新元は shipping_profile 同様 📤eBay反映時 _sync_db_to_actual /
        #     単発 ↻ 再取得のみ (定期 sync は GetMyeBaySelling が返さず相乗り
        #     不可、fetched_at 併記で鮮度を正直開示)。
        if schema_ver < 43:
            for _col_sql in (
                "ALTER TABLE ebay_listings "
                "ADD COLUMN shipping_additional_cost REAL",
                "ALTER TABLE ebay_listings "
                "ADD COLUMN shipping_additional_fetched_at TIMESTAMP",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("PRAGMA user_version = 43")

        # v44 (W140 / 2026-05-19): listing 単位メモ + 売却時警告。
        # listing 識別は ebay_item_id (sku-rules.md 厳守、SKU をキーにしない)。
        #   listing_notes        : 1 listing = 1 自由メモ (発送/通関の注意点)。
        #                          eBay へは送らず MonoDeck DB のみ保持。relist
        #                          で ebay_item_id が変わると別レコード = 旧メモ
        #                          は残存 (データ消失なし)。自動再出品の
        #                          inherit_listing_on_relist が旧→新へ引き継ぐ。
        #   listing_sale_warnings: メモ付き listing が売れた時の警告。
        #                          UNIQUE(order_id, ebay_item_id) で
        #                          claim-then-act (二重 polling/Discord 防止、
        #                          既存 inventory_decrement_log と同型)。
        #                          status: open|acked|dismissed (再通知なし、
        #                          MonoDeck バナーは open のみ)。note_snapshot
        #                          = 売却時点のメモ (後の編集で証跡を失わない)。
        # Q2: CREATE IF NOT EXISTS のみ・DROP/DELETE なし = init_db 2 回でも保持。
        if schema_ver < 44:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS listing_notes (
                        ebay_item_id TEXT PRIMARY KEY,
                        note_text    TEXT,
                        updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS listing_sale_warnings (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id      TEXT NOT NULL,
                        ebay_item_id  TEXT NOT NULL,
                        note_snapshot TEXT,
                        status        TEXT NOT NULL DEFAULT 'open',
                        discord_sent  INTEGER NOT NULL DEFAULT 0,
                        detected_at   TIMESTAMP NOT NULL,
                        acked_at      TIMESTAMP,
                        UNIQUE(order_id, ebay_item_id)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_lsw_open "
                    "ON listing_sale_warnings(status, detected_at DESC)"
                )
            except sqlite3.OperationalError:
                pass
            # Codex 2段 HIGH-2 (Q2 自己修復): CREATE が万一 OperationalError
            # (disk full / lock 等) で握り潰された場合に version だけ進むと、
            # 次回以降 `if schema_ver < 44` を skip し W140 (Q0 安全網) が
            # 永久欠落する。両テーブル実在を確認できた時のみ版数を進め、
            # 失敗時は schema_ver < 44 のまま = 次回 init_db で自動再試行。
            # (既存 v40-v43 の無条件 bump は K2 で本 PR では触らない。
            #  新規 W140 ブロックのみ堅牢化 = db-migration-rules Q2 趣旨)
            _w140_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('listing_notes','listing_sale_warnings')"
            ).fetchone()[0]
            if _w140_ok == 2:
                conn.execute("PRAGMA user_version = 44")

        # v45 (W133-FU / 2026-05-21): 無在庫が売れた仕入 (fulfillment) と
        # 有在庫補充 (restock) を区別するため purchase_confirmation_log に
        # fulfillment_kind 列追加 (additive nullable、Q2 冪等)。
        # 'restock' = 有在庫 SKU の在庫補充 (inventory_count 加算する従来動作)
        # 'fulfillment' = 無在庫 SKU が売れて発注した仕入 (inventory 加算しない、
        #                 purchase_confirmation_log で「仕入完了」マーキングのみ)
        if schema_ver < 45:
            try:
                conn.execute(
                    "ALTER TABLE purchase_confirmation_log "
                    "ADD COLUMN fulfillment_kind TEXT DEFAULT 'restock'"
                )
            except sqlite3.OperationalError:
                pass  # 既存列ありで OK (冪等)
            # 自己修復: 列存在確認後にのみ version bump (W140 v44 と同流儀)。
            _cols = [
                r[1] for r in conn.execute(
                    "PRAGMA table_info(purchase_confirmation_log)"
                ).fetchall()
            ]
            if 'fulfillment_kind' in _cols:
                conn.execute("PRAGMA user_version = 45")

        # v46 (W148 / 2026-05-21): キーワード新着監視 (AlertCrawler 移植)
        # 検索 URL : N 商品 hits 軸 (在庫監視と概念独立、SKU 不使用)。
        # claim-then-act dedupe = UNIQUE(watch_id, found_item_url) で
        # 二重巡回・二重 Discord を物理排除 (inventory_decrement_log v37 /
        # listing_sale_warnings v44 と同型 idiom)。
        # is_sentinel: サイトの DOM 変更/bot ban を検知する番人 watch (v2.1)。
        # 全 sentinel が同時 0 件 = site-wide 異常 → Discord 警告 1 回/run。
        if schema_ver < 46:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS keyword_watches (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        site            TEXT NOT NULL,
                        search_url      TEXT NOT NULL,
                        keyword         TEXT NOT NULL,
                        price_min_jpy   INTEGER,
                        price_max_jpy   INTEGER,
                        memo            TEXT,
                        is_active       INTEGER NOT NULL DEFAULT 1,
                        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_crawled_at TIMESTAMP,
                        last_error      TEXT,
                        is_sentinel     INTEGER NOT NULL DEFAULT 0,
                        source          TEXT DEFAULT 'manual',
                        UNIQUE(site, search_url)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS keyword_watch_hits (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        watch_id        INTEGER NOT NULL,
                        found_item_url  TEXT NOT NULL,
                        title           TEXT,
                        price_jpy       INTEGER,
                        image_url       TEXT,
                        in_price_range  INTEGER NOT NULL,
                        discord_sent    INTEGER NOT NULL DEFAULT 0,
                        detected_at     TIMESTAMP NOT NULL,
                        notified_at     TIMESTAMP,
                        FOREIGN KEY (watch_id) REFERENCES keyword_watches(id),
                        UNIQUE(watch_id, found_item_url)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kw_active "
                    "ON keyword_watches(is_active, last_crawled_at)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kwh_recent "
                    "ON keyword_watch_hits(watch_id, detected_at DESC)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kwh_unnotified "
                    "ON keyword_watch_hits(discord_sent, detected_at DESC) "
                    "WHERE in_price_range = 1"
                )
            except sqlite3.OperationalError:
                pass
            # 自己修復: 両テーブル実在を確認してから version bump (W140 v44 流儀)。
            # 失敗時は schema_ver < 46 のまま = 次回 init_db で自動再試行。
            _w148_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('keyword_watches','keyword_watch_hits')"
            ).fetchone()[0]
            if _w148_ok == 2:
                conn.execute("PRAGMA user_version = 46")

        # v47 (W149 / 2026-05-22): eBay 売却注文 API 直接取得 + fulfillment 自動ひも付け
        # (a) sales_history.ebay_order_id 追加 (UNIQUE で再実行冪等、INSERT OR IGNORE で衝突 skip).
        # (b) fulfillment_order_link 新規 (purchase_confirmation_log と sales_history の 1:1 対応).
        # (c) sales_history_fetch_failures 新規 (30 min polling 失敗 retry queue、5 回失敗で Discord).
        # 自己修復 (W148 v46 / W140 v44 流儀): 必須 column/table 全実在を sqlite_master で確認後に user_version bump.
        if schema_ver < 47:
            try:
                conn.execute("ALTER TABLE sales_history ADD COLUMN ebay_order_id TEXT")
            except sqlite3.OperationalError:
                pass  # 既存
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_history_ebay_order_id "
                    "ON sales_history(ebay_order_id) WHERE ebay_order_id IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS fulfillment_order_link (
                        id                           INTEGER PRIMARY KEY AUTOINCREMENT,
                        purchase_confirmation_log_id INTEGER NOT NULL,
                        sales_history_id             INTEGER NOT NULL,
                        matched_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        match_method                 TEXT NOT NULL,
                        FOREIGN KEY (purchase_confirmation_log_id) REFERENCES purchase_confirmation_log(id),
                        FOREIGN KEY (sales_history_id) REFERENCES sales_history(id),
                        UNIQUE(purchase_confirmation_log_id),
                        UNIQUE(sales_history_id)
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fol_pcl "
                    "ON fulfillment_order_link(purchase_confirmation_log_id)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fol_sh "
                    "ON fulfillment_order_link(sales_history_id)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sales_history_fetch_failures (
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        ebay_order_id     TEXT NOT NULL UNIQUE,
                        first_attempt_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_attempt_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        attempt_count     INTEGER NOT NULL DEFAULT 1,
                        last_error        TEXT,
                        discord_notified  INTEGER NOT NULL DEFAULT 0
                    )
                """)
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_shff_last_attempt "
                    "ON sales_history_fetch_failures(last_attempt_at)"
                )
            except sqlite3.OperationalError:
                pass
            # 自己修復: 必須 column + 必須 table 全実在を確認してから version bump.
            _cols_sh = [
                r[1] for r in conn.execute("PRAGMA table_info(sales_history)").fetchall()
            ]
            _w149_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('fulfillment_order_link','sales_history_fetch_failures')"
            ).fetchone()[0]
            if 'ebay_order_id' in _cols_sh and _w149_tables == 2:
                conn.execute("PRAGMA user_version = 47")

        # v48 (W149 / 2026-05-22 Phase D self-discover): UNIQUE INDEX 入れ替え.
        # v47 の idx_sales_history_ebay_order_id (ebay_order_id 単独) は 1 注文 N 商品の場合
        # 同 order_id を 2 度 INSERT で 2 回目 UNIQUE 衝突 skip = silent line item 消失
        # (buyer まとめ買い時に sales_history が 1 行欠ける、利益計算が部分欠落する).
        # 設計書 v2 §5「line_item 単位で 1 行ずつ」の意図に合わせ、複合キー
        # (ebay_order_id, ebay_item_id) に変更. backfill dry-run で 101 transaction →
        # 100 件 INSERT で 1 件 silent skip した実測で発見.
        if schema_ver < 48:
            try:
                conn.execute("DROP INDEX IF EXISTS idx_sales_history_ebay_order_id")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_history_order_item "
                    "ON sales_history(ebay_order_id, ebay_item_id) "
                    "WHERE ebay_order_id IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass
            # 自己修復: 新 INDEX 実在確認後に version bump
            _v48_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                "AND name = 'idx_sales_history_order_item'"
            ).fetchone()[0]
            if _v48_ok == 1:
                conn.execute("PRAGMA user_version = 48")

        # v49 (W151 / 2026-05-22): 初期登録 trackering. user 業務 = 各 listing の
        # 初期登録 (ライバル登録 / 物理属性入力 / 仕入先候補確定 等) 完了時に
        # checkbox を入れて未完了 / 完了済をフィルタ可能化. initial_registered_at
        # は W153 (新規ライバル発見) の「初期登録以降の rival」base point として参照.
        # additive nullable column (Q2 冪等性), 既存データ非破壊.
        if schema_ver < 49:
            try:
                conn.execute(
                    "ALTER TABLE ebay_listings ADD COLUMN "
                    "initial_registered INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # 既存
            try:
                conn.execute(
                    "ALTER TABLE ebay_listings ADD COLUMN "
                    "initial_registered_at TIMESTAMP"
                )
            except sqlite3.OperationalError:
                pass
            # 自己修復: 2 列実在確認後に version bump
            _cols_el = [
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ebay_listings)"
                ).fetchall()
            ]
            if ('initial_registered' in _cols_el
                    and 'initial_registered_at' in _cols_el):
                conn.execute("PRAGMA user_version = 49")

        # v50 (W153 / 2026-05-22): 商品別ライバル検出.
        # listing 識別は ebay_item_id (sku-rules、SKU 不使用).
        # anchor は MAX(initial_registered_at, rival_watch_started_at) — H-A 対策.
        # additive nullable 4 列 + 新 table listing_rival_discoveries + 3 index.
        # H-B 対策: drift recovery を schema_ver と独立に毎回 check (W149 v2 設計と同型).
        _W153_DDL_MAP = {
            'rival_watch_enabled': 'INTEGER DEFAULT 0',
            'rival_search_keywords': 'TEXT',
            'rival_search_keywords_generated_at': 'TIMESTAMP',
            'rival_watch_started_at': 'TIMESTAMP',
        }
        _W153_LRD_CREATE_SQL = """
        CREATE TABLE IF NOT EXISTS listing_rival_discoveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_item_id TEXT NOT NULL,
            competitor_seller TEXT NOT NULL,
            competitor_item_id TEXT NOT NULL,
            competitor_title TEXT,
            competitor_price_usd REAL,
            search_keyword TEXT,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'new',
            status_changed_at TIMESTAMP,
            UNIQUE(ebay_item_id, competitor_seller, competitor_item_id)
        )
        """
        _W153_LRD_INDEXES = (
            "CREATE INDEX IF NOT EXISTS idx_lrd_listing_status "
            "ON listing_rival_discoveries(ebay_item_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_lrd_first_seen "
            "ON listing_rival_discoveries(first_seen_at)",
            "CREATE INDEX IF NOT EXISTS idx_lrd_status_new "
            "ON listing_rival_discoveries(status) WHERE status = 'new'",
        )

        # (1) 列存在 check & 欠損 ALTER (schema_ver 無関係 / H-B drift recovery)
        _w153_cols_el = set(
            r[1] for r in conn.execute(
                "PRAGMA table_info(ebay_listings)"
            ).fetchall()
        )
        _w153_missing = {
            c for c in _W153_DDL_MAP if c not in _w153_cols_el
        }
        for _col in _w153_missing:
            try:
                conn.execute(
                    f"ALTER TABLE ebay_listings ADD COLUMN "
                    f"{_col} {_W153_DDL_MAP[_col]}"
                )
                logger.info(
                    f"[init_db v50] recovered missing column: "
                    f"ebay_listings.{_col}"
                )
            except sqlite3.OperationalError:
                pass

        # (2) listing_rival_discoveries table 存在 check & 欠損 CREATE
        _w153_has_lrd = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='listing_rival_discoveries'"
        ).fetchone()
        if not _w153_has_lrd:
            try:
                conn.execute(_W153_LRD_CREATE_SQL)
                logger.info(
                    "[init_db v50] recovered missing table: "
                    "listing_rival_discoveries"
                )
            except sqlite3.OperationalError:
                pass

        # (3) index 存在 check & 欠損 CREATE (M-internal-8、CREATE IF NOT EXISTS で冪等)
        for _idx_sql in _W153_LRD_INDEXES:
            try:
                conn.execute(_idx_sql)
            except sqlite3.OperationalError:
                pass

        # (4) 完全に揃った後でのみ user_version bump
        _w153_cols_post = set(
            r[1] for r in conn.execute(
                "PRAGMA table_info(ebay_listings)"
            ).fetchall()
        )
        _w153_lrd_post = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='listing_rival_discoveries'"
        ).fetchone()
        if (set(_W153_DDL_MAP).issubset(_w153_cols_post)
                and _w153_lrd_post is not None):
            if schema_ver < 50:
                conn.execute("PRAGMA user_version = 50")
                logger.info("[init_db v50] schema_ver bumped to 50")

        # v51/v52 (W153 v2 / 2026-05-22 PM): rival shipping 情報を listing_rival_discoveries
        # に保存. UI で送料 + 配達日数 + 発送方法名表示 + Economy 系 hide.
        # 業務知識: Economy 系は安商品で seller が使い分けるため seller block list は誤り
        # (reference_ebay_economy_shipping_seller_pattern.md). 検索段階 skip → UI hide に変更.
        # v51 で 3 列 (shipping_cost/min/max_delivery_date)、v52 で shipping_service_code 追加.
        # search response には shipping_service_code 含まれないため詳細 API enrich で取得.
        # additive nullable、drift recovery 対応 (v50 と同型).
        _W153_V51_LRD_COLS = {
            'competitor_shipping_cost_usd': 'REAL',
            'min_delivery_date': 'TEXT',
            'max_delivery_date': 'TEXT',
            # v52 (2026-05-22 PM): 発送方法名 (get_item_by_legacy_id 経由で取得).
            # search response には含まれないため新規 rival のみ詳細 API で enrich.
            'shipping_service_code': 'TEXT',
        }
        # listing_rival_discoveries が存在する場合のみ ALTER 試行
        _w153v51_has_lrd = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='listing_rival_discoveries'"
        ).fetchone()
        if _w153v51_has_lrd:
            _w153v51_existing = set(
                r[1] for r in conn.execute(
                    "PRAGMA table_info(listing_rival_discoveries)"
                ).fetchall()
            )
            _w153v51_missing = {
                c for c in _W153_V51_LRD_COLS if c not in _w153v51_existing
            }
            for _col in _w153v51_missing:
                try:
                    conn.execute(
                        f"ALTER TABLE listing_rival_discoveries ADD COLUMN "
                        f"{_col} {_W153_V51_LRD_COLS[_col]}"
                    )
                    logger.info(
                        f"[init_db v51] recovered missing column: "
                        f"listing_rival_discoveries.{_col}"
                    )
                except sqlite3.OperationalError:
                    pass

            # 完全に揃った後でのみ user_version bump
            _w153v51_post = set(
                r[1] for r in conn.execute(
                    "PRAGMA table_info(listing_rival_discoveries)"
                ).fetchall()
            )
            if set(_W153_V51_LRD_COLS).issubset(_w153v51_post):
                if schema_ver < 52:
                    conn.execute("PRAGMA user_version = 52")
                    logger.info("[init_db v52] schema_ver bumped to 52")

        # v54 (W182, 2026-05-28): supplier_candidates に availability check カラム追加.
        # sold_out 商品を candidate として登録する bug の恒久対策 (Codex 2026-05-28 調査).
        # 詳細: .company/engineering/migration/codex-supplier-bug-investigation.md
        # v53 は W139-revisit Phase 1 (coverage_anomaly_log) 予約済のため v54 を採番.
        if schema_ver < 54:
            _W182_V54_SC_COLS = {
                "availability_status": "TEXT",
                "availability_checked_at": "TIMESTAMP",
                "availability_signal": "TEXT",
            }
            for _col, _type in _W182_V54_SC_COLS.items():
                try:
                    conn.execute(
                        f"ALTER TABLE supplier_candidates ADD COLUMN "
                        f"{_col} {_type}"
                    )
                    logger.info(
                        f"[init_db v54] supplier_candidates.{_col} added"
                    )
                except sqlite3.OperationalError:
                    pass
            _w182v54_post = set(
                r[1] for r in conn.execute(
                    "PRAGMA table_info(supplier_candidates)"
                ).fetchall()
            )
            if set(_W182_V54_SC_COLS).issubset(_w182v54_post):
                conn.execute("PRAGMA user_version = 54")
                logger.info("[init_db v54] schema_ver bumped to 54")

        # v55 (W183, 2026-05-28): EC サイト直接 URL 無在庫出品対応 + 楽天/Amazon 在庫判定修正.
        # source_url_manual=1 で SKU 同期による source_url 上書きを防ぎ、Amazon/楽天等の
        # SKU 規則性のない EC サイトを商品管理で直接 URL 設定して無在庫監視できるようにする.
        # 併せて楽天 (schema.org microdata) / Amazon (add-to-cart-button) の在庫判定 signal を
        # 実 HTML と一致する値に修正 (Codex 2026-05-28 実機調査).
        # 詳細: .company/engineering/migration/codex-ec-direct-url-design.md
        if schema_ver < 55:
            _W183_V55_COLS = {
                "ebay_listings": {
                    "source_url_manual": "INTEGER NOT NULL DEFAULT 0",
                    "source_url_updated_at": "TIMESTAMP",
                },
                "monitored_items": {
                    "source_url_manual": "INTEGER NOT NULL DEFAULT 0",
                    "source_url_updated_at": "TIMESTAMP",
                },
            }
            for _tbl, _cols in _W183_V55_COLS.items():
                for _col, _type in _cols.items():
                    try:
                        conn.execute(
                            f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_type}"
                        )
                        logger.info(f"[init_db v55] {_tbl}.{_col} added")
                    except sqlite3.OperationalError:
                        pass
            # 楽天 / Amazon の在庫判定 signal を実 HTML 一致値に修正 (冪等 UPDATE).
            # 楽天: schema.org microdata (InStock/OutOfStock 排他)。旧 'かごに追加' は
            #       売切ページでも disabled button として残るため誤判定 (Codex 検証).
            # Amazon: id="add-to-cart-button" で主ボタン特定。旧 'カートに入れる' は
            #         nav / 関連商品にも出て誤判定 (Codex 検証)。CAPTCHA は scraper 側で unknown 化.
            try:
                conn.execute(
                    "UPDATE site_configs SET "
                    "in_stock_text1=?, in_stock_text2='', sold_out_text=? "
                    "WHERE convert_url='ebayRT_' AND url_keyword='item.rakuten'",
                    ('itemprop="availability" content="http://schema.org/InStock"',
                     'itemprop="availability" content="http://schema.org/OutOfStock"'),
                )
                conn.execute(
                    "UPDATE site_configs SET "
                    "in_stock_text1=?, in_stock_text2=?, sold_out_text=? "
                    "WHERE convert_url='ebayAM_' AND url_keyword='www.amazon.co.jp'",
                    ('id="add-to-cart-button"', 'name="submit.add-to-cart"', '現在在庫切れ'),
                )
                logger.info("[init_db v55] 楽天/Amazon site_configs signal 更新")
            except sqlite3.OperationalError:
                pass
            # 全列が揃った後でのみ user_version bump (冪等性: 部分適用で bump しない)
            _v55_ok = True
            for _tbl, _cols in _W183_V55_COLS.items():
                _post = set(
                    r[1] for r in conn.execute(
                        f"PRAGMA table_info({_tbl})"
                    ).fetchall()
                )
                if not set(_cols).issubset(_post):
                    _v55_ok = False
            if _v55_ok:
                conn.execute("PRAGMA user_version = 55")
                logger.info("[init_db v55] schema_ver bumped to 55")

        # ---- v56: supplier_candidates UNIQUE(sku,candidate_url) → UNIQUE(ebay_item_id,candidate_url) ----
        # W185 (2026-05-29 Opus 4.8 総チェック H3): sku は listing 一意キーに使えない (sku-rules.md)。
        # init_db 内で RECREATE しない (Q2: DROP/DELETE 禁止)。旧 UNIQUE が残っていれば warning のみ出し、
        # 実際の張り替えは scripts/migrate_supplier_candidates_v56.py one-shot で行う (v28 前例に倣う)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver < 56:
            _has_old_sku_unique = False
            for _idx in conn.execute(
                "PRAGMA index_list(supplier_candidates)"
            ).fetchall():
                # index_list: (seq, name, unique, origin, partial). origin 'u' = UNIQUE 制約由来。
                if str(_idx[1]).startswith("sqlite_autoindex") and _idx[3] == "u":
                    _cols = [
                        r[2]
                        for r in conn.execute(
                            f"PRAGMA index_info({_idx[1]})"
                        ).fetchall()
                    ]
                    # 厳密な列セット一致で旧 UNIQUE(sku, candidate_url) を判定 (先頭列のみ一致の誤検知回避)。
                    if _cols == ["sku", "candidate_url"]:
                        _has_old_sku_unique = True
            if not _has_old_sku_unique:
                conn.execute("PRAGMA user_version = 56")
                logger.info("[init_db v56] schema_ver bumped to 56")
            else:
                logger.warning(
                    "[init_db v56] migration pending: supplier_candidates に "
                    "UNIQUE(sku, candidate_url) 残存。scripts/migrate_supplier_candidates_v56.py "
                    "を実行して UNIQUE(ebay_item_id, candidate_url) へ張り替えてください。"
                )

        # ---- v57: health-check auto-fix system (監査ログ + DB書込提案) ----
        # 健康チェックが検知した異常を自動対処する仕組み (task_health_autofix) の基盤。
        # autofix_attempt_log: 全自動対処の痕跡 (Q0 silent skip 防止) + 当日試行回数追跡
        #   (ループガード。finding_hash 単位で count して N 回超で打ち止め)。
        # autofix_db_proposal: Tier3 (DB 書込を伴う修正) の修正案。自動実行せず user 承認
        #   待ち (Q2 本番DB直接書込は原則禁止)。
        # CREATE TABLE IF NOT EXISTS のみ (DROP/DELETE/ALTER 不使用 = 冪等)。
        # gate は `== 56` (`< 57` ではない): v56 は旧 UNIQUE 残存時に bump せず 55 据え置きで
        # 手動移行を強制するガード。`< 57` だと 55 から v57 へ leapfrog して v56 ガードを
        # 無効化するため、v56 完了 (=56) を確認してからのみ進む。fresh DB は同一 init_db 内で
        # v56 block が 56 に bump 済 → 直後に本 block が 57 へ進む (cascade 成立)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 56:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS autofix_attempt_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_date    TEXT    NOT NULL,
                    finding_hash    TEXT    NOT NULL,
                    tier            TEXT    NOT NULL,
                    kind            TEXT    NOT NULL,
                    target_task_key TEXT,
                    action          TEXT    NOT NULL,
                    status          TEXT    NOT NULL,
                    commit_hash     TEXT,
                    gate_report     TEXT,
                    cost_usd        REAL    DEFAULT 0.0,
                    detail          TEXT,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_autofix_date_hash
                    ON autofix_attempt_log(attempt_date, finding_hash)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS autofix_db_proposal (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    finding_hash      TEXT    NOT NULL,
                    kind              TEXT    NOT NULL,
                    diagnosis_sql     TEXT,
                    diagnosis_result  TEXT,
                    proposed_sql      TEXT    NOT NULL,
                    affected_rows_est INTEGER,
                    status            TEXT    NOT NULL DEFAULT 'pending',
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    user_action_at    TIMESTAMP,
                    auto_rejected     INTEGER DEFAULT 0
                )
                """
            )
            # 全テーブル実在を確認後でのみ bump (部分適用で bump しない、冪等性: v55 流儀)。
            _v57_tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('autofix_attempt_log', 'autofix_db_proposal')"
                ).fetchall()
            }
            if {"autofix_attempt_log", "autofix_db_proposal"}.issubset(_v57_tables):
                conn.execute("PRAGMA user_version = 57")
                logger.info(
                    "[init_db v57] autofix tables created, schema_ver bumped to 57"
                )

        # ---- v58: Yahoo!ショッピング site_config を実 HTML 一致値へ修正 (W192) ----
        # 2026-05-30 実機検証: 旧設定は url_keyword='yahoo shopping' (URL に出ない語) で
        # 在庫監視が全件 unmatch、in_stock_text='カートに入れる' は売切ページにも残り誤判定。
        # 実態に合わせ url_keyword=ドメイン部分一致 / sold_out='在庫がありません' / 在庫有 signal 空
        # (clean marker 不在のため、売切と HTTP 404 のみで判定し在庫有は unknown=false-OOS 回避)。
        # v55 の楽天/Amazon UPDATE と同じ冪等 UPDATE パターン (DEFAULT_SITE_CONFIGS も cascade 済)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 57:
            try:
                conn.execute(
                    "UPDATE site_configs SET "
                    "url_keyword=?, in_stock_text1='', in_stock_text2='', "
                    "sold_out_text=?, no_page_text='' "
                    "WHERE convert_url='ebayYS_'",
                    ("shopping.yahoo.co.jp", "在庫がありません"),
                )
                logger.info("[init_db v58] Yahoo!ショッピング site_config signal 更新")
            except sqlite3.OperationalError:
                pass
            # UPDATE が反映されたことを確認後でのみ bump (部分適用で bump しない、v55 流儀)。
            _v58_row = conn.execute(
                "SELECT url_keyword, sold_out_text FROM site_configs "
                "WHERE convert_url='ebayYS_'"
            ).fetchone()
            if _v58_row and _v58_row[0] == "shopping.yahoo.co.jp" \
                    and _v58_row[1] == "在庫がありません":
                conn.execute("PRAGMA user_version = 58")
                logger.info("[init_db v58] schema_ver bumped to 58")

        # ---- v59 (W206, 2026-06-01): keyword_watches.ebay_item_id 追加 ----
        # W206「キーワード新着監視 拡張」: 各 watch を自社の eBay listing と紐付けて
        # Discord 通知 embed に「eBay Item ID」「eBay 販売価格 (USD)」を併記する。
        # 任意メタ列 (NULL 可)。SKU ではなく ebay_item_id 単位の任意紐付け
        # (sku-rules.md: listing 識別は ebay_item_id)。
        # v55/v58 と同じ「ALTER 試行 → table_info で列存在確認 → bump」冪等パターン。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 58:
            try:
                conn.execute(
                    "ALTER TABLE keyword_watches ADD COLUMN ebay_item_id TEXT"
                )
                logger.info(
                    "[init_db v59] keyword_watches.ebay_item_id added"
                )
            except sqlite3.OperationalError:
                pass
            _v59_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(keyword_watches)"
                ).fetchall()
            }
            if "ebay_item_id" in _v59_cols:
                conn.execute("PRAGMA user_version = 59")
                logger.info("[init_db v59] schema_ver bumped to 59")
            else:
                # ALTER が duplicate-column 以外 (ロック/スキーマ異常等) で失敗 = 列未追加。
                # bump せず次回 init_db で再試行するが、痕跡を残さないと Q0 silent skip に
                # なるため warning で可視化 (Codex 2段レビュー LOW 指摘、2026-06-01)。
                logger.warning(
                    "[init_db v59] ebay_item_id 列が未追加 (ALTER 失敗)。"
                    "user_version は 58 のまま、次回 init_db で再試行。"
                )

        # ---- v60 (W209, 2026-06-02): ダッシュボードニュース AI活用アクション化 ----
        # news_items にスコア/軸を持たせ、深掘り結果は news_action_reports に保存。
        # 軸 (relevance_axis):
        #   a = Claude/Codex/MCP/Agent 技術
        #   b = LLM/Agent 新能力の出品文/価格/仕入れ/CS 応用
        #   c = eBay/越境 EC/関税制度
        #   d = スクレイピング/anti-bot
        # listing 識別は ebay_item_id (sku-rules.md) だが、本機能は news 単位 = URL 一意
        # で SKU 規約は非該当。
        # 冪等性パターン: ALTER は try/except sqlite3.OperationalError、CREATE は IF NOT
        # EXISTS、bump は table_info で列実在確認後のみ。v55/v58/v59 と同じ流儀。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 59:
            # news_items に relevance_score / relevance_axis を追加
            for _col, _typ in [
                ("relevance_score", "INTEGER"),
                ("relevance_axis", "TEXT"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE news_items ADD COLUMN {_col} {_typ} DEFAULT NULL"
                    )
                    logger.info(f"[init_db v60] news_items.{_col} added")
                except sqlite3.OperationalError:
                    pass  # カラム既存

            # news_action_reports 新設 (深掘り結果)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_action_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    news_item_id INTEGER,
                    title TEXT,
                    url TEXT,
                    axis TEXT,
                    relevance_score INTEGER,
                    summary_ja TEXT,
                    target_module TEXT,
                    integration_ja TEXT,
                    benefit_ja TEXT,
                    effort_estimate TEXT,
                    confidence TEXT,
                    model TEXT,
                    cost_usd REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(url)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nar_created "
                "ON news_action_reports (created_at DESC)"
            )

            # bump は news_items の 2 列実在 + news_action_reports 実在を全て確認後のみ
            _v60_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(news_items)"
                ).fetchall()
            }
            _v60_tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if (
                "relevance_score" in _v60_cols
                and "relevance_axis" in _v60_cols
                and "news_action_reports" in _v60_tables
            ):
                conn.execute("PRAGMA user_version = 60")
                logger.info("[init_db v60] schema_ver bumped to 60")
            else:
                logger.warning(
                    "[init_db v60] 部分適用: news_items 列 or "
                    "news_action_reports table 未作成。次回 init_db で再試行。"
                )

        # ---- v61 (W212 prep, 2026-06-02): per-listing 関税分類カラム ----
        # Section 232 該当商品ごとに duty 分類と実効関税率(%)を保持。
        # 非該当 (NULL) は calculator が global duty_rate (20%) に fallback。
        # ⚠️ 本カラムはデータ保持のみ。calculator が読む配線は W212 本実装 (washing 修正)
        #   で行う。現時点では breakeven/profit 計算は従来どおり global 20% 固定 = 表示値不変。
        # listing 識別は ebay_item_id (sku-rules.md)。section232_class: NULL / 'I-A' / 'I-B'。
        # duty_rate_pct: 実効関税率 (+5% 手数料 buffer 込、I-A=55 / I-B=30)。
        # 冪等性: ALTER は try/except sqlite3.OperationalError、bump は列実在確認後 (v60 流儀)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 60:
            for _col, _typ in [
                ("section232_class", "TEXT"),
                ("duty_rate_pct", "REAL"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE ebay_listings ADD COLUMN {_col} {_typ} DEFAULT NULL"
                    )
                    logger.info(f"[init_db v61] ebay_listings.{_col} added")
                except sqlite3.OperationalError:
                    pass  # カラム既存
            _v61_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ebay_listings)"
                ).fetchall()
            }
            if "section232_class" in _v61_cols and "duty_rate_pct" in _v61_cols:
                conn.execute("PRAGMA user_version = 61")
                logger.info("[init_db v61] schema_ver bumped to 61")
            else:
                logger.warning(
                    "[init_db v61] 部分適用: ebay_listings の関税カラム未追加。"
                    "次回 init_db で再試行。"
                )

        # ---- v62 (W220, 2026-06-04): per-listing ポイント額 + description 下書き ----
        # point_yen: 仕入先/カードで還元率が違うため per-listing 実ポイント額(¥)を保持。
        #   settings.point_reward_rate (global) は実質 0 で機能せず → calculator は
        #   point_yen 指定時にそれを優先 (未指定は従来の purchase_yen×rate = 後方互換)。
        # listing_description: description 編集の下書き (ローカル保持)。eBay 反映は
        #   明示「📤 eBay反映」ボタン経由 (ReviseFixedPriceItem) で行う。
        # listing 識別は ebay_item_id (sku-rules.md)。冪等: ALTER は try/except
        #   sqlite3.OperationalError、bump は列実在確認後 (v60/v61 流儀)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 61:
            for _col, _typ in [
                ("point_yen", "REAL"),
                ("listing_description", "TEXT"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE ebay_listings ADD COLUMN {_col} {_typ} DEFAULT NULL"
                    )
                    logger.info(f"[init_db v62] ebay_listings.{_col} added")
                except sqlite3.OperationalError:
                    pass  # カラム既存
            _v62_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ebay_listings)"
                ).fetchall()
            }
            if "point_yen" in _v62_cols and "listing_description" in _v62_cols:
                conn.execute("PRAGMA user_version = 62")
                logger.info("[init_db v62] schema_ver bumped to 62")
            else:
                logger.warning(
                    "[init_db v62] 部分適用: ebay_listings の point_yen/"
                    "listing_description 未追加。次回 init_db で再試行。"
                )

        # ---- v63 (W223 step1, 2026-06-05): eBay 商品画像 URL cache 列 ----
        # 仕入先候補の AI 評価に eBay 側商品画像を渡す穴を塞ぐための cache。
        # eBay 画像は DB に保持されておらず GetItem API 経由でしか取れない
        #   (ebay_image_fetcher.py 前例) → 評価毎に API を叩くとコスト/レイテンシ増。
        # ebay_image_url: 取得済 eBay 代表画像 URL (GetItem PictureURL の 1 枚目)。
        # ebay_image_fetched_at: 取得時刻 (UTC, CURRENT_TIMESTAMP)。30 日 cache の鮮度判定。
        # listing 識別は ebay_item_id (sku-rules.md)。冪等: ALTER は try/except
        #   sqlite3.OperationalError、bump は列実在確認後 (v60/v61/v62 流儀)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 62:
            for _col, _typ in [
                ("ebay_image_url", "TEXT"),
                ("ebay_image_fetched_at", "TEXT"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE ebay_listings ADD COLUMN {_col} {_typ} DEFAULT NULL"
                    )
                    logger.info(f"[init_db v63] ebay_listings.{_col} added")
                except sqlite3.OperationalError:
                    pass  # カラム既存
            _v63_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ebay_listings)"
                ).fetchall()
            }
            if "ebay_image_url" in _v63_cols and "ebay_image_fetched_at" in _v63_cols:
                conn.execute("PRAGMA user_version = 63")
                logger.info("[init_db v63] schema_ver bumped to 63")
            else:
                logger.warning(
                    "[init_db v63] 部分適用: ebay_listings の ebay_image_url/"
                    "ebay_image_fetched_at 未追加。次回 init_db で再試行。"
                )

        # ---- v64 (W223 step3, 2026-06-05): 仕入先候補 AI 評価の台帳 ----
        # 同一 (ebay_item_id, 正規化候補URL) を 30 日以内に再評価しない (AI コスト削減)。
        # 却下含む全 AI 評価を記録し、再出現時は過去 AI 判定を再利用 (初回は必ず AI =
        # 「型番一致でも AI スキップ禁止」の制約に抵触しない。再利用は同一候補の過去 AI
        # 判定の流用であって新規候補の AI 省略ではない)。価格変動は呼出側 save loop で
        # profit 再計算するため score のみ再利用。candidate_url は _normalize_url 済を
        # 保存 (scheme/query 揺れ吸収)。listing 識別は ebay_item_id (sku-rules.md)。
        # UNIQUE(ebay_item_id, candidate_url) が lookup index を兼ねる。
        # 冪等: CREATE TABLE IF NOT EXISTS + 実在確認後 bump (v60 流儀)。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 63:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS supplier_candidate_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ebay_item_id TEXT NOT NULL,
                    candidate_url TEXT NOT NULL,
                    source_platform TEXT,
                    candidate_title TEXT,
                    candidate_price_jpy INTEGER,
                    match_score INTEGER,
                    match_reasoning TEXT,
                    junk_likely_untested INTEGER DEFAULT 0,
                    alt_listing_possible INTEGER DEFAULT 0,
                    alt_listing_note TEXT,
                    eval_model TEXT,
                    evaluated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ebay_item_id, candidate_url)
                )
            """)
            _v64_tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "supplier_candidate_evaluations" in _v64_tables:
                conn.execute("PRAGMA user_version = 64")
                logger.info("[init_db v64] schema_ver bumped to 64")
            else:
                logger.warning(
                    "[init_db v64] 部分適用: supplier_candidate_evaluations "
                    "未作成。次回 init_db で再試行。"
                )

        # ---- v65 (W222, 2026-06-05): ebay_listings.category_id (カテゴリ別 FVF floor) ----
        # floor(lp_breakeven_usd) は従来 compute_breakeven_price_usd の固定 category_id=
        # 58248 (Store FVF 12.7%) で計算。実カテゴリ (例 Headphones/Home Audio 9.35%) は
        # FVF が安く floor が下がる = 自動値下げ下限が下がる (money-direct)。本列で
        # per-listing 実カテゴリを保持し update_listing_breakeven が引く。NULL は 58248
        # fallback (後方互換)。v59 流儀: ALTER 試行 → table_info 列実在確認 → bump。
        # ⚠️ 列追加のみでは floor 不変 (update_listing_breakeven が明示再計算されるまで
        #   lp_breakeven_usd は旧値)。floor 全件再計算は DRY-RUN→user 承認後の別 script。
        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_ver == 64:
            try:
                conn.execute("ALTER TABLE ebay_listings ADD COLUMN category_id INTEGER")
                logger.info("[init_db v65] ebay_listings.category_id added")
            except sqlite3.OperationalError:
                pass  # カラム既存
            _v65_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ebay_listings)"
                ).fetchall()
            }
            if "category_id" in _v65_cols:
                conn.execute("PRAGMA user_version = 65")
                logger.info("[init_db v65] schema_ver bumped to 65")
            else:
                logger.warning(
                    "[init_db v65] 部分適用: ebay_listings.category_id 未追加。"
                    "次回 init_db で再試行。"
                )


# ---- サイト設定 ----

def get_site_configs() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM site_configs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def save_site_config(config: dict):
    """サイト設定を保存（id指定あり→更新、なし→新規）"""
    with get_conn() as conn:
        if config.get("id"):
            conn.execute(
                """UPDATE site_configs SET
                   site_name=?, url_keyword=?, in_stock_text1=?, in_stock_text2=?,
                   sold_out_text=?, no_page_text=?, common_url=?, convert_url=?, is_active=?
                   WHERE id=?""",
                (
                    config["site_name"], config.get("url_keyword", ""),
                    config.get("in_stock_text1", ""), config.get("in_stock_text2", ""),
                    config.get("sold_out_text", ""), config.get("no_page_text", ""),
                    config.get("common_url", ""), config["convert_url"],
                    int(config.get("is_active", 1)), config["id"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO site_configs
                   (site_name, url_keyword, in_stock_text1, in_stock_text2,
                    sold_out_text, no_page_text, common_url, convert_url)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    config["site_name"], config.get("url_keyword", ""),
                    config.get("in_stock_text1", ""), config.get("in_stock_text2", ""),
                    config.get("sold_out_text", ""), config.get("no_page_text", ""),
                    config.get("common_url", ""), config["convert_url"],
                ),
            )


def delete_site_config(config_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM site_configs WHERE id=?", (config_id,))


def find_site_config_by_sku(sku: str) -> Optional[dict]:
    """SKUプレフィックスからサイト設定を検索"""
    configs = get_site_configs()
    for cfg in configs:
        prefix = cfg.get("convert_url", "")
        if prefix and sku.startswith(prefix):
            return cfg
    return None


def build_source_url(sku: str) -> Optional[str]:
    """SKU → 仕入元URLを生成"""
    cfg = find_site_config_by_sku(sku)
    if not cfg:
        return None
    prefix = cfg["convert_url"]
    item_id = sku[len(prefix):]
    common = cfg.get("common_url", "")
    return common + item_id if common else None


def find_site_config_by_url(url: str) -> Optional[dict]:
    """URL から site_config を検索 (SKU prefix 非依存、W183).

    url_keyword の部分一致で判定。Amazon/楽天等を直接 URL で監視する際、
    SKU prefix に頼らず site の在庫判定文字列 (in_stock/sold_out/no_page) を引く。
    """
    if not url:
        return None
    for cfg in get_site_configs():
        kw = cfg.get("url_keyword", "")
        if kw and kw in url:
            return cfg
    return None


def set_listing_source_url_manual(
    ebay_item_id: str, source_url: str, manual: bool = True
) -> bool:
    """listing の source_url を手動設定し SKU 同期上書きから保護する (W183).

    manual=True : source_url を直接設定 + source_url_manual=1 で固定。以後
                  upsert_item / upsert_ebay_listing / _sync_monitored_items_sku は
                  この URL を SKU 派生で上書きしない。
    manual=False: 固定解除 (source_url_manual=0)。SKU 派生に戻る。

    listing 識別は ebay_item_id (sku-rules.md 準拠)。ebay_listings を更新し、
    同 ebay_item_id の monitored_items があれば同期。site_config_id は URL から解決。
    Returns: ebay_listings を更新できたら True / listing 不在で False.
    """
    if not ebay_item_id:
        return False
    src = (source_url or "").strip()
    cfg = find_site_config_by_url(src) if src else None
    site_config_id = cfg["id"] if cfg else None
    manual_flag = 1 if manual else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        # 既存 listing 取得 (不在なら False / URL 変更検知 / 監視台帳新規作成用)
        row = conn.execute(
            "SELECT source_url, sku, title FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if row is None:
            return False
        url_changed = (row[0] or "") != (src or "")
        if url_changed:
            # URL が変わったら旧在庫判定は無効 → 次回 inventory_check が再評価
            # (upsert_ebay_listing 非 manual 経路と同 semantics)。
            conn.execute(
                "UPDATE ebay_listings SET source_url=?, source_url_manual=?, "
                "source_url_updated_at=?, source_status='unknown', "
                "source_last_checked=NULL WHERE ebay_item_id=?",
                (src or None, manual_flag, now, ebay_item_id),
            )
        else:
            conn.execute(
                "UPDATE ebay_listings SET source_url=?, source_url_manual=?, "
                "source_url_updated_at=? WHERE ebay_item_id=?",
                (src or None, manual_flag, now, ebay_item_id),
            )
        upd = conn.execute(
            "UPDATE monitored_items SET source_url=?, source_url_manual=?, "
            "source_url_updated_at=?, site_config_id=? WHERE ebay_item_id=?",
            (src or None, manual_flag, now, site_config_id, ebay_item_id),
        )
        # W183 HIGH-1 (code-reviewer 2026-05-28): manual=True で監視台帳に未登録なら
        # 新規 INSERT。これが無いと ensure_monitor_coverage が後で SKU 派生 URL で
        # monitored_items 行を作り、手動 URL が誤 URL に汚染される (W139 同型の
        # 仕入先 OOS 見逃し → 履行不能)。listing 識別は ebay_item_id (sku-rules)。
        if manual and (upd.rowcount or 0) == 0:
            conn.execute(
                "INSERT INTO monitored_items (ebay_item_id, title, sku, source_url, "
                "site_config_id, source_url_manual, source_url_updated_at, is_active) "
                "VALUES (?,?,?,?,?,?,?,1)",
                (ebay_item_id, row[2] or "", row[1] or "", src or None,
                 site_config_id, manual_flag, now),
            )
    return True


# ---- 監視アイテム ----

def upsert_item(sku: str, ebay_item_id: str = "", title: str = "") -> int:
    """ebay_item_id 優先 / source_url fallback で識別、既存行は UPDATE / 新規は INSERT.

    SKU rule (.claude/rules/sku-rules.md) 準拠: SKU 経由 lookup は禁止.
    source_url は sku から派生計算 = 許容用途.
    同 source_url 多 listing は 1 行に集約 (在庫切れ監視は URL 単位で十分).

    W139-fix HIGH-3 (Codex 2026-05-19): source_url は
    `_build_source_url_from_sku(sku) or build_source_url(sku)` で生成する。
    build_source_url (site_configs) は mercari で .../item/123 を返すが、
    本番 ebay_listings.source_url と実際の scrape 対象は
    _build_source_url_from_sku 形 (.../item/m123)。build_source_url 単独だと
    coverage 登録時に誤 URL の monitored 行を作り inventory_check が別 URL を
    scrape → 仕入先 OOS 見逃し → 履行不能。yahoo 等は両者一致 / 未知 prefix は
    後者が None で従来通り build_source_url に fallback (挙動不変)。
    """
    cfg = find_site_config_by_sku(sku)
    source_url = _build_source_url_from_sku(sku) or build_source_url(sku)
    site_config_id = cfg["id"] if cfg else None

    with get_conn() as conn:
        # ebay_item_id 優先 → source_url fallback で identify (W72: SKU rule 準拠).
        # source_url は _build_source_url_from_sku→build_source_url で sku から
        # 派生計算 (許容用途、W139-fix HIGH-3 = ebay_listings と同形)。
        existing = None
        if ebay_item_id:
            existing = conn.execute(
                "SELECT id FROM monitored_items WHERE ebay_item_id=?", (ebay_item_id,)
            ).fetchone()
        if not existing and source_url:
            existing = conn.execute(
                "SELECT id FROM monitored_items WHERE source_url=?", (source_url,)
            ).fetchone()

        if existing:
            # W183 (2026-05-28): source_url_manual=1 の行は手動設定 URL を維持し、
            # SKU 派生 source_url で上書きしない (EC 直接 URL 無在庫監視の保護).
            _manual_row = conn.execute(
                "SELECT COALESCE(source_url_manual, 0) FROM monitored_items WHERE id=?",
                (existing["id"],),
            ).fetchone()
            if _manual_row and int(_manual_row[0]) == 1:
                conn.execute(
                    """UPDATE monitored_items SET title=?, sku=?, is_active=1,
                       ebay_item_id=COALESCE(NULLIF(?, ''), ebay_item_id)
                       WHERE id=?""",
                    (title, sku, ebay_item_id, existing["id"]),
                )
            else:
                conn.execute(
                    """UPDATE monitored_items SET title=?, sku=?, source_url=?,
                       site_config_id=?, is_active=1, ebay_item_id=COALESCE(NULLIF(?, ''), ebay_item_id)
                       WHERE id=?""",
                    (title, sku, source_url, site_config_id, ebay_item_id, existing["id"]),
                )
            return existing["id"]

        conn.execute(
            """INSERT INTO monitored_items (ebay_item_id, title, sku, source_url, site_config_id)
               VALUES (?,?,?,?,?)""",
            (ebay_item_id, title, sku, source_url, site_config_id),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def add_item_manual(sku: str, title: str = "") -> int:
    return upsert_item(sku=sku, title=title)


def get_active_items() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM monitored_items WHERE is_active=1 ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_items() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM monitored_items ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_item_status(item_id: int, status: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "UPDATE monitored_items SET last_status=?, last_check=? WHERE id=?",
            (status, now, item_id),
        )


def add_check_log(item_id: int, status: str, discord_sent: bool = False):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO check_log (item_id, status, discord_sent) VALUES (?,?,?)",
            (item_id, status, int(discord_sent)),
        )


def get_prev_status(item_id: int) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM check_log WHERE item_id=? ORDER BY checked_at DESC LIMIT 1",
            (item_id,),
        ).fetchone()
    return row["status"] if row else None


def get_recent_logs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.*, m.title, m.sku, m.source_url, m.ebay_item_id
               FROM check_log l
               JOIN monitored_items m ON l.item_id = m.id
               ORDER BY l.checked_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_item(item_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM check_log WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM monitored_items WHERE id=?", (item_id,))


def prune_old_logs(days: int = 30):
    """古いチェックログを削除（デフォルト30日以上前）"""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM check_log WHERE checked_at < datetime('now', ?)",
            (f"-{days} days",),
        )


# ---- eBay出品管理 ----

# W191 (2026-05-30): 4 区分 primary_market の canonical 表記.
# 個別出品 UI は lowercase 'us_only' を使うが、Terapeak 経路 (_judge_primary_market)
# と codex_lint_runner の語彙は 'US_only' (US 大文字). 保存値を 1 つに揃えるため
# UI の 'us_only' を 'US_only' に寄せる (他 3 区分は両経路とも lowercase で一致).
_PRIMARY_MARKET_CANONICAL: dict[str, str] = {
    "us_only": "US_only",
}


def _normalize_primary_market(value: Optional[str]) -> Optional[str]:
    """primary_market を canonical 表記に正規化する。None / 空文字は None (列に触れない)。

    'us_only' (UI lowercase) → 'US_only' (Terapeak / lint 語彙). 既に 'US_only' の場合や
    mixed_global / global_only / unknown はそのまま返す (未知値も素通し = 上位の責務)。
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    return _PRIMARY_MARKET_CANONICAL.get(v.lower(), v)


def upsert_ebay_listing(ebay_item_id: str, sku: str, title: str = "",
                        current_price: float = 0.0, quantity_ebay: int = 0,
                        shipping_cost: float = 0.0,
                        primary_market: Optional[str] = None,
                        category_id: Optional[int] = None) -> int:
    """eBay出品を挿入または更新。

    重要: eBay 側で SKU が変更された場合 (既存 vs 今回のSKUが異なる):
      - `sku` を新しい値に追従
      - `source_url` を新SKUから再構築
      - `source_status` を 'unknown' にリセット (inventory_check が再評価する)
      - `risk_confirmed` を 0 にリセット (古い確認フラグは新SKUに無効)
    これがないと「古いSKU時代のOOS状態」が居座り続ける。

    W191 (2026-05-30): primary_market を指定すると ebay_listings.primary_market に
    反映する (個別出品 UI で user が選んだ出品区分の永続化). None / 空文字 のときは
    primary_market 列に一切触れない (Terapeak 解析 / 承認 UI が確定した既存値を
    upsert で踏み潰さないため). 値は _normalize_primary_market で canonical 化
    ('us_only' → 'US_only' 等) してから保存し、Terapeak 経路 (_judge_primary_market)
    や lowest_price 表示層と表記を揃える.
    """
    pm_norm = _normalize_primary_market(primary_market)
    # W222 (2026-06-05): category_id は COALESCE(?, category_id) で保存 (None/0 は
    # 既存維持 = primary_market と同 semantics)。同期で取得失敗 (None) 時に既存実カテゴリ
    # を潰さない。category_id は floor の SELECT のみで使い、変更時の lp_breakeven_usd
    # 無効化はしない (同期毎 NULL 化で auto-pricedown が常時 skip するのを避ける)。
    _cat_id = int(category_id) if category_id else None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, sku, primary_market FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,)
        ).fetchone()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if existing:
            existing_sku = existing["sku"] or ""
            sku_changed = (sku or "") != existing_sku
            # W212 (2026-06-03, Codex HIGH fix v2 = fail-closed): primary_market が実際に
            # 変わるなら breakeven(floor)の前提が変わる (global_only=DDU / 他=US DDP)。
            # 同一 transaction で lp_breakeven_usd=NULL に無効化し stale floor の時間窓を閉じる
            # (auto-pricedown は floor=NULL なら skip)。再計算は呼出元 (個別出品 UI) が実施。
            # 個別出品経路のみ pm_norm 指定 (ebay_sync は None=COALESCE で不変なので非該当)。
            _pm_changed = pm_norm is not None and pm_norm != (existing["primary_market"] or None)
            if _pm_changed:
                conn.execute(
                    "UPDATE ebay_listings SET lp_breakeven_usd=NULL WHERE ebay_item_id=?",
                    (ebay_item_id,),
                )
            # W183 (2026-05-28): source_url_manual=1 の listing は手動設定 URL を保護.
            # SKU が変わっても source_url / source_status / source_last_checked を
            # SKU 派生で上書きしない (EC 直接 URL 無在庫監視の継続性確保).
            _manual_row = conn.execute(
                "SELECT COALESCE(source_url_manual, 0) FROM ebay_listings "
                "WHERE ebay_item_id=?",
                (ebay_item_id,),
            ).fetchone()
            is_manual = bool(_manual_row and int(_manual_row[0]) == 1)

            if sku_changed and not is_manual:
                # 2026-05-20 Codex 指摘 HIGH 対応: 旧 `if sku_changed and sku:`
                # は eBay 側で SKU が空文字に変わった場合 (W139 後の filter 解除で
                # SKU 空 listing も DB に流入するように変更) を skip して旧 SKU
                # が DB に残留 → 仕入先マッチング誤動作。`and sku:` を外し、
                # 空文字化も同 reset semantics で吸収する。
                # SKU が変わった場合 (空文字化を含む): source_* と risk_confirmed
                # をリセット。new_source_url は sku 空なら None (COALESCE で
                # 既存維持、downstream は SKU prefix 判定で全 skip となるため
                # 実害なし)。SKU 復帰時に再計算される。
                new_source_url = _build_source_url_from_sku(sku) if sku else None
                conn.execute(
                    """UPDATE ebay_listings SET
                          sku=?, title=?, current_price=?, quantity_ebay=?,
                          shipping_cost=?, last_synced_at=?,
                          source_url=COALESCE(?, source_url),
                          source_status='unknown',
                          source_last_checked=NULL,
                          risk_confirmed=0,
                          primary_market=COALESCE(?, primary_market),
                          category_id=COALESCE(?, category_id)
                       WHERE ebay_item_id=?""",
                    (sku, title, current_price, quantity_ebay, shipping_cost, now,
                     new_source_url, pm_norm, _cat_id, ebay_item_id),
                )
                # W139-fix (2026-05-18): eBay 側 SKU 変更検知時も monitored_items
                # を追従 (同一 conn 原子的)。汚染源 2 経路目 (user 承認済)。
                # 2026-05-20: sku='' でも追従 (旧 sku の monitored_items 行が
                # 残ると find_coverage_gaps が誤判定するため)。
                _sync_monitored_items_sku(conn, ebay_item_id, sku)
            elif sku_changed and is_manual:
                # W183: 手動 URL listing は sku のみ追従、source_url / source_status /
                # source_last_checked / risk_confirmed は維持 (手動 URL は不変なので
                # 在庫状態を reset する必要なし)。
                conn.execute(
                    """UPDATE ebay_listings SET
                          sku=?, title=?, current_price=?, quantity_ebay=?,
                          shipping_cost=?, last_synced_at=?,
                          primary_market=COALESCE(?, primary_market),
                          category_id=COALESCE(?, category_id)
                       WHERE ebay_item_id=?""",
                    (sku, title, current_price, quantity_ebay, shipping_cost, now,
                     pm_norm, _cat_id, ebay_item_id),
                )
                _sync_monitored_items_sku(conn, ebay_item_id, sku)
            else:
                conn.execute(
                    """UPDATE ebay_listings SET title=?, current_price=?, quantity_ebay=?,
                       shipping_cost=?, last_synced_at=?,
                       primary_market=COALESCE(?, primary_market),
                       category_id=COALESCE(?, category_id)
                       WHERE ebay_item_id=?""",
                    (title, current_price, quantity_ebay, shipping_cost, now,
                     pm_norm, _cat_id, ebay_item_id),
                )
            return existing["id"]

        conn.execute(
            """INSERT INTO ebay_listings (ebay_item_id, sku, title, current_price, quantity_ebay, shipping_cost, last_synced_at, primary_market, category_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ebay_item_id, sku, title, current_price, quantity_ebay, shipping_cost, now, pm_norm, _cat_id),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_ebay_listings() -> list[dict]:
    """すべてのeBay出品を取得"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ebay_listings ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_ebay_listing_by_item_id(ebay_item_id: str) -> Optional[dict]:
    """ebay_item_id で 1 listing を特定する canonical 関数 (migration v26 listing 単位化後の正規 API).

    .claude/rules/sku-rules.md 準拠: listing 識別キーは ebay_item_id を使う.
    `ebay_listings.ebay_item_id` は UNIQUE 制約 (`monitor/database.py:407`) のため一意取得.
    """
    if not ebay_item_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebay_listings WHERE ebay_item_id=?", (ebay_item_id,)
        ).fetchone()
    return dict(row) if row else None


# 2026-05-01 W75 完走 (4a + 4b + 4c) で deprecated `get_ebay_listing_by_sku()` を削除.
# listing 識別は `get_ebay_listing_by_item_id(ebay_item_id)` を使用 (.claude/rules/sku-rules.md 準拠).


def set_initial_registered(ebay_item_id: str, registered: bool) -> bool:
    """W151 (2026-05-22): 初期登録 status の on/off.

    on (registered=True):  initial_registered=1, initial_registered_at=CURRENT_TIMESTAMP
    off (registered=False): initial_registered=0, initial_registered_at=NULL
    K1 simplicity: シンプル 2 状態 (履歴は別 W で audit log 化検討).
    Returns: True if UPDATE 成立 (rowcount==1).
    """
    with get_conn() as conn:
        if registered:
            cur = conn.execute(
                "UPDATE ebay_listings SET initial_registered = 1, "
                "initial_registered_at = CURRENT_TIMESTAMP "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            )
        else:
            cur = conn.execute(
                "UPDATE ebay_listings SET initial_registered = 0, "
                "initial_registered_at = NULL "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            )
    return cur.rowcount == 1


# ============================================================
# W153 (2026-05-22): 商品別ライバル検出 helpers
# ============================================================

def set_rival_watch_enabled(ebay_item_id: str, enabled: bool) -> bool:
    """W153: ライバル監視 ON/OFF.

    ON 時は rival_watch_started_at = COALESCE(既存, NOW()) も set (H-A、再 ON で巻き戻さない).
    OFF 時は rival_watch_started_at を維持 (NULL に戻さない).

    v2.1 設計判断 (HIGH-1 admit): OFF→keyword 変更→再 ON で「履歴連続性」を優先.
    「監視リセット」UI button は別 W (本 W K1 scope 外).

    Returns: True if UPDATE 成立 (rowcount==1).
    """
    with get_conn() as conn:
        if enabled:
            cur = conn.execute(
                "UPDATE ebay_listings "
                "SET rival_watch_enabled = 1, "
                "    rival_watch_started_at = COALESCE(rival_watch_started_at, CURRENT_TIMESTAMP) "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            )
        else:
            cur = conn.execute(
                "UPDATE ebay_listings SET rival_watch_enabled = 0 "
                "WHERE ebay_item_id = ?",
                (ebay_item_id,),
            )
    return cur.rowcount == 1


def set_rival_search_keywords(
    ebay_item_id: str,
    keywords_text: str,
    *,
    mark_generated: bool = False,
) -> bool:
    """W153: 検索ワード text_input 内容を保存.

    keywords_text: 空白区切り 1 query (Browse API AND 検索用).
    v2 (2026-05-22 PM): 改行混じり入力は runtime で空白に collapse + trim.
    過去 data の \\n も次回保存で正規化 (自己修復).
    mark_generated: True なら rival_search_keywords_generated_at = CURRENT_TIMESTAMP
                    (🤖 生成ボタン経路). False なら timestamp 維持 (💾 保存ボタン経路).

    Returns:
        True: 保存成功.
        False: listing 不在 or 1-word query (DB layer guard、HIGH-2 fix).
               空文字列は OK (user が keyword を消した = 検索停止).
    """
    import re as _re
    normalized = _re.sub(r"\s+", " ", keywords_text or "").strip()
    # HIGH-2 fix (2026-05-22 PM internal review): 1-word query は AND 検索成立せず
    # noise 過多 (Black 単独 50 件 hit 事故再発防止). 空文字列は許可 (削除目的).
    if normalized and len(normalized.split(" ")) < 2:
        _logger = logging.getLogger(__name__)
        _logger.warning(
            f"[W153 set_rival_search_keywords] {ebay_item_id}: "
            f"refuse 1-word query (would cause AND-search noise): {normalized!r}"
        )
        return False
    with get_conn() as conn:
        if mark_generated:
            cur = conn.execute(
                "UPDATE ebay_listings SET rival_search_keywords = ?, "
                "rival_search_keywords_generated_at = CURRENT_TIMESTAMP "
                "WHERE ebay_item_id = ?",
                (normalized, ebay_item_id),
            )
        else:
            cur = conn.execute(
                "UPDATE ebay_listings SET rival_search_keywords = ? "
                "WHERE ebay_item_id = ?",
                (normalized, ebay_item_id),
            )
    return cur.rowcount == 1


def record_rival_discovery(
    *,
    ebay_item_id: str,
    competitor_seller: str,
    competitor_item_id: str,
    competitor_title: str = "",
    competitor_price_usd: Optional[float] = None,
    search_keyword: str = "",
    competitor_shipping_cost_usd: Optional[float] = None,
    min_delivery_date: Optional[str] = None,
    max_delivery_date: Optional[str] = None,
    shipping_service_code: Optional[str] = None,
) -> Optional[int]:
    """W153: claim-then-act. INSERT OR IGNORE → rowcount==1 で新規 id、0 で既存更新.

    v51 (2026-05-22 PM): shipping_cost_usd / min/max_delivery_date を保存.
    v52 (2026-05-22 PM): shipping_service_code を保存 (詳細 API 経由 enrich).
    UI で送料 + 配達日数 + 発送方法名表示 + Economy hide に使用.

    Returns: 新規 INSERT した場合 lastrowid、既存重複なら None (last_seen_at + price 更新).
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO listing_rival_discoveries
               (ebay_item_id, competitor_seller, competitor_item_id,
                competitor_title, competitor_price_usd, search_keyword,
                competitor_shipping_cost_usd, min_delivery_date, max_delivery_date,
                shipping_service_code,
                first_seen_at, last_seen_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'new')""",
            (ebay_item_id, competitor_seller, competitor_item_id,
             competitor_title, competitor_price_usd, search_keyword,
             competitor_shipping_cost_usd, min_delivery_date, max_delivery_date,
             shipping_service_code),
        )
        if cur.rowcount == 1:
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # 既存: last_seen_at + 価格 / shipping 情報を更新 (COALESCE で NULL は維持)
        conn.execute(
            """UPDATE listing_rival_discoveries
               SET last_seen_at = CURRENT_TIMESTAMP,
                   competitor_price_usd = COALESCE(?, competitor_price_usd),
                   competitor_shipping_cost_usd = COALESCE(?, competitor_shipping_cost_usd),
                   min_delivery_date = COALESCE(?, min_delivery_date),
                   max_delivery_date = COALESCE(?, max_delivery_date),
                   shipping_service_code = COALESCE(?, shipping_service_code)
               WHERE ebay_item_id = ? AND competitor_seller = ?
                 AND competitor_item_id = ?""",
            (competitor_price_usd, competitor_shipping_cost_usd,
             min_delivery_date, max_delivery_date, shipping_service_code,
             ebay_item_id, competitor_seller, competitor_item_id),
        )
        return None


def enrich_rival_discovery_shipping(
    discovery_id: int,
    *,
    shipping_service_code: Optional[str] = None,
    shipping_cost_usd: Optional[float] = None,
    min_delivery_date: Optional[str] = None,
    max_delivery_date: Optional[str] = None,
) -> bool:
    """v52: 既存 discovery に詳細 API 経由で取得した shipping 情報を後から enrich.

    record_rival_discovery で INSERT 直後に呼ぶ用途 (新規 rival のみ enrich).
    NULL の field は更新しない (COALESCE).

    Returns: True if rowcount==1.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE listing_rival_discoveries
               SET shipping_service_code = COALESCE(?, shipping_service_code),
                   competitor_shipping_cost_usd = COALESCE(?, competitor_shipping_cost_usd),
                   min_delivery_date = COALESCE(?, min_delivery_date),
                   max_delivery_date = COALESCE(?, max_delivery_date)
               WHERE id = ?""",
            (shipping_service_code, shipping_cost_usd,
             min_delivery_date, max_delivery_date, discovery_id),
        )
        return cur.rowcount == 1


def get_rival_discoveries(
    ebay_item_id: str,
    status: str = 'new',
    *,
    since: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """W153: discoveries を取得.

    since: ISO timestamp. 通常は呼び側 (UI) で
           `rival_watch_started_at or initial_registered_at` を計算して渡す (H-A).
    """
    sql = (
        "SELECT * FROM listing_rival_discoveries "
        "WHERE ebay_item_id = ? AND status = ?"
    )
    args: list = [ebay_item_id, status]
    if since:
        sql += " AND first_seen_at >= ?"
        args.append(since)
    sql += " ORDER BY first_seen_at DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def update_rival_discovery_status(discovery_id: int, new_status: str) -> bool:
    """W153: 監視追加 / 却下 button から呼ばれる.

    allowed: 'new' / 'monitoring_added' / 'dismissed'. invalid で ValueError.
    """
    if new_status not in ('new', 'monitoring_added', 'dismissed'):
        raise ValueError(f"invalid status: {new_status}")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listing_rival_discoveries "
            "SET status = ?, status_changed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (new_status, discovery_id),
        )
    return cur.rowcount == 1


def add_or_reactivate_competitor(
    *,
    our_item_id: str,
    our_sku: str,    # 補助情報 (sku-rules: 識別キー化はしない)
    competitor_seller: str,
    competitor_item_id: str,
) -> tuple[int, str]:
    """W153 → W183 流入の単一エントリポイント.

    過去 is_active=0 にした listing から再追加で IntegrityError で永久に W183 流入
    しない silent gap を根治 (H-C).

    Returns:
        (id, action) where action in {'added', 'reactivated', 'conflict'}
        - 'added':       新規 INSERT (id = lastrowid)
        - 'reactivated': 同 our_item_id で is_active=0 → 1 復活 (id = 既存 row id)
        - 'conflict':    別 our_item_id で既登録 (N:1 不可、id = 既存 row id)

    v2.1 MED-6 fix: reactivation で our_sku stale を防ぐため
    `COALESCE(NULLIF(?, ''), our_sku)` で更新 + updated_at 更新.
    """
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO competitor_products
                   (our_item_id, our_sku, competitor_seller, competitor_item_id,
                    seller_location, is_active)
                   VALUES (?, ?, ?, ?, 'Japan', 1)""",
                (our_item_id, our_sku, competitor_seller, competitor_item_id),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return (new_id, 'added')
        except sqlite3.IntegrityError:
            # UNIQUE(competitor_item_id) 違反 → 既存 row 判定
            row = conn.execute(
                "SELECT id, our_item_id, is_active FROM competitor_products "
                "WHERE competitor_item_id = ?",
                (competitor_item_id,),
            ).fetchone()
            if row is None:
                # 想定外 (極稀 race)
                raise
            existing_id = row['id']
            existing_our_iid = row['our_item_id']
            if existing_our_iid == our_item_id:
                # 同 listing → reactivate (v2.1 MED-6: our_sku 更新も)
                conn.execute(
                    "UPDATE competitor_products "
                    "SET is_active = 1, "
                    "    our_sku = COALESCE(NULLIF(?, ''), our_sku), "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (our_sku, existing_id),
                )
                return (existing_id, 'reactivated')
            # 別 listing → conflict (本 W では N:1 不可)
            return (existing_id, 'conflict')


def update_ebay_listing_status(ebay_item_id: str, source_status: str):
    """eBay出品の仕入元在庫状態を更新.

    2026-05-07: source_status が '在庫有' に遷移したら risk_confirmed を 0 に戻す.
      → 仕入先が在庫復活したら user 確認はリセット (次回 OOS 検知で再表示).
      これがないと「risk_confirmed=1 + 在庫有」で sleeping risk として残り、
      仕入先が再 OOS になっても user は永遠に気付けない (Q0 silent skip).
    """
    with get_conn() as conn:
        if source_status == '在庫有':
            conn.execute(
                "UPDATE ebay_listings SET source_status=?, risk_confirmed=0 "
                "WHERE ebay_item_id=?",
                (source_status, ebay_item_id),
            )
        else:
            conn.execute(
                "UPDATE ebay_listings SET source_status=? WHERE ebay_item_id=?",
                (source_status, ebay_item_id),
            )


def update_ebay_listing_quantity(ebay_item_id: str, quantity: int):
    """eBay出品の数量を更新"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET quantity_ebay=?, last_synced_at=? WHERE ebay_item_id=?",
            (quantity, now, ebay_item_id),
        )


def _build_source_url_from_sku(sku: str) -> Optional[str]:
    """SKU prefix + item_id から仕入先URLを組み立てる。未知prefixは None。"""
    try:
        from sku_mapping_manager import load_mappings
    except ImportError:
        return None
    mappings = load_mappings()
    for prefix, m in mappings.items():
        if sku.startswith(prefix):
            base = m.get("common_url") or ""
            pattern = m.get("pattern", "{item_id}")
            item_id = sku[len(prefix):]
            # Mercari だけ pattern が "m{item_id}" なので item_id を使って構築
            path = pattern.replace("{item_id}", item_id)
            return base + path
    return None


# （旧 update_ebay_listing_sku は下の統合版に置き換え済み）


def set_ebay_listing_risk_confirmed(ebay_item_id: str, confirmed: int = 1):
    """仕入先在庫リスクの確認済みフラグを設定"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET risk_confirmed=? WHERE ebay_item_id=?",
            (confirmed, ebay_item_id),
        )


def get_ebay_listings_supply_risk() -> dict[str, list[dict]]:
    """仕入先在庫リスク商品を取得（在庫切れ / 確認不可 に区分）。
    業務ロジック: 在庫監視 = 無在庫出品 で 仕入先OOS になった RISK の検知。
    qty>=1 (販売中) + 仕入先OOS のみ RISK 対象。

    フィルタ条件 (2026-04-30 改訂、user 公認 Q1-A + Q3):
    - quantity_ebay >= 1: 在庫 0 化されたら一覧から即消す (不具合 1 修正)
    - is_ended = 0: daily_relist で退役した旧 ItemID は除外 (不具合 3 修正)
    - source_status NOT IN ('在庫有', 'unknown')
    - sku GLOB 'ebay*' (無在庫のみ、case-sensitive): 2026-05-05 修正. stock:01 等の
      有在庫 SKU は内部 stock pool 管理で source_status は無関係なため除外
      (Google Pixel Tablet が毎回出てた事象の root cause). SKU rule
      (.claude/rules/sku-rules.md) は prefix 完全一致 case-sensitive を要求するため、
      GLOB を使用 (LIKE は default で case-insensitive、仕様乖離).
    - risk_confirmed = 0 (= user 未確認のみ): 2026-05-05 修正. user が確認チェック入れた
      listing は対応済とみなして表示しない (Baccarat case 対策). 確認済リスト UI で別途
      確認可能にする.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ebay_item_id, sku, title, quantity_ebay, source_status,
                   source, current_price, rank, source_url, source_last_checked,
                   COALESCE(risk_confirmed, 0) as risk_confirmed
            FROM ebay_listings
            WHERE quantity_ebay >= 1
              AND COALESCE(is_ended, 0) = 0
              AND source_status IS NOT NULL
              AND source_status NOT IN ('在庫有', 'unknown')
              AND sku GLOB 'ebay*'
              AND COALESCE(risk_confirmed, 0) = 0
            ORDER BY CASE rank
                WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2
                WHEN 'C' THEN 3 WHEN 'D' THEN 4 WHEN 'E' THEN 5
                ELSE 6 END ASC, current_price DESC
        """).fetchall()
    out_of_stock = []
    page_not_found = []
    for r in rows:
        item = dict(r)
        if item["source_status"] == "在庫無":
            out_of_stock.append(item)
        elif item["source_status"] == "ページなし":
            page_not_found.append(item)
    return {"out_of_stock": out_of_stock, "page_not_found": page_not_found}


def delete_ebay_listing(ebay_item_id: str):
    """eBay出品を削除"""
    with get_conn() as conn:
        conn.execute("DELETE FROM ebay_listings WHERE ebay_item_id=?", (ebay_item_id,))


# ---- 競合商品管理 ----

def add_competitor_product(our_item_id: str, competitor_item_id: str,
                           competitor_seller: str, seller_location: str,
                           price_rule: str = "competitor - 0.01",
                           min_price: float = 0.0, max_discount: float = 10.0,
                           our_sku: str = "") -> int:
    """競合商品を登録"""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO competitor_products
               (our_item_id, our_sku, competitor_item_id, competitor_seller,
                seller_location, price_rule, min_price, max_discount)
               VALUES (?,?,?,?,?,?,?,?)""",
            (our_item_id, our_sku, competitor_item_id, competitor_seller,
             seller_location, price_rule, min_price, max_discount),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_competitors_for_item(our_item_id: str) -> list[dict]:
    """特定のeBay出品の競合商品一覧を取得"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM competitor_products WHERE our_item_id=? AND is_active=1",
            (our_item_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_competitors() -> list[dict]:
    """すべての競合商品を取得"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM competitor_products WHERE is_active=1 ORDER BY our_item_id"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_competitor_product(competitor_item_id: str):
    """競合商品を削除"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE competitor_products SET is_active=0 WHERE competitor_item_id=?",
            (competitor_item_id,)
        )


# ---- 新規ライバルアラート ----

def add_new_competitor_alert(our_item_id: str, keyword: str,
                             found_item_id: str, found_seller: str,
                             found_location: str, found_price: float,
                             is_japan_seller: int) -> int:
    """新規ライバル検知をログに記録"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO new_competitor_alerts
               (our_item_id, keyword, found_item_id, found_seller, found_location,
                found_price, is_japan_seller, found_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (our_item_id, keyword, found_item_id, found_seller, found_location,
             found_price, is_japan_seller, now),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_japan_competitor_alerts(action: str = "pending") -> list[dict]:
    """Japan セラーの新規ライバルアラートを取得"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM new_competitor_alerts WHERE is_japan_seller=1 AND action=? ORDER BY found_at DESC",
            (action,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_competitor_alerts(limit: int = 50) -> list[dict]:
    """すべての新規ライバルアラートを取得（Japan 以外も含む）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM new_competitor_alerts ORDER BY found_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_alert_action(alert_id: int, action: str):
    """アラートのアクションを更新（pending -> registered/ignored など）"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE new_competitor_alerts SET action=?, notified=1 WHERE id=?",
            (action, alert_id)
        )


def mark_alert_as_notified(alert_id: int):
    """アラートを通知済みにマーク"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE new_competitor_alerts SET notified=1 WHERE id=?",
            (alert_id,)
        )


# ---- 商品ランク管理 ----

def update_ebay_listing_rank(ebay_item_id: str, rank: str):
    """eBay出品のランクを更新 (S, A, B, C, D, E)"""
    if rank not in ('S', 'A', 'B', 'C', 'D', 'E'):
        raise ValueError(f"Invalid rank: {rank}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET rank=? WHERE ebay_item_id=?",
            (rank, ebay_item_id)
        )


VALID_PRIMARY_MARKETS: tuple[str, ...] = (
    "US_only", "mixed_global", "global_only", "unknown",
)


def update_ebay_listing_primary_market(ebay_item_id: str, primary_market: str):
    """eBay 出品の 4 区分 (primary_market) を更新する。

    区分は送料・関税計算の前提 (reference_shipping_tariff_logic.md)。
    listing 識別は ebay_item_id (SKU 不使用 / sku-rules)。
    eBay 側への送料反映は別経路 (本関数は DB 保存のみ = user 判断で
    📤eBay反映 ボタンを使う)。
    """
    if primary_market not in VALID_PRIMARY_MARKETS:
        raise ValueError(
            f"Invalid primary_market: {primary_market!r} "
            f"(valid: {VALID_PRIMARY_MARKETS})"
        )
    # W212 (2026-06-03, Codex HIGH fix v2 = fail-closed): primary_market は breakeven
    # (floor) の前提 (global_only=DDU / 他=US DDP)。区分変更で旧 floor が残ると US_only 化後に
    # 自動値下げが赤字価格まで下げうる (stale floor = money-direct)。
    # 同一 transaction で lp_breakeven_usd=NULL に無効化 → 時間窓・再計算失敗の両方を閉じる
    # (auto-pricedown は floor=NULL なら skip_no_floor で安全側に倒れる)。commit 後に再計算。
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET primary_market=?, lp_breakeven_usd=NULL "
            "WHERE ebay_item_id=?",
            (primary_market, ebay_item_id),
        )
    try:
        from monitor.lowest_price import update_listing_breakeven
        from calculator import load_settings
        update_listing_breakeven(ebay_item_id, load_settings())
    except Exception as e:  # noqa: BLE001 — 再計算失敗でも floor=NULL=fail-closed
        import logging
        logging.getLogger(__name__).warning(
            f"primary_market 変更後の breakeven 再計算失敗 ({ebay_item_id}): {e}. "
            f"floor=NULL のまま (自動値下げ skip で安全)。次回 利益計算ボタンで復旧。"
        )


def get_ebay_listings_by_rank(rank: str = None, order_by_rank: bool = True) -> list[dict]:
    """ランク別にeBay出品を取得 (退役済 is_ended=1 は除外、W76 T2: 2026-05-01)."""
    with get_conn() as conn:
        if rank:
            query = ("SELECT * FROM ebay_listings "
                     "WHERE rank=? AND COALESCE(is_ended, 0) = 0")
            rows = conn.execute(query, (rank,)).fetchall()
        else:
            if order_by_rank:
                # ランク順（S → A → B → C → D → E）で取得
                query = """SELECT * FROM ebay_listings
                           WHERE COALESCE(is_ended, 0) = 0
                           ORDER BY CASE rank
                                    WHEN 'S' THEN 0
                                    WHEN 'A' THEN 1
                                    WHEN 'B' THEN 2
                                    WHEN 'C' THEN 3
                                    WHEN 'D' THEN 4
                                    WHEN 'E' THEN 5
                                    ELSE 6
                                   END, created_at DESC"""
                rows = conn.execute(query).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ebay_listings "
                    "WHERE COALESCE(is_ended, 0) = 0 "
                    "ORDER BY created_at DESC"
                ).fetchall()

    return [dict(r) for r in rows]


def get_rank_stats() -> dict:
    """ランク別の出品数を集計 (退役済 is_ended=1 除外、W76 T2: 2026-05-01)."""
    with get_conn() as conn:
        results = conn.execute(
            """SELECT rank, COUNT(*) as count FROM ebay_listings
               WHERE COALESCE(is_ended, 0) = 0
               GROUP BY rank ORDER BY rank"""
        ).fetchall()

    stats = {row['rank']: row['count'] for row in results}
    # 0件のランクも含める
    for rank in ['S', 'A', 'B', 'C', 'D', 'E']:
        if rank not in stats:
            stats[rank] = 0

    return stats


# ---- メトリクス・スコア管理（自動ランク付け） ----

def update_ebay_listing_metrics(ebay_item_id: str, metrics: dict) -> None:
    """
    eBay出品のメトリクスを更新（Watch数、View数、販売数）
    前回値を last_* に移動し、新しい値を設定
    metrics: {watch_count, view_count, sales_count_30d} または部分更新
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        # 現在の値を前回値に移動
        current = conn.execute(
            """SELECT watch_count, view_count, sales_count_30d
               FROM ebay_listings WHERE ebay_item_id=?""",
            (ebay_item_id,)
        ).fetchone()

        last_watch = (current['watch_count'] if current else 0) or 0
        last_view = (current['view_count'] if current else 0) or 0
        last_sales = (current['sales_count_30d'] if current else 0) or 0

        # 新しい値を取得（部分更新対応）
        watch_count = metrics.get('watch_count', last_watch)
        view_count = metrics.get('view_count', last_view)
        sales_count = metrics.get('sales_count_30d', last_sales)

        conn.execute(
            """UPDATE ebay_listings SET
               watch_count=?, view_count=?, sales_count_30d=?,
               last_watch_count=?, last_view_count=?, last_sales_count_30d=?,
               last_metrics_updated_at=?
               WHERE ebay_item_id=?""",
            (watch_count, view_count, sales_count,
             last_watch, last_view, last_sales,
             now, ebay_item_id)
        )


def update_ebay_listing_growth_rates(ebay_item_id: str, watch_rate: float,
                                      view_rate: float, sales_rate: float) -> None:
    """計算済み伸び率をDBに保存"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET
               watch_growth_rate=?, view_growth_rate=?, sales_growth_rate=?
               WHERE ebay_item_id=?""",
            (watch_rate, view_rate, sales_rate, ebay_item_id)
        )


def update_ebay_listing_metrics_score(ebay_item_id: str, score: float, rank: str) -> None:
    """複合スコアとランクを保存"""
    if rank not in ('S', 'A', 'B', 'C', 'D', 'E'):
        raise ValueError(f"Invalid rank: {rank}")

    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET
               metrics_score=?, rank=?
               WHERE ebay_item_id=?""",
            (score, rank, ebay_item_id)
        )


def get_all_listing_metrics() -> list[dict]:
    """すべての出品のメトリクスを取得（ランク計算用）。退役済(is_ended=1)は除外。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, sku, title,
                      watch_count, view_count, sales_count_30d,
                      last_watch_count, last_view_count, last_sales_count_30d,
                      metrics_score, rank
               FROM ebay_listings
               WHERE (is_ended IS NULL OR is_ended=0)
               ORDER BY created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_rank_distribution_details() -> dict:
    """ランク別の詳細統計（平均値など）を取得 (退役済 is_ended=1 除外、W76 T2: 2026-05-01)."""
    with get_conn() as conn:
        results = conn.execute(
            """SELECT rank,
                      COUNT(*) as count,
                      ROUND(AVG(watch_count), 1) as avg_watch,
                      ROUND(AVG(view_count), 1) as avg_view,
                      ROUND(AVG(sales_count_30d), 1) as avg_sales,
                      ROUND(AVG(watch_growth_rate), 1) as avg_watch_growth,
                      ROUND(AVG(view_growth_rate), 1) as avg_view_growth
               FROM ebay_listings
               WHERE COALESCE(is_ended, 0) = 0
               GROUP BY rank ORDER BY
                   CASE rank WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2
                             WHEN 'C' THEN 3 WHEN 'D' THEN 4 WHEN 'E' THEN 5 ELSE 6 END"""
        ).fetchall()

    return {row['rank']: dict(row) for row in results}


# ---- データストア統合（v2）----

def update_ebay_listing_source_info(ebay_item_id: str, source: str,
                                     source_url: str, classification: str):
    """SKU変換データをebay_listingsに統合"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET source=?, source_url=?, classification=?
               WHERE ebay_item_id=?""",
            (source, source_url, classification, ebay_item_id),
        )


def update_ebay_listing_weight_estimate(
    ebay_item_id: str, weight_g: float, confidence: str = "medium",
) -> None:
    """Claude等による推定 weight を書き込む。weight_source='claude' をマーク。"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings
               SET weight_g=?, weight_source='claude',
                   weight_confidence=?, weight_estimated_at=CURRENT_TIMESTAMP
               WHERE ebay_item_id=?""",
            (weight_g, confidence, ebay_item_id),
        )


def update_ebay_listing_physical(ebay_item_id: str, weight_g: float,
                                  length_cm: float, width_cm: float,
                                  height_cm: float, includes: str = "",
                                  warranty: str = ""):
    """物理データをebay_listingsに統合"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET weight_g=?, length_cm=?, width_cm=?,
               height_cm=?, includes=?, warranty=?
               WHERE ebay_item_id=?""",
            (weight_g, length_cm, width_cm, height_cm, includes, warranty, ebay_item_id),
        )


def update_ebay_listing_source_check(ebay_item_id: str, source_status: str,
                                      source_last_checked: str,
                                      source_out_of_stock_since: str = None):
    """在庫チェック結果をebay_listingsに反映"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET source_status=?, source_last_checked=?,
               source_out_of_stock_since=?
               WHERE ebay_item_id=?""",
            (source_status, source_last_checked, source_out_of_stock_since, ebay_item_id),
        )


def update_ebay_listing_competitor_info(ebay_item_id: str, competitor_min_price: float,
                                         competitor_count: int):
    """競合情報をebay_listingsに反映"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET competitor_min_price=?, competitor_count=?
               WHERE ebay_item_id=?""",
            (competitor_min_price, competitor_count, ebay_item_id),
        )


def update_ebay_listing_price_suggestion(ebay_item_id: str, suggestion: float, reason: str):
    """価格提案をebay_listingsに反映"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE ebay_listings SET price_suggestion=?, price_suggestion_reason=?
               WHERE ebay_item_id=?""",
            (suggestion, reason, ebay_item_id),
        )


# ---- 売上トラッキング ----

def add_sale(ebay_item_id: str, sku: str, title: str, sold_price_usd: float,
             sold_at: str = None, buyer_country: str = "",
             shipping_cost_usd: float = 0, ebay_fee_usd: float = 0,
             source_cost_jpy: float = 0, profit_jpy: float = 0,
             ebay_order_id: str | None = None) -> int:
    """売上を記録. W149 (2026-05-22) で ebay_order_id 引数追加.
    ebay_order_id 付き = UNIQUE INDEX により再実行冪等 (INSERT OR IGNORE で衝突 skip).
    戻り値: 新規 INSERT 成立時 = sale_id (lastrowid), UNIQUE 衝突 skip 時 = 0,
            旧 API (ebay_order_id=None) = 常に sale_id (INSERT 必ず成立).
    total_sold_count / total_revenue_usd の累計 UPDATE は INSERT 成立時のみ実行.
    """
    if sold_at is None:
        sold_at = datetime.now().isoformat()

    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO sales_history
               (ebay_item_id, sku, title, sold_price_usd, sold_at,
                buyer_country, shipping_cost_usd, ebay_fee_usd,
                source_cost_jpy, profit_jpy, ebay_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ebay_item_id, sku, title, sold_price_usd, sold_at,
             buyer_country, shipping_cost_usd, ebay_fee_usd,
             source_cost_jpy, profit_jpy, ebay_order_id),
        )
        if cursor.rowcount == 0:
            return 0  # UNIQUE 衝突で skip (再実行冪等)

        sale_id = cursor.lastrowid

        # ebay_listings の累計も更新 (INSERT 成立時のみ)
        conn.execute(
            """UPDATE ebay_listings SET
               total_sold_count = total_sold_count + 1,
               total_revenue_usd = total_revenue_usd + ?,
               last_sold_at = ?
               WHERE ebay_item_id=?""",
            (sold_price_usd, sold_at, ebay_item_id),
        )

    return sale_id


def get_sales_summary(days: int = 30) -> dict:
    """期間別の売上サマリー"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as count,
                      COALESCE(SUM(sold_price_usd), 0) as revenue_usd,
                      COALESCE(SUM(profit_jpy), 0) as total_profit_jpy,
                      COALESCE(AVG(sold_price_usd), 0) as avg_price
               FROM sales_history
               WHERE sold_at >= datetime('now', ?)""",
            (f'-{days} days',),
        ).fetchone()
    return dict(row) if row else {}


def get_sales_by_date(days: int = 30) -> list:
    """日別の売上推移"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date(sold_at) as date,
                      COUNT(*) as count,
                      SUM(sold_price_usd) as revenue_usd,
                      SUM(profit_jpy) as profit_jpy
               FROM sales_history
               WHERE sold_at >= datetime('now', ?)
               GROUP BY date(sold_at)
               ORDER BY date DESC""",
            (f'-{days} days',),
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_selling_items(limit: int = 10) -> list:
    """売上TOP商品"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ebay_item_id, sku, title,
                      COUNT(*) as sold_count,
                      SUM(sold_price_usd) as total_revenue,
                      AVG(sold_price_usd) as avg_price
               FROM sales_history
               GROUP BY ebay_item_id
               ORDER BY sold_count DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---- W13 ニュース拡張: news_items v17 helpers ----

def save_news_item_v2(
    *,
    source: str,
    title: str,
    url: str,
    source_type: str,                       # 'x' / 'reddit' / 'hn' / 'web'
    source_handle: Optional[str] = None,    # @AnthropicAI, r/ClaudeAI 等
    summary_ja: Optional[str] = None,
    impact_ja: Optional[str] = None,
    impact_level: Optional[str] = None,     # 'high' / 'medium' / 'low' / 'none'
    categories: Optional[str] = None,
    published_at: Optional[str] = None,
    engagement_count: int = 0,
    raw_content: Optional[str] = None,
) -> Optional[int]:
    """W13: news_items へ X/Reddit/HN 対応の拡張 insert.

    URL 一意 (v17 UNIQUE index) でないものは (source, title) UNIQUE で除外.
    既存レコードがあれば engagement_count の UPDATE のみ行い、id は返さない (None).
    """
    _st = (source_type or "web").lower()
    if _st not in ("x", "reddit", "hn", "web"):
        _st = "web"
    with get_conn() as conn:
        # L1: URL 一致で dedupe & engagement update
        if url:
            ex = conn.execute(
                "SELECT id FROM news_items WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
            if ex:
                # engagement が増えていれば更新、それ以外は no-op
                conn.execute(
                    "UPDATE news_items SET engagement_count = MAX(engagement_count, ?) "
                    "WHERE id = ?",
                    (int(engagement_count or 0), ex[0]),
                )
                return None
        # source + title の UNIQUE (既存 v8 制約) 衝突時は IGNORE
        cur = conn.execute(
            """INSERT OR IGNORE INTO news_items
               (source, title, url, summary_ja, impact_ja, impact_level, categories,
                published_at, source_type, source_handle, engagement_count, raw_content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, title, url, summary_ja, impact_ja, impact_level, categories,
             published_at, _st, source_handle,
             int(engagement_count or 0), raw_content),
        )
        return cur.lastrowid if cur.rowcount else None


def get_news_items_recent(days: int = 7, limit: int = 100) -> list[dict]:
    """直近 N 日のニュース取得 (impact_level 降順, checked_at 降順)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source, title, url, summary_ja, impact_ja, impact_level,
                      categories, published_at, checked_at,
                      COALESCE(source_type, 'web')    AS source_type,
                      COALESCE(source_handle, '')     AS source_handle,
                      COALESCE(engagement_count, 0)   AS engagement_count
               FROM news_items
               WHERE checked_at >= datetime('now', ?)
               ORDER BY
                 CASE COALESCE(impact_level, 'none')
                   WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                   WHEN 'low'  THEN 2 ELSE 3
                 END,
                 checked_at DESC
               LIMIT ?""",
            (f"-{int(days)} days", int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ---- W13 API コスト atomic 管理 ----

def add_api_cost(provider: str, cost_usd: float,
                 context: Optional[str] = None) -> float:
    """API コスト 1 回分を atomic に加算し、当日累計を返す.

    Race condition 対策: immediate transaction + RETURNING を SQLite で代用.
    (SQLite 3.35+ の RETURNING を使わず、同一 conn 内で SUM 集計する.)
    """
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        # IMMEDIATE でロックを取り、SUM 後に INSERT し cumulative スナップショットを保存
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_budget_log "
                "WHERE date = ? AND provider = ?",
                (today, provider),
            ).fetchone()
            prev = float(row[0] or 0.0)
            cumulative = prev + float(cost_usd)
            conn.execute(
                """INSERT INTO api_budget_log
                   (date, provider, cost_usd, cumulative_cost, context)
                   VALUES (?, ?, ?, ?, ?)""",
                (today, provider, float(cost_usd), cumulative, context),
            )
            conn.execute("COMMIT")
            return cumulative
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise


def get_todays_api_cost(provider: str) -> float:
    """当日の provider 別累計コスト (USD)."""
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_budget_log "
            "WHERE date = ? AND provider = ?",
            (today, provider),
        ).fetchone()
    return float(row[0] or 0.0)


def get_todays_api_cost_by_context(context: str,
                                   provider: Optional[str] = None) -> float:
    """W209: 当日の context (task/component 名) 別累計コスト (USD).

    news_deep_dive の sub-budget 監視で使用 ($0.45/日 上限を超えたら打切り)。
    api_budget_log.context 列 (v17 で追加済) で filter。

    Args:
        context: 'news_deep_dive' / 'news_relevance' / 'news_x' 等。
        provider: None なら全 provider 合算。'anthropic' / 'xai' 等で絞り込み可。
    """
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        if provider:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_budget_log "
                "WHERE date = ? AND context = ? AND provider = ?",
                (today, context, provider),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_budget_log "
                "WHERE date = ? AND context = ?",
                (today, context),
            ).fetchone()
    return float(row[0] or 0.0)


# ---- W209 ニュース深掘り (news_action_reports) ----

def save_news_action_report(
    *,
    news_item_id: Optional[int],
    title: str,
    url: str,
    axis: str,                       # 'a' / 'b' / 'c' / 'd'
    relevance_score: int,
    summary_ja: str,
    target_module: str,
    integration_ja: str,
    benefit_ja: str,
    effort_estimate: str,            # 'S' / 'M' / 'L'
    confidence: str,                 # 'high' / 'medium' / 'low'
    model: str,
    cost_usd: float,
) -> Optional[int]:
    """W209: 深掘りレポート 1 件を保存。UNIQUE(url) 衝突時は IGNORE。

    Q0 silent skip 防止: rowcount=0 (=既存) は None 返却で呼び出し側が判別可能。
    実 INSERT 件数の集計に使う。

    Returns:
        lastrowid (新規 INSERT 時) / None (URL 既存 or 失敗)
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO news_action_reports
               (news_item_id, title, url, axis, relevance_score,
                summary_ja, target_module, integration_ja, benefit_ja,
                effort_estimate, confidence, model, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                news_item_id, title, url, axis, int(relevance_score),
                summary_ja, target_module, integration_ja, benefit_ja,
                effort_estimate, confidence, model, float(cost_usd),
            ),
        )
        return cur.lastrowid if cur.rowcount else None


def get_news_action_reports_recent(days: int = 7, limit: int = 5) -> list[dict]:
    """W209: 直近 N 日の深掘りレポート (created_at 降順、relevance_score 降順 tiebreak)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, news_item_id, title, url, axis, relevance_score,
                      summary_ja, target_module, integration_ja, benefit_ja,
                      effort_estimate, confidence, model, cost_usd, created_at
               FROM news_action_reports
               WHERE created_at >= datetime('now', ?)
               ORDER BY created_at DESC, relevance_score DESC
               LIMIT ?""",
            (f"-{int(days)} days", int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ---- メール管理 ----

def get_recent_emails(limit: int = 30,
                      exclude_categories: tuple = ('listing_notification',),
                      include_categories: Optional[tuple] = None) -> list[dict]:
    """最新のメール一覧を取得（Claude要約カラム含む）.

    2026-05-07: exclude_categories で listing_notification (user 自身の出品通知)
    をデフォルトで除外. 「重要なメールを優先表示」が DASHBOARD の業務目的のため.

    2026-05-20: include_categories 追加. tab_purchase_confirm「入荷確認」UI が
    category='supplier_purchase' のみに絞り込むため。None なら従来挙動
    (exclude_categories のみ適用)。include_categories 指定時は exclude_
    categories より優先 (IN フィルタ的に振る舞う)。
    """
    with get_conn() as conn:
        if include_categories:
            # category IN include_categories で絞る (NULL は除外、exclude より優先)
            ph = ",".join("?" * len(include_categories))
            sql = f"""SELECT gmail_id, subject, sender, date, body_text,
                          COALESCE(body_ja, '') as body_ja, category, fetched_at,
                          COALESCE(confirmed, 0) as confirmed,
                          COALESCE(summary_ja, '') as summary_ja,
                          COALESCE(action_ja, '') as action_ja,
                          COALESCE(buyer_message_ja, '') as buyer_message_ja,
                          COALESCE(priority_ai, '') as priority_ai,
                          COALESCE(category_ai, '') as category_ai
                   FROM emails
                   WHERE category IN ({ph})
                   ORDER BY fetched_at DESC LIMIT ?"""
            rows = conn.execute(
                sql, (*include_categories, limit),
            ).fetchall()
        elif exclude_categories:
            placeholder = ",".join("?" * len(exclude_categories))
            sql = f"""SELECT gmail_id, subject, sender, date, body_text,
                          COALESCE(body_ja, '') as body_ja, category, fetched_at,
                          COALESCE(confirmed, 0) as confirmed,
                          COALESCE(summary_ja, '') as summary_ja,
                          COALESCE(action_ja, '') as action_ja,
                          COALESCE(buyer_message_ja, '') as buyer_message_ja,
                          COALESCE(priority_ai, '') as priority_ai,
                          COALESCE(category_ai, '') as category_ai
                   FROM emails
                   WHERE COALESCE(category, '') NOT IN ({placeholder})
                   ORDER BY fetched_at DESC LIMIT ?"""
            rows = conn.execute(
                sql, (*exclude_categories, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT gmail_id, subject, sender, date, body_text,
                          COALESCE(body_ja, '') as body_ja, category, fetched_at,
                          COALESCE(confirmed, 0) as confirmed,
                          COALESCE(summary_ja, '') as summary_ja,
                          COALESCE(action_ja, '') as action_ja,
                          COALESCE(buyer_message_ja, '') as buyer_message_ja,
                          COALESCE(priority_ai, '') as priority_ai,
                          COALESCE(category_ai, '') as category_ai
                   FROM emails ORDER BY fetched_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_email_category_counts() -> dict[str, int]:
    """カテゴリ別メール件数"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM emails GROUP BY category"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_email_confirmed(gmail_ids: list[str], confirmed: int = 1):
    """メールの確認済みフラグを設定"""
    if not gmail_ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE emails SET confirmed=? WHERE gmail_id=?",
            [(confirmed, gid) for gid in gmail_ids],
        )


def reset_confirmed_emails():
    """[DEPRECATED] 以前は confirmed=1 を削除していたが、Gmail の INSERT OR IGNORE
    が gmail_id PK で動くため DELETE 後の再 fetch で同じメールが confirmed=0 として
    再挿入され、「同じメールが何度も出る」重大バグの原因となっていた。

    2026-04-22: 本関数は no-op 化。古い confirmed=1 レコードは
    `prune_old_confirmed_emails()` で age ベースに削除する。
    """
    return


def prune_old_confirmed_emails(days: int = 30) -> int:
    """指定日数より古い confirmed=1 メールを DELETE。

    age-based cleanup により、DB 肥大を抑えつつ Gmail 側の INSERT OR IGNORE と
    整合する運用を実現する。戻り値: 削除件数。
    """
    if days <= 0:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM emails "
            "WHERE confirmed=1 AND fetched_at < datetime('now', ?)",
            (f'-{int(days)} days',),
        )
        return cur.rowcount or 0


# ---- 仕入先候補（#9） ----

def add_supplier_candidate(
    sku: str,
    candidate_url: str,
    source_platform: str,
    candidate_price_jpy: Optional[int] = None,
    candidate_title: Optional[str] = None,
    match_score: Optional[int] = None,
    match_reasoning: Optional[str] = None,
    profit_jpy: Optional[float] = None,
    profitable: int = 0,
    ebay_item_id: Optional[str] = None,
    discovered_via: Optional[str] = None,
    junk_likely_untested: int = 0,
    alt_listing_possible: int = 0,
    alt_listing_note: Optional[str] = None,
    eval_model: Optional[str] = None,
    availability_status: Optional[str] = None,
    availability_checked_at: Optional[str] = None,
    availability_signal: Optional[str] = None,
) -> Optional[int]:
    """
    仕入先候補を登録（同一 ebay_item_id + candidate_url の重複は無視）。
    eval_model: AI 評価に使った model (claude-opus-4-7 / claude-haiku-4-5 等).
    availability_*: W182 (2026-05-28) 在庫 gate を通過した時点の判定結果.
        - status='available' 以外は呼び出し側で reject 済の想定 (二重防御として記録).
    Returns: 挿入された行のid、重複なら None

    W185 (2026-05-29): dedup キーが ebay_item_id ベースになったため (sku-rules.md)、
    ebay_item_id 必須。None/空は UNIQUE 上 distinct 扱いで dedup 無効化 = sold_out 候補の
    重複登録に繋がるため、silent NULL 挿入せず ValueError を送出 (Q0 偽装成功防止)。
    """
    if not ebay_item_id or not ebay_item_id.strip():
        raise ValueError(
            "add_supplier_candidate: ebay_item_id は必須です (W185 dedup キー)。"
            f"sku={sku!r} candidate_url={candidate_url!r}"
        )
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO supplier_candidates
               (sku, ebay_item_id, source_platform, candidate_url,
                candidate_price_jpy, candidate_title, match_score,
                match_reasoning, profit_jpy, profitable, discovered_via,
                junk_likely_untested, alt_listing_possible, alt_listing_note,
                eval_model, availability_status, availability_checked_at,
                availability_signal)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sku, ebay_item_id, source_platform, candidate_url,
             candidate_price_jpy, candidate_title, match_score,
             match_reasoning, profit_jpy, profitable, discovered_via,
             junk_likely_untested, alt_listing_possible, alt_listing_note,
             eval_model, availability_status, availability_checked_at,
             availability_signal),
        )
        return cur.lastrowid if cur.rowcount else None


def record_candidate_evaluation(
    ebay_item_id: str,
    candidate_url: str,
    *,
    source_platform: Optional[str] = None,
    candidate_title: Optional[str] = None,
    candidate_price_jpy: Optional[int] = None,
    match_score: Optional[int] = None,
    match_reasoning: Optional[str] = None,
    junk_likely_untested: int = 0,
    alt_listing_possible: int = 0,
    alt_listing_note: Optional[str] = None,
    eval_model: Optional[str] = None,
) -> None:
    """W223 step3: 仕入先候補の AI 評価結果を台帳 (却下含む全件) に upsert.

    candidate_url は呼出側で `_normalize_url` 済を渡すこと (scheme/query 揺れ吸収)。
    再評価時は score 等と evaluated_at を更新 (30 日窓を最新評価から測る)。
    **API エラー評価 (match_score=0/error) は記録しない**こと = 呼出側責務
    (一時失敗を 30 日固定すると次回再評価されず候補が silent 抑制される)。
    listing 識別は ebay_item_id (sku-rules.md)。ebay_item_id/candidate_url 必須。
    """
    if not ebay_item_id or not ebay_item_id.strip():
        raise ValueError("record_candidate_evaluation: ebay_item_id は必須です")
    if not candidate_url or not candidate_url.strip():
        raise ValueError("record_candidate_evaluation: candidate_url は必須です")
    try:
        _record_candidate_evaluation_row(
            ebay_item_id, candidate_url, source_platform, candidate_title,
            candidate_price_jpy, match_score, match_reasoning,
            junk_likely_untested, alt_listing_possible, alt_listing_note,
            eval_model,
        )
    except sqlite3.OperationalError as e:
        # 台帳テーブル未作成 (migration v64 未適用) 時は no-op で degrade。
        # prod は init_db で必ず作成済。fail-open でも評価自体は実行される (Q0)。
        logger.debug(f"[sce] record skipped (schema?): {e}")


def _record_candidate_evaluation_row(
    ebay_item_id, candidate_url, source_platform, candidate_title,
    candidate_price_jpy, match_score, match_reasoning,
    junk_likely_untested, alt_listing_possible, alt_listing_note, eval_model,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO supplier_candidate_evaluations
               (ebay_item_id, candidate_url, source_platform, candidate_title,
                candidate_price_jpy, match_score, match_reasoning,
                junk_likely_untested, alt_listing_possible, alt_listing_note,
                eval_model, evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(ebay_item_id, candidate_url) DO UPDATE SET
                 source_platform=excluded.source_platform,
                 candidate_title=excluded.candidate_title,
                 candidate_price_jpy=excluded.candidate_price_jpy,
                 match_score=excluded.match_score,
                 match_reasoning=excluded.match_reasoning,
                 junk_likely_untested=excluded.junk_likely_untested,
                 alt_listing_possible=excluded.alt_listing_possible,
                 alt_listing_note=excluded.alt_listing_note,
                 eval_model=excluded.eval_model,
                 evaluated_at=CURRENT_TIMESTAMP""",
            (ebay_item_id, candidate_url, source_platform, candidate_title,
             candidate_price_jpy, match_score, match_reasoning,
             int(junk_likely_untested), int(alt_listing_possible),
             alt_listing_note, eval_model),
        )


def get_recent_candidate_evaluation(
    ebay_item_id: str,
    candidate_url: str,
    within_days: int = 30,
) -> Optional[dict]:
    """W223 step3: (ebay_item_id, 正規化URL) の直近評価を返す (within_days 以内のみ)。

    無ければ None。candidate_url は `_normalize_url` 済を渡すこと。
    SQLite TIMESTAMP は UTC (sqlite-timezone.md)。CURRENT_TIMESTAMP 保存・
    datetime('now','-N days') (UTC) 比較で TZ 整合。
    """
    if not ebay_item_id or not candidate_url:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM supplier_candidate_evaluations "
                "WHERE ebay_item_id=? AND candidate_url=? "
                "AND evaluated_at >= datetime('now', ?) "
                "ORDER BY evaluated_at DESC LIMIT 1",
                (ebay_item_id, candidate_url, f"-{int(within_days)} days"),
            ).fetchone()
    except sqlite3.OperationalError as e:
        # 台帳テーブル未作成時は「過去評価なし」として degrade (= 必ず AI 評価へ)。
        logger.debug(f"[sce] lookup skipped (schema?): {e}")
        return None
    return dict(row) if row else None


def get_supplier_candidates(
    sku: Optional[str] = None,
    status: Optional[str] = None,
    min_score: Optional[int] = None,
    ebay_item_id: Optional[str] = None,
) -> list[dict]:
    """
    仕入先候補を取得。
    2026-04-23 ソート変更: profit_jpy DESC → match_score DESC → created_at DESC
    (利益額大の候補を最優先、同利益なら一致度、同一致度なら新しいもの)

    ebay_item_id: listing 単位で候補を取得 (W185, sku-rules.md = listing 識別は ebay_item_id)。
    sku: 有/無在庫 prefix フィルタ等の UI 用途で残置 (listing 一意特定には ebay_item_id を使う)。
    """
    clauses: list[str] = []
    params: list = []
    if ebay_item_id is not None:
        clauses.append("ebay_item_id = ?")
        params.append(ebay_item_id)
    if sku is not None:
        clauses.append("sku = ?")
        params.append(sku)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if min_score is not None:
        clauses.append("match_score >= ?")
        params.append(min_score)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT * FROM supplier_candidates
        {where}
        ORDER BY (profit_jpy IS NULL), profit_jpy DESC,
                 (match_score IS NULL), match_score DESC,
                 created_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def update_supplier_candidate_status(candidate_id: int, status: str):
    """採用/不採用/反映済みなどのステータス更新（user_action_at も同時記録）"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE supplier_candidates
               SET status=?, user_action_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (status, candidate_id),
        )


def set_yahoo_grace_until(ebay_item_id: str, until_ts: Optional[str]):
    """ヤフオク 24h 猶予 (W100 / 2026-05-06 再定義): リサーチ実行可能時刻を設定.

    意味:
      - inventory_check が「落札者なし終了 (再出品慣行該当)」を検知時、
        auction_end_time + 24h をセット
      - supplier_sweep / Pattern 1 は yahoo_grace_until > now の listing を
        リサーチ対象から除外 (再出品を待つ)
      - 24h 経過後の最初の sweep がリサーチ実行 + clear_yahoo_grace_if_due でクリア
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET yahoo_grace_until=? WHERE ebay_item_id=?",
            (until_ts, ebay_item_id),
        )


def clear_yahoo_grace_if_due(ebay_item_id: str) -> int:
    """W100 H-1 fix (2026-05-06): grace の有効期限が過ぎている場合のみ NULL 化.

    旧実装の `set_yahoo_grace_until(eid, None)` は無条件 UPDATE で、
    inventory_check が新規セットした未来の grace を supplier_sweep が
    NULL で上書きする race condition があった (silent regression).

    本関数は WHERE yahoo_grace_until <= datetime('now') で条件付き UPDATE.
    進行中 grace は touch しない.

    Returns: 更新行数 (0=未満了 / 1=クリア完了)
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE ebay_listings SET yahoo_grace_until=NULL "
            "WHERE ebay_item_id=? AND yahoo_grace_until IS NOT NULL "
            "AND yahoo_grace_until <= datetime('now')",
            (ebay_item_id,),
        )
        return cur.rowcount


def get_supplier_candidate_by_id(candidate_id: int) -> Optional[dict]:
    """supplier_candidates を id で 1件取得。見つからない場合 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM supplier_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    return dict(row) if row else None


def _sync_monitored_items_sku(conn, ebay_item_id: str, new_sku: str) -> None:
    """W139-fix (2026-05-18): listing の SKU 変更時、その ebay_item_id に
    紐づく monitored_items 行を追従させる (sku/source_url/site_config_id)。

    sku-rules.md 準拠: 識別キーは ebay_item_id (WHERE sku=? は使わない)。
    呼び出し元の open conn を共有し ebay_listings 更新と同一トランザクション
    で原子的に更新する (片方だけ反映の窓を作らない)。

    source_url は **更新直後の ebay_listings.source_url を同一 conn で読み
    mirror** する (生成器非依存)。理由 (W139-fix HIGH-1, 2026-05-18 実証):
    build_source_url (site_configs) と _build_source_url_from_sku
    (sku_mapping) は mercari 等で出力が食い違う (前者 .../item/123、後者
    .../item/m123) 2 生成器並存負債があり、本番 ebay_listings.source_url は
    後者形。ヘルパが前者で書くと monitored が listing と不一致になり
    inventory_check が誤 URL を scrape → 仕入先 OOS 検知不能 → 履行不能
    (= W139 原事故再現)。listing 値を mirror すれば必ず一致する。
    listing 側が NULL の時のみ ebay_listings と同じ生成器
    (_build_source_url_from_sku) で fallback。site_config_id は prefix 派生
    (find_site_config_by_sku、生成器非依存) で算出。
    生成器 2 系統の統一は本修正 scope 外 = 別 W ROADMAP (K2)。

    対象 0 件 = no-op。まだ監視台帳未登録なだけで、次 main batch の
    ensure_monitor_coverage が新 sku/source_url で正しく登録する自己修復
    (Q0 silent skip ではない: 次タスクで必ず拾われ DB log/Discord に出る)。
    ここで例外を投げると ReviseItem 成功後の正常フローを壊す (K1)。

    根本原因: 旧実装は ebay_listings.sku だけ更新し monitored_items を
    放置 → find_coverage_gaps の旧 m.sku=l.sku 結合が外れ phantom gap 化。
    汚染源は 2 経路 (update_ebay_listing_sku = MonoDeck 手動編集 /
    upsert_ebay_listing sku_changed = ebay_sync が eBay 側変更検知)。
    本ヘルパを両経路から呼び汚染源を断つ。
    """
    if not ebay_item_id:
        return
    # 2026-05-20: new_sku='' (eBay 側で SKU を消した) でも monitored_items を
    # 追従させる (sku-rules: 識別キーは ebay_item_id、旧 sku が残ると
    # find_coverage_gaps 誤判定 = W139 phantom gap 再発の原因になる)。
    # source_url / site_config_id は sku 空では生成器が None 返却となるが、
    # COALESCE で既存維持 (sku 復帰時に再計算される)。
    # 更新直後の ebay_listings.source_url を mirror (生成器非依存で必ず
    # listing と一致 = HIGH-1 根治)。listing 側 NULL 時のみ ebay_listings
    # と同じ生成器 (_build_source_url_from_sku) で fallback。
    lr = conn.execute(
        "SELECT source_url FROM ebay_listings WHERE ebay_item_id=?",
        (ebay_item_id,),
    ).fetchone()
    listing_url = lr[0] if lr else None
    new_source_url = (listing_url
                      or (_build_source_url_from_sku(new_sku) if new_sku else None)
                      or (build_source_url(new_sku) if new_sku else None))
    cfg = find_site_config_by_sku(new_sku) if new_sku else None
    site_config_id = cfg["id"] if cfg else None
    # W183 (2026-05-28): source_url_manual=1 の monitored_items 行は手動 URL を維持
    # (sku のみ追従、source_url / site_config_id は SKU 派生で上書きしない).
    conn.execute(
        """UPDATE monitored_items
              SET sku=?,
                  source_url=CASE WHEN COALESCE(source_url_manual, 0)=1
                                  THEN source_url
                                  ELSE COALESCE(?, source_url) END,
                  site_config_id=CASE WHEN COALESCE(source_url_manual, 0)=1
                                      THEN site_config_id
                                      ELSE ? END
            WHERE ebay_item_id=? AND ebay_item_id IS NOT NULL
              AND ebay_item_id <> ''""",
        (new_sku, new_source_url, site_config_id, ebay_item_id),
    )


def update_ebay_listing_sku(ebay_item_id: str, new_sku: str):
    """ReviseItem 成功後、ローカルDBの ebay_listings.sku を新SKUに追従させる。
    source_url も SKU から再構築して同時更新。
    source_status/source_last_checked/source_out_of_stock_since/risk_confirmed を
    全部リセットする（新SKUは別の仕入先で、次回 inventory_check が再評価するまで状態不明）。

    2026-04-20 修正 (HIGH-2): source_out_of_stock_since を残すと Pattern 2 sweep
    (`task_supplier_sweep`) が新SKUを即再掃引してしまう（同タスクは source_status を見ずに
    oos_since のみで target 選定する設計のため）。ここでクリアして防衛する。
    """
    new_source_url = _build_source_url_from_sku(new_sku)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        # W183 (2026-05-28): source_url_manual=1 の listing は手動 URL + 在庫状態を維持
        # (手動 URL は SKU 変更で変わらないので source_* reset 不要).
        _mrow = conn.execute(
            "SELECT COALESCE(source_url_manual,0) FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if _mrow and int(_mrow[0]) == 1:
            conn.execute(
                "UPDATE ebay_listings SET sku=?, last_synced_at=? WHERE ebay_item_id=?",
                (new_sku, now, ebay_item_id),
            )
        else:
            conn.execute(
                """UPDATE ebay_listings SET
                      sku=?,
                      source_url=COALESCE(?, source_url),
                      source_status='unknown',
                      source_last_checked=NULL,
                      source_out_of_stock_since=NULL,
                      risk_confirmed=0,
                      last_synced_at=?
                   WHERE ebay_item_id=?""",
                (new_sku, new_source_url, now, ebay_item_id),
            )
        # W139-fix (2026-05-18): 同一 conn で monitored_items も ebay_item_id
        # キーで追従 (原子的)。これがないと find_coverage_gaps が phantom gap 化。
        _sync_monitored_items_sku(conn, ebay_item_id, new_sku)


def update_ebay_listing_timing(ebay_item_id: str, time_left_seconds: Optional[int],
                                start_time: Optional[str]) -> None:
    """ebay_sync 時に TimeLeft（残り秒）と StartTime を ebay_listings に保存。
    End→Relist 選定用。ebay_sync Step 2 の各 listing ループから呼ぶ。
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listings SET time_left_seconds=?, start_time=? WHERE ebay_item_id=?",
            (time_left_seconds, start_time or None, ebay_item_id),
        )


def record_relist(old_item_id: str, new_item_id: Optional[str],
                  sku: Optional[str], title: Optional[str],
                  end_reason: str, success: bool,
                  error_message: Optional[str] = None) -> int:
    """relist_history に履歴を記録。cooldown判定に利用。Returns: row id"""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO relist_history
               (old_item_id, new_item_id, sku, title, end_reason, success, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (old_item_id, new_item_id, sku, title, end_reason,
             1 if success else 0, error_message),
        )
        return cur.lastrowid


# ---- W140: listing 単位メモ + 売却時警告 ----
# listing 識別は ebay_item_id 固定 (sku-rules.md: SKU をキーにしない)。

def get_listing_note(ebay_item_id: str) -> Optional[str]:
    """listing の自由メモを返す (無ければ None)。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT note_text FROM listing_notes WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
    return row["note_text"] if row else None


def upsert_listing_note(ebay_item_id: str, note_text: str) -> None:
    """listing メモを保存。空文字 = メモ削除扱い (売却検知は TRIM!='' で判定)。
    ebay_item_id キー (sku-rules: SKU をキーにしない)。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO listing_notes (ebay_item_id, note_text, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(ebay_item_id) DO UPDATE SET "
            "note_text=excluded.note_text, updated_at=datetime('now')",
            (ebay_item_id, note_text),
        )


def record_sale_warning(
    order_id: str, ebay_item_id: str, note_snapshot: Optional[str],
) -> bool:
    """メモ付き listing 売却の警告を claim-then-act で 1 行確保。

    UNIQUE(order_id, ebay_item_id) + INSERT OR IGNORE + rowcount で、
    同一注文の二重 polling でも 1 回だけ True を返す (Discord 二重通知防止、
    既存 inventory_decrement_log と同型)。detected_at は datetime('now')
    = UTC 保存 (sqlite-timezone.md、他 timestamp と整合)。

    Returns: この呼出が最初に claim したら True、既存 (重複) なら False。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO listing_sale_warnings "
            "(order_id, ebay_item_id, note_snapshot, status, detected_at) "
            "VALUES (?, ?, ?, 'open', datetime('now'))",
            (order_id, ebay_item_id, note_snapshot),
        )
        return cur.rowcount == 1


def set_sale_warning_discord_sent(
    order_id: str, ebay_item_id: str, sent: bool,
) -> None:
    """売却警告の Discord 送信可否を記録 (claim 成立後に呼ぶ痕跡列)。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE listing_sale_warnings SET discord_sent=? "
            "WHERE order_id=? AND ebay_item_id=?",
            (1 if sent else 0, order_id, ebay_item_id),
        )


def get_open_sale_warnings() -> list[dict]:
    """未対応 (status='open') の売却警告。title は ebay_item_id で LEFT JOIN
    (sku-rules: JOIN は ebay_item_id)。新しい順。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT w.id, w.order_id, w.ebay_item_id, w.note_snapshot, "
            "w.detected_at, l.title "
            "FROM listing_sale_warnings w "
            "LEFT JOIN ebay_listings l ON l.ebay_item_id = w.ebay_item_id "
            "WHERE w.status='open' "
            "ORDER BY w.detected_at DESC, w.id DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def ack_sale_warning(warning_id: int) -> bool:
    """売却警告を「了解」状態へ。open のみ遷移 = 冪等。Returns: 遷移したら True。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listing_sale_warnings SET status='acked', "
            "acked_at=datetime('now') WHERE id=? AND status='open'",
            (warning_id,),
        )
        return cur.rowcount == 1


def dismiss_sale_warning(warning_id: int) -> bool:
    """売却警告を「不要 (誤検知/対応不要)」へ。open のみ遷移 = 冪等。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listing_sale_warnings SET status='dismissed', "
            "acked_at=datetime('now') WHERE id=? AND status='open'",
            (warning_id,),
        )
        return cur.rowcount == 1


def mark_ebay_listing_ended(ebay_item_id: str, reason: str = "not_in_active_list") -> bool:
    """退役マーキング（既に is_ended=1 ならスキップ）。Returns: 新たに退役化されたら True"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_ended FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if not row:
            return False
        if (row["is_ended"] or 0) == 1:
            return False
        conn.execute(
            "UPDATE ebay_listings SET is_ended=1, ended_at=CURRENT_TIMESTAMP, ended_reason=? "
            "WHERE ebay_item_id=?",
            (reason, ebay_item_id),
        )
        return True


def unmark_ebay_listing_ended(ebay_item_id: str) -> bool:
    """復活したlistingの退役印をクリア。Returns: 実際にクリアしたら True"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_ended FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if not row or (row["is_ended"] or 0) == 0:
            return False
        conn.execute(
            "UPDATE ebay_listings SET is_ended=0, ended_at=NULL, ended_reason=NULL "
            "WHERE ebay_item_id=?",
            (ebay_item_id,),
        )
        return True


def cleanup_stale_supplier_candidates() -> dict:
    """退役済・孤児SKU・仕入先復活SKUに紐づく pending supplier_candidates を auto-reject する。

    対象:
      1. status='pending' かつ 親listingが is_ended=1
      2. status='pending' かつ 親SKUが ebay_listings に存在しない（SKU書き換え後の孤児）
      3. status='pending' かつ 親listingの source_status='在庫有' (仕入先復活、候補不要)
         ※ accepted 候補は auto-reject しない（ユーザー判断を尊重、UI警告に委ねる）

    Returns: {rejected_ended, rejected_orphan, rejected_recovered}
    """
    with get_conn() as conn:
        # 1. parent ended — auto_rejected=1 でマーク（Phase 1 学習時に除外）
        cur1 = conn.execute(
            """UPDATE supplier_candidates
               SET status='rejected', auto_rejected=1, user_action_at=CURRENT_TIMESTAMP
               WHERE status='pending'
                 AND sku IN (
                     SELECT sku FROM ebay_listings WHERE is_ended=1
                 )"""
        )
        rejected_ended = cur1.rowcount

        # 2. orphan (parent SKU 存在しない) - 作成後1時間の猶予を設ける
        # SKU書き換え直後に同一SKUの pending が巻き添えで reject される事故を防ぐ
        cur2 = conn.execute(
            """UPDATE supplier_candidates
               SET status='rejected', auto_rejected=1, user_action_at=CURRENT_TIMESTAMP
               WHERE status='pending'
                 AND sku NOT IN (SELECT sku FROM ebay_listings)
                 AND created_at < datetime('now', '-1 hour')"""
        )
        rejected_orphan = cur2.rowcount

        # 3. 仕入先復活 (2026-04-20 追加): 親 listing が source_status='在庫有' に戻った
        # SKU の pending を auto-reject。accepted は対象外 (UI で警告表示→手動判断)。
        cur3 = conn.execute(
            """UPDATE supplier_candidates
               SET status='rejected', auto_rejected=1, user_action_at=CURRENT_TIMESTAMP
               WHERE status='pending'
                 AND sku IN (
                     SELECT sku FROM ebay_listings
                     WHERE source_status='在庫有'
                       AND (is_ended IS NULL OR is_ended=0)
                 )"""
        )
        rejected_recovered = cur3.rowcount

    return {
        "rejected_ended": rejected_ended,
        "rejected_orphan": rejected_orphan,
        "rejected_recovered": rejected_recovered,
    }


def get_stale_ebay_item_ids(threshold_hours: int = 48) -> list[str]:
    """last_synced_at が threshold_hours 以上前、かつ is_ended=0 の ebay_item_id 一覧。

    退役検出の候補抽出に使う。48hデフォルトは "1回の sync スキップ程度では退役扱いしない" 安全マージン。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ebay_item_id FROM ebay_listings "
            "WHERE (is_ended IS NULL OR is_ended=0) "
            "AND last_synced_at < datetime('now', ?)",
            (f"-{threshold_hours} hours",),
        ).fetchall()
    return [r["ebay_item_id"] for r in rows if r["ebay_item_id"]]


# ---- W9 個別新規出品: description テンプレート CRUD ----

def get_description_templates() -> list[dict]:
    """description テンプレートを全件取得。デフォルト→名前順。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM description_templates "
            "ORDER BY is_default DESC, name ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_description_template(template_id: int) -> Optional[dict]:
    """description テンプレートを id で 1件取得。見つからなければ None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM description_templates WHERE id = ?",
            (template_id,),
        ).fetchone()
    return dict(row) if row else None


def get_default_description_template() -> Optional[dict]:
    """is_default=1 のテンプレートを 1件取得。見つからなければ None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM description_templates WHERE is_default = 1 LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def save_description_template(
    name: str,
    body: str,
    is_default: bool = False,
    template_id: Optional[int] = None,
) -> int:
    """description テンプレートを保存。template_id 指定なし→INSERT、あり→UPDATE。

    is_default=True 指定時は、同一トランザクション内で他の全レコードの
    is_default を 0 にしてから対象レコードを 1 に設定する（単一デフォルト保証）。
    戻り値: 保存した（または更新した）レコードのID。
    """
    is_default_int = 1 if is_default else 0
    with get_conn() as conn:
        # is_default=True なら既存のデフォルトを全てリセット
        if is_default:
            if template_id is None:
                conn.execute("UPDATE description_templates SET is_default = 0")
            else:
                conn.execute(
                    "UPDATE description_templates SET is_default = 0 WHERE id != ?",
                    (template_id,),
                )

        if template_id is None:
            cur = conn.execute(
                """INSERT INTO description_templates
                   (name, body, is_default, created_at, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (name, body, is_default_int),
            )
            return int(cur.lastrowid)
        else:
            conn.execute(
                """UPDATE description_templates
                   SET name = ?, body = ?, is_default = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (name, body, is_default_int, template_id),
            )
            return int(template_id)


def delete_description_template(template_id: int) -> None:
    """description テンプレートを削除。"""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM description_templates WHERE id = ?",
            (template_id,),
        )


# ---- W9 個別新規出品: listing_drafts CRUD ----

# listing_drafts の JSON シリアライズ対象カラム
_LISTING_DRAFT_JSON_COLUMNS = (
    "supplier_image_urls",
    "selected_image_urls",
    "reference_item_specifics_keys",
    "item_specifics",
    "processed_image_urls",
)

# listing_drafts の書込可能カラム（id, created_at, updated_at は自動管理なので除外）
_LISTING_DRAFT_WRITABLE_COLUMNS = (
    "sku",
    "supplier_url",
    "supplier_platform",
    "supplier_title_ja",
    "supplier_price_jpy",
    "supplier_condition_ja",
    "supplier_includes_ja",
    "supplier_image_urls",
    "selected_image_urls",
    "reference_ebay_url",
    "reference_ebay_item_id",
    "reference_category_id",
    "reference_item_specifics_keys",
    "reference_condition_id",
    "rank_code",
    "rank_label",
    "quick_notes",
    "ebay_title",
    "ebay_description",
    "ebay_category_id",
    "ebay_category_name",
    "ebay_condition_id",
    "item_specifics",
    "listing_price_usd",
    "weight_g",
    "in_stock",
    "shipping_policy_id",
    "template_id",
    "scheduled_time",
    "ebay_item_id",
    "status",
    "api_error_message",
    "processed_image_urls",
    "primary_market",
)


def _serialize_listing_draft_value(column: str, value):
    """listing_drafts の INSERT/UPDATE 用に値をシリアライズ。

    JSON カラムは dict/list を json.dumps する（既に str なら素通し）。
    """
    if value is None:
        return None
    if column in _LISTING_DRAFT_JSON_COLUMNS and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value


def _deserialize_listing_draft_row(row: sqlite3.Row) -> dict:
    """listing_drafts の Row を dict 化し、JSON カラムは json.loads() で復元。"""
    result = dict(row)
    for col in _LISTING_DRAFT_JSON_COLUMNS:
        raw = result.get(col)
        if raw:
            try:
                result[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                # 壊れたJSONは生文字列のまま残す（読み出し側で対応可能）
                pass
        else:
            result[col] = None
    return result


def save_listing_draft(data: dict) -> int:
    """listing_drafts に新規行を INSERT し、rowid を返す。

    data は全カラム対応（不要なキーは無視）。JSON カラムは dict/list を自動 dumps。
    """
    columns: list[str] = []
    placeholders: list[str] = []
    values: list = []
    for col in _LISTING_DRAFT_WRITABLE_COLUMNS:
        if col in data:
            columns.append(col)
            placeholders.append("?")
            values.append(_serialize_listing_draft_value(col, data[col]))

    if not columns:
        # 空 INSERT（全カラム DEFAULT で行を作る）
        sql = "INSERT INTO listing_drafts DEFAULT VALUES"
        with get_conn() as conn:
            cur = conn.execute(sql)
            return int(cur.lastrowid)

    sql = (
        f"INSERT INTO listing_drafts ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    with get_conn() as conn:
        cur = conn.execute(sql, values)
        return int(cur.lastrowid)


def update_listing_draft(draft_id: int, data: dict) -> None:
    """listing_drafts の部分更新。updated_at は CURRENT_TIMESTAMP に更新。

    data に含まれる書込可能カラムのみを UPDATE 対象とする。
    """
    set_clauses: list[str] = []
    values: list = []
    for col in _LISTING_DRAFT_WRITABLE_COLUMNS:
        if col in data:
            set_clauses.append(f"{col} = ?")
            values.append(_serialize_listing_draft_value(col, data[col]))

    if not set_clauses:
        # 更新対象カラムが無ければ updated_at だけ触る必要もないので no-op
        return

    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    sql = f"UPDATE listing_drafts SET {', '.join(set_clauses)} WHERE id = ?"
    values.append(draft_id)
    with get_conn() as conn:
        conn.execute(sql, values)


def update_listing_draft_status(
    draft_id: int,
    status: str,
    ebay_item_id: Optional[str] = None,
    api_error_message: Optional[str] = None,
) -> None:
    """listing_drafts の status を更新（'draft'/'submitted'/'api_failed'/'published'）。

    ebay_item_id / api_error_message は指定時のみ上書き。updated_at も更新。
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE listing_drafts
               SET status = ?,
                   ebay_item_id = COALESCE(?, ebay_item_id),
                   api_error_message = COALESCE(?, api_error_message),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, ebay_item_id, api_error_message, draft_id),
        )


def get_listing_draft(draft_id: int) -> Optional[dict]:
    """listing_drafts を id で 1件取得。JSON カラムは dict/list に復元。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM listing_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
    return _deserialize_listing_draft_row(row) if row else None


def get_listing_drafts(
    status: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """listing_drafts の一覧取得。status 指定時はフィルタ。created_at DESC 順。"""
    clauses: list[str] = []
    params: list = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # 2026-04-21 LOW fix: created_at は秒精度のため同一秒INSERT が競合。
    # id DESC を tiebreaker にすることで順序を決定的にする。
    sql = (
        f"SELECT * FROM listing_drafts {where} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_deserialize_listing_draft_row(r) for r in rows]
