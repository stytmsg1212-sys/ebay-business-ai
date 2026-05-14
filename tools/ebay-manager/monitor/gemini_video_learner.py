#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini 2.5 Pro 動画学習モジュール

YouTube URL の動画を Gemini に直接渡して、eBay 物販視点で構造化知識を抽出する。
Whisper + Claude 要約よりも:
  - スライド/画面共有の視覚情報を拾える
  - 話者の指示代名詞（「ここ」「これ」）を視覚で補完できる
  - 画面上の数値（価格、割合、ページ）を正確に抽出できる

Google AI Studio で GOOGLE_API_KEY を取得済みであること (.env 経由)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# .env 経由のキー読み込み
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

try:
    from google import genai
    from google.genai import types
    _GENAI_OK = True
except ImportError:
    _GENAI_OK = False

# 無料枠対応で Flash をデフォルト化。高品質が必要なら .env GEMINI_MODEL=gemini-2.5-pro で切替
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# eBay物販視点で動画から知識を抽出するシステムプロンプト
EXTRACTION_PROMPT = """この動画は eBay 越境EC物販に関する解説/セミナー/チュートリアルです。
越境ECセラー向けに、動画内容から実用的な知識を構造化して抽出してください。

**音声だけでなく、画面共有されているUI・スライド・数値・表も必ず読んで活用してください。**
話者が「ここをクリック」「この設定」と言う場合、画面で何を指しているか視覚情報から特定してください。

## 重要な時代背景 (eBay越境EC)

2025-10月に **デミニミス撤廃（トランプ関税）** があり、それを境にアメリカ向け配送・価格構造が激変した。
- **2025-08以前**: DDU（Delivered Duty Unpaid）が基本。アメリカ向け小額輸入は関税フリー。
- **2025-08〜2025-10 (移行期)**: 関税対応の緊急議論時期
- **2025-10以降**: DDP（Delivered Duty Paid）が主流。関税を送料に含めて販売。

送料・関税・価格戦略の知識は **時代によって有効性が変わる**。動画内の発言時点・タイトルの日付・画面に映る日付から動画の時期を推定し、tariff_era を正しく判定すること。

## 出力

以下の厳密な JSON 形式で回答してください（```json フェンス禁止、前後にテキスト禁止）:

{
  "summary_ja": "動画全体の要約 (3〜5文、重要ポイントを凝縮)",
  "published_date": "YYYY-MM-DD (動画内で言及された日付、タイトル日付、撮影時期から推定)。不明なら空文字",
  "tariff_era": "pre_tariff | transition | post_tariff | evergreen のいずれか",
  "time_sensitive_topics": [
    "時代依存するトピック配列。例: shipping, tariff, pricing_strategy, ddu_vs_ddp など。時代に依存しない内容なら空配列"
  ],
  "key_insights": [
    "動画内で提示された重要な気づき・ノウハウを具体的に（5〜10件、箇条書きで一文ずつ）"
  ],
  "products_mentioned": [
    {"name": "商品名やブランド名", "category": "カテゴリ", "price_range": "$XX-$YY or 不明"}
  ],
  "platforms_mentioned": [
    "eBay/Mercari/Yahoo Auctions/PayPayフリマ/Amazon など話題に登場したプラットフォーム"
  ],
  "actionable_steps": [
    "動画で説明された具体的な実行手順や戦略（5〜10件、実際にセラーが真似できる粒度）"
  ],
  "pricing_hints": [
    {"product": "何の価格か", "range": "価格帯", "reasoning": "そう判断した根拠（画面情報優先）"}
  ],
  "topics": ["sourcing", "pricing", "shipping", "listing_optimization", "competitor_analysis" など該当するタグを複数"],
  "keywords_for_index": [
    "将来この知識を検索するときのキーワード（商品名・ブランド・戦略名・専門用語、最低10件）"
  ]
}

## tariff_era 判定ルール

- **pre_tariff**: 2025-08以前の動画、または DDU前提の送料・関税議論が中心 → 送料/関税の知識は古い可能性
- **transition**: 2025-08〜2025-10の動画、関税対策・緊急対応の議論が中心
- **post_tariff**: 2025-10以降の動画、DDP前提の送料・関税議論、新関税制度を前提とした戦略
- **evergreen**: 送料・関税と関係なく、リサーチ手法・マインド・商品選定など時代依存しない内容が主

判断材料が曖昧な時は `evergreen` にし、`time_sensitive_topics` を空にせず該当トピックを明記する（後で人間が判断できるように）。

## 原則

- 曖昧な一般論を書かない。動画内で実際に示された具体的数値・手法・商品名を使う。
- 画面上の数値を優先（例: 話者が「だいたい10%くらい」と言っても画面に「12.5%」と表示されていれば 12.5% を記載）。
- eBayセラーが明日から使える形で `actionable_steps` を書く。
- 動画にこれらの情報が含まれなければ空配列 [] を返して良い（捏造禁止）。
"""


def _get_client():
    if not _GENAI_OK:
        raise RuntimeError("google-genai package not installed")
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY missing in .env")
    return genai.Client(api_key=api_key)


def extract_video_id(url: str) -> Optional[str]:
    """YouTube URL から video_id 抽出。失敗時 None。"""
    if not url:
        return None
    patterns = [
        r'(?:v=|/)([a-zA-Z0-9_-]{11})(?:[?&#]|$)',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'embed/([a-zA-Z0-9_-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def download_video(url: str, output_dir: Path) -> Optional[Path]:
    """yt-dlp で動画ダウンロード。成功時ローカルファイルパス、失敗時 None。

    MP4 で統一（Gemini が扱いやすい）、音声付き、720p以下にリサイズ。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    vid = extract_video_id(url) or "video"
    out_tpl = str(output_dir / f"{vid}.%(ext)s")

    import subprocess
    # YouTube bot 検出回避: cookie ファイル指定（環境変数 YTDLP_COOKIES_FILE）または
    # リクエスト間隔を確保。連続実行時の Sign in 要求軽減用。
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best",
        "--merge-output-format", "mp4",
        "-o", out_tpl,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--retries", "5",
        "--retry-sleep", "5",
        "--sleep-requests", "1",
        # YouTube SABR-only streaming + JS challenge 突破に必須
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=tv,web,android",
    ]
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        cmd += ["--cookies", cookies_file]
    cmd.append(url)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except subprocess.CalledProcessError as e:
        logger.warning(f"yt-dlp failed: {e.stderr}")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timeout (>10min)")
        return None
    except FileNotFoundError:
        logger.warning("yt-dlp not installed in PATH")
        return None

    # 生成ファイル確認
    for cand in output_dir.glob(f"{vid}.*"):
        if cand.suffix in (".mp4", ".webm", ".mkv"):
            return cand
    return None


def get_video_metadata(url: str) -> dict:
    """yt-dlp でメタデータのみ取得（title, channel, duration）。"""
    import subprocess
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--retries", "5",
        "--retry-sleep", "5",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=tv,web,android",
    ]
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        cmd += ["--cookies", cookies_file]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout)
        return {
            "title": d.get("title", ""),
            "channel": d.get("channel") or d.get("uploader") or "",
            "duration": int(d.get("duration") or 0),
            "video_id": d.get("id", ""),
        }
    except Exception as e:
        logger.warning(f"metadata fetch failed: {e}")
        return {}


def _repair_truncated_json(s: str) -> str:
    """Gemini 応答が max_output_tokens 到達で切断された場合、安全な truncation 点まで
    切り詰め、波括弧/角括弧を補完してパース可能な形に修復する。

    戦略:
      1. 一度パースを試行、成功すればそのまま返す
      2. 失敗時は文字列を走査し各深さで「直前値が完了した時点」（,, または } / ] 直後）を記録
      3. 最後に記録した安全点まで切り詰めて、残った depth_stack を閉じる
    """
    if not s:
        return s
    s = s.strip()
    if not s.startswith('{'):
        return s
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass

    in_str = False
    escape = False
    depth_stack: list[str] = []
    # 各深さレベルで「ここで切れば値が完結している」位置を記録
    safe_cut: dict[int, int] = {0: 0}  # depth → position
    awaiting_value = False  # 直前が ":" だったら値を待っている状態
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            if in_str:
                in_str = False
                if not awaiting_value:
                    # キー終了
                    pass
                else:
                    # 値（文字列）終了
                    awaiting_value = False
                    safe_cut[len(depth_stack)] = i + 1
            else:
                in_str = True
            continue
        if in_str:
            continue
        if ch == '{' or ch == '[':
            depth_stack.append(ch)
            # 開いた直後を fallback の安全点として記録（後続が壊れていても空 {} / [] で閉じられる）
            safe_cut[len(depth_stack)] = i + 1
            awaiting_value = False
        elif ch == '}' or ch == ']':
            if depth_stack:
                depth_stack.pop()
            safe_cut[len(depth_stack)] = i + 1
            awaiting_value = False
        elif ch == ':':
            awaiting_value = True
        elif ch == ',':
            awaiting_value = False
            safe_cut[len(depth_stack)] = i  # カンマの直前
        elif ch.strip():
            # bool/number/null など非空白文字
            if awaiting_value and len(depth_stack) > 0:
                # 値の途中。完了は次の , } ] で記録される
                pass

    # 最も浅い深さで最大の safe_cut を採用（大は小を兼ねる）
    if not safe_cut:
        return s
    cut = max(safe_cut.values())
    if cut == 0:
        return s
    candidate = s[:cut].rstrip().rstrip(',').rstrip()
    # この時点で candidate の depth_stack を再計算して閉じ括弧を補完
    in_str = False
    escape = False
    final_stack: list[str] = []
    for ch in candidate:
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{' or ch == '[':
            final_stack.append(ch)
        elif ch == '}' or ch == ']':
            if final_stack:
                final_stack.pop()
    for ch in reversed(final_stack):
        candidate += '}' if ch == '{' else ']'
    return candidate


def _strip_fenced_json(text: str) -> Optional[str]:
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if fence:
        return fence.group(1)
    greedy = re.search(r'\{[\s\S]*\}', text)
    if greedy:
        return greedy.group(0)
    # 切断された JSON を修復試行（{ で始まるが } で終わらないケース）
    head = text.lstrip()
    if head.startswith('{'):
        return _repair_truncated_json(head)
    return None


def learn_from_video_file(video_path: Path, timeout_sec: int = 600) -> Optional[dict]:
    """ローカル動画ファイルを Gemini に投げて構造化知識を抽出。

    Returns: 構造化 dict or None (失敗時)
    """
    if not video_path.exists():
        logger.error(f"video file not found: {video_path}")
        return None

    client = _get_client()

    # Files API にアップロード (>20MB は必須、小さくても推奨)
    try:
        logger.info(f"Uploading {video_path.name} ({video_path.stat().st_size / 1e6:.1f} MB) to Gemini...")
        uploaded = client.files.upload(file=str(video_path))
    except Exception as e:
        logger.error(f"Gemini upload failed: {e}")
        return None

    # 処理完了まで待つ (Gemini 側で動画解析の前処理)
    start = time.time()
    while uploaded.state.name == "PROCESSING":
        if time.time() - start > timeout_sec:
            logger.error("Gemini file processing timeout")
            return None
        time.sleep(5)
        uploaded = client.files.get(name=uploaded.name)

    if uploaded.state.name != "ACTIVE":
        logger.error(f"Gemini file not ACTIVE after processing: state={uploaded.state.name}")
        return None

    # 抽出リクエスト
    # mediaResolution=LOW で動画トークンを 1/4 に削減（スライド読解は十分な精度を維持）
    # これで 1M 制限内に長尺動画も収まる
    from monitor.api_logger import log_gemini_response, _Timer
    response = None
    try:
        logger.info(f"Sending extraction request to {MODEL}...")
        with _Timer() as _tt:
            response = client.models.generate_content(
                model=MODEL,
                contents=[uploaded, EXTRACTION_PROMPT],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                    max_output_tokens=24576,
                    media_resolution="MEDIA_RESOLUTION_LOW",
                ),
            )
        log_gemini_response("video_learning", MODEL, response,
                            duration_ms=_tt.duration_ms, success=True)
    except Exception as e:
        logger.error(f"Gemini extraction failed: {e}")
        log_gemini_response("video_learning", MODEL, None,
                            success=False, error_message=str(e)[:500])
        return None
    finally:
        # アップロードしたファイルをクリーンアップ（無料枠を食わないように）
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

    text = getattr(response, "text", "") or ""
    cand = _strip_fenced_json(text)
    if not cand:
        logger.warning(f"no JSON in Gemini response: {text[:200]!r}")
        return None

    try:
        data = json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode: {e}, raw={text[:300]!r}")
        return None

    data["_raw_response"] = text
    return data


def learn_from_youtube_url_direct(url: str) -> Optional[dict]:
    """Gemini API に YouTube URL を直接渡して構造化知識を抽出。

    ローカルダウンロードを介さず Google 側で動画を取り込む方式。
    yt-dlp 経由の YouTube bot 検出/SABR ブロックの影響を受けない。

    Returns: 構造化 dict or None (失敗時)
    """
    if not _GENAI_OK:
        logger.error("google-genai SDK not installed")
        return None
    client = _get_client()

    from monitor.api_logger import log_gemini_response, _Timer
    response = None
    try:
        logger.info(f"Sending direct YouTube URL to {MODEL}: {url}")
        with _Timer() as _tt:
            response = client.models.generate_content(
                model=MODEL,
                contents=types.Content(
                    parts=[
                        types.Part(
                            file_data=types.FileData(
                                file_uri=url, mime_type="video/*",
                            ),
                        ),
                        types.Part(text=EXTRACTION_PROMPT),
                    ],
                ),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                    max_output_tokens=24576,
                    media_resolution="MEDIA_RESOLUTION_LOW",
                ),
            )
        log_gemini_response("video_learning", MODEL, response,
                            duration_ms=_tt.duration_ms, success=True)
    except Exception as e:
        logger.error(f"Gemini direct URL extraction failed: {e}")
        log_gemini_response("video_learning", MODEL, None,
                            success=False, error_message=str(e)[:500])
        return None

    text = getattr(response, "text", "") or ""
    cand = _strip_fenced_json(text)
    if not cand:
        logger.warning(f"no JSON in Gemini response: {text[:200]!r}")
        return None

    try:
        data = json.loads(cand)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode: {e}, raw={text[:300]!r}")
        return None

    data["_raw_response"] = text
    return data


def learn_from_youtube_url(
    url: str, workdir: Path, keep_video: bool = False,
) -> tuple[dict, Optional[dict]]:
    """YouTube URL 一発処理.

    優先: Gemini に URL を直接渡す方式（DL 不要、bot block 回避）。
    fallback: yt-dlp で DL 後に Gemini にアップロード。

    環境変数 VIDEO_LEARNING_DOWNLOAD=1 を設定すると DL 方式に強制。

    Returns: (metadata, extracted) — extracted が None なら失敗
    """
    meta = get_video_metadata(url)
    logger.info(f"Metadata: title={meta.get('title')!r} ch={meta.get('channel')!r} dur={meta.get('duration')}s")

    force_dl = os.environ.get("VIDEO_LEARNING_DOWNLOAD") == "1"
    if not force_dl:
        # 直接 URL 取り込みを試行
        extracted = learn_from_youtube_url_direct(url)
        if extracted is not None:
            return meta, extracted
        logger.warning("direct URL ingestion failed, falling back to yt-dlp download")

    # フォールバック: yt-dlp で DL → アップロード
    video_path = download_video(url, workdir)
    if not video_path:
        return meta, None

    try:
        extracted = learn_from_video_file(video_path)
    finally:
        if not keep_video and video_path.exists():
            try:
                video_path.unlink()
            except Exception:
                pass

    return meta, extracted


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m monitor.gemini_video_learner <YouTube URL>")
        sys.exit(1)

    url = sys.argv[1]
    workdir = Path(__file__).resolve().parent.parent / "data" / "video_cache"
    meta, extracted = learn_from_youtube_url(url, workdir)
    print("\n=== METADATA ===")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print("\n=== EXTRACTED ===")
    if extracted:
        extracted.pop("_raw_response", None)
        print(json.dumps(extracted, indent=2, ensure_ascii=False))
    else:
        print("(extraction failed)")
