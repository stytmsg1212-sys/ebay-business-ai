#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仕入先 URL スクレイピングモジュール（W9 Phase 2）

単一の仕入先商品 URL (ヤフオク / メルカリ / PayPayフリマ) を入力し、
出品情報生成に必要なメタデータを抽出する。

設計方針:
  - プラットフォームを URL から自動判定し、内部関数に dispatch する。
  - Playwright sync_playwright をメインに使用 (既存 yahoo_search.py と同パターン)。
  - Playwright 失敗時は httpx + BeautifulSoup に自動フォールバック。
    少なくとも title / description / image_urls は取得できるようにする。
  - 例外は呼出側に飛ばさず ScrapedProduct.scrape_error にメッセージを格納する。
  - 各セレクタは関数冒頭の定数タプルにまとめ、DOM 変更時の修正コストを下げる。
  - 画像 URL の重複排除は順序保持のため `dict.fromkeys(...).keys()` を使う。

制約:
  - sync_playwright はスレッド非安全。Pattern 1 async 経路から呼ぶ場合は
    既存コードと同様に subprocess / asyncio ベースを検討する必要がある。
    (本モジュール自体は sync 実装で、W9 UI 側で threading.Thread に載せる前提)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# pythonw gotcha ガード: pythonw 起動時は sys.stdout が None のことがある
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (ValueError, OSError):
        pass

logger = logging.getLogger(__name__)


# =========================================================================
# dataclass
# =========================================================================

@dataclass
class ScrapedProduct:
    """仕入先スクレイプ結果。Claude 生成・ドラフト作成の原料になる。"""
    url: str
    platform: str                            # 'yahoo_auctions' / 'mercari' / 'paypay' / 'unknown'
    title_ja: Optional[str] = None
    price_jpy: Optional[int] = None
    condition_ja: Optional[str] = None       # 商品状態テキスト（日本語）
    includes_ja: Optional[str] = None        # 付属品テキスト（日本語）
    image_urls: list[str] = field(default_factory=list)  # 最大10枚
    description_ja: Optional[str] = None     # 商品説明全文
    seller_name: Optional[str] = None
    weight_hint_g: Optional[int] = None      # 本文から抽出できた重量
    # 2026-04-22 追加: 寸法 (mm)。ebay_lister の ShippingPackageDetails 用
    length_mm: Optional[int] = None          # 縦 (mm)
    width_mm: Optional[int] = None           # 横 (mm)
    depth_mm: Optional[int] = None           # 奥行 (mm)
    scrape_error: Optional[str] = None       # 例外 / フォールバック理由


# =========================================================================
# 共通ヘルパ
# =========================================================================

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_MAX_IMAGES = 10


def _parse_price(text: Optional[str]) -> Optional[int]:
    """価格テキスト "¥1,234" "1,234円" → 1234 に変換。"""
    if not text:
        return None
    digits = re.sub(r'[^\d]', '', text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _detect_platform(url: str) -> str:
    """URL ドメインからプラットフォームを判定する。"""
    if not url:
        return 'unknown'
    try:
        host = (urlparse(url).netloc or '').lower()
    except (ValueError, AttributeError):
        return 'unknown'

    if 'auctions.yahoo.co.jp' in host:
        return 'yahoo_auctions'
    if 'paypayfleamarket.yahoo.co.jp' in host:
        # PayPay は yahoo.co.jp の下位ドメインなので先に判定すること。
        return 'paypay'
    if 'mercari.com' in host or 'jp.mercari.com' in host:
        return 'mercari'
    return 'unknown'


def _extract_weight_hint(text: Optional[str]) -> Optional[int]:
    """本文から重量ヒントを抽出。kg 表記は g に換算。見つからなければ None。

    マッチ対象例 (2026-04-22 拡張):
      - "重量 1200 g" / "重量: 約1.2kg" / "約 500 グラム" / "Weight: 750 g"
      - "本体重量約 1.5kg" / "総重量 2kg" / "質量: 800g" / "weight=0.5kg"
      - "質量\n800g" / "重さ 1.2kg" / "Mass: 1kg"
    """
    if not text:
        return None

    # 現実的な商品重量範囲 (1g 〜 50kg)。この範囲外は誤抽出として弾く
    _MIN_G, _MAX_G = 1, 50_000

    def _try(value_str: str, unit_kg: bool) -> Optional[int]:
        try:
            v = float(value_str)
        except ValueError:
            return None
        g = int(v * 1000) if unit_kg else int(v)
        if _MIN_G <= g <= _MAX_G:
            return g
        return None

    # 2026-04-22 FIX (code-reviewer HIGH-2): 英字単位 (g/G/kg) は直後が英字でない
    # ことを確認 (negative lookahead `(?![A-Za-z])`)。
    # これが無いと「500 GB ストレージ」の "500 G" を 500g として誤抽出し、
    # ScrapedProduct.weight_hint_g → ebay_lister の ShippingPackageDetails に
    # 軽量値が流れ、実重量との乖離で eBay 送料赤字が発生する。
    # 電子機器/PC パーツカテゴリ (GB/MB/TB 表記多発) で頻発するバグ。

    # kg 優先 (通常は g より先に書かれる)
    # 2026-04-22: ラベル多様化対応 (重量/質量/本体重量/総重量/重さ/Weight/Mass)
    kg_patterns = [
        r'(?:本体)?(?:重量|質量|重さ|総重量)[:：=\s\n]*約?\s*([\d.]+)\s*(?:kg|KG|Kg)(?![A-Za-z])',
        r'(?:本体)?(?:重量|質量|重さ|総重量)[:：=\s\n]*約?\s*([\d.]+)\s*(?:キログラム|㎏)',
        r'(?:Weight|Mass|WEIGHT)[:=\s]*約?\s*([\d.]+)\s*(?:kg|KG|Kg)(?![A-Za-z])',
        r'約\s*([\d.]+)\s*(?:kg|KG|Kg)(?![A-Za-z])',
        r'約\s*([\d.]+)\s*(?:キログラム|㎏)',
    ]
    for pat in kg_patterns:
        m = re.search(pat, text)
        if m:
            result = _try(m.group(1), unit_kg=True)
            if result is not None:
                return result

    g_patterns = [
        r'(?:本体)?(?:重量|質量|重さ|総重量)[:：=\s\n]*約?\s*(\d+)\s*(?:g|G)(?![A-Za-z])',
        r'(?:本体)?(?:重量|質量|重さ|総重量)[:：=\s\n]*約?\s*(\d+)\s*(?:ｇ|グラム|ｸﾞﾗﾑ)',
        r'(?:Weight|Mass|WEIGHT)[:=\s]*約?\s*(\d+)\s*(?:g|G)(?![A-Za-z])',
        r'約\s*(\d+)\s*(?:g|G)(?![A-Za-z])',
        r'約\s*(\d+)\s*(?:グラム)',
    ]
    for pat in g_patterns:
        m = re.search(pat, text)
        if m:
            result = _try(m.group(1), unit_kg=False)
            if result is not None:
                return result

    return None


def _extract_dimensions(text: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """本文から寸法 (長さ/幅/奥行 mm) を抽出。見つからなければ (None, None, None)。

    マッチ対象例 (2026-04-22 新設):
      - "サイズ: 30×20×10 cm" / "サイズ 30x20x10cm" / "30*20*10cm"
      - "W30×D20×H10cm" / "幅30cm 奥行20cm 高さ10cm"
      - "Dimensions: 300mm × 200mm × 100mm"

    戻り値: (mm単位の値, 大きい順ではなく記載順 = 通常 長×幅×高)。
    変換: cm→mm は ×10。
    """
    if not text:
        return None, None, None

    # 現実的範囲 (1mm 〜 5m)
    _MIN_MM, _MAX_MM = 1, 5000

    def _to_mm(value_str: str, unit: str) -> Optional[int]:
        try:
            v = float(value_str)
        except ValueError:
            return None
        u = unit.lower()
        if u in ('cm', '㎝'):
            mm = int(v * 10)
        elif u in ('mm', '㎜'):
            mm = int(v)
        elif u in ('m',):
            mm = int(v * 1000)
        else:
            return None
        return mm if _MIN_MM <= mm <= _MAX_MM else None

    # パターン1: 3つの数値を × or x or * で繋いだ形
    # "30×20×10cm" / "300x200x100mm"
    triple = re.search(
        r'([\d.]+)\s*[×x*＊✕]\s*([\d.]+)\s*[×x*＊✕]\s*([\d.]+)\s*(cm|㎝|mm|㎜)',
        text,
    )
    if triple:
        unit = triple.group(4)
        a = _to_mm(triple.group(1), unit)
        b = _to_mm(triple.group(2), unit)
        c = _to_mm(triple.group(3), unit)
        if a and b and c:
            return a, b, c

    # パターン2: 日本語個別ラベル "幅 30cm 奥行 20cm 高さ 10cm"
    w = re.search(r'幅[:：\s]*([\d.]+)\s*(cm|㎝|mm|㎜)', text)
    d = re.search(r'奥行[きゆ]?[:：\s]*([\d.]+)\s*(cm|㎝|mm|㎜)', text)
    h = re.search(r'(?:高さ|高)[:：\s]*([\d.]+)\s*(cm|㎝|mm|㎜)', text)
    if w and d and h:
        wv = _to_mm(w.group(1), w.group(2))
        dv = _to_mm(d.group(1), d.group(2))
        hv = _to_mm(h.group(1), h.group(2))
        if wv and dv and hv:
            return wv, dv, hv

    # パターン3: W×D×H (英数 prefix 式)
    wdh = re.search(
        r'[WwＷ][:\s]*([\d.]+)\s*[×x*＊]\s*[DdＤ][:\s]*([\d.]+)\s*[×x*＊]\s*[HhＨ][:\s]*([\d.]+)\s*(cm|㎝|mm|㎜)',
        text,
    )
    if wdh:
        unit = wdh.group(4)
        wv = _to_mm(wdh.group(1), unit)
        dv = _to_mm(wdh.group(2), unit)
        hv = _to_mm(wdh.group(3), unit)
        if wv and dv and hv:
            return wv, dv, hv

    return None, None, None


def _dedupe_ordered(urls: list[str]) -> list[str]:
    """順序を保ったまま重複を除去 + ロゴ/バナー系 URL を除外する。

    2026-04-21 追加: 無在庫出品の意図がバレるのを防ぐため、
    Yahoo/Mercari/PayPay のロゴ・バナー画像は scrape 結果から除外する。
    """
    return [u for u in dict.fromkeys(u for u in urls if u) if not _is_branding_image(u)]


# 無在庫出品を強調するプラットフォーム由来の画像を識別する URL パターン
_BRANDING_URL_PATTERNS = (
    'yauc_logo', 'yahoo_logo', 'yauctions_logo',
    '/logo/', '/logos/', '_logo.', '_logo_',
    '/banner/', '/banners/', '_banner.', '_banner_',
    'footer_bnr', 'header_bnr', 'bnr_',
    'mercari_logo', 'paypay_logo', 'paypayflea_logo',
    'emblem', '/icon/', 'watermark',
    # yimg.jp 配下の UI パーツ (商品画像は auctions.c.yimg.jp だが
    # s.yimg.jp / i.yimg.jp / img01.auctions... はUIパーツのことが多い)
    's.yimg.jp/', 'i.yimg.jp/', '/common/',
    # 2026-06-08: PayPayフリマ等 Next.js サイトのビルド資産 (banner_down /
    # image_phone_pc / icon_* 等は /_next/static/media/ 配下)。これらは商品画像
    # ではないので除外。商品画像は auctions.c.yimg.jp/images... 配下で別経路。
    # 出典: z606464462 で banner_down を商品画像と誤取得した不具合。
    '/_next/',
)


def _is_branding_image(url: str) -> bool:
    """URL がロゴ/バナー/UI パーツなど「無在庫出品の出どころ」が見える画像か判定。"""
    if not url:
        return True
    lower = url.lower()
    for pat in _BRANDING_URL_PATTERNS:
        if pat in lower:
            return True
    return False


def _normalize_image_url(src: Optional[str], base_url: str) -> Optional[str]:
    """画像 URL を絶対 URL に正規化。data:URI / SVG placeholder / ブランド画像は除外。"""
    if not src:
        return None
    src = src.strip()
    if not src:
        return None
    if src.startswith(('data:', 'javascript:')):
        return None
    if src.endswith('.svg') or 'placeholder' in src.lower():
        return None
    try:
        absolute = urljoin(base_url, src)
    except ValueError:
        return None
    # 2026-04-21 追加: ロゴ/バナー URL を除外
    if _is_branding_image(absolute):
        return None
    return absolute


# =========================================================================
# 公開 API
# =========================================================================

def scrape_supplier_url(url: str, timeout_sec: int = 15) -> ScrapedProduct:
    """仕入先 URL から商品情報をスクレイプする。

    失敗時も例外を投げず、ScrapedProduct.scrape_error にメッセージを格納して返す。

    Args:
        url: 仕入先商品ページ URL
        timeout_sec: Playwright / httpx のタイムアウト (秒)

    Returns:
        ScrapedProduct (部分取得可、scrape_error に失敗理由)
    """
    platform = _detect_platform(url)

    if platform == 'unknown':
        return ScrapedProduct(
            url=url,
            platform='unknown',
            scrape_error='unsupported_platform',
        )

    # 2026-04-22 追加: Streamlit (Windows) から呼ばれる場合、asyncio SelectorEventLoop が
    # subprocess_exec を拒否するため Playwright が必ず NotImplementedError で失敗する。
    # 子 Python プロセスに隔離して実行することで event loop 衝突を完全回避する。
    # CLI / scheduler からの呼出しは従来通り in-process で動作する。
    if _should_isolate_playwright():
        isolated = _scrape_via_subprocess(url, timeout_sec)
        if isolated is not None:
            return isolated
        # subprocess 失敗時はそのまま in-process 経路にフォールスルー

    try:
        if platform == 'yahoo_auctions':
            return _scrape_yahoo_auctions(url, timeout_sec)
        if platform == 'mercari':
            return _scrape_mercari(url, timeout_sec)
        if platform == 'paypay':
            return _scrape_paypay(url, timeout_sec)
    except Exception as e:  # noqa: BLE001 — 呼出側 UI を絶対に壊さないため広く catch
        logger.warning(f"scrape failed for {url}: {e!r}")
        # Playwright 経路が丸ごと失敗した場合の最終フォールバック
        try:
            fallback = _httpx_fallback_scrape(url, platform, timeout_sec)
            # 2026-04-22 FIX: httpx fallback が成功したのに scrape_error を塗り潰して
            # UI を手動入力フォームに落としていた論理反転バグを修正。
            # fallback が成功 (scrape_error is None) なら、そのまま返す。
            # 失敗している場合のみ、その理由に Playwright 失敗情報を付記する。
            if fallback.scrape_error:
                e_str = str(e) or type(e).__name__
                fallback.scrape_error = (
                    f'{fallback.scrape_error} (playwright also failed: {e_str})'
                )
            return fallback
        except Exception as fe:  # noqa: BLE001
            logger.warning(f"httpx fallback also failed for {url}: {fe!r}")
            e_str = str(e) or type(e).__name__
            fe_str = str(fe) or type(fe).__name__
            return ScrapedProduct(
                url=url,
                platform=platform,
                scrape_error=f'all_scrape_failed: {e_str} / fallback: {fe_str}',
            )

    # 念のため
    return ScrapedProduct(url=url, platform=platform, scrape_error='unreachable')


def _should_isolate_playwright() -> bool:
    """Playwright を subprocess 隔離するべきか判定する。

    Streamlit 配下で呼ばれた場合 (Windows asyncio SelectorEventLoop 制約) と、
    明示的に環境変数 `EBAY_MANAGER_SCRAPE_SUBPROCESS=1` が設定された場合に True。
    """
    # 明示的な強制 / 無効化オーバーライド (テスト / 緊急時)
    override = os.environ.get('EBAY_MANAGER_SCRAPE_SUBPROCESS', '').strip()
    if override == '1':
        return True
    if override == '0':
        return False
    # Windows 上で streamlit プロセス配下なら自動的に isolate
    if sys.platform != 'win32':
        return False
    # streamlit runtime の存在で判定 (import だけで副作用なし)
    try:
        import streamlit.runtime.scriptrunner as _sr
        ctx = _sr.get_script_run_ctx()
        return ctx is not None
    except Exception:  # noqa: BLE001
        return False


def _scrape_via_subprocess(url: str, timeout_sec: int) -> Optional[ScrapedProduct]:
    """Python 子プロセスで scrape を実行し、JSON 経由で結果を受け取る。

    子プロセスは独立した asyncio ProactorEventLoop を使うため、Streamlit の
    SelectorEventLoop 制約を回避できる。失敗時 (subprocess 起動エラー等) は
    None を返し、呼出側の in-process 経路にフォールスルー。
    """
    import json as _json
    import subprocess
    # 2026-04-22 FIX: Windows console の cp932 encoding が UTF-8 JSON を mojibake
    # させるため、ensure_ascii=True で ASCII-only な JSON を出力する (\uXXXX escape)。
    # これなら stdout encoding に依存せずに日本語を受け渡しできる。
    script = (
        'import json, sys; '
        'from monitor.supplier_scraper import scrape_supplier_url, _dataclass_to_dict_safe; '
        'url, t = sys.argv[1], int(sys.argv[2]); '
        'p = scrape_supplier_url(url, timeout_sec=t); '
        'print(json.dumps(_dataclass_to_dict_safe(p), ensure_ascii=True))'
    )
    env = dict(os.environ)
    # 子プロセス側では in-process 実行させる (再帰防止)
    env['EBAY_MANAGER_SCRAPE_SUBPROCESS'] = '0'
    # 2026-04-22 FIX: Windows で subprocess の stdout が cp932 にフォールバックして
    # 日本語 title/price を mojibake させる致命的バグ。PYTHONIOENCODING=utf-8 で強制する。
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'  # Python 3.7+: UTF-8 mode を子プロセスに伝播
    # 子プロセスの CWD は ebay-manager ルートに揃える
    project_root = str(Path(__file__).resolve().parent.parent)
    try:
        proc = subprocess.run(
            [sys.executable, '-c', script, url, str(timeout_sec)],
            capture_output=True, text=True, encoding='utf-8',
            timeout=timeout_sec + 30, env=env, cwd=project_root,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f'subprocess scrape timeout for {url}')
        return ScrapedProduct(
            url=url, platform=_detect_platform(url),
            scrape_error='subprocess_timeout',
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f'subprocess scrape launch failed: {e!r}')
        return None  # in-process にフォールスルー

    if proc.returncode != 0:
        logger.warning(
            f'subprocess scrape returncode={proc.returncode}, '
            f'stderr={proc.stderr[:500]!r}'
        )
        return None

    try:
        data = _json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        logger.warning(f'subprocess scrape JSON parse failed: {e!r}, out={proc.stdout[:300]!r}')
        return None

    # dict → ScrapedProduct へ再構築
    return ScrapedProduct(
        url=data.get('url') or url,
        platform=data.get('platform') or _detect_platform(url),
        title_ja=data.get('title_ja'),
        price_jpy=data.get('price_jpy'),
        condition_ja=data.get('condition_ja'),
        description_ja=data.get('description_ja'),
        image_urls=list(data.get('image_urls') or []),
        seller_name=data.get('seller_name'),
        includes_ja=data.get('includes_ja'),
        weight_hint_g=data.get('weight_hint_g'),
        length_mm=data.get('length_mm'),
        width_mm=data.get('width_mm'),
        depth_mm=data.get('depth_mm'),
        scrape_error=data.get('scrape_error'),
    )


def _dataclass_to_dict_safe(obj) -> dict:
    """ScrapedProduct → dict (subprocess JSON 送受信用)。"""
    return {
        'url': getattr(obj, 'url', None),
        'platform': getattr(obj, 'platform', None),
        'title_ja': getattr(obj, 'title_ja', None),
        'price_jpy': getattr(obj, 'price_jpy', None),
        'condition_ja': getattr(obj, 'condition_ja', None),
        'description_ja': getattr(obj, 'description_ja', None),
        'image_urls': list(getattr(obj, 'image_urls', []) or []),
        'seller_name': getattr(obj, 'seller_name', None),
        'includes_ja': getattr(obj, 'includes_ja', None),
        'weight_hint_g': getattr(obj, 'weight_hint_g', None),
        'length_mm': getattr(obj, 'length_mm', None),
        'width_mm': getattr(obj, 'width_mm', None),
        'depth_mm': getattr(obj, 'depth_mm', None),
        'scrape_error': getattr(obj, 'scrape_error', None),
    }


# =========================================================================
# ヤフオク
# =========================================================================

_YAHOO_TITLE_SELECTORS = (
    'h1.ProductTitle__text',
    'h1[class*="ProductTitle"]',
    'h1[class*="Title"]',
    'h1',
)
_YAHOO_PRICE_SELECTORS = (
    '.Price__value',
    '[class*="Price__value"]',
    '[class*="Price__price"]',
)
_YAHOO_IMAGE_SELECTORS = (
    'img[class*="ProductImage"]',
    '.ProductImage img',
    '[class*="ProductImage"] img',
)
_YAHOO_DESC_SELECTORS = (
    '.ProductExplanation__commentArea',
    '[class*="ProductExplanation"]',
    '#ProductExplanation',
)
_YAHOO_DETAIL_SELECTORS = (
    '.ProductDetail__description',
    '[class*="ProductDetail"]',
)
_YAHOO_SELLER_SELECTORS = (
    '.Seller__name',
    '[class*="Seller"] a',
)


def _extract_yahoo_next_data(html: str) -> Optional[dict]:
    """Yahoo Auctions の __NEXT_DATA__ を JSON として取出す.

    2026-04-23: 新 UI で CSS 難読化されたため JSON 経路を primary に.
    成功: dict / 失敗: None
    """
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(\{.*?\})</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return (
            (data.get('props') or {})
            .get('pageProps', {})
            .get('initialState', {})
            .get('item', {})
            .get('detail', {})
            .get('item')
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _populate_from_yahoo_json(product: ScrapedProduct, item: dict) -> None:
    """__NEXT_DATA__ の item dict から ScrapedProduct を埋める."""
    if not isinstance(item, dict):
        return

    # タイトル
    title = item.get('title')
    if title and not product.title_ja:
        product.title_ja = str(title).strip()[:300]

    # 価格 (現在価格 → 即決 → 開始価格 の優先順)
    for field in ('price', 'bidorbuy', 'initPrice', 'lastInitPrice'):
        if product.price_jpy:
            break
        v = item.get(field)
        try:
            if v is not None and int(v) > 0:
                product.price_jpy = int(v)
        except (ValueError, TypeError):
            continue

    # 商品状態
    cond = item.get('conditionName') or item.get('itemCondition')
    if cond and not product.condition_ja:
        product.condition_ja = str(cond).strip()[:200]

    # 画像 (img list には {image: URL} 形式で入る)
    if not product.image_urls:
        imgs_raw = item.get('img') or []
        urls: list[str] = []
        for entry in imgs_raw:
            if isinstance(entry, dict):
                u = entry.get('image') or entry.get('url')
            else:
                u = entry
            normalized = _normalize_image_url(u, product.url) if u else None
            if normalized:
                urls.append(normalized)
        product.image_urls = _dedupe_ordered(urls)[:_MAX_IMAGES]

    # 説明文 (description list を join、descriptionHtml があれば HTML も保存)
    if not product.description_ja:
        desc_raw = item.get('description')
        if isinstance(desc_raw, list):
            desc_text = '\n'.join(str(s) for s in desc_raw if s)
        elif isinstance(desc_raw, str):
            desc_text = desc_raw
        else:
            desc_text = ''
        desc_text = desc_text.strip()
        if desc_text and not _looks_like_platform_boilerplate(desc_text):
            product.description_ja = desc_text[:10000]


def _scrape_yahoo_auctions(url: str, timeout_sec: int) -> ScrapedProduct:
    """ヤフオク個別商品ページをスクレイプ。

    2026-04-23: UI リニューアルで CSS 難読化されたため、__NEXT_DATA__ JSON
    を primary に切替。JSON で取れなかった項目のみ CSS selector fallback。
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    product = ScrapedProduct(url=url, platform='yahoo_auctions')
    timeout_ms = timeout_sec * 1000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=_USER_AGENT,
                locale='ja-JP',
                viewport={'width': 1280, 'height': 900},
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            except PWTimeoutError:
                product.scrape_error = 'yahoo_goto_timeout'
                return product

            # 最優先: __NEXT_DATA__ JSON 経路で一括抽出
            try:
                html = page.content()
                item_data = _extract_yahoo_next_data(html)
                if item_data:
                    _populate_from_yahoo_json(product, item_data)
                    logger.debug(
                        f"yahoo JSON extracted: price={product.price_jpy}, "
                        f"cond={product.condition_ja}, imgs={len(product.image_urls)}"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"yahoo __NEXT_DATA__ extract failed: {e}")

            # タイトル (JSON で取れなかった場合のみ CSS fallback)
            try:
                if not product.title_ja:
                    for sel in _YAHOO_TITLE_SELECTORS:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            product.title_ja = (loc.inner_text(timeout=1000) or '').strip()[:300]
                            if product.title_ja:
                                break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"yahoo title extract failed: {e}")

            # 価格 (JSON 済でなければ CSS fallback)
            try:
                if not product.price_jpy:
                    for sel in _YAHOO_PRICE_SELECTORS:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            txt = loc.inner_text(timeout=1000) or ''
                            parsed = _parse_price(txt)
                            if parsed:
                                product.price_jpy = parsed
                                break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"yahoo price extract failed: {e}")

            # 商品の状態 (JSON 済でなければ CSS fallback)
            try:
                if not product.condition_ja:
                    for sel in _YAHOO_DETAIL_SELECTORS:
                        detail_loc = page.locator(sel).first
                        if detail_loc.count() == 0:
                            continue
                        detail_text = detail_loc.inner_text(timeout=1000) or ''
                        m = re.search(
                            r'(?:商品の状態|状態)[\s:：]*([^\n]+)',
                            detail_text,
                        )
                        if m:
                            product.condition_ja = m.group(1).strip()[:200]
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"yahoo condition extract failed: {e}")

            # 説明 (JSON 済でなければ CSS fallback)
            try:
                if not product.description_ja:
                    for sel in _YAHOO_DESC_SELECTORS:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            desc = (loc.inner_text(timeout=2000) or '').strip()
                            if desc and not _looks_like_platform_boilerplate(desc):
                                product.description_ja = desc[:10000]
                                break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"yahoo description extract failed: {e}")
            if product.description_ja and _looks_like_platform_boilerplate(product.description_ja):
                logger.warning(f"yahoo description rejected as boilerplate (url={url})")
                product.description_ja = None

            # 画像 (JSON 済でなければ CSS fallback)
            if not product.image_urls:
                images: list[str] = []
                try:
                    for sel in _YAHOO_IMAGE_SELECTORS:
                        img_loc = page.locator(sel)
                        count = img_loc.count()
                        for i in range(count):
                            try:
                                src = (
                                    img_loc.nth(i).get_attribute('src')
                                    or img_loc.nth(i).get_attribute('data-src')
                                )
                                normalized = _normalize_image_url(src, url)
                                if normalized:
                                    images.append(normalized)
                            except Exception:  # noqa: BLE001
                                continue
                        if images:
                            break
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"yahoo image extract failed: {e}")
                product.image_urls = _dedupe_ordered(images)[:_MAX_IMAGES]

            # 出品者
            try:
                for sel in _YAHOO_SELLER_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        product.seller_name = (loc.inner_text(timeout=1000) or '').strip()[:100]
                        if product.seller_name:
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"yahoo seller extract failed: {e}")

            # 付属品テキスト / 重量ヒント (description から抽出)
            if product.description_ja:
                product.weight_hint_g = _extract_weight_hint(product.description_ja)
                includes = _extract_includes(product.description_ja)
                if includes:
                    product.includes_ja = includes
                l, w, d = _extract_dimensions(product.description_ja)
                product.length_mm = l
                product.width_mm = w
                product.depth_mm = d
                # 2026-04-22: regex で取れなかった項目を Claude Haiku で補完
                _enrich_product_attrs_via_llm(product)

        finally:
            browser.close()

    return product


# =========================================================================
# メルカリ
# =========================================================================

_MERCARI_TITLE_SELECTORS = (
    # 2026-04-22 E2E 検証で発見: main article スコープに絞らないと
    # 「この商品を見ている人におすすめ」枠の別商品を拾ってしまう。
    'main article h1[class*="heading"]',
    'main article h1',
    'h1[class*="heading"]',
    'h1',
)
_MERCARI_PRICE_SELECTORS = (
    # 2026-04-22 E2E FIX: https://jp.mercari.com/item/m80776447154 で
    # 実価格 ¥99,999 の代わりに ¥105,000 (recommended枠の別商品) を返していた。
    # `main article` スコープ内のみを検索するように修正。
    'main article [data-testid="price"]',
    'main article [class*="Price"]',
    'main article [class*="price"]',
    # fallback: article が無い古い構造向け
    '[data-testid="price"]',
    '[class*="Price"]',
    '[class*="price"]',
)
_MERCARI_CONDITION_SELECTORS = (
    # 2026-04-22 FIX: Mercari の DOM 更新対応。従来の data-testid 限定では
    # condition_ja が常に None になるケースが頻発 (ユーザー実例: RSA-1 スライダック
    # 未使用に近い → condition_ja=None で rank 誤判定 B に落ちた)。
    '[data-testid="商品の状態"]',
    '[data-testid*="condition"]',
    '[data-testid="item-detail-condition"]',
    # 見出しテーブル形式: 「商品の状態」の dt に続く dd / span を拾う
    'dt:has-text("商品の状態") + dd',
    'dt:has-text("商品の状態") + dd span',
    # mer-* カスタム要素 (Mercari Web Component) 経由
    'mer-show-more[data-testid*="condition"]',
)
_MERCARI_IMAGE_SELECTORS = (
    'img[data-testid*="image"]',
    '[data-testid*="image"] img',
    'main img',
)
_MERCARI_DESC_SELECTORS = (
    # 2026-04-22 FIX: [class*="description"] が overly broad で meta boilerplate を
    # 拾っていた ("...メルカリでお得に通販、誰でも安心して..." 型の platform 説明)。
    # data-testid 確定のものに限定。
    '[data-testid="description"]',
    'pre[data-testid="description"]',
    'div[data-testid="item-description"]',
    # 最低限の fallback のみ残す (class先頭一致でスコープ狭め)
    'pre[class^="description"]',
)

# 2026-04-22 追加: プラットフォーム汎用 boilerplate の痕跡。これを含む description は
# 偽陽性として空文字に差し替え → Claude 生成に悪影響を与えない。
_PLATFORM_BOILERPLATE_MARKERS = (
    'メルカリでお得に通販',
    '誰でも安心して簡単に売り買い',
    'フリマサービスです',
    'ヤフオク!は、お客様どうしのオークション',
    'ペイペイフリマは、安心・安全のフリマサービス',
    'PayPayフリマは',
    'クレジットカード・キャリア決済・コンビニ',
    '品物が届いてから出品者に入金',
)


def _looks_like_platform_boilerplate(text: str) -> bool:
    """Scrape した description が platform 汎用文言 (商品固有でない) か判定。"""
    if not text:
        return False
    for marker in _PLATFORM_BOILERPLATE_MARKERS:
        if marker in text:
            return True
    return False
_MERCARI_SELLER_SELECTORS = (
    '[data-testid="seller"] a',
    '[data-testid*="seller"]',
)


def _scrape_mercari(url: str, timeout_sec: int) -> ScrapedProduct:
    """メルカリ個別商品ページをスクレイプ。"""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    product = ScrapedProduct(url=url, platform='mercari')
    timeout_ms = timeout_sec * 1000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=_USER_AGENT,
                locale='ja-JP',
                viewport={'width': 1280, 'height': 900},
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            except PWTimeoutError:
                product.scrape_error = 'mercari_goto_timeout'
                return product

            # SPA なので少し待つ
            try:
                page.wait_for_selector('h1', timeout=5000)
            except PWTimeoutError:
                pass

            # タイトル
            try:
                for sel in _MERCARI_TITLE_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        txt = (loc.inner_text(timeout=1000) or '').strip()
                        if txt:
                            product.title_ja = txt[:300]
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"mercari title extract failed: {e}")

            # 価格
            try:
                for sel in _MERCARI_PRICE_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        txt = loc.inner_text(timeout=1000) or ''
                        parsed = _parse_price(txt)
                        if parsed:
                            product.price_jpy = parsed
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"mercari price extract failed: {e}")

            # 商品の状態
            try:
                for sel in _MERCARI_CONDITION_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        txt = (loc.inner_text(timeout=1000) or '').strip()
                        if txt:
                            product.condition_ja = txt[:200]
                            break
                # フォールバック: 全テーブルから「商品の状態」行を探す
                # 2026-04-22 強化: Mercari の 6 段階標準ラベルを貪欲にマッチ。
                # DOM では「商品の状態」と値の間に改行/空白/タブが入ることがあるため
                # 複数の regex で順次トライする。
                if not product.condition_ja:
                    body_text = page.locator('body').inner_text(timeout=2000) or ''
                    # 6 段階標準ラベルに直接ヒットするのが最強
                    _STD_LABELS = (
                        '新品、未使用',
                        '未使用に近い',
                        '目立った傷や汚れなし',
                        'やや傷や汚れあり',
                        '傷や汚れあり',
                        '全体的に状態が悪い',
                    )
                    for _lbl in _STD_LABELS:
                        if _lbl in body_text:
                            product.condition_ja = _lbl
                            break
                    # 上記にヒットしなかった場合「商品の状態」直後の文字列を拾う
                    # (ユーザー定義カスタム文言対応)
                    if not product.condition_ja:
                        m = re.search(
                            r'商品の状態[\s:：]*([^\n\r]{2,100})',
                            body_text,
                        )
                        if m:
                            product.condition_ja = m.group(1).strip()[:200]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"mercari condition extract failed: {e}")

            # 説明 (2026-04-22 FIX: boilerplate 拒否フィルタ追加)
            try:
                for sel in _MERCARI_DESC_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        desc = (loc.inner_text(timeout=2000) or '').strip()
                        if desc and not _looks_like_platform_boilerplate(desc):
                            product.description_ja = desc[:10000]
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"mercari description extract failed: {e}")
            # boilerplate を拾ってしまった場合は空に戻す (Claude 生成への汚染防止)
            if product.description_ja and _looks_like_platform_boilerplate(product.description_ja):
                logger.warning(
                    f"mercari description rejected as platform boilerplate (url={url})"
                )
                product.description_ja = None

            # 画像
            images: list[str] = []
            try:
                for sel in _MERCARI_IMAGE_SELECTORS:
                    img_loc = page.locator(sel)
                    count = img_loc.count()
                    for i in range(count):
                        try:
                            src = (
                                img_loc.nth(i).get_attribute('src')
                                or img_loc.nth(i).get_attribute('data-src')
                            )
                            normalized = _normalize_image_url(src, url)
                            if normalized:
                                images.append(normalized)
                        except Exception:  # noqa: BLE001
                            continue
                    if images:
                        break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"mercari image extract failed: {e}")
            product.image_urls = _dedupe_ordered(images)[:_MAX_IMAGES]

            # 出品者
            try:
                for sel in _MERCARI_SELLER_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        product.seller_name = (loc.inner_text(timeout=1000) or '').strip()[:100]
                        if product.seller_name:
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"mercari seller extract failed: {e}")

            # 付属品 / 重量ヒント
            if product.description_ja:
                product.weight_hint_g = _extract_weight_hint(product.description_ja)
                includes = _extract_includes(product.description_ja)
                if includes:
                    product.includes_ja = includes
                l, w, d = _extract_dimensions(product.description_ja)
                product.length_mm = l
                product.width_mm = w
                product.depth_mm = d
                # 2026-04-22: regex で取れなかった項目を Claude Haiku で補完
                _enrich_product_attrs_via_llm(product)

        finally:
            browser.close()

    return product


# =========================================================================
# PayPayフリマ
# =========================================================================

# PayPay は class が難読化されているため属性ベースを優先する。
_PAYPAY_TITLE_SELECTORS = (
    'h1',
    '[data-testid*="title"]',
)
_PAYPAY_PRICE_PATTERNS = (
    r'[¥￥]\s*([\d,]+)',
    r'([\d,]+)\s*円',
)
_PAYPAY_CONDITION_SELECTORS = (
    'dt:has-text("商品の状態") + dd',
    '[data-testid*="condition"]',
)
_PAYPAY_IMAGE_SELECTORS = (
    'img[src*="paypay"]',
    'img[src*="yimg.jp"]',
    'main img',
)
_PAYPAY_DESC_SELECTORS = (
    '[class*="description"]',
    '[class*="Description"]',
    'pre',
)
_PAYPAY_SELLER_SELECTORS = (
    'a[href*="/user/"]',
    '[data-testid*="seller"]',
)


def _scrape_paypay(url: str, timeout_sec: int) -> ScrapedProduct:
    """PayPayフリマ個別商品ページをスクレイプ。

    SPA で class 名が難読化されているため属性ベースで探す。
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    product = ScrapedProduct(url=url, platform='paypay')
    timeout_ms = timeout_sec * 1000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=_USER_AGENT,
                locale='ja-JP',
                viewport={'width': 1280, 'height': 900},
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            except PWTimeoutError:
                product.scrape_error = 'paypay_goto_timeout'
                return product

            # SPA 描画待ち
            try:
                page.wait_for_selector('h1', timeout=5000)
            except PWTimeoutError:
                pass

            # タイトル
            try:
                for sel in _PAYPAY_TITLE_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        txt = (loc.inner_text(timeout=1000) or '').strip()
                        if txt:
                            product.title_ja = txt[:300]
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"paypay title extract failed: {e}")

            # 価格 (body テキストから regex 抽出)
            try:
                body_text = page.locator('body').inner_text(timeout=2000) or ''
                for pat in _PAYPAY_PRICE_PATTERNS:
                    m = re.search(pat, body_text)
                    if m:
                        parsed = _parse_price(m.group(1))
                        if parsed and parsed >= 100:  # 100円未満は誤検出の可能性
                            product.price_jpy = parsed
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"paypay price extract failed: {e}")

            # 商品の状態
            try:
                for sel in _PAYPAY_CONDITION_SELECTORS:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            txt = (loc.inner_text(timeout=1000) or '').strip()
                            if txt:
                                product.condition_ja = txt[:200]
                                break
                    except Exception:  # noqa: BLE001 — :has-text サポート外セレクタ等を無視
                        continue
                # フォールバック: body から regex
                if not product.condition_ja:
                    body_text = page.locator('body').inner_text(timeout=2000) or ''
                    m = re.search(r'商品の状態\s*([^\n]+)', body_text)
                    if m:
                        product.condition_ja = m.group(1).strip()[:200]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"paypay condition extract failed: {e}")

            # 説明 (2026-04-22 FIX: boilerplate 拒否)
            try:
                for sel in _PAYPAY_DESC_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        desc = (loc.inner_text(timeout=2000) or '').strip()
                        if desc and not _looks_like_platform_boilerplate(desc):
                            product.description_ja = desc[:10000]
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"paypay description extract failed: {e}")
            if product.description_ja and _looks_like_platform_boilerplate(product.description_ja):
                logger.warning(f"paypay description rejected as boilerplate (url={url})")
                product.description_ja = None

            # 画像
            images: list[str] = []
            try:
                for sel in _PAYPAY_IMAGE_SELECTORS:
                    img_loc = page.locator(sel)
                    count = img_loc.count()
                    for i in range(count):
                        try:
                            src = (
                                img_loc.nth(i).get_attribute('src')
                                or img_loc.nth(i).get_attribute('data-src')
                            )
                            alt = img_loc.nth(i).get_attribute('alt') or ''
                            # アイコン系除外 (alt が極端に短く、商品と無関係そうなもの)
                            normalized = _normalize_image_url(src, url)
                            if normalized and ('paypay' in normalized or 'yimg' in normalized or alt):
                                images.append(normalized)
                        except Exception:  # noqa: BLE001
                            continue
                    if images:
                        break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"paypay image extract failed: {e}")
            product.image_urls = _dedupe_ordered(images)[:_MAX_IMAGES]

            # 出品者
            try:
                for sel in _PAYPAY_SELLER_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        product.seller_name = (loc.inner_text(timeout=1000) or '').strip()[:100]
                        if product.seller_name:
                            break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"paypay seller extract failed: {e}")

            # 付属品 / 重量ヒント
            if product.description_ja:
                product.weight_hint_g = _extract_weight_hint(product.description_ja)
                includes = _extract_includes(product.description_ja)
                if includes:
                    product.includes_ja = includes
                l, w, d = _extract_dimensions(product.description_ja)
                product.length_mm = l
                product.width_mm = w
                product.depth_mm = d
                # 2026-04-22: regex で取れなかった項目を Claude Haiku で補完
                _enrich_product_attrs_via_llm(product)

        finally:
            browser.close()

    return product


# =========================================================================
# 付属品抽出 (簡易)
# =========================================================================

_INCLUDES_PATTERNS = (
    # 括弧型を最優先 (基本パターンが "付属品】" を誤捕獲するのを防ぐため)
    r'[【\[［](?:付属品|セット内容|同梱(?:物|品)?|内容物)[】\]］]\s*([^\n]+)',
    # 明示ラベル型 (高確度)
    r'付属品[:：\s]+([^\n]+)',
    r'(?:セット内容|同梱物|同梱品|内容物)[:：\s]+([^\n]+)',
    r'Includes?[:\s]+([^\n]+)',
    # 2026-04-22 追加: 自然文型
    # "以下が付属します: リモコン、説明書、ACアダプター"
    r'以下(?:が|を)(?:付属|同梱)(?:します|いたします)[:：\s]*([^\n]+)',
)

# 付属品の natural-language indicators (文章内抽出用)
# これらキーワードが文末にあれば、直前の名詞句が付属品候補
_INCLUDES_NARRATIVE_PATTERNS = (
    # "X, Y, Z 付き" / "X, Y, Z が付属" / "X, Y が同梱"
    r'((?:[^\s、。,]+(?:、|,|・)){1,8}[^\s、。,]+)\s*(?:付き|付属|同梱)',
    # "X でお送りします" / "X を同封" は少し広め
    r'((?:[^\s、。,]+(?:、|,|・)){1,8}[^\s、。,]+)\s*(?:でお送り|を同封|をお付け)',
)


def _extract_includes(text: str) -> Optional[str]:
    """商品説明から付属品の行を抽出する。見つからなければ None。

    2026-04-22 拡張: 明示ラベル (【付属品】等) に加え、自然文 (「リモコン、説明書付き」等)
    のパターンも拾う。長すぎる抽出結果は捨てる (誤抽出防止)。
    """
    if not text:
        return None
    # Priority 1: 明示ラベル
    for pat in _INCLUDES_PATTERNS:
        m = re.search(pat, text)
        if m:
            cand = m.group(1).strip()[:300]
            if cand:
                return cand
    # Priority 2: 自然文 (誤抽出リスク高いので short candidates に絞る)
    for pat in _INCLUDES_NARRATIVE_PATTERNS:
        m = re.search(pat, text)
        if m:
            cand = m.group(1).strip()
            # 2026-04-22 FIX (code-reviewer HIGH-1): Python 演算子優先順位の罠を解消。
            # 旧コード: `cand and len(cand) <= 80 and '、' in cand or '・' in cand or ',' in cand`
            # → `(...'、' in cand) or '・' in cand or ',' in cand` と解釈され、
            # 長文でも '・' か ',' があれば通ってしまい、付属品として誤登録→Defect リスク。
            if (
                cand
                and len(cand) <= 80
                and ('、' in cand or '・' in cand or ',' in cand)
            ):
                return cand[:300]
    return None


def _enrich_product_attrs_via_llm(product: 'ScrapedProduct') -> None:
    """regex で取れなかった weight/dimensions/includes を Claude Haiku で補完する。

    2026-04-22 追加: 自然文による記述を救済する。
    product を in-place で更新。API 未設定 / 短い説明 / 例外時は no-op。
    既に regex で取れた項目は上書きしない (信頼度高い source 優先)。
    """
    desc = (product.description_ja or '').strip()
    # 短すぎる説明では LLM を呼ばない (コスト節約 + hallucination リスク)
    if len(desc) < 30:
        return
    # 全項目揃っていれば呼ばない
    if (product.weight_hint_g is not None
            and product.includes_ja
            and product.length_mm and product.width_mm and product.depth_mm):
        return
    try:
        from monitor.product_attrs_extractor import extract_product_attrs
        attrs = extract_product_attrs(desc)
    except Exception as e:  # noqa: BLE001
        logger.debug(f'LLM product_attrs fallback skipped: {e!r}')
        return

    # regex 取得済の項目は上書きしない
    if product.weight_hint_g is None and attrs.get('weight_g'):
        product.weight_hint_g = int(attrs['weight_g'])
    if product.length_mm is None and attrs.get('length_mm'):
        product.length_mm = int(attrs['length_mm'])
    if product.width_mm is None and attrs.get('width_mm'):
        product.width_mm = int(attrs['width_mm'])
    if product.depth_mm is None and attrs.get('depth_mm'):
        product.depth_mm = int(attrs['depth_mm'])
    if not product.includes_ja and attrs.get('includes_list'):
        product.includes_ja = '、'.join(attrs['includes_list'])[:300]


# =========================================================================
# httpx フォールバック (Playwright 起動失敗時)
# =========================================================================

def _httpx_fallback_scrape(url: str, platform: str, timeout_sec: int) -> ScrapedProduct:
    """Playwright が使えない環境向けの簡易 httpx + BeautifulSoup フォールバック。

    最低限 title / description / image_urls を取得する。price / condition は
    DOM 構造依存なので取れない場合が多い。
    """
    import httpx
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ScrapedProduct(
            url=url,
            platform=platform,
            scrape_error='httpx_fallback_missing_bs4',
        )

    product = ScrapedProduct(url=url, platform=platform)
    headers = {
        'User-Agent': _USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8',
    }

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_sec, follow_redirects=True)
        if resp.status_code == 404:
            product.scrape_error = 'httpx_404'
            return product
        if resp.status_code != 200:
            product.scrape_error = f'httpx_http_{resp.status_code}'
            return product
        html = resp.text
    except httpx.TimeoutException:
        product.scrape_error = 'httpx_timeout'
        return product
    except Exception as e:  # noqa: BLE001
        product.scrape_error = f'httpx_error: {e}'
        return product

    soup = BeautifulSoup(html, 'html.parser')

    # タイトル: og:title → h1 → <title>
    og_title = soup.find('meta', property='og:title')
    if og_title and og_title.get('content'):
        product.title_ja = og_title['content'].strip()[:300]
    else:
        h1 = soup.find('h1')
        if h1 and h1.get_text(strip=True):
            product.title_ja = h1.get_text(strip=True)[:300]
        elif soup.title and soup.title.string:
            product.title_ja = soup.title.string.strip()[:300]

    # 説明: og:description → meta description (2026-04-22: boilerplate 拒否)
    # NOTE: メルカリ等の og:description は platform 汎用文のことが多いので
    # _looks_like_platform_boilerplate で弾く。
    og_desc = soup.find('meta', property='og:description')
    cand = og_desc['content'].strip() if og_desc and og_desc.get('content') else ''
    if cand and not _looks_like_platform_boilerplate(cand):
        product.description_ja = cand[:10000]
    else:
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        cand2 = meta_desc['content'].strip() if meta_desc and meta_desc.get('content') else ''
        if cand2 and not _looks_like_platform_boilerplate(cand2):
            product.description_ja = cand2[:10000]

    # 画像: og:image + 全 <img>
    images: list[str] = []
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        normalized = _normalize_image_url(og_image['content'], url)
        if normalized:
            images.append(normalized)
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        normalized = _normalize_image_url(src, url)
        if normalized:
            images.append(normalized)
    product.image_urls = _dedupe_ordered(images)[:_MAX_IMAGES]

    # 付属品 / 重量ヒント
    if product.description_ja:
        product.weight_hint_g = _extract_weight_hint(product.description_ja)
        product.includes_ja = _extract_includes(product.description_ja)
        l, w, d = _extract_dimensions(product.description_ja)
        product.length_mm = l
        product.width_mm = w
        product.depth_mm = d
        _enrich_product_attrs_via_llm(product)

    return product


if __name__ == '__main__':
    # 手動テスト例:
    #   python -m monitor.supplier_scraper https://auctions.yahoo.co.jp/jp/auction/xxxx
    import json
    logging.basicConfig(level=logging.INFO)
    test_url = sys.argv[1] if len(sys.argv) > 1 else 'https://auctions.yahoo.co.jp/jp/auction/x1234567'
    result = scrape_supplier_url(test_url)
    print(json.dumps({
        'url': result.url,
        'platform': result.platform,
        'title_ja': result.title_ja,
        'price_jpy': result.price_jpy,
        'condition_ja': result.condition_ja,
        'includes_ja': result.includes_ja,
        'image_count': len(result.image_urls),
        'weight_hint_g': result.weight_hint_g,
        'scrape_error': result.scrape_error,
    }, ensure_ascii=False, indent=2))
