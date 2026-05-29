Reading additional input from stdin...
OpenAI Codex v0.130.0
--------
workdir: C:\Users\gucch\projects\claude\tools\ebay-manager
model: gpt-5.5
provider: openai
approval: never
sandbox: read-only
reasoning effort: none
reasoning summaries: none
session id: 019e6efa-f4e6-7901-bc4b-3c33ef22a125
--------
user
You are fixing a money-critical bug in a Japanese cross-border eBay reselling tool (Python / Streamlit / SQLite) located at tools/ebay-manager. Investigate the root cause AND apply the code fix.

BUG: The Rakuten supplier-inventory check reports an item as OUT OF STOCK when it is actually IN STOCK, producing false fulfillment-risk flags (direct financial harm).

CONCRETE FAILING CASE:
- eBay listing ebay_item_id = 357039030883, supplier source_url = https://item.rakuten.co.jp/tuzukiya/m20-5806/ (Mitutoyo KA-212 counter).
- The user confirms this item is currently IN STOCK (販売中) in a real browser, but the system returns "unavailable" (OutOfStock).

EVIDENCE ALREADY COLLECTED (verify against the saved file):
- The raw HTTP response (httpx, NO JavaScript) for that URL contains EXACTLY ONE availability microdata, located in the MAIN product offer block (immediately adjacent to `<meta itemprop="price" content="69883">` and `<meta itemprop="offerCount" content="1">`): `<meta itemprop="availability" content="http://schema.org/OutOfStock">`. There is NO `schema.org/InStock` anywhere in the raw HTML, and NO "かごに追加" cart text.
- Saved raw HTML for inspection: data/tmp/rakuten_357_raw.html
- CONCLUSION: the server-rendered raw HTML schema.org availability microdata is a STALE / placeholder value (OutOfStock) that does NOT reflect true availability for this shop's pages; correct availability appears only after JavaScript rendering. The earlier design assumption in .company/engineering/migration/codex-ec-direct-url-design.md (that raw-HTML schema.org microdata is the most reliable Rakuten signal) is WRONG for this page.

WHERE THE BUG LIVES:
- monitor/database.py: site_configs for Rakuten (convert_url='ebayRT_', url_keyword='item.rakuten') have in_stock_text1 = 'itemprop="availability" content="http://schema.org/InStock"' and sold_out_text = 'itemprop="availability" content="http://schema.org/OutOfStock"'. These values are set in BOTH DEFAULT_SITE_CONFIGS and migration v55. Amazon (ebayAM_) uses id="add-to-cart-button" / name="submit.add-to-cart" / 現在在庫切れ.
- monitor/scrapers.py: _check_with_httpx() substring-matches the RAW httpx HTML. Finding only the OutOfStock string (sold_out) and no in_stock / no_page match, _detect_status_single() returns "unavailable" DEFINITIVELY, so the Playwright fallback (_check_urls_batch_async / check_items_batch) is NEVER invoked for this item. That is the root cause of the false negative.

TASK (investigate and FIX — apply the changes):
1. Make Rakuten availability detection robust to stale raw-HTML schema.org microdata. A raw-httpx OutOfStock signal for Rakuten must NOT be treated as a definitive "unavailable"; the check must escalate to JavaScript-rendered (Playwright) HTML and trust the rendered result instead. You have Playwright installed — render https://item.rakuten.co.jp/tuzukiya/m20-5806/ to confirm the correct post-JS signal (e.g. corrected microdata and/or an enabled cart button) and base the fix on that. If your sandbox blocks network and you cannot render, implement the escalation logic anyway (raw OutOfStock for Rakuten => defer to Playwright, never definitive from httpx) and state the limitation.
2. Do NOT break detection of genuinely out-of-stock Rakuten items, and do not regress Amazon or other sites.
3. Keep changes minimal and idempotent. Follow the repo rules in CLAUDE.md and .claude/rules/: SKU rules (listing identity is ebay_item_id, never SKU as a key); migration idempotency (ALTER inside try/except sqlite3.OperationalError, bump PRAGMA user_version only after success, never edit an already-shipped migration in place — add a new vNN); Q0 no silent skip (log any exclusions).
4. Update / extend tests/test_w183_ec_direct_url.py to reflect the new contract and add a regression test for this false-OutOfStock case (a raw-HTML page whose ONLY availability microdata is OutOfStock must NOT be reported as definitively unavailable from the httpx layer for Rakuten — it should escalate so Playwright decides).
5. If you change site_configs availability strings, update BOTH DEFAULT_SITE_CONFIGS and a NEW idempotent migration (do not mutate v55 in place), and keep them consistent (cascade-update rule).

Report concisely: root cause, the exact files and functions you changed, and how you verified that the in-stock item (357039030883 / tuzukiya/m20-5806) now resolves to available.
codex
I’ll inspect the repo rules and the Rakuten detection path first, then patch the smallest surface that forces stale raw Rakuten OutOfStock through the Playwright path. After that I’ll run the focused tests and, if network is available, render the live Rakuten page to confirm the post-JS signal.
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'Get-Content -Path CLAUDE.md' in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-ChildItem -Path .claude\\rules -File | Select-Object -ExpandProperty FullName" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'rg -n "Rakuten|ebayRT_|DEFAULT_SITE_CONFIGS|v55|availability|_check_with_httpx|_detect_status_single|check_items_batch|_check_urls_batch_async" monitor tests/test_w183_ec_direct_url.py' in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 718ms:
# eBay Manager (tools/ebay-manager) 蝗ｺ譛峨Ν繝ｼ繝ｫ

縺薙・繝輔ぃ繧､繝ｫ縺ｯ `tools/ebay-manager/**` 驟堺ｸ九ｒ邱ｨ髮・☆繧矩圀縺ｫ Claude Code 縺瑚・蜍・load 縺吶ｋ subdir CLAUDE.md (蜈ｬ蠑・lazy-load 讖溯・)縲・eBay 蜃ｺ蜩・/ 髢｢遞・/ 騾∵侭 / 蝠・刀繝ｩ繝ｳ繧ｯ遲峨・ **隕丞宛讌ｭ蜍・rules** 繧・1 繝輔ぃ繧､繝ｫ縺ｫ髮・ｴ・(Cal Rueb red flag #3 蟇ｾ蠢・縲・
讓ｪ譁ｭ rule (Karpathy 4 蜴溷援 / DB migration 蜀ｪ遲画ｧ / silent skip 遖∵ｭ｢ / 莉募・蜈亥愛螳・ 縺ｯ `.claude/rules/` 驟堺ｸ九ｒ蜿ら・縲・
---

## 蜃ｺ蜩√Ν繝ｼ繝ｫ

### 萓｡譬ｼ邂｡逅・
- USD 蝓ｺ譛ｬ騾夊ｲｨ縲・PY 謠帷ｮ励′蠢・ｦ√↑蝣ｴ蜷医・險倬鹸譎ゅ↓轤ｺ譖ｿ繝ｬ繝ｼ繝医ｒ譏手ｨ・- **邀ｳ蝗ｽ蜷代￠縺ｯ DDP 蜃ｺ闕ｷ = 髢｢遞弱・螢ｲ荳ｻ雋諡・*縲４ection 232 豢ｾ逕溷刀 25% 逶ｴ謦・〒襍､蟄怜喧繝ｪ繧ｹ繧ｯ
- 雋ｩ螢ｲ萓｡譬ｼ縺ｫ **髢｢遞・buffer 蠢・・* (隧ｳ邏ｰ縺ｯ譛ｬ繝輔ぃ繧､繝ｫ荳九・縲轡DP 蜃ｺ闕ｷ / Section 232縲行ection)
- 蛻ｩ逶顔紫繧貞ｿ・★險倬鹸 (莉募・萓｡譬ｼ / 雋ｩ螢ｲ萓｡譬ｼ / 蛻ｩ逶顔紫)

### 騾∵侭繝ｫ繝ｼ繝ｫ (US 霆ｸ蟾ｮ蛻・ｼ・+ 4 蛹ｺ蛻・primary_market)

隧ｳ邏ｰ: `reference_shipping_tariff_logic.md` v1.0 (2026-05-01 蛻ｶ螳壹∵･ｭ蜍吩ｻ墓ｧ倥・讓ｩ螽・.

- **險育ｮ怜ｼ・*: `蜷・嵜陦ｨ遉ｺ騾∵侭 = (蜷・嵜螳滄∵侭 - US 螳滄∵侭) + (DDP 髢｢遞・if 邀ｳ蝗ｽ蜷代￠)`
- **DDP 髢｢遞・*: 邀ｳ蝗ｽ蜷代￠縺ｮ縺ｿ騾∵侭谺・↓荳贋ｹ励○ (蝠・刀萓｡譬ｼ縺ｫ縺ｯ蜷ｫ繧√↑縺・√◆縺縺・US_only 蛹ｺ蛻・・蝠・刀萓｡譬ｼ蛹・性)
- **4 蛹ｺ蛻・*: US_only / mixed_global / global_only / unknown (Terapeak 365 譌･ sold 縺ｧ listing 蜊倅ｽ榊愛螳壹」2.0 / W110(2) 2026-05-09)
- **證ｫ螳夐°逕ｨ**: 4 蛹ｺ蛻・挨螳溯｣・・蛟呵｣・C/D 騾ｲ陦御ｸｭ縲∫樟陦・`ebay_lister.py` L222 縺ｯ `price * 0.20` (ﾎｲ fix `<ShippingServiceCostOverrideList>` 縺ｧ BP override 邨檎罰)
- XML 蠢・郁ｦ∫ｴ: `<ShippingType>Flat</ShippingType>` 邯ｭ謖・- ShipToLocations: 蜈ｨ 4 蛹ｺ蛻・→繧ょ・蝗ｽ蠢・・(eBay 莉墓ｧ倥〒 US 髯､螟紋ｸ榊庄)
- 讀懆ｨｼ: eBay GetItem API 縺ｧ螳溷渚譏遒ｺ隱・(pytest 縺縺代〒螳御ｺ・ｮ｣險遖∵ｭ｢ / Q1)

### Country of Origin / Manufacturer 縺ｮ layer 蛻・屬

- **eBay 蜃ｺ蜩∵枚 (Title / HTML description / Item Specifics)**: Country of Origin / Country of Manufacture / Manufacturer 縺ｮ **縺・★繧後ｂ險倩ｼ臥ｦ∵ｭ｢** (US Customs 縺悟次逕｣蝗ｽ繧貞・險育ｮ励☆繧区ｹ諡繧剃ｸ弱∴縺ｪ縺・
- **騾夐未譖ｸ鬘・(FedEx Invoice / HS code)**: Manufacturer = **譌･譛ｬ莉｣逅・ｺ・* (隧ｳ邏ｰ縺ｯ譛ｬ繝輔ぃ繧､繝ｫ荳九・縲碁夐未繝ｫ繝ｼ繝ｫ縲行ection)
- 豺ｷ蜷御ｺ区腐髦ｲ豁｢: eBay XML builder 縺ｯ Manufacturer 谺・ｒ **遨ｺ譁・ｭ怜・縺ｧ騾∝・**

### eBay XML 蛻ｶ邏・(蜃ｺ蜩∝燕 閾ｪ蜍・validate)

- **Title 竕､ 80 譁・ｭ・* (Mojibake 蠕梧枚蟄玲焚 / 繝舌う繝域焚豕ｨ諢・
- **Item Specifics 蜷・､ 竕､ 65 譁・ｭ・*縲・*Brand / MPN 蠢・・* (Listing Quality 逶ｴ謦・
- 荳ｭ蜿､蜩・(S/A/B/C/D/PO/As-Is) 縺ｯ **ConditionDescription 蠢・・* (縺薙ｌ辟｡縺・→ defect 蠅・
- VeRO 隧ｲ蠖薙ヶ繝ｩ繝ｳ繝峨・ `data/vero_brands.json` 縺ｧ莠句燕蛻､螳・
### SKU 隕冗ｴ・(逕ｨ騾斐・ 2 縺､縺ｮ縺ｿ縲√く繝ｼ菴ｿ逕ｨ遖∵ｭ｢)

蜃ｺ蜈ｸ: 2026-04-30 SKU 隕冗ｴ・隼險ゅりｩｳ邏ｰ邨檎ｷｯ: `feedback_sku_misuse_repeat_offense.md` / `.claude/rules/sku-rules.md`

| 蝨ｨ蠎ｫ遞ｮ蛻･ | SKU 蠖｢蠑・| 諤ｧ雉ｪ |
|---|---|---|
| **譛牙惠蠎ｫ** | `stock**` 縺ｧ蟋九∪繧区枚蟄怜・ (stock:01 / stock1 / stock 遲峨∬｡ｨ險俶昭繧後≠繧・ | **蜷御ｸ SKU 繧貞､壽焚 listing 縺梧戟縺､縺ｮ縺梧ｭ｣蟶ｸ** (蝨ｨ蠎ｫ遞ｮ蛻･繝輔Λ繧ｰ縺ｧ縺ゅ▲縺ｦ髮・ｴ・く繝ｼ縺ｧ縺ｯ縺ｪ縺・ょ惠蠎ｫ謨ｰ繝ｻ隴伜挨縺ｯ `ebay_item_id` 蜊倅ｽ阪ヾKU 縺ｧ譚溘・縺ｪ縺・ |
| **辟｡蝨ｨ蠎ｫ** | `ebay**_*****` (萓・ `ebayyh_p1221413657` / `ebayme_m32400850054`) | SKU 螟画鋤 竊・莉募・蜈亥呵｣・URL |

**SKU 縺ｮ逕ｨ騾斐・ 2 縺､縺縺・* (縺薙ｌ莉･螟悶〒 SKU 繧剃ｽｿ縺・・縺ｯ邨ｶ蟇ｾ遖∵ｭ｢):
1. 譛牙惠蠎ｫ / 辟｡蝨ｨ蠎ｫ 縺ｮ蛻､螳・(prefix 縺ｧ蛻､蛻･)
2. 辟｡蝨ｨ蠎ｫ縺ｮ蝣ｴ蜷医ヾKU 螟画鋤 竊・莉募・蜈亥呵｣・URL 繧貞ｾ励ｋ

**邨ｶ蟇ｾ遖∵ｭ｢** (驕募渚 = 蜩∬ｳｪ莠区腐縲りｩｳ邏ｰ: `.claude/rules/sku-rules.md`):
- 笶・SKU 繧・listing 荳諢上く繝ｼ (荳ｻ繧ｭ繝ｼ / 驥崎､・､懷・繧ｭ繝ｼ) 縺ｨ縺励※菴ｿ縺・- 笶・`WHERE sku=?` 縺ｧ 1 listing 迚ｹ螳・/ `WHERE sku IN (...)` 隍・焚謚ｽ蜃ｺ / `GROUP BY sku` 髮・ｨ・/ `UNIQUE(sku)` 蛻ｶ邏・- 笶・`JOIN ON a.sku = b.sku` / `dict[sku] = listing` / `set(skus)` 驥崎､・賜髯､
- 笶・縲悟酔 SKU 縺瑚､・焚 listing 縺ｫ蟄伜惠 = 逡ｰ蟶ｸ縲阪→蛻､螳壹☆繧・
**listing 隴伜挨縺ｯ蠢・★ `ebay_item_id`** 繧剃ｽｿ縺・(eBay 蛛ｴ縺ｮ荳諢・ID縲［igration v26 縺ｧ listing 蜊倅ｽ榊喧貂・縲・
**蛻､螳・OK 縺ｪ菴ｿ縺・婿** (2026-04-30 user 蜈ｬ隱阪∽ｸ願ｨ倡畑騾・2 縺､縺ｫ髯仙ｮ・:
- 譛・辟｡蝨ｨ蠎ｫ蛻､螳・ `sku.startswith("stock")` / `sku.startswith("ebay")` / SQL `WHERE sku LIKE 'stock%' OR sku LIKE 'ebay%'`
- 辟｡蝨ｨ蠎ｫ縺ｮ莉募・蜈・URL 螟画鋤: `sku_mapping_manager.generate_url(sku)`

繝ｭ繧ｰ / pending_market_changes 騾夂衍 = SKU + 繧ｿ繧､繝医Ν菴ｵ險・(蛹ｺ蛻･螳ｹ譏灘喧縺ｮ縺溘ａ)縲・
### 蝠・刀縺ｮ蜻ｼ遘ｰ (UI / 蟇ｾ user 繝｡繝・そ繝ｼ繧ｸ)

- 笶・縲茎tock:01 縺・..縲阪憩bayyh_p1221413657 繧貞・蜩√・竊・SKU 縺ｯ蝠・刀隴伜挨縺ｫ菴ｿ縺医↑縺・(譛牙惠蠎ｫ縺ｯ蜷・SKU 蜈ｱ譛峨′豁｣蟶ｸ縲∫┌蝨ｨ蠎ｫ縺ｯ URL 縺昴・繧ゅ・縺ｧ蜀鈴聞)
- 笨・縲窟udio-Technica ATH-CKS330NC 縺・..縲・竊・`ebay_listings.title` 縺ｧ蜻ｼ縺ｶ
- 蜷後ち繧､繝医Ν listing 縺瑚､・焚蟄伜惠縺吶ｋ蝣ｴ蜷・= `title (ebay_item_id 譛ｫ蟆ｾ 4 譯・` 縺ｧ蛹ｺ蛻･

### eBay 繝昴Μ繧ｷ繝ｼ驕ｵ螳・(蟶ｸ譎ょ盾辣ｧ)

- 蜃ｺ蜩√・繝ｪ繧ｷ繝ｼ / 遖∵ｭ｢蝠・刀 / VeRO 繧貞・蜩∝燕縺ｫ蠢・★蜿ら・
- 繧ｫ繧ｹ繧ｿ繝槭・繧ｵ繝昴・繝・ 霑比ｿ｡ **24h 莉･蜀・*
- **Defect 邇・怙蜆ｪ蜈・* (繧｢繧ｫ繧ｦ繝ｳ繝亥●豁｢繝ｪ繧ｹ繧ｯ逶ｴ邨・
- Promoted Listings = 2% (W21 讓呎ｺ・

### 讀懆ｨｼ (Q1 DoD 11 繧ｹ繝・ャ繝励↓蠕薙≧)

- 蜃ｺ蜩・/ 萓｡譬ｼ / 騾∵侭 / 譁・ｨ螟画峩蠕後・ **eBay GetItem 縺ｧ螳溷渚譏遒ｺ隱・* 蠢・・- pytest PASS 縺ｮ縺ｿ縺ｧ螳御ｺ・ｮ｣險縺ｯ K3 驕募渚 = 遖∵ｭ｢
- Streamlit 蜀崎ｵｷ蜍・+ Playwright MCP 縺ｧ 1 蠕蠕ｩ E2E + DB 逶ｴ謗･繧ｯ繧ｨ繝ｪ

---

## 騾夐未繝ｫ繝ｼ繝ｫ (FedEx / UPS / DHL 騾夐未譖ｸ鬘槫屓遲・

蜃ｺ蜈ｸ: 2026-04-24 FedEx TRK#xxxxxxxxxxxx 繝峨Λ繝輔ヨ莠区腐 (鬥ｬ鮖ｿ豁｣逶ｴ險倩ｿｰ縺ｧ user 蜴ｳ驥肴ｳｨ諢・

**譬ｸ蠢・*: 蝠・･ｭ霆｢螢ｲ / 荳ｭ蝗ｽ蜴溽肇 / 鬮倬未遞主ｯｾ雎｡繧・**荳榊ｿ・ｦ√↓閾ｪ隱阪＠縺ｪ縺・*縲ょ屓遲斐・莠句ｮ溘□縺梧姶逡･逧・↓縲・
### Manufacturer Name

- **隨ｬ荳驕ｸ謚・*: 譌･譛ｬ蝗ｽ蜀・・豁｣隕丈ｻ｣逅・ｺ・(SKT譬ｪ蠑丈ｼ夂､ｾ / Razer / Le Creuset Japan K.K. 遲・
- **NG**: 荳ｭ蝗ｽ繝ｻ譚ｱ蜊励い繧ｸ繧｢譛ｬ遉ｾ繧堤ｬｬ荳驕ｸ謚槭↓縺励↑縺・ょ次逕｣蝗ｽ諠・ｱ繧剃ｸ榊ｿ・ｦ√↓蠑ｷ隱ｿ縺励↑縺・- **萓句､・*: 繝悶Λ繝ｳ繝画悽遉ｾ縺檎ｱｳ蝗ｽ蜀・(Razer Inc. 邀ｳ蝗ｽ HQ 遲・ 縺ｪ繧牙ゅ・→險倩ｼ・OK

### End Use

- **隨ｬ荳驕ｸ謚・*: 蝠・刀縺ｮ **螳溽畑騾・* 縺ｮ縺ｿ (萓・ e-reader 竊・"Personal e-book reading device")
- **NG**: 雋ｩ螢ｲ繝√Ε繝阪Ν / `resale` / `commercial` / `eBay` 繧呈嶌縺九↑縺・- 迢吶＞: FedEx 縺ｮ "End Use" 縺ｯ縲御ｽ輔↓菴ｿ縺・ｂ縺ｮ縺九阪ｒ蝠上≧縺ｦ縺翫ｊ縲∝膚蜿門ｼ募ｽ｢諷九ｒ豎ゅａ縺ｦ縺・↑縺・
### 邏譚占ｨ倩ｿｰ (驩・蕎繝ｻ繧｢繝ｫ繝滄未遞主ｯｾ遲・

- 繧｢繝ｫ繝溘・驩・ｒ蜷ｫ縺ｾ縺ｪ縺・膚蜩√・ **譏守､ｺ逧・↓ "No aluminum or steel parts"** 縺ｨ譖ｸ縺・(Section 232 豢ｾ逕溷刀縺ｮ蟇ｾ雎｡螟門ｮ｣險縲∬ｩｳ邏ｰ縺ｯ譛ｬ繝輔ぃ繧､繝ｫ荳九・縲轡DP 蜃ｺ闕ｷ / Section 232縲行ection)

### 螳壼梛蜿･ (譛ｫ蟆ｾ蠢・・

```
The shipper is a retailer and is not the manufacturer.
```

竊・逋ｺ騾∽ｺｺ = 陬ｽ騾蜈・〒縺ｪ縺・％縺ｨ繧呈・險倥∵ｳ慕噪遶句ｴ縺ｮ蛻・ｊ蛻・￠

### HTS 繧ｳ繝ｼ繝・
- 譬ｹ諡 Ruling (萓・ NY N215220) 繧定・豕ｨ縺ｧ蠑慕畑
- 譛邨ょ愛譁ｭ縺ｯ迴ｾ蝨ｰ騾夐未螢ｫ縺ｫ蟋斐・繧・ `Please verify with your customs broker.`

### 驕狗畑

- 繝峨Λ繝輔ヨ縺ｯ蠢・★ `.company/daily-operations/fedex-drafts/YYYY-MM-DD-TRK_xxx.md` 縺ｫ菫晏ｭ・- 蝠・刀蜀咏悄縺ｯ `*-photos/` 驟堺ｸ九↓ DL 縺励※豺ｻ莉俶ｺ門ｙ
- **v2 繝ｬ繝薙Η繝ｼ蠢・・*: 莉･荳・2 邨瑚ｷｯ縺ｧ驕主悉蠢懃ｭ斐→辣ｧ蜷・  - Gmail (MCP or web): `to:paperwork@fedex.com OR to:customs@fedex.com` 縺ｧ驕主悉 1 蟷ｴ讀懃ｴ｢
  - 0 莉ｶ譎・ `.company/daily-operations/fedex-drafts/` 驟堺ｸ九・逶ｴ霑・5 莉ｶ繧・grep

---

## DDP 蜃ｺ闕ｷ / Section 232

蜃ｺ蜈ｸ: 2026-04-25 TRK#xxxxxxxxxxxx (Netsuken NV-25 / $798) 縺ｧ Section 232 豢ｾ逕溷刀 25% 髢｢遞・~$200 螢ｲ荳ｻ雋諡・′蛻､譏・
### DDP 繝ｫ繝ｼ繝ｫ (邀ｳ蝗ｽ蜷代￠蜴溷援)

- 邀ｳ蝗ｽ蜷代￠逋ｺ騾・= **DDP (Delivered Duty Paid)** 驕狗畑
- 騾夐未譎ゅ・ **蜈ｨ髢｢遞弱・遞朱≡繝ｻFedEx Disbursement Fee 縺ｯ螢ｲ荳ｻ雋諡・* (TOYOTASUMI)
- buyer 縺ｯ霑ｽ蜉隲区ｱゅ↑縺・(Negative feedback 繝ｪ繧ｹ繧ｯ縺ｪ縺励√◆縺縺・**蛻ｩ逶顔峩謦・*)
- DDU 縺ｨ縺ｮ豺ｷ蜷檎ｦ∵ｭ｢: DDU=諠・ｱ謠蝉ｾ帙・縺ｿ / DDP=逶ｴ謗･謳咲寢

### 雋ｩ螢ｲ萓｡譬ｼ險ｭ險亥ｼ・
```
雋ｩ螢ｲ萓｡譬ｼ = 蜴滉ｾ｡ + 蝗ｽ髫幃∵侭 + 髢｢遞・buffer + PLS 2% + eBay fee + 蛻ｩ逶・                          笏・譛菴・15% (IEEPA reciprocal)
                          笏・Section 232: I-A=50% (邏秘≡螻・ / I-B=25% (豢ｾ逕溷刀) / III=15% (transitional)
```

### Section 232 隧ｲ蠖・HS 繝ｪ繧ｹ繝・(3 髫主ｱ､縲？S 縺ｧ蛻､螳・

#### Annex I-A (50%縲，hapter 72-74/76 邏秘≡螻櫁｣ｽ蜩√・*驥埼㍼髢ｾ蛟､縺ｪ縺・閾ｪ蜍戊ｪｲ遞・*)

- HS 73xx (驩・蕎陬ｽ蜩・= 繧ｹ繝医・繝・/ 骰・/ 繝輔Λ繧､繝代Φ / 菫晄ｸｩ繧ｸ繝｣繝ｼ)
- HS 76xx (繧｢繝ｫ繝溯｣ｽ蜩・
- HS 74xx (驫・｣ｽ蜩・

#### Annex I-B (25% 豢ｾ逕溷刀縲，hapter 84-87縲・*metal weight 竕･15% 縺ｧ隱ｲ遞・*)

- HS 8516.60.40 (髮ｻ豌礼ｊ鬟ｯ蝎ｨ / 繧ｪ繝ｼ繝悶Φ) 窶・Netsuken NV-25 隧ｲ蠖・- HS 8418.10/21/29/30/40 (蜀ｷ阡ｵ繝ｻ蜀ｷ蜃・
- HS 8501.64 (迚ｹ螳壹Δ繝ｼ繧ｿ繝ｼ) / 8504.31-33 (螟牙悸蝎ｨ)
- HS 8415 (繧ｨ繧｢繧ｳ繝ｳ) / 8517.71 / 8544.42-49 (髮ｻ邱・
- HS 8708.xx (閾ｪ蜍戊ｻ企Κ蜩・ / 8716.xx (繝医Ξ繝ｼ繝ｩ繝ｼ)
- 驥埼㍼邂怜ｮ壽ｹ諡繧定ｨ倬鹸 (customs_requests 縺ｫ WORKSHEET 豺ｻ莉・

#### Annex III (15% transitional縲×2027-12-31)

- HS 8421.29 (豸ｲ菴薙ｍ驕・ / 8424.89.90 / 8428.32-70 (繧ｳ繝ｳ繝吶い / 逕｣讌ｭ繝ｭ繝懊ャ繝・

### IEEPA 驥崎､・屓驕ｿ

- **Section 232 隧ｲ蠖灘刀縺ｯ IEEPA reciprocal 15% exempt** (莠碁㍾隱ｲ遞朱亟豁｢)
- 隧ｲ蠖灘愛螳壼ｾ後・ IEEPA 險育ｮ鈴勁螟悶ヾection 232 縺ｮ縺ｿ驕ｩ逕ｨ
- 笞・・**萓句､・*: semiconductors / automotive parts (HS 8708 遲・ 縺ｯ IEEPA exempt 蟇ｾ雎｡螟悶！EEPA 驥崎､・←逕ｨ繝ｪ繧ｹ繧ｯ縺ゅｊ

### 蜃ｺ蜩∝愛譁ｭ繝ｫ繝ｼ繝ｫ

- **High-value 蝠・刀 ($500+)**: 蜃ｺ蜩∝燕縺ｫ customs broker classification 遒ｺ隱肴耳螂ｨ
- **蜷悟梛逡ｪ繝ｪ繝斐・繝亥・蜩・*: `customs_requests` / freee 縺ｮ隧ｲ蠖捺｡井ｻｶ繧貞盾辣ｧ縲・未遞主ｮ溽ｸｾ繧剃ｾ｡譬ｼ蜿肴丐
- **襍､蟄玲｡井ｻｶ蛹門愛螳・*: 邊怜茜邇・30% 譛ｪ貅 + Section 232 隧ｲ蠖・= **user 縺ｫ騾夂衍縺励※謇ｿ隱榊ｾ・■** (assistant 閾ｪ蜍・BLOCK 縺励↑縺・「ser 讖滉ｼ壽錐螟ｱ繝ｪ繧ｹ繧ｯ蝗樣∩)

### 隧ｳ邏ｰ KB 蜿ら・

`.company/ebay-knowledge/topics/section_232_tariff_2026_04.md` (2026-04-06 謾ｹ險ゅ、nnex I-A/I-B/II/III/IV 蜈ｨ HTS 繝ｪ繧ｹ繝医∬ｨ育ｮ励Ρ繝ｼ繧ｯ繝輔Ο繝ｼ縲√こ繝ｼ繧ｹ繧ｹ繧ｿ繝・ぅ蜿朱鹸)

笞・・**譛邨ら｢ｺ隱・ 2026-04-30 / 鬮・value 蝠・刀 ($500+) 蜃ｺ蜩∵凾縺ｯ CBP CSMS 縺ｧ蜀咲｢ｺ隱榊ｿ・・* (CBP CSMS 縺ｯ 2-4 騾ｱ縺ｧ謾ｹ險ゅ・霑ｽ陬懊′蜃ｺ繧・

---

## 繧ｳ繝ｳ繝・ぅ繧ｷ繝ｧ繝ｳ繝ｩ繝ｳ繧ｯ 8 谿ｵ髫・
蜃ｺ蜈ｸ: 蜈ｨ eBay 蜃ｺ蜩√〒荳雋ｫ驕ｩ逕ｨ (W9 individual-listing 縺ｧ Claude 閾ｪ蜍墓耳螳・縲ょ､冶ｦｳ ﾃ・蜍穂ｽ懃｢ｺ隱阪・ 2 霆ｸ邨ｱ蜷医・
### 8 谿ｵ髫惹ｽ鍋ｳｻ

| Rank | EN | JP | eBay Cond ID | 驕ｩ逕ｨ |
|---|---|---|---|---|
| N | New (Unopened) | 譁ｰ蜩√・譛ｪ髢句ｰ・| 1000 | 繧ｷ繝･繝ｪ繝ｳ繧ｯ / 蟾･蝣ｴ蜃ｺ闕ｷ |
| S | Like New | 譁ｰ蜩∝酔讒・| 1500 (窶ｻ) | 髢句ｰ∵ｸ医∩縺縺梧悴菴ｿ逕ｨ縲∽ｽｿ逕ｨ逞輔↑縺・|

窶ｻ **Cond ID 1500 縺ｯ繧ｫ繝・ざ繝ｪ萓晏ｭ・* (Consumer Electronics > Portable Audio & Headphones 遲峨〒蛻ｶ髯・縲・etCategoryFeatures / Taxonomy API 縺ｧ莠句燕遒ｺ隱阪∽ｸ榊庄繧ｫ繝・ざ繝ｪ縺ｧ縺ｯ **1000 fallback** (譚｡莉ｶ貅縺溘☆蝣ｴ蜷・ or **3000 + "Open box" description** 縺ｫ髯肴ｼ縲ょ・蜩∵凾 VerifyAdd 縺ｧ蜀肴､懆ｨｼ蠢・・(Q0 繧ｵ繧､繝ｬ繝ｳ繝医せ繧ｭ繝・・髦ｲ豁｢)縲・
| A | Excellent | 鄒主刀繝ｻ蜍穂ｽ懃｢ｺ隱肴ｸ・| 3000 | 蟆上＆縺ｪ菴ｿ逕ｨ逞輔∝・讖溯・蜍穂ｽ・|
| B | Good | 荳ｦ蜩√・蜍穂ｽ懃｢ｺ隱肴ｸ・| 3000 | 逶ｮ遶九▽菴ｿ逕ｨ逞輔∝・讖溯・蜍穂ｽ・|
| C | Fair | 菴ｿ逕ｨ諢溘≠繧翫・蜍穂ｽ懃｢ｺ隱肴ｸ・| 3000 | 菴ｿ逕ｨ諢溷ｼｷ縺・∝・讖溯・蜍穂ｽ・|
| D | Issues | 髮｣縺ゅｊ繝ｻ蜍穂ｽ懃｢ｺ隱肴ｸ・| 3000 | 螟冶ｦｳ/讖溯・縺ｫ蝠城｡後∝虚菴懊☆繧九′髯仙ｮ・|
| PO | Power-On Only | 騾夐崕縺ｮ縺ｿ縲∝虚菴懈悴遒ｺ隱・| 3000 | 髮ｻ貅・ON 遒ｺ隱阪□縺・|
| As-Is | As-Is | 譛ｪ遒ｺ隱・or 驛ｨ蜩∝叙繧・| 7000 | 辟｡菫晁ｨｼ雋ｩ螢ｲ縲・*逅・罰蠢・・* |

### N vs S 蛻､蛻･

- 笨・螳ｶ髮ｻ驥剰ｲｩ蠎励・譁ｰ蜩√す繝･繝ｪ繝ｳ繧ｯ蜩・竊・**N**
- 笶・繝・ャ繝峨せ繝医ャ繧ｯ / 譛ｪ菴ｿ逕ｨ縺縺御ｿ晉ｮ｡蟷ｴ謨ｰ髟ｷ縺・竊・**S 謗ｨ螂ｨ**
- 笶・蛟倶ｺｺ蜃ｺ蜩√・縲梧眠蜩∵悴菴ｿ逕ｨ縲・髢句ｰ∫藍遒ｺ隱榊峅髮｣) 竊・**S 謗ｨ螂ｨ**
- **VeRO 繝ｪ繧ｹ繧ｯ** (Apple / Nintendo 遲・: 髱樊ｭ｣隕上Ν繝ｼ繝亥刀縺ｯ **S 莉･荳・* 縺悟ｮ牙・

### Claude 閾ｪ蜍墓耳螳・(莉募・蜈域律譛ｬ隱槭く繝ｼ繝ｯ繝ｼ繝・

| 莉募・蜈郁｡ｨ險・| 謗ｨ螳壹Λ繝ｳ繧ｯ |
|---|---|
| 縲梧眠蜩√阪梧悴髢句ｰ√阪後す繝･繝ｪ繝ｳ繧ｯ莉倥″縲・| **N** |
| 縲梧眠蜩∝酔讒倥阪梧悴菴ｿ逕ｨ縲阪碁幕蟆∝刀縲・| **S** |
| 縲檎ｾ主刀縲阪檎ｾ主刀縺ｫ霑代＞縲・| **A** |
| 縲瑚憶蜩√阪御ｸｦ蜩√阪梧勸騾壹・| **B** |
| 縲御ｽｿ逕ｨ諢溘≠繧翫・| **C** |
| 縲悟す縺ゅｊ縲阪碁屮縺ゅｊ縲阪瑚ｨｳ縺ゅｊ縲・| **D** |
| 縲碁夐崕遒ｺ隱阪・縺ｿ縲阪碁夐崕縺ｮ縺ｿ縲・| **PO** |
| 縲悟虚菴懈悴遒ｺ隱阪阪後ず繝｣繝ｳ繧ｯ縲阪碁Κ蜩∝叙繧翫阪梧腐髫懊・| **As-Is** |

### 繝悶Λ繝ｳ繝牙挨迚ｹ萓・
- **PIONEER Lonesome Carboy 遲牙ｹｴ莉｣迚ｩ AV**: 蜍穂ｽ懃｢ｺ隱榊ｿ・医√ず繝｣繝ｳ繧ｯ蜊ｳ As-Is
- **KEYENCE 繧ｻ繝ｳ繧ｵ繝ｼ蜊倅ｽ・*: 繧ｸ繝｣繝ｳ繧ｯ縺ｧ繧ゅユ繧ｹ繝亥燕謠舌〒 B/C 謗ｨ螳壼庄
- **譛ｬ rule 蜀・〒縺ｯ 2 萓九・縺ｿ謚懃ｲ・*縲７eRO 繝悶Λ繝ｳ繝・(Apple / Nintendo / SONY 遲・ 繧・Audio/AV/逕｣讌ｭ讖溷勣縺ｮ蛻､螳壹・蠢・★ `feedback_condition_by_brand.md` 繧貞盾辣ｧ縲よ悴蜿手ｼ峨ヶ繝ｩ繝ｳ繝峨・ N/S 蛻､螳壼燕縺ｫ隧ｲ蠖・memory check 蠢・・
### Quick Notes (description aside 蜀帝ｭ縲ヽank Definition Table 縺ｨ菴ｵ險ｭ)

- **A/B/C/D**: 蜈ｷ菴鍋噪蜍穂ｽ懃｢ｺ隱咲ｵ先棡縲ゆｾ・ `Tested and confirmed working (2026-04). Power on/off: OK / Audio: OK / Bluetooth: OK`
- **PO**: `Powered on, but full function not verified`
- **As-Is**: **蠢・★逅・罰譏守､ｺ**縲ゆｾ・ `No AC adapter for testing` / `PCB burn damage` / `Heavy contamination prevented testing`

### As-Is 蜃ｺ蜩√・ XML 蠢・郁ｦ∽ｻｶ

- eBay XML `<ConditionDescription>` 縺ｫ Quick Notes 縺ｮ As-Is 逅・罰繧・**蠢・★霆｢險・*
- **65 蟄嶺ｻ･蜀・* (eBay 蛻ｶ邏・ / 闍ｱ譁・/ `As-Is 窶・<reason>` 蠖｢蠑・- 谺關ｽ譎ゅ・ VerifyAdd 隴ｦ蜻翫□縺碁壹ｋ 竊・buyer 邏帑ｺ峨〒 **Defect 遒ｺ螳壹Μ繧ｹ繧ｯ** (繧｢繧ｫ繧ｦ繝ｳ繝亥●豁｢逶ｴ邨・

### 繧ｿ繧､繝医Ν / description

- 繧ｿ繧､繝医Ν縺ｫ縺ｯ Rank 陦ｨ險・**縺励↑縺・* (80 蟄怜宛髯仙悸霑ｫ髦ｲ豁｢)
- description aside 蜀帝ｭ縺ｫ **Rank Definition Table** 蜷ｫ繧√ｋ
- 繝・Φ繝励Ξ繝ｼ繝・ `.company/ebay-knowledge/topics/listing-description-template.md`

---

## 髢｢騾｣ rule (讓ｪ譁ｭ)

always-load (`.claude/rules/` 驟堺ｸ・:
- `karpathy-principles.md` 窶・Karpathy 4 蜴溷援 (K0-K3 蟶ｸ譎る←逕ｨ)
- `db-migration-rules.md` 窶・DB 蜀ｪ遲画ｧ (try/except OperationalError縲．ROP one-shot 蛹悶・4h retrospective review)
- `silent-skip-prevention.md` 窶・Q0 繧ｵ繧､繝ｬ繝ｳ繝医せ繧ｭ繝・・ / 蛛ｽ陬・・蜉・/ 騾・∩菫ｮ豁｣ 邨ｶ蟇ｾ遖∵ｭ｢

on-demand snippet (`.claude/rule-snippets/` 驟堺ｸ九・026-05-21 hybrid 蛹・:
- `supplier-matching-rules.md` 窶・莉募・蜈亥呵｣懷愛螳・(match_score < 60 髯､螟悶∝挨 SKU 讖滉ｼ壹√ず繝｣繝ｳ繧ｯ陦ｨ險伜愛蛻･)

2026-05-28T14:26:54.907966Z ERROR codex_core::tools::router: error=Exit code: 1
Wall time: 1 seconds
Output:
Get-ChildItem : パス 'C:\Users\gucch\projects\claude\tools\ebay-manager\.claude\rules' が存在しないため検出できません。
発生場所 行:2 文字:1
+ Get-ChildItem -Path .claude\rules -File | Select-Object -ExpandProper ...
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...r\.claude\rules:String) [Get-ChildItem], ItemNotFound 
   Exception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetChildItemCommand
 

 exited 1 in 960ms:
Get-ChildItem : パス 'C:\Users\gucch\projects\claude\tools\ebay-manager\.claude\rules' が存在しないため検出できません。
発生場所 行:2 文字:1
+ Get-ChildItem -Path .claude\rules -File | Select-Object -ExpandProper ...
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...r\.claude\rules:String) [Get-ChildItem], ItemNotFound 
   Exception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetChildItemCommand
 

 succeeded in 1483ms:
tests/test_w183_ec_direct_url.py:4:- migration v55: ebay_listings / monitored_items に source_url_manual + source_url_updated_at
tests/test_w183_ec_direct_url.py:25:# ---- migration v55 ----
tests/test_w183_ec_direct_url.py:27:def test_w183_v55_migration_columns_present():
tests/test_w183_ec_direct_url.py:66:    assert rk is not None and rk.get("convert_url") == "ebayRT_"
tests/test_w183_ec_direct_url.py:123:    upsert_ebay_listing(eid, sku="ebayRT_old", title="t1")
tests/test_w183_ec_direct_url.py:126:    update_ebay_listing_sku(eid, "ebayRT_new")
tests/test_w183_ec_direct_url.py:132:    assert r[0] == "ebayRT_new"
tests/test_w183_ec_direct_url.py:189:    from monitor.scrapers import _detect_status_single
tests/test_w183_ec_direct_url.py:191:    rk = _cfg_by_prefix()["ebayRT_"]
tests/test_w183_ec_direct_url.py:195:    in_html = '<html><meta itemprop="availability" content="http://schema.org/InStock"></html>'
tests/test_w183_ec_direct_url.py:196:    oos_html = '<html><meta itemprop="availability" content="http://schema.org/OutOfStock"></html>'
tests/test_w183_ec_direct_url.py:197:    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
tests/test_w183_ec_direct_url.py:198:    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
tests/test_w183_ec_direct_url.py:203:    from monitor.scrapers import _detect_status_single
tests/test_w183_ec_direct_url.py:211:    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
tests/test_w183_ec_direct_url.py:212:    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"
tests/test_w183_ec_direct_url.py:217:    from monitor.scrapers import _check_with_httpx
tests/test_w183_ec_direct_url.py:222:        r = _check_with_httpx(
monitor\database.py:14:DEFAULT_SITE_CONFIGS = [
monitor\database.py:70:        # W183 (2026-05-28): schema.org microdata で在庫判定 (migration v55 と同値に統一).
monitor\database.py:73:        "in_stock_text1": 'itemprop="availability" content="http://schema.org/InStock"',
monitor\database.py:75:        "sold_out_text": 'itemprop="availability" content="http://schema.org/OutOfStock"',
monitor\database.py:78:        "convert_url": "ebayRT_",
monitor\database.py:103:        # W183 (2026-05-28): add-to-cart-button で主ボタン特定 (migration v55 と同値に統一).
monitor\database.py:480:            for cfg in DEFAULT_SITE_CONFIGS:
monitor\database.py:1917:        # 2) 楽天市場 site_config の sold_out_text / no_page_text を UPDATE (DEFAULT_SITE_CONFIGS は空文字、追加が必要)
monitor\database.py:1935:                       WHERE convert_url = 'ebayRT_'
monitor\database.py:2563:        # v54 (W182, 2026-05-28): supplier_candidates に availability check カラム追加.
monitor\database.py:2569:                "availability_status": "TEXT",
monitor\database.py:2570:                "availability_checked_at": "TIMESTAMP",
monitor\database.py:2571:                "availability_signal": "TEXT",
monitor\database.py:2593:        # v55 (W183, 2026-05-28): EC サイト直接 URL 無在庫出品対応 + 楽天/Amazon 在庫判定修正.
monitor\database.py:2616:                        logger.info(f"[init_db v55] {_tbl}.{_col} added")
monitor\database.py:2628:                    "WHERE convert_url='ebayRT_' AND url_keyword='item.rakuten'",
monitor\database.py:2629:                    ('itemprop="availability" content="http://schema.org/InStock"',
monitor\database.py:2630:                     'itemprop="availability" content="http://schema.org/OutOfStock"'),
monitor\database.py:2638:                logger.info("[init_db v55] 楽天/Amazon site_configs signal 更新")
monitor\database.py:2642:            _v55_ok = True
monitor\database.py:2650:                    _v55_ok = False
monitor\database.py:2651:            if _v55_ok:
monitor\database.py:2653:                logger.info("[init_db v55] schema_ver bumped to 55")
monitor\database.py:4162:    availability_status: Optional[str] = None,
monitor\database.py:4163:    availability_checked_at: Optional[str] = None,
monitor\database.py:4164:    availability_signal: Optional[str] = None,
monitor\database.py:4169:    availability_*: W182 (2026-05-28) 在庫 gate を通過した時点の判定結果.
monitor\database.py:4180:                eval_model, availability_status, availability_checked_at,
monitor\database.py:4181:                availability_signal)
monitor\database.py:4187:             eval_model, availability_status, availability_checked_at,
monitor\database.py:4188:             availability_signal),
monitor\price_extractor.py:6:  - ebayRT_*  → 楽天市場
monitor\price_extractor.py:100:    if sku.startswith("ebayRT_"):
monitor\scrapers.py:24:def _check_with_httpx(
monitor\scrapers.py:53:        return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
monitor\scrapers.py:64:def _detect_status_single(
monitor\scrapers.py:106:    result = _detect_status_single(rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
monitor\scrapers.py:110:    result = _detect_status_single(content, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
monitor\scrapers.py:114:    result = _detect_status_single(content + "\n" + rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=False)
monitor\scrapers.py:120:async def _check_urls_batch_async(
monitor\scrapers.py:227:            _check_urls_batch_async(items, headless=headless, use_chrome=use_chrome)
monitor\scrapers.py:235:def check_items_batch(items_with_config: list[dict]) -> dict[int, str]:
monitor\scrapers.py:247:        result = _check_with_httpx(item["url"], item["in_stock"], item["sold_out"], item["no_page"])
monitor\scrapers.py:279:    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
monitor\scrapers.py:291:    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
monitor\scrapers.py:435:def check_candidate_availability(url: str, timeout_sec: int = _AVAILABILITY_HTTPX_TIMEOUT) -> dict:
monitor\scrapers.py:437:    候補 URL の在庫状態を判定し、availability dict を返す。
monitor\scrapers.py:452:        return _check_paypay_availability(url, timeout_sec, checked_at)
monitor\scrapers.py:454:        return _check_yahoo_auctions_availability(url, timeout_sec, checked_at)
monitor\scrapers.py:459:def _check_paypay_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
monitor\scrapers.py:487:def _check_yahoo_auctions_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
monitor\scrapers.py:523:            status = _check_with_httpx(url, in_stock, sold_out, no_page)
monitor\runner.py:14:from .scrapers import prepare_batch_items, check_items_batch
monitor\runner.py:46:    results = check_items_batch(batch)

codex
The local `.claude/rules` directory referenced by `CLAUDE.md` is not present at this repo path, so I’ll use the rules embedded in `CLAUDE.md` and the user’s constraints. I’ve found the direct path: `check_items_batch()` trusts `_check_with_httpx()` whenever it returns a concrete status, which is the failure mode for Rakuten raw `OutOfStock`.
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\scrapers.py -TotalCount 330" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\database.py -TotalCount 130" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path tests\\test_w183_ec_direct_url.py" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path data\\tmp\\rakuten_357_raw.html -TotalCount 80" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 327ms:
"""
逶｣隕悶い繧､繝・Β縺ｮSQLite繝・・繧ｿ繝吶・繧ｹ邂｡逅・"""
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "monitor.db"
logger = logging.getLogger(__name__)

DEFAULT_SITE_CONFIGS = [
    # ---- 繝輔Μ繝槭・繧ｪ繝ｼ繧ｯ繧ｷ繝ｧ繝ｳ ----
    {
        "site_name": "繝｡繝ｫ繧ｫ繝ｪ",
        "url_keyword": "mercari",
        "in_stock_text1": "雉ｼ蜈･謇狗ｶ壹″縺ｸ",
        "in_stock_text2": "",
        "sold_out_text": "螢ｲ繧雁・繧後∪縺励◆",
        "no_page_text": "繝壹・繧ｸ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ",
        "common_url": "https://jp.mercari.com/item/",
        "convert_url": "ebayme_",
    },
    {
        "site_name": "繝｡繝ｫ繧ｫ繝ｪ繧ｷ繝ｧ繝・・",
        "url_keyword": "jp.mercari.com/shops",
        "in_stock_text1": "雉ｼ蜈･謇狗ｶ壹″縺ｸ",
        "in_stock_text2": "",
        "sold_out_text": "螢ｲ繧雁・繧・,
        "no_page_text": "繝壹・繧ｸ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ",
        "common_url": "",
        "convert_url": "ebayMS_",
    },
    {
        "site_name": "繝ｩ繧ｯ繝・,
        "url_keyword": "item.fril",
        "in_stock_text1": "雉ｼ蜈･縺ｫ騾ｲ繧",
        "in_stock_text2": "",
        "sold_out_text": "SOLD OUT",
        "no_page_text": "",
        "common_url": "https://fril.jp/item/",
        "convert_url": "ebayrm_",
    },
    {
        "site_name": "Paypay繝輔Μ繝・,
        "url_keyword": "paypayflexmarket",
        "in_stock_text1": "雉ｼ蜈･謇狗ｶ壹″縺ｸ",
        "in_stock_text2": "",
        "sold_out_text": "髢｢騾｣蝠・刀繧偵い繝励Μ縺ｧ謗｢縺・,
        "no_page_text": "縺薙・蝠・刀縺ｯ蟄伜惠縺励∪縺帙ｓ",
        "common_url": "https://paypayfleamarket.yahoo.co.jp/item/",
        "convert_url": "ebayPF_",
    },
    {
        "site_name": "繝､繝輔が繧ｯ",
        "url_keyword": "yahoo.co.jp/auction",
        "in_stock_text1": "蜈･譛ｭ縺吶ｋ",
        "in_stock_text2": "莉翫☆縺占誠譛ｭ",
        "sold_out_text": "縺薙・繧ｪ繝ｼ繧ｯ繧ｷ繝ｧ繝ｳ縺ｯ邨ゆｺ・,
        "no_page_text": "縺薙・繧ｪ繝ｼ繧ｯ繧ｷ繝ｧ繝ｳ縺ｯ蟄伜惠縺励∪縺帙ｓ",
        "common_url": "https://page.auctions.yahoo.co.jp/jp/auction/",
        "convert_url": "ebayh_",
    },
    # ---- EC繝｢繝ｼ繝ｫ ----
    {
        "site_name": "讌ｽ螟ｩ蟶ょｴ",
        "url_keyword": "item.rakuten",
        # W183 (2026-05-28): schema.org microdata 縺ｧ蝨ｨ蠎ｫ蛻､螳・(migration v55 縺ｨ蜷悟､縺ｫ邨ｱ荳).
        # 譌ｧ '縺九＃縺ｫ霑ｽ蜉'/'螢ｲ繧雁・繧・ 縺ｯ螢ｲ蛻・・繝ｼ繧ｸ縺ｫ繧・disabled 縺ｧ谿九ｊ隱､蛻､螳・(Codex 螳滓ｩ溯ｪｿ譟ｻ).
        # 隧ｳ邏ｰ: .company/engineering/migration/codex-ec-direct-url-design.md
        "in_stock_text1": 'itemprop="availability" content="http://schema.org/InStock"',
        "in_stock_text2": "",
        "sold_out_text": 'itemprop="availability" content="http://schema.org/OutOfStock"',
        "no_page_text": "縺疲欠螳壹・繝壹・繧ｸ縺ｯ隕九▽縺九ｊ縺ｾ縺帙ｓ",
        "common_url": "https://x.gd/",
        "convert_url": "ebayRT_",
    },
    {
        "site_name": "讌ｽ螟ｩ繝悶ャ繧ｯ繧ｹ",
        "url_keyword": "books.rakuten",
        "in_stock_text1": "蝨ｨ蠎ｫ縺ゅｊ",
        "in_stock_text2": "雋ｷ縺・黄縺九＃縺ｫ蜈･繧後ｋ",
        "sold_out_text": "蜀榊・闕ｷ",
        "no_page_text": "縺頑爾縺励・繝壹・繧ｸ縺瑚ｦ九▽縺・,
        "common_url": "https://books.rakuten.co.jp/rb/",
        "convert_url": "ebayRB_",
    },
    {
        "site_name": "Yahoo!繧ｷ繝ｧ繝・ヴ繝ｳ繧ｰ",
        "url_keyword": "yahoo shopping",
        "in_stock_text1": "繧ｫ繝ｼ繝医↓蜈･繧後ｋ",
        "in_stock_text2": "縺雁ｱ翫￠蜈医・蝠・刀繧偵き繝ｼ繝医↓",
        "sold_out_text": "",
        "no_page_text": "",
        "common_url": "https://store.shopping.yahoo.co.jp/",
        "convert_url": "ebayYS_",
    },
    {
        "site_name": "Amazon",
        "url_keyword": "www.amazon.co.jp",
        # W183 (2026-05-28): add-to-cart-button 縺ｧ荳ｻ繝懊ち繝ｳ迚ｹ螳・(migration v55 縺ｨ蜷悟､縺ｫ邨ｱ荳).
        # 譌ｧ '繧ｫ繝ｼ繝医↓蜈･繧後ｋ' 縺ｯ nav / 髢｢騾｣蝠・刀縺ｫ繧ょ・縺ｦ隱､蛻､螳・(Codex 螳滓ｩ溯ｪｿ譟ｻ).
        "in_stock_text1": 'id="add-to-cart-button"',
        "in_stock_text2": 'name="submit.add-to-cart"',
        "sold_out_text": "迴ｾ蝨ｨ蝨ｨ蠎ｫ蛻・ｌ",
        "no_page_text": "縺薙・蝠・刀縺ｯ迴ｾ蝨ｨ縺雁叙繧頑桶縺・〒縺阪∪縺帙ｓ",
        "common_url": "https://www.amazon.co.jp/dp/",
        "convert_url": "ebayAM_",
    },
    # ---- 荳ｭ蜿､繝ｻ繝ｪ繝ｦ繝ｼ繧ｹ ----
    {
        "site_name": "OFF繝｢繝ｼ繝ｫ・医ワ繝ｼ繝峨が繝包ｼ・,
        "url_keyword": "netmall.hardoff",
        "in_stock_text1": "繧ｫ繝ｼ繝医↓蜈･繧後ｋ",
        "in_stock_text2": "",
        "sold_out_text": "縺薙・蝠・刀縺ｯ螢ｲ繧雁・繧後↑",
        "no_page_text": "",
        "common_url": "https://netmall.hardoff.co.jp/product/",
        "convert_url": "ebayOFF_",
    },
    {
        "site_name": "鬧ｿ豐ｳ螻・,
        "url_keyword": "suruga-ya",
        "in_stock_text1": "繧ｫ繝ｼ繝医↓蜈･繧後ｋ",
        "in_stock_text2": "",
        "sold_out_text": "蜩∝・繧御ｸｭ縺ｧ縺吶・,
        "no_page_text": "The requested page c",
        "common_url": "https://www.suruga-ya.jp/product/detail/",
        "convert_url": "ebaySU_",

 succeeded in 349ms:
"""
莉募・蜈・し繧､繝医・蝨ｨ蠎ｫ繝√ぉ繝・け・・laywright + httpx 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ・・URL縺ｫ逶ｴ謗･繧｢繧ｯ繧ｻ繧ｹ縺励※蝨ｨ蠎ｫ繝・く繧ｹ繝医ｒ讀懷・縺吶ｋ譁ｹ蠑・"""
import asyncio
import logging
import random
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----

def _check_with_httpx(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> Optional[str]:
    """httpx 縺ｧ HTML 繧貞叙蠕励＠繧ｭ繝ｼ繝ｯ繝ｼ繝画､懃ｴ｢縲ょ愛螳壻ｸ崎・縺ｪ繧・None縲・""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        if resp.status_code == 404:
            return "not_found"
        if resp.status_code != 200:
            logger.debug(f"httpx HTTP {resp.status_code}: {url}")
            return None

        html = resp.text
        # W183 (2026-05-28): Amazon 遲峨・ anti-bot 繝壹・繧ｸ (Robot Check / CAPTCHA)
        # 縺ｯ蝨ｨ蠎ｫ蛻､螳壻ｸ崎・ = unknown 謇ｱ縺・(None 縺ｧ Playwright fallback)縲ょ惠蠎ｫ蛻・ｌ縺ｨ
        # 隱､隱阪☆繧九→荳崎ｦ√↑蛟､荳九￡ / 蜃ｺ蜩∝●豁｢縺ｫ逶ｴ邨舌☆繧九◆繧∝ｿ・★ unknown 縺ｫ蛟偵☆縲・        low = html.lower()
        if "robot check" in low or "validatecaptcha" in low:
            logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
            return None
        return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    except httpx.TimeoutException:
        logger.debug(f"httpx timeout: {url}")
        return None
    except Exception as e:
        logger.debug(f"httpx error: {url}: {e}")
        return None


# ---- 蛻､螳壹Ο繧ｸ繝・け ----

def _detect_status_single(
    content: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
    strict: bool = False,
) -> Optional[str]:
    """
    蜊倅ｸ繝・く繧ｹ繝茨ｼ・TML or rendered text・峨°繧牙愛螳壹ょ愛螳壻ｸ崎・縺ｪ繧・None縲・    strict=True: 蝨ｨ蠎ｫ譛峨→蝨ｨ蠎ｫ辟｡縺悟酔譎よ､懷・縺輔ｌ縺溷ｴ蜷医・ None・・PA蟇ｾ遲厄ｼ・    """
    active_np = [t for t in no_page_texts if t]
    active_so = [t for t in sold_out_texts if t]
    active_is = [t for t in in_stock_texts if t]

    found_np = any(t in content for t in active_np)
    found_so = any(t in content for t in active_so)
    found_is = active_is and any(t in content for t in active_is)

    # SPA蟇ｾ遲・ 蝨ｨ蠎ｫ譛峨・蝨ｨ蠎ｫ辟｡繝ｻ繝壹・繧ｸ縺ｪ縺励′蜈ｨ縺ｦ隕九▽縺九ｋ蝣ｴ蜷医・JS繝・Φ繝励Ξ繝ｼ繝域ｷｷ蜈･
    if strict and sum([found_np, found_so, found_is]) >= 2:
        logger.debug("Ambiguous detection (SPA?) - deferring to Playwright")
        return None

    if found_np:
        return "not_found"
    if found_so:
        return "unavailable"
    if found_is:
        return "available"
    return None


def _detect_status(
    content: str,
    rendered_text: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """繝ｬ繝ｳ繝繝ｪ繝ｳ繧ｰ貂医∩繝・く繧ｹ繝亥━蜈遺・HTML 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ縲・""
    # Step 1: 繝ｬ繝ｳ繝繝ｪ繝ｳ繧ｰ貂医∩繝・く繧ｹ繝茨ｼ医Θ繝ｼ繧ｶ繝ｼ縺ｫ隕九∴繧句・螳ｹ・峨〒蛻､螳・    result = _detect_status_single(rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    if result is not None:
        return result
    # Step 2: HTML蜈ｨ菴薙〒蛻､螳夲ｼ・S縺ｧ蜍慕噪逕滓・縺輔ｌ繧玖ｦ∫ｴ繧ゅき繝舌・・・    result = _detect_status_single(content, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
    if result is not None:
        return result
    # Step 3: strict辟｡縺励〒蜀榊愛螳夲ｼ・縺､縺ｧ繧りｦ九▽縺九ｌ縺ｰ蛻､螳夲ｼ・    result = _detect_status_single(content + "\n" + rendered_text, in_stock_texts, sold_out_texts, no_page_texts, strict=False)
    return result or "unknown"


# ---- Playwright 繝舌ャ繝√メ繧ｧ繝・け・医ヶ繝ｩ繧ｦ繧ｶ蜀榊茜逕ｨ・・----

async def _check_urls_batch_async(
    items: list[dict],
    headless: bool = True,
    use_chrome: bool = False,
) -> dict[int, str]:
    """
    隍・焚URL繧・縺､縺ｮ繝悶Λ繧ｦ繧ｶ繧､繝ｳ繧ｹ繧ｿ繝ｳ繧ｹ縺ｧ鬆・ｬ｡繝√ぉ繝・け縲・    items: [{id, url, in_stock, sold_out, no_page}, ...]
    Returns: {item_id: status}
    """
    results = {}
    launch_opts = {
        "headless": headless,
        "args": ["--disable-http2", "--disable-blink-features=AutomationControlled"],
    }
    if use_chrome:
        launch_opts["channel"] = "chrome"

    browser = None
    try:
        async with async_playwright() as p:
            # 繝悶Λ繧ｦ繧ｶ襍ｷ蜍輔・繝ｪ繝医Λ繧､・域怙螟ｧ3蝗橸ｼ・            for launch_attempt in range(3):
                try:
                    browser = await p.chromium.launch(**launch_opts)
                    break
                except Exception as e:
                    logger.debug(f"Browser launch attempt {launch_attempt + 1} failed: {e}")
                    if launch_attempt < 2:
                        await asyncio.sleep(2)
                    else:
                        raise

            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()

            for item in items:
                item_id = item["id"]
                url = item["url"]
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response and response.status == 404:
                        results[item_id] = "not_found"
                        continue

                    # SPA蟇ｾ遲・ networkidle + 繧ｳ繝ｳ繝・Φ繝・ｾ・ｩ・                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # SPA譛ｪ繝ｬ繝ｳ繝繝ｪ繝ｳ繧ｰ讀懷・竊定ｿｽ蜉蠕・ｩ滂ｼ域怙螟ｧ2蝗槭Μ繝医Λ繧､・・                    rendered_text = await page.inner_text("body")
                    for _ in range(2):
                        if len(rendered_text.strip()) >= 1500:
                            break
                        await asyncio.sleep(3)
                        rendered_text = await page.inner_text("body")

                    content = await page.content()

                    status = _detect_status(
                        content, rendered_text,
                        item["in_stock"], item["sold_out"], item["no_page"],
                    )
                    results[item_id] = status

                except PlaywrightTimeout:
                    logger.warning(f"Playwright timeout: {url}")
                    results[item_id] = "error"
                except Exception as e:
                    logger.warning(f"Playwright error: {url}: {e}")
                    results[item_id] = "error"

            if browser:
                await browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        for item in items:
            results.setdefault(item["id"], "error")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    return results


def _run_playwright_batch(items: list[dict], headless: bool = True, use_chrome: bool = False) -> dict[int, str]:
    """蜷梧悄繝ｩ繝・ヱ繝ｼ"""
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _check_urls_batch_async(items, headless=headless, use_chrome=use_chrome)
        )
    finally:
        loop.close()


# ---- 邨ｱ蜷医メ繧ｧ繝・け・・ttpx 竊・Playwright batch 竊・Chrome headed batch・・----

def check_items_batch(items_with_config: list[dict]) -> dict[int, str]:
    """
    隍・焚繧｢繧､繝・Β繧貞柑邇・噪縺ｫ繝√ぉ繝・け縲・    items_with_config: [{id, url, in_stock, sold_out, no_page}, ...]
    Returns: {item_id: status}
    """
    results = {}
    playwright_needed = []
    chrome_needed = []

    # Step 1: httpx 縺ｧ鬮倬溘メ繧ｧ繝・け
    for item in items_with_config:
        result = _check_with_httpx(item["url"], item["in_stock"], item["sold_out"], item["no_page"])
        if result is not None:
            results[item["id"]] = result
        else:
            playwright_needed.append(item)

    # Step 2: Playwright headless・医ヰ繝・メ縲√ヶ繝ｩ繧ｦ繧ｶ1蝗櫁ｵｷ蜍包ｼ・    if playwright_needed:
        pw_results = _run_playwright_batch(playwright_needed, headless=True, use_chrome=False)
        for item in playwright_needed:
            status = pw_results.get(item["id"], "error")
            if status not in ("error", "unknown"):
                results[item["id"]] = status
            else:
                chrome_needed.append(item)

    # Step 3: Chrome headed・域怙邨よ焔谿ｵ縲√ヰ繝・メ・・    if chrome_needed:
        chrome_results = _run_playwright_batch(chrome_needed, headless=False, use_chrome=True)
        for item in chrome_needed:
            results[item["id"]] = chrome_results.get(item["id"], "error")

    return results


def check_url_sync_httpx_only(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """httpx縺ｮ縺ｿ縺ｧ蜊倅ｸURL繧偵メ繧ｧ繝・け・・laywright繧ｹ繧ｭ繝・・・・""
    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
    return result or "unknown"


def check_url_sync(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> str:
    """蜊倅ｸURL蜷梧悄繝√ぉ繝・け・・ttpx 竊・Playwright fallback・・""
    # Step 1: httpx 縺ｧ鬮倬溘メ繧ｧ繝・け
    result = _check_with_httpx(url, in_stock_texts, sold_out_texts, no_page_texts)
    if result is not None:
        return result

    # Step 2: Playwright 縺ｧ蜀崎ｩｦ陦鯉ｼ域怙螟ｧ3蝗橸ｼ・    for attempt in range(3):
        try:
            pw_result = _run_playwright_batch(
                [{
                    "id": 1,
                    "url": url,
                    "in_stock": in_stock_texts,
                    "sold_out": sold_out_texts,
                    "no_page": no_page_texts,
                }],
                headless=True,
                use_chrome=False,
            )
            status = pw_result.get(1, "unknown")
            if status not in ("error", "unknown"):
                return status
        except Exception as e:
            logger.debug(f"Playwright attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                import time
                time.sleep(2)  # 谺｡蝗櫁ｩｦ陦悟燕縺ｫ蠕・ｩ・            continue

    # Step 3: Chrome headed 縺ｧ譛邨りｩｦ陦・    try:
        pw_result = _run_playwright_batch(
            [{
                "id": 1,
                "url": url,
                "in_stock": in_stock_texts,
                "sold_out": sold_out_texts,
                "no_page": no_page_texts,
            }],
            headless=False,
            use_chrome=True,
        )
        status = pw_result.get(1, "unknown")
        return status
    except Exception as e:
        logger.debug(f"Chrome headed attempt failed: {e}")
        return "unknown"


def check_item_by_config(item: dict, site_config: dict) -> str:
    """繧｢繧､繝・Β縺ｨ繧ｵ繧､繝郁ｨｭ螳壹°繧牙惠蠎ｫ繝√ぉ繝・け・亥腰荳繧｢繧､繝・Β・・""
    source_url = item.get("source_url", "")
    if not source_url:
        return "error"
    in_stock = [site_config.get("in_stock_text1", ""), site_config.get("in_stock_text2", "")]
    sold_out = [site_config.get("sold_out_text", "")]
    no_page = [site_config.get("no_page_text", "")]
    return check_url_sync(source_url, in_stock, sold_out, no_page)


 succeeded in 361ms:
"""W183 (2026-05-28) EC 逶ｴ謗･ URL 辟｡蝨ｨ蠎ｫ逶｣隕悶・ unit test.

scope (codex-ec-direct-url-design.md 螳溯｣・・step 8):
- migration v55: ebay_listings / monitored_items 縺ｫ source_url_manual + source_url_updated_at
- init_db 2 蝗槭〒繝・・繧ｿ菫晄戟 (蜀ｪ遲画ｧ)
- set_listing_source_url_manual round-trip + find_site_config_by_url
- 謇句虚 URL (source_url_manual=1) 縺・upsert_item / upsert_ebay_listing /
  update_ebay_listing_sku 縺ｮ SKU 螟画峩縺ｧ荳頑嶌縺阪＆繧後↑縺・- 讌ｽ螟ｩ (schema.org microdata) / Amazon (add-to-cart-button) 縺ｮ蝨ｨ蠎ｫ蛻､螳・- Amazon anti-bot (Robot Check) 縺ｯ unknown (隱､ OOS 髦ｲ豁｢)
- prefix 荳堺ｸ閾ｴ縺ｮ逶ｴ謗･ URL 縺・prepare_batch_items 縺九ｉ關ｽ縺｡縺ｪ縺・(url_keyword fallback)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---- migration v55 ----

def test_w183_v55_migration_columns_present():
    from monitor.database import init_db, get_conn
    init_db()
    with get_conn() as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        el = {r[1] for r in c.execute("PRAGMA table_info(ebay_listings)").fetchall()}
        mi = {r[1] for r in c.execute("PRAGMA table_info(monitored_items)").fetchall()}
    assert ver >= 55, f"expected user_version >= 55, got {ver}"
    for col in ("source_url_manual", "source_url_updated_at"):
        assert col in el, f"ebay_listings.{col} missing"
        assert col in mi, f"monitored_items.{col} missing"


def test_w183_init_db_idempotent_retains_data():
    """init_db 2 蝗樣｣邯壹〒謇句虚 URL listing 縺御ｿ晄戟縺輔ｌ繧・(蜀ｪ遲画ｧ)."""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_IDEM_1"
    url = "https://www.amazon.co.jp/dp/B0TESTIDEM1"
    upsert_ebay_listing(eid, sku="ebayAM_idem", title="idem test")
    assert set_listing_source_url_manual(eid, url, manual=True) is True
    init_db()  # 蜀榊ｮ溯｡・    with get_conn() as c:
        row = c.execute(
            "SELECT source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert row is not None, "listing 縺・init_db 蜀榊ｮ溯｡後〒豸亥､ｱ (蜀ｪ遲画ｧ驕募渚)"
    assert row[0] == url
    assert int(row[1]) == 1


# ---- find_site_config_by_url ----

def test_w183_find_site_config_by_url():
    from monitor.database import init_db, find_site_config_by_url
    init_db()
    rk = find_site_config_by_url("https://item.rakuten.co.jp/shop/abc123/")
    am = find_site_config_by_url("https://www.amazon.co.jp/dp/B0XXXX")
    assert rk is not None and rk.get("convert_url") == "ebayRT_"
    assert am is not None and am.get("convert_url") == "ebayAM_"
    assert find_site_config_by_url("") is None
    assert find_site_config_by_url("https://unknown-ec.example.com/item/1") is None


# ---- set / unset ----

def test_w183_set_and_unset_manual_url():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_SETUNSET"
    upsert_ebay_listing(eid, sku="ebayAM_x", title="t")
    url = "https://www.amazon.co.jp/dp/B0SETUNSET"
    assert set_listing_source_url_manual(eid, url, manual=True) is True
    with get_conn() as c:
        r = c.execute(
            "SELECT source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == url and int(r[1]) == 1
    # 蝗ｺ螳夊ｧ｣髯､
    assert set_listing_source_url_manual(eid, url, manual=False) is True
    with get_conn() as c:
        r2 = c.execute(
            "SELECT source_url_manual FROM ebay_listings WHERE ebay_item_id=?", (eid,)
        ).fetchone()
    assert int(r2[0]) == 0
    # listing 荳榊惠縺ｯ False
    assert set_listing_source_url_manual("NO_SUCH_LISTING", url, manual=True) is False


# ---- 謇句虚 URL 菫晁ｭｷ (SKU 螟画峩縺ｧ荳頑嶌縺阪＆繧後↑縺・ ----

def test_w183_manual_url_survives_upsert_ebay_listing_sku_change():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_UEL"
    upsert_ebay_listing(eid, sku="ebayAM_old", title="t1")
    manual_url = "https://www.amazon.co.jp/dp/B0MANUAL"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    # SKU 繧貞､画峩縺励※蜀・upsert (sku_changed=True + is_manual=True 邨瑚ｷｯ)
    upsert_ebay_listing(eid, sku="ebayAM_new", title="t2", current_price=9.9)
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayAM_new"          # SKU 縺ｯ霑ｽ蠕・    assert r[1] == manual_url            # 謇句虚 URL 縺ｯ邯ｭ謖・    assert int(r[2]) == 1


def test_w183_manual_url_survives_update_ebay_listing_sku():
    from monitor.database import init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual, update_ebay_listing_sku
    init_db()
    eid = "TESTW183_UELS"
    upsert_ebay_listing(eid, sku="ebayRT_old", title="t1")
    manual_url = "https://item.rakuten.co.jp/shop/manual-item/"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    update_ebay_listing_sku(eid, "ebayRT_new")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayRT_new"
    assert r[1] == manual_url
    assert int(r[2]) == 1


def test_w183_non_manual_listing_sku_change_still_resets():
    """蝗槫ｸｰ髦ｲ豁｢: source_url_manual=0 縺ｮ騾壼ｸｸ listing 縺ｯ蠕捺擂騾壹ｊ SKU 螟画峩縺ｧ source_* reset."""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, update_ebay_listing_sku
    init_db()
    eid = "TESTW183_NONMANUAL"
    upsert_ebay_listing(eid, sku="ebayme_old", title="t1")
    with get_conn() as c:
        c.execute(
            "UPDATE ebay_listings SET source_status='out_of_stock', risk_confirmed=1 WHERE ebay_item_id=?",
            (eid,),
        )
    update_ebay_listing_sku(eid, "ebayme_new")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_status, risk_confirmed FROM ebay_listings WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayme_new"
    assert r[1] == "unknown"      # reset 縺輔ｌ縺・(蠕捺擂蜍穂ｽ懃ｶｭ謖・
    assert int(r[2]) == 0


def test_w183_manual_url_survives_upsert_item():
    """monitored_items 繧・source_url_manual=1 縺ｪ繧・upsert_item 縺ｧ URL 邯ｭ謖・"""
    from monitor.database import init_db, get_conn, upsert_ebay_listing, upsert_item, set_listing_source_url_manual
    init_db()
    eid = "TESTW183_UI"
    upsert_ebay_listing(eid, sku="ebayAM_old", title="t")
    upsert_item(sku="ebayAM_old", ebay_item_id=eid, title="t")
    manual_url = "https://www.amazon.co.jp/dp/B0UPSERTITEM"
    set_listing_source_url_manual(eid, manual_url, manual=True)
    # SKU 螟画峩縺ｧ upsert_item 蜀榊他蜃ｺ (譌｢蟄倩｡・manual=1 邨瑚ｷｯ)
    upsert_item(sku="ebayAM_new", ebay_item_id=eid, title="t2")
    with get_conn() as c:
        r = c.execute(
            "SELECT sku, source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r[0] == "ebayAM_new"
    assert r[1] == manual_url
    assert int(r[2]) == 1


# ---- 蝨ｨ蠎ｫ蛻､螳・(讌ｽ螟ｩ / Amazon site_configs signal) ----

def _cfg_by_prefix():
    from monitor.database import get_site_configs
    return {c["convert_url"]: c for c in get_site_configs() if c.get("convert_url")}


def test_w183_rakuten_schema_org_detection():
    from monitor.database import init_db
    from monitor.scrapers import _detect_status_single
    init_db()
    rk = _cfg_by_prefix()["ebayRT_"]
    is_texts = [rk.get("in_stock_text1", ""), rk.get("in_stock_text2", "")]
    so_texts = [rk.get("sold_out_text", "")]
    np_texts = [rk.get("no_page_text", "")]
    in_html = '<html><meta itemprop="availability" content="http://schema.org/InStock"></html>'
    oos_html = '<html><meta itemprop="availability" content="http://schema.org/OutOfStock"></html>'
    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"


def test_w183_amazon_add_to_cart_detection():
    from monitor.database import init_db
    from monitor.scrapers import _detect_status_single
    init_db()
    am = _cfg_by_prefix()["ebayAM_"]
    is_texts = [am.get("in_stock_text1", ""), am.get("in_stock_text2", "")]
    so_texts = [am.get("sold_out_text", "")]
    np_texts = [am.get("no_page_text", "")]
    in_html = '<input id="add-to-cart-button" name="submit.add-to-cart" title="繧ｫ繝ｼ繝医↓蜈･繧後ｋ">'
    oos_html = '<div>迴ｾ蝨ｨ蝨ｨ蠎ｫ蛻・ｌ縺ｧ縺・/div>'
    assert _detect_status_single(in_html, is_texts, so_texts, np_texts, strict=True) == "available"
    assert _detect_status_single(oos_html, is_texts, so_texts, np_texts, strict=True) == "unavailable"


def test_w183_amazon_captcha_is_unknown():
    """Amazon anti-bot 繝壹・繧ｸ (Robot Check) 縺ｯ unknown = None (隱､ OOS 髦ｲ豁｢)."""
    from monitor.scrapers import _check_with_httpx
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "<html><title>Robot Check</title>Enter the characters you see below</html>"
    with patch("monitor.scrapers.httpx.get", return_value=fake):
        r = _check_with_httpx(
            "https://www.amazon.co.jp/dp/B0CAPTCHA",
            ['id="add-to-cart-button"'], ["迴ｾ蝨ｨ蝨ｨ蠎ｫ蛻・ｌ"], ["縺薙・蝠・刀縺ｯ迴ｾ蝨ｨ縺雁叙繧頑桶縺・〒縺阪∪縺帙ｓ"],
        )
    assert r is None


# ---- prepare_batch_items url_keyword fallback ----

def test_w183_prepare_batch_items_url_fallback():
    """prefix 荳堺ｸ閾ｴ縺ｧ繧・source_url 縺ｮ url_keyword 縺ｧ config 隗｣豎ｺ縺輔ｌ batch 縺ｫ谿九ｋ."""
    from monitor.database import init_db
    from monitor.scrapers import prepare_batch_items
    init_db()
    cfgs = _cfg_by_prefix()
    # SKU 縺ｯ譌｢蟄・prefix 縺ｮ縺ｩ繧後↓繧ゆｸ閾ｴ縺励↑縺・(逶ｴ謗･ URL 逶｣隕門膚蜩・ 縺・URL 縺ｯ讌ｽ螟ｩ
    items = [
        {"id": 1, "sku": "stock-direct-1", "source_url": "https://item.rakuten.co.jp/shop/xyz/"},
        {"id": 2, "sku": "ebayAM_known", "source_url": "https://www.amazon.co.jp/dp/B0KNOWN"},
        {"id": 3, "sku": "weird", "source_url": "https://no-config-site.example.com/item/9"},
        {"id": 4, "sku": "nourl", "source_url": ""},
    ]
    batch = prepare_batch_items(items, cfgs)
    ids = {b["id"] for b in batch}
    assert 1 in ids, "url_keyword fallback 縺ｧ讌ｽ螟ｩ逶ｴ謗･ URL 縺・batch 縺ｫ谿九ｋ縺ｹ縺・
    assert 2 in ids, "prefix 荳閾ｴ (Amazon) 縺ｯ蠕捺擂騾壹ｊ谿九ｋ"
    assert 3 not in ids, "config 隗｣豎ｺ荳崎・縺ｯ髯､螟・(繝ｭ繧ｰ貂・"
    assert 4 not in ids, "source_url 遨ｺ縺ｯ髯､螟・


# ---- HIGH-1 蝗槫ｸｰ: ensure_monitor_coverage 縺ｧ謇句虚 URL 縺梧ｱ壽沒縺輔ｌ縺ｪ縺・----

def test_w183_manual_url_survives_ensure_monitor_coverage():
    """謇句虚 URL listing 縺檎屮隕門床蟶ｳ縺ｫ逋ｻ骭ｲ縺輔ｌ縲‘nsure_monitor_coverage 縺ｧ
    SKU 豢ｾ逕・URL 縺ｫ荳頑嶌縺阪＆繧後↑縺・(code-reviewer HIGH-1)."""
    from monitor.database import (
        init_db, get_conn, upsert_ebay_listing, set_listing_source_url_manual,
    )
    from tasks.task_ensure_monitor_coverage import run_ensure_monitor_coverage
    init_db()
    eid = "TESTW183_ENSURE"
    # 逶ｴ謗･ URL 逶｣隕門膚蜩・(ebayAM_ SKU 縺縺・URL 縺ｯ SKU 縺九ｉ蟆主・荳崎・縲［onitored 譛ｪ逋ｻ骭ｲ)
    upsert_ebay_listing(eid, sku="ebayAM_directonly1", title="direct url item",
                        quantity_ebay=1)
    manual_url = "https://www.amazon.co.jp/dp/B0DIRECTONLY"
    assert set_listing_source_url_manual(eid, manual_url, manual=True) is True
    # set 譎らせ縺ｧ逶｣隕門床蟶ｳ縺ｫ謇句虚 URL 陦後′菴懊ｉ繧後※縺・ｋ縺ｯ縺・(HIGH-1 fix)
    with get_conn() as c:
        r0 = c.execute(
            "SELECT source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r0 is not None, "set_listing_source_url_manual 縺檎屮隕門床蟶ｳ縺ｫ陦後ｒ菴懊ｉ縺ｪ縺・(silent unmonitored)"
    assert r0[0] == manual_url and int(r0[1]) == 1
    # 逶｣隕門床蟶ｳ陬懷ｮ後ｒ襍ｰ繧峨○縺ｦ繧よ焔蜍・URL 縺・SKU 豢ｾ逕・URL 縺ｫ豎壽沒縺輔ｌ縺ｪ縺・    run_ensure_monitor_coverage({})
    with get_conn() as c:
        r1 = c.execute(
            "SELECT source_url, source_url_manual FROM monitored_items WHERE ebay_item_id=?",
            (eid,),
        ).fetchone()
    assert r1 is not None
    assert r1[0] == manual_url, f"謇句虚 URL 縺梧ｱ壽沒縺輔ｌ縺・ {r1[0]}"
    assert int(r1[1]) == 1

 succeeded in 677ms:
<html xmlns:og="http://ogp.me/ns#" xmlns:mixi="http://mixi-platform.com/ns#" xmlns:gr="http://gree.jp/ns" xmlns:fb="http://www.facebook.com/2008/fbml" lang="ja">
<head>
<meta http-equiv="expires" content="">
<meta http-equiv="Content-Type" content="text/html; charset=EUC-JP">
<meta http-equiv="Content-Language" content="ja">
<meta name="description" content="繝溘ヤ繝医Κ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟・>
<meta name="keywords" content="讌ｽ螟ｩ蟶ょｴ,騾壻ｿ｡雋ｩ螢ｲ,騾夊ｲｩ,繧ｷ繝ｧ繝・ヴ繝ｳ繧ｰ,繧ｪ繝ｳ繝ｩ繧､繝ｳ繧ｷ繝ｧ繝・ヴ繝ｳ繧ｰ,雋ｷ縺・黄,繝励Ξ繧ｼ繝ｳ繝・繧ｮ繝輔ヨ,雍医ｊ迚ｩ,雍育ｭ泌刀,縺贋ｸｭ蜈・縺頑ｭｳ證ｮ,縺願ｲｷ縺・ｾ・繝溘ヤ繝医Κ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟・>
<base href="https://image.rakuten.co.jp/">
<title>縲先･ｽ螟ｩ蟶ょｴ縲代Α繝・ヨ繝ｨ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟具ｼ咼IY FACTORY ONLINE SHOP</title>
<style TYPE="text/css">
<!--
 td.sdtext { color:#000000; font-size:80%;}
 td.topT_sdtext { color:#000000; font-size:80%;}
 td.exT_sdtext { color:#000000; font-size:80%;}
 td.inT_sdtext { color:#000000; font-size:80%;}
body { background-color: #FFFFFF; } 
span.item_desc { color: #000000; font-size: 100%; }
span.sale_desc { color: #000000; font-size: 100%; }
td.lossleader_item_name { color: #333333; font-size: 80%; }
a.lossleader_item_name:link { color: #333333; font-size: 80%; }
a.lossleader_item_name:visited { color: #333333; font-size: 80%; }
a.lossleader_item_name:active { color: #333333; font-size: 80%; }
span.lossleader_price1 { color: #C20000; font-size: 80%; }
span.lossleader_price2 { color: #C20000; font-size: 80%; }
-->
</style>
<link type="text/css" rel="stylesheet" href="/com/css/rms/storefront/pc/page/aroundcart-1.9.0.css">
<link rel="SHORTCUT ICON" href="https://www.rakuten.co.jp/favicon.ico">
<link type="text/css" rel="stylesheet" href="/com/css/rms/storefront/pc/page/page_header_w-2.6.4.css">
<link type="text/css" rel="stylesheet" href="https://r.r10s.jp/com/inc/navi/spu/css/spux-pc-1.1.1.css">
<link type="text/css" rel="stylesheet" media="all" href="/com/css/rms/storefront/pc/page/page_suggest-1.0.5.css">
<link rel="stylesheet" href="/com/css/rms/storefront/pc/page/page_header_reco-1.2.1.css">
<link rel="stylesheet" href="/com/css/rms/storefront/pc/page/page_images-1.1.0.css">
<link rel="stylesheet" href="/com/css/rms/storefront/pc/page/page_recommend-1.0.0.css">
<link type="text/css" rel="stylesheet" media="all" href="/com/css/rms/storefront/pc/page/rms_item_table_pc-1.0.1.css">
<link type="text/css" rel="stylesheet" href="/com/css/rms/storefront/pc/page/ranking-inshop-pcc-1.1.1.css">
<link type="text/css" rel="stylesheet" href="/com/css/rms/storefront/pc/page/rchat_widget-1.0.0.css">
<meta property="og:type" content="product">
<meta property="og:title" content="縲先･ｽ螟ｩ蟶ょｴ縲代Α繝・ヨ繝ｨ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟具ｼ咼IY FACTORY ONLINE SHOP">
<meta property="og:description" content="繝溘ヤ繝医Κ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟・>
<meta property="og:url" content="https://item.rakuten.co.jp/tuzukiya/m20-5806/">
<meta property="og:image" content="https://shop.r10s.jp/tuzukiya/cabinet/0/1/818/m20-5806.jpg">
<meta property="og:site_name" content="讌ｽ螟ｩ蟶ょｴ">
<meta property="fb:app_id" content="157315820947832">
<meta name="twitter:card" content="summary">
<meta name="twitter:site" content="@RakutenJP">
<meta name="twitter:title" content="縲先･ｽ螟ｩ蟶ょｴ縲代Α繝・ヨ繝ｨ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟具ｼ咼IY FACTORY ONLINE SHOP">
<meta name="twitter:description" content="繝溘ヤ繝医Κ 繝ｪ繝九Ζ繧ｹ繧ｱ繝ｼ繝ｫ逕ｨ繧ｫ繧ｦ繝ｳ繧ｿ繝ｼ KA-212 174-183 1蛟・>
<meta name="twitter:image:src" content="https://shop.r10s.jp/tuzukiya/cabinet/0/1/818/m20-5806.jpg">
<link rel="canonical" href="https://item.rakuten.co.jp/tuzukiya/m20-5806/">
<link type="text/css" rel="stylesheet" href="https://r.r10s.jp/com/itempage/assets/app/pages/item-pc/css/main-459f555a0c11e7332540.bundle.css" charset="utf-8">
<link type="text/css" rel="stylesheet" href="https://r.r10s.jp/com/itempage/assets/app/pages/item-pc/css/pc-459f555a0c11e7332540.bundle.css" charset="utf-8">
</head>
<body>
<script src="https://r.r10s.jp/com/js/d/shisa/shisa-1.0.2.min.js" id="script-shisa" data-url="https://rat.rakuten.co.jp/" data-group="ichiba-item"></script><script type="text/javascript" src="https://www.rakuten.co.jp/com/tls/tls.js" charset="utf-8"></script><script src="https://r.r10s.jp/com/js/d/Rmodules/1.28/Rmodules-1.28.0.min.js"></script><script src="//r.r10s.jp/com/inc/navi/common_banner/mno/js/create_ichiba.js" charset="utf-8"></script>
<div id="mkdiv_header_pitari"></div>
<script type="text/javascript" src="https://r.r10s.jp/com/js/rms/storefront/pc/page/page_header_banner-1.0.2.min.js" language="JavaScript"></script>
<div id="globalBannerWrapper" class="pc-item-page-header">
<div data-path="https://www.rakuten.co.jp/com/inc/navi/item/prod_#LANG#" class="global-banner-wrapper">
<div class="global-banner-contents"></div>
<div class="global-banner-settings-api"></div>
<script type="text/javascript" src="https://r.r10s.jp/com/js/d/global_banner_event/1.2/global_banner_event-1.2.0.min.js"></script>
</div>
</div>
<div id="rakutenLimitedId_header" class="">
<div style="position:relative!important;" class="riShopHdrWrap">
<div class="riShopHdrLogoArea">
<div class="riShopHdrLogo">
<a onclick="this.href=this.href+'?l2-id=shop_header_logo'" href="https://www.rakuten.co.jp/" class="ris-hd-sprite ris-rakuten-logo">讌ｽ螟ｩ蟶ょｴ</a>
</div>
</div>
<div class="riShopHdrArea">
<form style="display: block!important;" onsubmit="sc_searchBox();" name="myForm" method="get" id="myForm" class="rakutenHeaderSearch01Form" action="https://search.rakuten.co.jp/search/mall" accept-charset="utf-8">
<input value="s_search" type="hidden" name="l-id"><input value="shop_header_search" type="hidden" name="l2-id">
<div class="sggstInputWrap riShopHdrInputArea">
<input value="" type="text" style="" size="40" placeholder="繧ｭ繝ｼ繝ｯ繝ｼ繝峨°繧画爾縺・ name="sitem" maxlength="2048" id="sitem" class="searchInput" autocomplete="off">
<div style="" class="ris-search-btn">
<input value="讀懃ｴ｢" type="submit" id="searchBtn" class="ris-hd-sprite searchBtn" alt="讀懃ｴ｢">
</div>
</div>

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-ChildItem -Path ..\\..\\.. -Recurse -Filter db-migration-rules.md -ErrorAction SilentlyContinue | Select-Object -First 5 -ExpandProperty FullName" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\database.py | Select-Object -Skip 2588 -First 90" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\database.py | Select-Object -Skip 440 -First 70" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "rg -n \"PRAGMA user_version|schema_ver bumped|user_version = 55|user_version = 56|v56|schema_ver\" monitor\\database.py" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 354ms:
1609:        schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
1610:        if schema_ver < 26:
1698:            conn.execute("PRAGMA user_version = 26")
1704:        if schema_ver < 27:
1714:                conn.execute("PRAGMA user_version = 27")
1722:        if schema_ver < 28:
1730:                conn.execute("PRAGMA user_version = 28")
1744:        if schema_ver < 29:
1762:                conn.execute("PRAGMA user_version = 29")
1773:        if schema_ver < 30:
1783:            conn.execute("PRAGMA user_version = 30")
1787:        if schema_ver < 31:
1794:            conn.execute("PRAGMA user_version = 31")
1798:        if schema_ver < 32:
1808:            conn.execute("PRAGMA user_version = 32")
1813:        if schema_ver < 33:
1846:            conn.execute("PRAGMA user_version = 33")
1853:        if schema_ver < 34:
1863:            conn.execute("PRAGMA user_version = 34")
1870:        if schema_ver < 35:
1879:            conn.execute("PRAGMA user_version = 35")
1885:        if schema_ver < 36:
1892:            conn.execute("PRAGMA user_version = 36")
1897:        if schema_ver < 37:
1913:            conn.execute("PRAGMA user_version = 37")
1918:        if schema_ver < 38:
1941:            conn.execute("PRAGMA user_version = 38")
1948:        if schema_ver < 39:
1990:            conn.execute("PRAGMA user_version = 39")
2003:        if schema_ver < 40:
2038:            conn.execute("PRAGMA user_version = 40")
2052:        if schema_ver < 41:
2062:            conn.execute("PRAGMA user_version = 41")
2073:        if schema_ver < 42:
2080:            conn.execute("PRAGMA user_version = 42")
2098:        if schema_ver < 43:
2109:            conn.execute("PRAGMA user_version = 43")
2126:        if schema_ver < 44:
2162:            # 次回以降 `if schema_ver < 44` を skip し W140 (Q0 安全網) が
2164:            # 失敗時は schema_ver < 44 のまま = 次回 init_db で自動再試行。
2172:                conn.execute("PRAGMA user_version = 44")
2180:        if schema_ver < 45:
2195:                conn.execute("PRAGMA user_version = 45")
2204:        if schema_ver < 46:
2269:            # 失敗時は schema_ver < 46 のまま = 次回 init_db で自動再試行。
2275:                conn.execute("PRAGMA user_version = 46")
2282:        if schema_ver < 47:
2354:                conn.execute("PRAGMA user_version = 47")
2363:        if schema_ver < 48:
2382:                conn.execute("PRAGMA user_version = 48")
2389:        if schema_ver < 49:
2412:                conn.execute("PRAGMA user_version = 49")
2418:        # H-B 対策: drift recovery を schema_ver と独立に毎回 check (W149 v2 設計と同型).
2450:        # (1) 列存在 check & 欠損 ALTER (schema_ver 無関係 / H-B drift recovery)
2506:            if schema_ver < 50:
2507:                conn.execute("PRAGMA user_version = 50")
2508:                logger.info("[init_db v50] schema_ver bumped to 50")
2559:                if schema_ver < 52:
2560:                    conn.execute("PRAGMA user_version = 52")
2561:                    logger.info("[init_db v52] schema_ver bumped to 52")
2567:        if schema_ver < 54:
2590:                conn.execute("PRAGMA user_version = 54")
2591:                logger.info("[init_db v54] schema_ver bumped to 54")
2599:        if schema_ver < 55:
2652:                conn.execute("PRAGMA user_version = 55")
2653:                logger.info("[init_db v55] schema_ver bumped to 55")

 succeeded in 494ms:
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

    # 繝・ヵ繧ｩ繝ｫ繝医し繧､繝郁ｨｭ螳壹ｒ謚募・・亥ｭ伜惠縺励↑縺・ｴ蜷医・縺ｿ・・    with get_conn() as conn:
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

    # 繝槭う繧ｰ繝ｬ繝ｼ繧ｷ繝ｧ繝ｳ: ebay_listings 縺ｫ rank 繧ｫ繝ｩ繝繧定ｿｽ蜉
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE ebay_listings ADD COLUMN rank TEXT DEFAULT 'C'")
        except sqlite3.OperationalError:
            # rank 繧ｫ繝ｩ繝縺梧里縺ｫ蟄伜惠縺吶ｋ蝣ｴ蜷医・繧ｹ繧ｭ繝・・
            pass

    # 繝槭う繧ｰ繝ｬ繝ｼ繧ｷ繝ｧ繝ｳ: 繝｡繝医Μ繧ｯ繧ｹ繝ｻ繝ｩ繝ｳ繧ｯ險育ｮ礼畑繧ｫ繝ｩ繝繧定ｿｽ蜉
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

 succeeded in 513ms:
def delete_site_config(config_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM site_configs WHERE id=?", (config_id,))


def find_site_config_by_sku(sku: str) -> Optional[dict]:
    """SKU繝励Ξ繝輔ぅ繝・け繧ｹ縺九ｉ繧ｵ繧､繝郁ｨｭ螳壹ｒ讀懃ｴ｢"""
    configs = get_site_configs()
    for cfg in configs:
        prefix = cfg.get("convert_url", "")
        if prefix and sku.startswith(prefix):
            return cfg
    return None


def build_source_url(sku: str) -> Optional[str]:
    """SKU 竊・莉募・蜈ザRL繧堤函謌・""
    cfg = find_site_config_by_sku(sku)
    if not cfg:
        return None
    prefix = cfg["convert_url"]
    item_id = sku[len(prefix):]
    common = cfg.get("common_url", "")
    return common + item_id if common else None


def find_site_config_by_url(url: str) -> Optional[dict]:
    """URL 縺九ｉ site_config 繧呈､懃ｴ｢ (SKU prefix 髱樔ｾ晏ｭ倥仝183).

    url_keyword 縺ｮ驛ｨ蛻・ｸ閾ｴ縺ｧ蛻､螳壹・mazon/讌ｽ螟ｩ遲峨ｒ逶ｴ謗･ URL 縺ｧ逶｣隕悶☆繧矩圀縲・    SKU prefix 縺ｫ鬆ｼ繧峨★ site 縺ｮ蝨ｨ蠎ｫ蛻､螳壽枚蟄怜・ (in_stock/sold_out/no_page) 繧貞ｼ輔￥縲・    """
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
    """listing 縺ｮ source_url 繧呈焔蜍戊ｨｭ螳壹＠ SKU 蜷梧悄荳頑嶌縺阪°繧我ｿ晁ｭｷ縺吶ｋ (W183).

    manual=True : source_url 繧堤峩謗･險ｭ螳・+ source_url_manual=1 縺ｧ蝗ｺ螳壹ゆｻ･蠕・                  upsert_item / upsert_ebay_listing / _sync_monitored_items_sku 縺ｯ
                  縺薙・ URL 繧・SKU 豢ｾ逕溘〒荳頑嶌縺阪＠縺ｪ縺・・    manual=False: 蝗ｺ螳夊ｧ｣髯､ (source_url_manual=0)縲４KU 豢ｾ逕溘↓謌ｻ繧九・
    listing 隴伜挨縺ｯ ebay_item_id (sku-rules.md 貅匁侠)縲Ｆbay_listings 繧呈峩譁ｰ縺励・    蜷・ebay_item_id 縺ｮ monitored_items 縺後≠繧後・蜷梧悄縲Ｔite_config_id 縺ｯ URL 縺九ｉ隗｣豎ｺ縲・    Returns: ebay_listings 繧呈峩譁ｰ縺ｧ縺阪◆繧・True / listing 荳榊惠縺ｧ False.
    """
    if not ebay_item_id:
        return False
    src = (source_url or "").strip()
    cfg = find_site_config_by_url(src) if src else None
    site_config_id = cfg["id"] if cfg else None
    manual_flag = 1 if manual else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        # 譌｢蟄・listing 蜿門ｾ・(荳榊惠縺ｪ繧・False / URL 螟画峩讀懃衍 / 逶｣隕門床蟶ｳ譁ｰ隕丈ｽ懈・逕ｨ)
        row = conn.execute(
            "SELECT source_url, sku, title FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if row is None:
            return False
        url_changed = (row[0] or "") != (src or "")
        if url_changed:
            # URL 縺悟､峨ｏ縺｣縺溘ｉ譌ｧ蝨ｨ蠎ｫ蛻､螳壹・辟｡蜉ｹ 竊・谺｡蝗・inventory_check 縺悟・隧穂ｾ｡
            # (upsert_ebay_listing 髱・manual 邨瑚ｷｯ縺ｨ蜷・semantics)縲・            conn.execute(
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
        # W183 HIGH-1 (code-reviewer 2026-05-28): manual=True 縺ｧ逶｣隕門床蟶ｳ縺ｫ譛ｪ逋ｻ骭ｲ縺ｪ繧・        # 譁ｰ隕・INSERT縲ゅ％繧後′辟｡縺・→ ensure_monitor_coverage 縺悟ｾ後〒 SKU 豢ｾ逕・URL 縺ｧ
        # monitored_items 陦後ｒ菴懊ｊ縲∵焔蜍・URL 縺瑚ｪ､ URL 縺ｫ豎壽沒縺輔ｌ繧・(W139 蜷悟梛縺ｮ
        # 莉募・蜈・OOS 隕矩・＠ 竊・螻･陦御ｸ崎・)縲Ｍisting 隴伜挨縺ｯ ebay_item_id (sku-rules)縲・        if manual and (upd.rowcount or 0) == 0:
            conn.execute(
                "INSERT INTO monitored_items (ebay_item_id, title, sku, source_url, "
                "site_config_id, source_url_manual, source_url_updated_at, is_active) "
                "VALUES (?,?,?,?,?,?,?,1)",

 succeeded in 566ms:
C:\Users\gucch\projects\claude\.claude\rules\db-migration-rules.md

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path ..\\..\\..\\.claude\\rules\\db-migration-rules.md" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path ..\\..\\..\\.claude\\rules\\sku-rules.md" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path ..\\..\\..\\.claude\\rules\\silent-skip-prevention.md" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\database.py | Select-Object -Skip 1598 -First 1070" in C:\Users\gucch\projects\claude\tools\ebay-manager
2026-05-28T14:27:18.786064Z ERROR codex_core::tools::router: error=Exit code: 1
Wall time: 0.5 seconds
Output:
Get-Content : Cannot find path 'C:\Users\gucch\projects\.claude\rules\db-migration-rules.md' because it does not exist.
At line:2 char:1
+ Get-Content -Path ..\..\..\.claude\rules\db-migration-rules.md
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...ration-rules.md:String) [Get-Content], ItemNotFoundEx 
   ception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand
 

 exited 1 in 454ms:
Get-Content : Cannot find path 'C:\Users\gucch\projects\.claude\rules\db-migration-rules.md' because it does not exist.
At line:2 char:1
+ Get-Content -Path ..\..\..\.claude\rules\db-migration-rules.md
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...ration-rules.md:String) [Get-Content], ItemNotFoundEx 
   ception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand
 

 exited 1 in 454ms:
Get-Content : Cannot find path 'C:\Users\gucch\projects\.claude\rules\sku-rules.md' because it does not exist.
At line:2 char:1
+ Get-Content -Path ..\..\..\.claude\rules\sku-rules.md
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...es\sku-rules.md:String) [Get-Content], ItemNotFoundEx 
   ception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand
 

2026-05-28T14:27:18.786911Z ERROR codex_core::tools::router: error=Exit code: 1
Wall time: 0.5 seconds
Output:
Get-Content : Cannot find path 'C:\Users\gucch\projects\.claude\rules\sku-rules.md' because it does not exist.
At line:2 char:1
+ Get-Content -Path ..\..\..\.claude\rules\sku-rules.md
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...es\sku-rules.md:String) [Get-Content], ItemNotFoundEx 
   ception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand
 

2026-05-28T14:27:18.788259Z ERROR codex_core::tools::router: error=Exit code: 1
Wall time: 0.5 seconds
Output:
Get-Content : Cannot find path 'C:\Users\gucch\projects\.claude\rules\silent-skip-prevention.md' because it does not ex
ist.
At line:2 char:1
+ Get-Content -Path ..\..\..\.claude\rules\silent-skip-prevention.md
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...p-prevention.md:String) [Get-Content], ItemNotFoundEx 
   ception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand
 

 exited 1 in 455ms:
Get-Content : Cannot find path 'C:\Users\gucch\projects\.claude\rules\silent-skip-prevention.md' because it does not ex
ist.
At line:2 char:1
+ Get-Content -Path ..\..\..\.claude\rules\silent-skip-prevention.md
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : ObjectNotFound: (C:\Users\gucch\...p-prevention.md:String) [Get-Content], ItemNotFoundEx 
   ception
    + FullyQualifiedErrorId : PathNotFound,Microsoft.PowerShell.Commands.GetContentCommand
 

 succeeded in 531ms:
                        WHERE pmc.market_analysis_id IS NOT NULL
                    """)
                except sqlite3.OperationalError:
                    # 譌ｧ繝・・繝悶Ν譌｢縺ｫ script 縺ｧ豸医∴縺ｦ縺・ｋ蝣ｴ蜷・(蜀榊ｮ溯｡後す繝翫Μ繧ｪ)
                    pass

            # canonical MSD 縺ｮ ebay_item_id 蛻怜ｭ伜惠 + NOT NULL 蛻ｶ邏・ｒ蜴ｳ蟇・メ繧ｧ繝・け.
            # 蛻励・縺ゅｋ縺・NULLABLE 縺ｪ荳ｭ髢鍋憾諷九〒繧・_new 菴懈・ path 繧貞ｮ溯ｵｰ (review M-2 蟇ｾ蠢・.
            msd_eii = next(
                (
                    r for r in conn.execute(
                        "PRAGMA table_info(market_strategy_decisions)"
                    ).fetchall() if r[1] == "ebay_item_id"
                ),
                None,
            )
            if msd_eii is None or msd_eii[3] != 1:
                # (b) market_strategy_decisions_new: listing 邊貞ｺｦ (ebay_item_id NOT NULL)
                # ALTER 縺ｧ NOT NULL 蠕御ｻ倥￠荳榊庄縺ｪ縺ｮ縺ｧ譁ｰ繝・・繝悶Ν菴懈・ (B option, user 謖・､ｺ).
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
                # 譌ｧ decisions 縺ｯ Phase 1 縺ｧ蜈ｨ蜑企勁貂・(遨ｺ) 竊・蠑戊ｶ翫＠ SQL 荳崎ｦ・
            conn.execute("PRAGMA user_version = 26")

        # v27 (W50 / 2026-04-30): Yahoo Auctions 縺ｮ ebayyh_ prefix 繧・site_configs 縺ｫ seed.
        # 譌ｧ邨瑚ｷｯ (CSV) 縺ｯ ebayh_縲∵眠邨瑚ｷｯ (DB monitored_items) 縺ｯ ebayyh_ 縺ｧ
        # convert_url 荳堺ｸ閾ｴ縺縺｣縺溘◆繧・prepare_batch_items 縺悟・莉ｶ髯､螟悶☆繧句撫鬘後∈縺ｮ譬ｹ譛ｬ蟇ｾ蠢・
        # convert_url UNIQUE 蛻ｶ邏・+ INSERT OR IGNORE = 蜀ｪ遲・(蜀榊ｮ溯｡後〒驥崎､・＠縺ｪ縺・.
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

        # v28 (W72 / 2026-05-01): monitored_items.UNIQUE(sku) 謦､蟒・
        # 譌ｧ DB 縺ｯ scripts/migrate_monitored_items_v28.py one-shot 縺ｧ RECREATE.
        # 譁ｰ隕冗腸蠅・・ L381 縺梧里縺ｫ譁ｰ繧ｹ繧ｭ繝ｼ繝・(UNIQUE 縺ｪ縺・ 竊・init_db 縺ｧ螳檎ｵ・
        # Q2 隕丞ｮ・(init_db 蜀・DROP TABLE 遖∵ｭ｢) 縺ｮ縺溘ａ閾ｪ蜍・RECREATE 縺励↑縺・
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
                # 譌｢蟄・v27 DB 縺ｧ UNIQUE(sku) 谿句ｭ・= one-shot script 譛ｪ螳溯｡・
                # init_db 蜀・RECREATE 縺ｯ Q2 隕丞ｮ壹〒遖∵ｭ｢縺ｮ縺溘ａ縲「ser 騾夂衍縺ｮ縺ｿ.
                import logging as _lg
                _lg.getLogger(__name__).warning(
                    "v28 migration pending: monitored_items 縺ｫ UNIQUE(sku) 谿句ｭ・ "
                    "scripts/migrate_monitored_items_v28.py 繧貞ｮ溯｡後＠縺ｦ縺上□縺輔＞."
                )

        # v29 (W94 / 2026-05-02): supplier_eval_pending DLQ table.
        # Anthropic Batch API + Cache stack 邨ｱ蜷・(id=181) 縺ｮ Tier 3 fallback 蜿励￠逧ｿ.
        # batch hard_timeout / errored 蠕後・騾壼ｸｸ API fallback 繧ょ､ｱ謨励＠縺・item 繧剃ｿ晏ｭ・
        # CREATE TABLE IF NOT EXISTS 縺ｧ蜀ｪ遲・ Q2: DROP/ALTER 荳堺ｽｿ逕ｨ.
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
                # Q2 silent skip 髦ｲ豁｢: 蜊頑・遶・(CREATE 騾壹▲縺ｦ PRAGMA 螟ｱ謨・遲・ 繧・surface.
                import logging as _lg29
                _lg29.getLogger(__name__).warning(
                    f"v29 migration (supplier_eval_pending DLQ) skipped: {e}"
                )

        # v30 (W98 / 2026-05-05): 譛螳牙､繝√ぉ繝・け (lowest price check) 逕ｨ繧ｫ繝ｩ繝霑ｽ蜉.
        # ebay_listings 縺ｫ 莉募・萓｡譬ｼ / 譛菴惹ｾ｡譬ｼ(荳矩剞) / 譛菴主茜逶贋ｾ｡譬ｼ繧ｭ繝｣繝・す繝･ 繧定ｿｽ蜉.
        # ALTER TABLE ADD COLUMN 縺ｯ驥崎､・←逕ｨ縺ｧ縲慧uplicate column name縲阪お繝ｩ繝ｼ 竊・蛟句挨 try/except.
        if schema_ver < 30:
            for _col_sql in (
                "ALTER TABLE ebay_listings ADD COLUMN purchase_yen REAL",
                "ALTER TABLE ebay_listings ADD COLUMN lp_min_price REAL",
                "ALTER TABLE ebay_listings ADD COLUMN lp_breakeven_usd REAL",
            ):
                try:
                    conn.execute(_col_sql)
                except sqlite3.OperationalError:
                    pass  # 譌｢縺ｫ蛻怜ｭ伜惠 = OK
            conn.execute("PRAGMA user_version = 30")

        # v31 (W98 / 2026-05-05): 譁ｰ隕冗匱隕九Λ繧､繝舌Ν縺ｮ騾∵侭繧ｫ繝ｩ繝霑ｽ蜉.
        # eBay GetItem API 縺ｧ蜿門ｾ励＠縺滄∵侭繧帝・蠎ｦ繧ｭ繝｣繝・す繝･.
        if schema_ver < 31:
            try:
                conn.execute(
                    "ALTER TABLE new_competitor_alerts ADD COLUMN found_shipping REAL"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 31")

        # v32 (W98 / 2026-05-05): 繝ｩ繧､繝舌Ν萓｡譬ｼ繝ｻ騾∵侭縺ｮ DB 繧ｭ繝｣繝・す繝･ + 閾ｪ蜍募叙蠕玲律譎・
        # UI 縺ｧ id/萓｡譬ｼ/騾∵侭/蜷郁ｨ・縺ｮ 4 諠・ｱ陦ｨ遉ｺ縺吶ｋ縺溘ａ縲。rowse API 蜿門ｾ怜､繧剃ｿ晄戟.
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

        # v33 (W183 / 2026-05-10): 蛟､荳九￡螻･豁ｴ繝・・繝悶Ν.
        # 6h scheduler / 謇句虚 button 縺ｫ繧医ｋ ReviseFixedPriceItem 螳溯｡悟ｱ･豁ｴ繧貞・莉ｶ險倬鹸.
        # L2 (1 譌･ 4 蝗樔ｸ企剞) 縺ｮ蛻､螳・+ UI 螻･豁ｴ陦ｨ遉ｺ + retrospective audit 縺ｫ菴ｿ縺・
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

        # v34 (W119 / 2026-05-10): ebay_listings 縺ｫ讀懃ｴ｢繧ｭ繝ｼ繝ｯ繝ｼ繝・3 蛻励ｒ霑ｽ蜉.
        # search_keyword: Opus 4.7 batch 縺ｧ title 縺九ｉ謚ｽ蜃ｺ縺励◆ eBay 讀懃ｴ｢繝ｯ繝ｼ繝・        # search_keyword_generated_at: 逕滓・譌･譎・(蜀咲函謌仙愛螳夂畑)
        # search_keyword_source: 'opus_batch' | 'manual_edit' (蜃ｺ謇隴伜挨)
        # Browse API 遶ｶ蜷域､懃ｴ｢ + eBay 繝ｪ繝ｳ繧ｯ繝懊ち繝ｳ URL 縺ｮ荳｡譁ｹ縺ｧ菴ｿ逕ｨ.
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

        # v35 (W119 / 2026-05-11): competitor_products 縺ｫ handling/delivery 蛻励ｒ霑ｽ蜉.
        # Browse API 縺ｯ handling_time 繧堤峩謗･霑斐＆縺ｪ縺・◆繧√・        # min_estimated_delivery_date / max_estimated_delivery_date 繧剃ｿ晏ｭ倥＠縲・        # UI 蛛ｴ縺ｧ縲檎匱騾∫岼螳画律謨ｰ縲阪ｒ險育ｮ苓｡ｨ遉ｺ縺吶ｋ.
        # 蝠・刀邂｡逅・ち繝悶・繝ｩ繧､繝舌Ν dataframe 縺ｧ陦ｨ遉ｺ.
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

        # v36 (W119 / 2026-05-12): ebay_listings.inventory_count 霑ｽ蜉.
        # 蝠・刀邂｡逅・ち繝悶〒 user 縺・譛牙惠蠎ｫ (SKU 縺・"stock" prefix) 縺ｮ迚ｩ逅・惠蠎ｫ謨ｰ繧貞・蜉・
        # 螢ｲ繧後◆繧・task_order_alert.py 縺・GetOrders API 縺ｧ讀懃衍 竊・閾ｪ蜍墓ｸ帷ｮ・
        # quantity_ebay (= eBay 蜃ｺ蜩∵焚) 縺ｨ縺ｯ蛻･邂｡逅・= user 縺ｮ迚ｩ逅・惠蠎ｫ邂｡逅・畑.
        if schema_ver < 36:
            try:
                conn.execute(
                    "ALTER TABLE ebay_listings ADD COLUMN inventory_count INTEGER"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 36")

        # v37 (W119 / 2026-05-12 Wave A): inventory_decrement_log 繧・init_db 縺ｫ髮・ｴ・
        # task_order_alert._decrement_inventory_for_stock_sku 蜀・CREATE TABLE 繧定ｧ｣豸・(Q2).
        # UNIQUE(order_id, ebay_item_id) 縺ｧ蜷・order 莠碁㍾ polling 繧貞・遲牙喧.
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

        # v38 (W120+W121 / 2026-05-12): 莉募・蜈・萓｡譬ｼ螟牙虚讀懃衍 + 讌ｽ螟ｩ text 陬懷ｮ・
        # 1) monitored_items 縺ｫ 4 蛻・ALTER (baseline / current / baseline_at / alert_state)
        # 2) 讌ｽ螟ｩ蟶ょｴ site_config 縺ｮ sold_out_text / no_page_text 繧・UPDATE (DEFAULT_SITE_CONFIGS 縺ｯ遨ｺ譁・ｭ励∬ｿｽ蜉縺悟ｿ・ｦ・
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
            # 讌ｽ螟ｩ蟶ょｴ縺ｮ sold_out / no_page text 繧定｣懷ｮ・(DEFAULT 縺ｯ遨ｺ譁・ｭ励∝愛螳壻ｸ崎・縺縺｣縺・.
            # 縲悟｣ｲ繧雁・繧後阪後＃謖・ｮ壹・繝壹・繧ｸ縺ｯ隕九▽縺九ｊ縺ｾ縺帙ｓ縲阪・讌ｽ螟ｩ蟶ょｴ縺ｧ鬆ｻ蜃ｺ縺吶ｋ讓呎ｺ冶｡ｨ險・
            try:
                conn.execute(
                    """UPDATE site_configs
                       SET sold_out_text = ?, no_page_text = ?
                       WHERE convert_url = 'ebayRT_'
                         AND (sold_out_text IS NULL OR sold_out_text = '')""",
                    ("螢ｲ繧雁・繧・, "縺疲欠螳壹・繝壹・繧ｸ縺ｯ隕九▽縺九ｊ縺ｾ縺帙ｓ"),
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 38")

        # v39 (W122 / 2026-05-13): morning_discovery_candidates 譁ｰ隕上ユ繝ｼ繝悶Ν.
        # 譛・07:00 縺ｫ Opus 4.7 縺檎匱謗倥＠縺滓眠蝠・刀蛟呵｣・3 莉ｶ繧剃ｿ晏ｭ倥＠縲・        # 繧ｯ繝ｪ繝・け蝙玖ｩ穂ｾ｡ (buy/skip/hold/listed) + 閾ｪ逕ｱ險倩ｿｰ (user_comment) 縺ｧ
        # 鄙梧律莉･髯阪・ Few-shot 繝励Ο繝ｳ繝励ヨ縺ｫ蟄ｦ鄙貞渚譏縺吶ｋ.
        # 隕ｪ = research_qa (source='morning_discovery') / 1 QA = 3 蛟呵｣・
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

        # v40 (W133 / 2026-05-16): 譛牙惠蠎ｫ邂｡逅・窶・eBay 蝨ｨ蠎ｫ謨ｰ sync 逞戊ｷ｡ + 莉募・遒ｺ隱阪Ο繧ｰ.
        # 1) ebay_listings 縺ｫ 3 蛻・ALTER:
        #    last_qty_sync_at      : 譛邨・ReviseInventoryStatus 謌仙粥譎ょ綾
        #    last_synced_quantity  : 縺昴・譎・eBay 縺ｸ騾√▲縺滓焚驥・(= inventory_count)
        #    qty_sync_error        : sync 螟ｱ謨・/ 謨ｰ驥・ revise 謚第ｭ｢ 縺ｮ逅・罰 (NULL=豁｣蟶ｸ)
        #    竊・Q0 silent skip 髦ｲ豁｢: sync 螟ｱ謨励′ DB 蛻励↓蠢・★谿九ｋ (UI + Discord 縺ｨ菴ｵ逕ｨ).
        # 2) purchase_confirmation_log 譁ｰ隕・
        #    莉募・蜈･闕ｷ繝｡繝ｼ繝ｫ 竊・user 縺・listing 縺ｨ莉募・蛟区焚繧堤｢ｺ螳壹＠縺溷ｱ･豁ｴ.
        #    dedupe = UNIQUE(gmail_id, ebay_item_id). SKU 縺ｯ dedupe 繧ｭ繝ｼ縺ｫ **蜷ｫ繧√↑縺・*
        #    (SKU 縺ｯ蝨ｨ蠎ｫ遞ｮ蛻･繝輔Λ繧ｰ縺ｧ縺ゅ▲縺ｦ listing 隴伜挨繧ｭ繝ｼ縺ｧ縺ｯ縺ｪ縺・/ sku-rules.md).
        #    listing 隴伜挨縺ｯ ebay_item_id 蜊倅ｽ・(migration v26 / W7-A 蜊倅ｽ榊喧貅匁侠).
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

        # v41 (W138-A / 2026-05-17): shipping policy (BP) 繧・DB 蛻怜喧縺・        # 蝠・刀邂｡逅・hero 縺ｫ萓｡譬ｼ蜷梧ｧ倥梧怙蛻昴°繧芽・蜍戊｡ｨ遉ｺ縲阪・        #    shipping_profile_id        : eBay Business Policy (fulfillment) ID縲・        #                                 NULL/'' 縺ｯ譁・ц萓晏ｭ・(荳玖ｨ・fetched_at 縺ｨ菴ｵ縺・3 蛻・ｲ・:
        #    shipping_profile_fetched_at : 蠖楢ｩｲ BP 繧貞ｮ・eBay GetItem 縺九ｉ譛蠕後↓蜿門ｾ励＠縺・        #                                 譎ょ綾 (UTC縲《qlite-timezone.md 貅匁侠)縲・        #    蛻､螳・(HIGH-2 NULL 螟夂ｾｩ諤ｧ隗｣豸・: fetched_at IS NULL=譛ｪ蜿門ｾ・(Inline 縺ｨ譁ｭ螳壻ｸ榊庄) /
        #      fetched_at NOT NULL & id NULL/''=遒ｺ螳・Inline (BP 縺ｪ縺・ /
        #      fetched_at NOT NULL & id 縺ゅｊ=BP 縺ゅｊ.
        #    魄ｮ蠎ｦ: GetMyeBaySelling 縺ｯ BP 繧定ｿ斐＆縺ｪ縺・◆繧∝ｮ壽悄 task_ebay_sync 縺ｫ
        #      逶ｸ荵励ｊ荳榊庄縲を沒､eBay蜿肴丐譎・_sync_db_to_actual / 蜊倡匱 竊ｻ 蜀榊叙蠕・/
        #      蛻晏屓 backfill 縺ｧ縺ｮ縺ｿ譖ｴ譁ｰ (= 萓｡譬ｼ縺ｫ蜉｣繧矩ｮｮ蠎ｦ繧・fetched_at 菴ｵ險倥〒豁｣逶ｴ髢狗､ｺ).
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

        # v42 (W7/W183 H4 race 蝣・欧蛹・/ 2026-05-17): price_change_log 縺ｫ
        # claim_status 繧定ｿｽ蜉縲ょ､荳九￡ 1 蝗槫・縺ｮ縲御ｺ育ｴ・(reservation)縲阪ｒ eBay API
        # 蜻ｼ蜃ｺ縺ｮ蜑阪↓遒ｺ菫昴＠縲《cheduler 繝励Ο繧ｻ繧ｹ縺ｨ Streamlit 繝励Ο繧ｻ繧ｹ縺悟酔縺・        # listing 繧貞酔譎ょ・逅・＠縺ｦ 1 譌･ 4 蝗樔ｸ企剞繧定ｶ・∴繧・race (H4) 繧帝亟縺舌・        #   claim_status: NULL      = legacy / 逶ｴ謗･險倬鹸陦・(success 縺ｧ邨先棡蛻､螳・
        #                 'pending' = 莠育ｴ・｢ｺ菫晄ｸ医・API 螳溯｡御ｸｭ (譫繧剃ｸ譎よｶ郁ｲｻ)
        #                 'final'   = 遒ｺ螳・(success=1 縺ｧ譫豸郁ｲｻ邯咏ｶ・/
        #                             success=0 縺ｧ譫隗｣謾ｾ = 螟ｱ謨励・譛ｬ譌･ 4 蝗槭↓
        #                             繧ｫ繧ｦ繝ｳ繝医＠縺ｪ縺・/ user 遒ｺ螳・2026-05-17)
        if schema_ver < 42:
            try:
                conn.execute(
                    "ALTER TABLE price_change_log ADD COLUMN claim_status TEXT"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA user_version = 42")

        # v43 (W142 / 2026-05-19): 騾∵侭 +each (ShippingServiceAdditionalCost)
        # 繧・DB 蛻怜喧縲よｹ譛ｬ蜴溷屏#5(b): ebay_listings 縺ｫ +each 菫晏ｭ伜・縺檎┌縺・◆繧・        # 蝠・刀邂｡逅・ち繝悶・縲碁∵侭 +each縲榊・蜉帙′蟶ｸ譎・value=None (陦ｨ遉ｺ source 逧・┌)縲・        # shipping_cost (v1) 縺ｨ蟇ｾ遘ｰ縺縺・DEFAULT 繧剃ｻ倥￠縺ｪ縺・(= NULL)縲ら炊逕ｱ:
        #   shipping_cost 縺ｯ v1 縺ｧ蜈ｨ listing 0.0 蛻晄悄蛹匁ｸ医・譌｢蟄倅ｺ句ｮ溘′縺ゅｊ莉翫・
        #   螟峨∴縺ｪ縺・(K2) 縺後・each 縺ｯ譁ｰ隕丞・縺ｪ縺ｮ縺ｧ縲梧悴蜿門ｾ・NULL縲阪→縲梧・遉ｺ
        #   $0.00縲阪ｒ譛蛻昴°繧牙玄蛻･縺ｧ縺阪＼sync_db_to_actual 縺ｮ None-skip 諷｣鄙・        #   (snap 縺ｮ蛟､縺・None 縺ｮ鬆・岼縺ｯ隗ｦ繧峨↑縺・ 縺ｨ謨ｴ蜷医☆繧・(HIGH-2 NULL
        #   螟夂ｾｩ諤ｧ縺ｮ雕剰･ｲ)縲・        #   shipping_additional_cost        : Domestic ShippingServiceAdditional
        #                                     Cost (2 蛟狗岼莉･髯阪・霑ｽ蜉騾∵侭 USD)縲・        #   shipping_additional_fetched_at  : 螳・eBay GetItem 縺九ｉ譛蠕後↓蜿門ｾ・        #                                     縺励◆譎ょ綾 (UTC縲《qlite-timezone.md)縲・        #     譖ｴ譁ｰ蜈・・ shipping_profile 蜷梧ｧ・豆eBay蜿肴丐譎・_sync_db_to_actual /
        #     蜊倡匱 竊ｻ 蜀榊叙蠕励・縺ｿ (螳壽悄 sync 縺ｯ GetMyeBaySelling 縺瑚ｿ斐＆縺夂嶌荵励ｊ
        #     荳榊庄縲’etched_at 菴ｵ險倥〒魄ｮ蠎ｦ繧呈ｭ｣逶ｴ髢狗､ｺ)縲・        if schema_ver < 43:
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

        # v44 (W140 / 2026-05-19): listing 蜊倅ｽ阪Γ繝｢ + 螢ｲ蜊ｴ譎りｭｦ蜻翫・        # listing 隴伜挨縺ｯ ebay_item_id (sku-rules.md 蜴ｳ螳医ヾKU 繧偵く繝ｼ縺ｫ縺励↑縺・縲・        #   listing_notes        : 1 listing = 1 閾ｪ逕ｱ繝｡繝｢ (逋ｺ騾・騾夐未縺ｮ豕ｨ諢冗せ)縲・        #                          eBay 縺ｸ縺ｯ騾√ｉ縺・MonoDeck DB 縺ｮ縺ｿ菫晄戟縲Ｓelist
        #                          縺ｧ ebay_item_id 縺悟､峨ｏ繧九→蛻･繝ｬ繧ｳ繝ｼ繝・= 譌ｧ繝｡繝｢
        #                          縺ｯ谿句ｭ・(繝・・繧ｿ豸亥､ｱ縺ｪ縺・縲り・蜍募・蜃ｺ蜩√・
        #                          inherit_listing_on_relist 縺梧立竊呈眠縺ｸ蠑輔″邯吶＄縲・        #   listing_sale_warnings: 繝｡繝｢莉倥″ listing 縺悟｣ｲ繧後◆譎ゅ・隴ｦ蜻翫・        #                          UNIQUE(order_id, ebay_item_id) 縺ｧ
        #                          claim-then-act (莠碁㍾ polling/Discord 髦ｲ豁｢縲・        #                          譌｢蟄・inventory_decrement_log 縺ｨ蜷悟梛)縲・        #                          status: open|acked|dismissed (蜀埼夂衍縺ｪ縺励・        #                          MonoDeck 繝舌リ繝ｼ縺ｯ open 縺ｮ縺ｿ)縲Ｏote_snapshot
        #                          = 螢ｲ蜊ｴ譎らせ縺ｮ繝｡繝｢ (蠕後・邱ｨ髮・〒險ｼ霍｡繧貞､ｱ繧上↑縺・縲・        # Q2: CREATE IF NOT EXISTS 縺ｮ縺ｿ繝ｻDROP/DELETE 縺ｪ縺・= init_db 2 蝗槭〒繧ゆｿ晄戟縲・        if schema_ver < 44:
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
            # Codex 2谿ｵ HIGH-2 (Q2 閾ｪ蟾ｱ菫ｮ蠕ｩ): CREATE 縺御ｸ・ｸ OperationalError
            # (disk full / lock 遲・ 縺ｧ謠｡繧頑ｽｰ縺輔ｌ縺溷ｴ蜷医↓ version 縺縺鷹ｲ繧縺ｨ縲・            # 谺｡蝗樔ｻ･髯・`if schema_ver < 44` 繧・skip 縺・W140 (Q0 螳牙・邯ｲ) 縺・            # 豌ｸ荵・ｬ關ｽ縺吶ｋ縲ゆｸ｡繝・・繝悶Ν螳溷惠繧堤｢ｺ隱阪〒縺阪◆譎ゅ・縺ｿ迚域焚繧帝ｲ繧√・            # 螟ｱ謨玲凾縺ｯ schema_ver < 44 縺ｮ縺ｾ縺ｾ = 谺｡蝗・init_db 縺ｧ閾ｪ蜍募・隧ｦ陦後・            # (譌｢蟄・v40-v43 縺ｮ辟｡譚｡莉ｶ bump 縺ｯ K2 縺ｧ譛ｬ PR 縺ｧ縺ｯ隗ｦ繧峨↑縺・・            #  譁ｰ隕・W140 繝悶Ο繝・け縺ｮ縺ｿ蝣・欧蛹・= db-migration-rules Q2 雜｣譌ｨ)
            _w140_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('listing_notes','listing_sale_warnings')"
            ).fetchone()[0]
            if _w140_ok == 2:
                conn.execute("PRAGMA user_version = 44")

        # v45 (W133-FU / 2026-05-21): 辟｡蝨ｨ蠎ｫ縺悟｣ｲ繧後◆莉募・ (fulfillment) 縺ｨ
        # 譛牙惠蠎ｫ陬懷・ (restock) 繧貞玄蛻･縺吶ｋ縺溘ａ purchase_confirmation_log 縺ｫ
        # fulfillment_kind 蛻苓ｿｽ蜉 (additive nullable縲＿2 蜀ｪ遲・縲・        # 'restock' = 譛牙惠蠎ｫ SKU 縺ｮ蝨ｨ蠎ｫ陬懷・ (inventory_count 蜉邂励☆繧句ｾ捺擂蜍穂ｽ・
        # 'fulfillment' = 辟｡蝨ｨ蠎ｫ SKU 縺悟｣ｲ繧後※逋ｺ豕ｨ縺励◆莉募・ (inventory 蜉邂励＠縺ｪ縺・・        #                 purchase_confirmation_log 縺ｧ縲御ｻ募・螳御ｺ・阪・繝ｼ繧ｭ繝ｳ繧ｰ縺ｮ縺ｿ)
        if schema_ver < 45:
            try:
                conn.execute(
                    "ALTER TABLE purchase_confirmation_log "
                    "ADD COLUMN fulfillment_kind TEXT DEFAULT 'restock'"
                )
            except sqlite3.OperationalError:
                pass  # 譌｢蟄伜・縺ゅｊ縺ｧ OK (蜀ｪ遲・
            # 閾ｪ蟾ｱ菫ｮ蠕ｩ: 蛻怜ｭ伜惠遒ｺ隱榊ｾ後↓縺ｮ縺ｿ version bump (W140 v44 縺ｨ蜷梧ｵ∝о)縲・            _cols = [
                r[1] for r in conn.execute(
                    "PRAGMA table_info(purchase_confirmation_log)"
                ).fetchall()
            ]
            if 'fulfillment_kind' in _cols:
                conn.execute("PRAGMA user_version = 45")

        # v46 (W148 / 2026-05-21): 繧ｭ繝ｼ繝ｯ繝ｼ繝画眠逹逶｣隕・(AlertCrawler 遘ｻ讀・
        # 讀懃ｴ｢ URL : N 蝠・刀 hits 霆ｸ (蝨ｨ蠎ｫ逶｣隕悶→讎ょｿｵ迢ｬ遶九ヾKU 荳堺ｽｿ逕ｨ)縲・        # claim-then-act dedupe = UNIQUE(watch_id, found_item_url) 縺ｧ
        # 莠碁㍾蟾｡蝗槭・莠碁㍾ Discord 繧堤黄逅・賜髯､ (inventory_decrement_log v37 /
        # listing_sale_warnings v44 縺ｨ蜷悟梛 idiom)縲・        # is_sentinel: 繧ｵ繧､繝医・ DOM 螟画峩/bot ban 繧呈､懃衍縺吶ｋ逡ｪ莠ｺ watch (v2.1)縲・        # 蜈ｨ sentinel 縺悟酔譎・0 莉ｶ = site-wide 逡ｰ蟶ｸ 竊・Discord 隴ｦ蜻・1 蝗・run縲・        if schema_ver < 46:
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
            # 閾ｪ蟾ｱ菫ｮ蠕ｩ: 荳｡繝・・繝悶Ν螳溷惠繧堤｢ｺ隱阪＠縺ｦ縺九ｉ version bump (W140 v44 豬∝о)縲・            # 螟ｱ謨玲凾縺ｯ schema_ver < 46 縺ｮ縺ｾ縺ｾ = 谺｡蝗・init_db 縺ｧ閾ｪ蜍募・隧ｦ陦後・            _w148_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('keyword_watches','keyword_watch_hits')"
            ).fetchone()[0]
            if _w148_ok == 2:
                conn.execute("PRAGMA user_version = 46")

        # v47 (W149 / 2026-05-22): eBay 螢ｲ蜊ｴ豕ｨ譁・API 逶ｴ謗･蜿門ｾ・+ fulfillment 閾ｪ蜍輔・繧ゆｻ倥￠
        # (a) sales_history.ebay_order_id 霑ｽ蜉 (UNIQUE 縺ｧ蜀榊ｮ溯｡悟・遲峨！NSERT OR IGNORE 縺ｧ陦晉ｪ・skip).
        # (b) fulfillment_order_link 譁ｰ隕・(purchase_confirmation_log 縺ｨ sales_history 縺ｮ 1:1 蟇ｾ蠢・.
        # (c) sales_history_fetch_failures 譁ｰ隕・(30 min polling 螟ｱ謨・retry queue縲・ 蝗槫､ｱ謨励〒 Discord).
        # 閾ｪ蟾ｱ菫ｮ蠕ｩ (W148 v46 / W140 v44 豬∝о): 蠢・・column/table 蜈ｨ螳溷惠繧・sqlite_master 縺ｧ遒ｺ隱榊ｾ後↓ user_version bump.
        if schema_ver < 47:
            try:
                conn.execute("ALTER TABLE sales_history ADD COLUMN ebay_order_id TEXT")
            except sqlite3.OperationalError:
                pass  # 譌｢蟄・            try:
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
            # 閾ｪ蟾ｱ菫ｮ蠕ｩ: 蠢・・column + 蠢・・table 蜈ｨ螳溷惠繧堤｢ｺ隱阪＠縺ｦ縺九ｉ version bump.
            _cols_sh = [
                r[1] for r in conn.execute("PRAGMA table_info(sales_history)").fetchall()
            ]
            _w149_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('fulfillment_order_link','sales_history_fetch_failures')"
            ).fetchone()[0]
            if 'ebay_order_id' in _cols_sh and _w149_tables == 2:
                conn.execute("PRAGMA user_version = 47")

        # v48 (W149 / 2026-05-22 Phase D self-discover): UNIQUE INDEX 蜈･繧梧崛縺・
        # v47 縺ｮ idx_sales_history_ebay_order_id (ebay_order_id 蜊倡峡) 縺ｯ 1 豕ｨ譁・N 蝠・刀縺ｮ蝣ｴ蜷・        # 蜷・order_id 繧・2 蠎ｦ INSERT 縺ｧ 2 蝗樒岼 UNIQUE 陦晉ｪ・skip = silent line item 豸亥､ｱ
        # (buyer 縺ｾ縺ｨ繧∬ｲｷ縺・凾縺ｫ sales_history 縺・1 陦梧ｬ縺代ｋ縲∝茜逶願ｨ育ｮ励′驛ｨ蛻・ｬ關ｽ縺吶ｋ).
        # 險ｭ險域嶌 v2 ﾂｧ5縲畦ine_item 蜊倅ｽ阪〒 1 陦後★縺､縲阪・諢丞峙縺ｫ蜷医ｏ縺帙∬､・粋繧ｭ繝ｼ
        # (ebay_order_id, ebay_item_id) 縺ｫ螟画峩. backfill dry-run 縺ｧ 101 transaction 竊・        # 100 莉ｶ INSERT 縺ｧ 1 莉ｶ silent skip 縺励◆螳滓ｸｬ縺ｧ逋ｺ隕・
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
            # 閾ｪ蟾ｱ菫ｮ蠕ｩ: 譁ｰ INDEX 螳溷惠遒ｺ隱榊ｾ後↓ version bump
            _v48_ok = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                "AND name = 'idx_sales_history_order_item'"
            ).fetchone()[0]
            if _v48_ok == 1:
                conn.execute("PRAGMA user_version = 48")

        # v49 (W151 / 2026-05-22): 蛻晄悄逋ｻ骭ｲ trackering. user 讌ｭ蜍・= 蜷・listing 縺ｮ
        # 蛻晄悄逋ｻ骭ｲ (繝ｩ繧､繝舌Ν逋ｻ骭ｲ / 迚ｩ逅・ｱ樊ｧ蜈･蜉・/ 莉募・蜈亥呵｣懃｢ｺ螳・遲・ 螳御ｺ・凾縺ｫ
        # checkbox 繧貞・繧後※譛ｪ螳御ｺ・/ 螳御ｺ・ｸ医ｒ繝輔ぅ繝ｫ繧ｿ蜿ｯ閭ｽ蛹・ initial_registered_at
        # 縺ｯ W153 (譁ｰ隕上Λ繧､繝舌Ν逋ｺ隕・ 縺ｮ縲悟・譛溽匳骭ｲ莉･髯阪・ rival縲甲ase point 縺ｨ縺励※蜿ら・.
        # additive nullable column (Q2 蜀ｪ遲画ｧ), 譌｢蟄倥ョ繝ｼ繧ｿ髱樒ｴ螢・
        if schema_ver < 49:
            try:
                conn.execute(
                    "ALTER TABLE ebay_listings ADD COLUMN "
                    "initial_registered INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # 譌｢蟄・            try:
                conn.execute(
                    "ALTER TABLE ebay_listings ADD COLUMN "
                    "initial_registered_at TIMESTAMP"
                )
            except sqlite3.OperationalError:
                pass
            # 閾ｪ蟾ｱ菫ｮ蠕ｩ: 2 蛻怜ｮ溷惠遒ｺ隱榊ｾ後↓ version bump
            _cols_el = [
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ebay_listings)"
                ).fetchall()
            ]
            if ('initial_registered' in _cols_el
                    and 'initial_registered_at' in _cols_el):
                conn.execute("PRAGMA user_version = 49")

        # v50 (W153 / 2026-05-22): 蝠・刀蛻･繝ｩ繧､繝舌Ν讀懷・.
        # listing 隴伜挨縺ｯ ebay_item_id (sku-rules縲ヾKU 荳堺ｽｿ逕ｨ).
        # anchor 縺ｯ MAX(initial_registered_at, rival_watch_started_at) 窶・H-A 蟇ｾ遲・
        # additive nullable 4 蛻・+ 譁ｰ table listing_rival_discoveries + 3 index.
        # H-B 蟇ｾ遲・ drift recovery 繧・schema_ver 縺ｨ迢ｬ遶九↓豈主屓 check (W149 v2 險ｭ險医→蜷悟梛).
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

        # (1) 蛻怜ｭ伜惠 check & 谺謳・ALTER (schema_ver 辟｡髢｢菫・/ H-B drift recovery)
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

        # (2) listing_rival_discoveries table 蟄伜惠 check & 谺謳・CREATE
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

        # (3) index 蟄伜惠 check & 谺謳・CREATE (M-internal-8縲，REATE IF NOT EXISTS 縺ｧ蜀ｪ遲・
        for _idx_sql in _W153_LRD_INDEXES:
            try:
                conn.execute(_idx_sql)
            except sqlite3.OperationalError:
                pass

        # (4) 螳悟・縺ｫ謠・▲縺溷ｾ後〒縺ｮ縺ｿ user_version bump
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

        # v51/v52 (W153 v2 / 2026-05-22 PM): rival shipping 諠・ｱ繧・listing_rival_discoveries
        # 縺ｫ菫晏ｭ・ UI 縺ｧ騾∵侭 + 驟埼＃譌･謨ｰ + 逋ｺ騾∵婿豕募錐陦ｨ遉ｺ + Economy 邉ｻ hide.
        # 讌ｭ蜍咏衍隴・ Economy 邉ｻ縺ｯ螳牙膚蜩√〒 seller 縺御ｽｿ縺・・縺代ｋ縺溘ａ seller block list 縺ｯ隱､繧・        # (reference_ebay_economy_shipping_seller_pattern.md). 讀懃ｴ｢谿ｵ髫・skip 竊・UI hide 縺ｫ螟画峩.
        # v51 縺ｧ 3 蛻・(shipping_cost/min/max_delivery_date)縲」52 縺ｧ shipping_service_code 霑ｽ蜉.
        # search response 縺ｫ縺ｯ shipping_service_code 蜷ｫ縺ｾ繧後↑縺・◆繧∬ｩｳ邏ｰ API enrich 縺ｧ蜿門ｾ・
        # additive nullable縲‥rift recovery 蟇ｾ蠢・(v50 縺ｨ蜷悟梛).
        _W153_V51_LRD_COLS = {
            'competitor_shipping_cost_usd': 'REAL',
            'min_delivery_date': 'TEXT',
            'max_delivery_date': 'TEXT',
            # v52 (2026-05-22 PM): 逋ｺ騾∵婿豕募錐 (get_item_by_legacy_id 邨檎罰縺ｧ蜿門ｾ・.
            # search response 縺ｫ縺ｯ蜷ｫ縺ｾ繧後↑縺・◆繧∵眠隕・rival 縺ｮ縺ｿ隧ｳ邏ｰ API 縺ｧ enrich.
            'shipping_service_code': 'TEXT',
        }
        # listing_rival_discoveries 縺悟ｭ伜惠縺吶ｋ蝣ｴ蜷医・縺ｿ ALTER 隧ｦ陦・        _w153v51_has_lrd = conn.execute(
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

            # 螳悟・縺ｫ謠・▲縺溷ｾ後〒縺ｮ縺ｿ user_version bump
            _w153v51_post = set(
                r[1] for r in conn.execute(
                    "PRAGMA table_info(listing_rival_discoveries)"
                ).fetchall()
            )
            if set(_W153_V51_LRD_COLS).issubset(_w153v51_post):
                if schema_ver < 52:
                    conn.execute("PRAGMA user_version = 52")
                    logger.info("[init_db v52] schema_ver bumped to 52")

        # v54 (W182, 2026-05-28): supplier_candidates 縺ｫ availability check 繧ｫ繝ｩ繝霑ｽ蜉.
        # sold_out 蝠・刀繧・candidate 縺ｨ縺励※逋ｻ骭ｲ縺吶ｋ bug 縺ｮ諱剃ｹ・ｯｾ遲・(Codex 2026-05-28 隱ｿ譟ｻ).
        # 隧ｳ邏ｰ: .company/engineering/migration/codex-supplier-bug-investigation.md
        # v53 縺ｯ W139-revisit Phase 1 (coverage_anomaly_log) 莠育ｴ・ｸ医・縺溘ａ v54 繧呈治逡ｪ.
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

        # v55 (W183, 2026-05-28): EC 繧ｵ繧､繝育峩謗･ URL 辟｡蝨ｨ蠎ｫ蜃ｺ蜩∝ｯｾ蠢・+ 讌ｽ螟ｩ/Amazon 蝨ｨ蠎ｫ蛻､螳壻ｿｮ豁｣.
        # source_url_manual=1 縺ｧ SKU 蜷梧悄縺ｫ繧医ｋ source_url 荳頑嶌縺阪ｒ髦ｲ縺弱、mazon/讌ｽ螟ｩ遲峨・
        # SKU 隕丞援諤ｧ縺ｮ縺ｪ縺・EC 繧ｵ繧､繝医ｒ蝠・刀邂｡逅・〒逶ｴ謗･ URL 險ｭ螳壹＠縺ｦ辟｡蝨ｨ蠎ｫ逶｣隕悶〒縺阪ｋ繧医≧縺ｫ縺吶ｋ.
        # 菴ｵ縺帙※讌ｽ螟ｩ (schema.org microdata) / Amazon (add-to-cart-button) 縺ｮ蝨ｨ蠎ｫ蛻､螳・signal 繧・        # 螳・HTML 縺ｨ荳閾ｴ縺吶ｋ蛟､縺ｫ菫ｮ豁｣ (Codex 2026-05-28 螳滓ｩ溯ｪｿ譟ｻ).
        # 隧ｳ邏ｰ: .company/engineering/migration/codex-ec-direct-url-design.md
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
            # 讌ｽ螟ｩ / Amazon 縺ｮ蝨ｨ蠎ｫ蛻､螳・signal 繧貞ｮ・HTML 荳閾ｴ蛟､縺ｫ菫ｮ豁｣ (蜀ｪ遲・UPDATE).
            # 讌ｽ螟ｩ: schema.org microdata (InStock/OutOfStock 謗剃ｻ・縲よ立 '縺九＃縺ｫ霑ｽ蜉' 縺ｯ
            #       螢ｲ蛻・・繝ｼ繧ｸ縺ｧ繧・disabled button 縺ｨ縺励※谿九ｋ縺溘ａ隱､蛻､螳・(Codex 讀懆ｨｼ).
            # Amazon: id="add-to-cart-button" 縺ｧ荳ｻ繝懊ち繝ｳ迚ｹ螳壹よ立 '繧ｫ繝ｼ繝医↓蜈･繧後ｋ' 縺ｯ
            #         nav / 髢｢騾｣蝠・刀縺ｫ繧ょ・縺ｦ隱､蛻､螳・(Codex 讀懆ｨｼ)縲・APTCHA 縺ｯ scraper 蛛ｴ縺ｧ unknown 蛹・
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
                    ('id="add-to-cart-button"', 'name="submit.add-to-cart"', '迴ｾ蝨ｨ蝨ｨ蠎ｫ蛻・ｌ'),
                )
                logger.info("[init_db v55] 讌ｽ螟ｩ/Amazon site_configs signal 譖ｴ譁ｰ")
            except sqlite3.OperationalError:
                pass
            # 蜈ｨ蛻励′謠・▲縺溷ｾ後〒縺ｮ縺ｿ user_version bump (蜀ｪ遲画ｧ: 驛ｨ蛻・←逕ｨ縺ｧ bump 縺励↑縺・
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


# ---- 繧ｵ繧､繝郁ｨｭ螳・----

def get_site_configs() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM site_configs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def save_site_config(config: dict):
    """繧ｵ繧､繝郁ｨｭ螳壹ｒ菫晏ｭ假ｼ・d謖・ｮ壹≠繧岩・譖ｴ譁ｰ縲√↑縺冷・譁ｰ隕擾ｼ・""
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
    """SKU繝励Ξ繝輔ぅ繝・け繧ｹ縺九ｉ繧ｵ繧､繝郁ｨｭ螳壹ｒ讀懃ｴ｢"""
    configs = get_site_configs()
    for cfg in configs:
        prefix = cfg.get("convert_url", "")
        if prefix and sku.startswith(prefix):
            return cfg
    return None


def build_source_url(sku: str) -> Optional[str]:
    """SKU 竊・莉募・蜈ザRL繧堤函謌・""
    cfg = find_site_config_by_sku(sku)
    if not cfg:
        return None
    prefix = cfg["convert_url"]
    item_id = sku[len(prefix):]
    common = cfg.get("common_url", "")
    return common + item_id if common else None


def find_site_config_by_url(url: str) -> Optional[dict]:
    """URL 縺九ｉ site_config 繧呈､懃ｴ｢ (SKU prefix 髱樔ｾ晏ｭ倥仝183).

    url_keyword 縺ｮ驛ｨ蛻・ｸ閾ｴ縺ｧ蛻､螳壹・mazon/讌ｽ螟ｩ遲峨ｒ逶ｴ謗･ URL 縺ｧ逶｣隕悶☆繧矩圀縲・    SKU prefix 縺ｫ鬆ｼ繧峨★ site 縺ｮ蝨ｨ蠎ｫ蛻､螳壽枚蟄怜・ (in_stock/sold_out/no_page) 繧貞ｼ輔￥縲・    """
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
    """listing 縺ｮ source_url 繧呈焔蜍戊ｨｭ螳壹＠ SKU 蜷梧悄荳頑嶌縺阪°繧我ｿ晁ｭｷ縺吶ｋ (W183).

    manual=True : source_url 繧堤峩謗･險ｭ螳・+ source_url_manual=1 縺ｧ蝗ｺ螳壹ゆｻ･蠕・                  upsert_item / upsert_ebay_listing / _sync_monitored_items_sku 縺ｯ
                  縺薙・ URL 繧・SKU 豢ｾ逕溘〒荳頑嶌縺阪＠縺ｪ縺・・    manual=False: 蝗ｺ螳夊ｧ｣髯､ (source_url_manual=0)縲４KU 豢ｾ逕溘↓謌ｻ繧九・
    listing 隴伜挨縺ｯ ebay_item_id (sku-rules.md 貅匁侠)縲Ｆbay_listings 繧呈峩譁ｰ縺励・    蜷・ebay_item_id 縺ｮ monitored_items 縺後≠繧後・蜷梧悄縲Ｔite_config_id 縺ｯ URL 縺九ｉ隗｣豎ｺ縲・    Returns: ebay_listings 繧呈峩譁ｰ縺ｧ縺阪◆繧・True / listing 荳榊惠縺ｧ False.
    """
    if not ebay_item_id:
        return False
    src = (source_url or "").strip()
    cfg = find_site_config_by_url(src) if src else None
    site_config_id = cfg["id"] if cfg else None
    manual_flag = 1 if manual else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        # 譌｢蟄・listing 蜿門ｾ・(荳榊惠縺ｪ繧・False / URL 螟画峩讀懃衍 / 逶｣隕門床蟶ｳ譁ｰ隕丈ｽ懈・逕ｨ)
        row = conn.execute(
            "SELECT source_url, sku, title FROM ebay_listings WHERE ebay_item_id=?",
            (ebay_item_id,),
        ).fetchone()
        if row is None:
            return False
        url_changed = (row[0] or "") != (src or "")
        if url_changed:
            # URL 縺悟､峨ｏ縺｣縺溘ｉ譌ｧ蝨ｨ蠎ｫ蛻､螳壹・辟｡蜉ｹ 竊・谺｡蝗・inventory_check 縺悟・隧穂ｾ｡
            # (upsert_ebay_listing 髱・manual 邨瑚ｷｯ縺ｨ蜷・semantics)縲・            conn.execute(
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

codex
I’m going to try a live Playwright render now. The key thing I need to learn is whether Rakuten’s rendered DOM contains a clean `InStock` marker, an enabled purchase/cart control, or both; the code should only distrust the raw server `OutOfStock`, not real rendered sold-out pages.
2026-05-28T14:27:34.207861Z ERROR codex_core::tools::router: error=`"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "@'
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

URL='https://item.rakuten.co.jp/tuzukiya/m20-5806/'
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='ja-JP', timezone_id='Asia/Tokyo')
        page = await context.new_page()
        resp = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
        print('status', resp.status if resp else None)
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except Exception as e:
            print('networkidle', type(e).__name__)
        await page.wait_for_timeout(3000)
        content = await page.content()
        text = await page.inner_text('body')
        print('len content/text', len(content), len(text))
        for needle in ['schema.org/InStock','schema.org/OutOfStock','かごに追加','買い物かご','購入手続き','売り切れ','販売中']:
            print(needle, content.find(needle), text.find(needle))
        # common clickable controls around cart words
        print('buttons', await page.locator('button, input[type=\"submit\"], a').filter(has_text='かご').count())
        await browser.close()
asyncio.run(main())
'@ | python -"` rejected: blocked by policy
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "@'
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

URL='https://item.rakuten.co.jp/tuzukiya/m20-5806/'
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='ja-JP', timezone_id='Asia/Tokyo')
        page = await context.new_page()
        resp = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
        print('status', resp.status if resp else None)
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except Exception as e:
            print('networkidle', type(e).__name__)
        await page.wait_for_timeout(3000)
        content = await page.content()
        text = await page.inner_text('body')
        print('len content/text', len(content), len(text))
        for needle in ['schema.org/InStock','schema.org/OutOfStock','かごに追加','買い物かご','購入手続き','売り切れ','販売中']:
            print(needle, content.find(needle), text.find(needle))
        # common clickable controls around cart words
        print('buttons', await page.locator('button, input[type=\"submit\"], a').filter(has_text='かご').count())
        await browser.close()
asyncio.run(main())
'@ | python -" in C:\Users\gucch\projects\claude\tools\ebay-manager
 declined in 0ms:
`"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "@'
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

URL='https://item.rakuten.co.jp/tuzukiya/m20-5806/'
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='ja-JP', timezone_id='Asia/Tokyo')
        page = await context.new_page()
        resp = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
        print('status', resp.status if resp else None)
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except Exception as e:
            print('networkidle', type(e).__name__)
        await page.wait_for_timeout(3000)
        content = await page.content()
        text = await page.inner_text('body')
        print('len content/text', len(content), len(text))
        for needle in ['schema.org/InStock','schema.org/OutOfStock','かごに追加','買い物かご','購入手続き','売り切れ','販売中']:
            print(needle, content.find(needle), text.find(needle))
        # common clickable controls around cart words
        print('buttons', await page.locator('button, input[type=\"submit\"], a').filter(has_text='かご').count())
        await browser.close()
asyncio.run(main())
'@ | python -"` rejected: blocked by policy
codex
The live render command was blocked by the current execution policy, so I can’t confirm the post-JS DOM from here. I’ll still implement the core safety rule: for `item.rakuten` pages, raw HTTP `OutOfStock` alone is treated as inconclusive and must go to Playwright; rendered checks can still classify genuinely sold-out pages.
codex
I have enough context for the patch. I’m changing scraper behavior only: no site config string changes, so no new migration is needed. The regression test will mock raw HTTP Rakuten `OutOfStock` and verify `check_items_batch()` reaches the Playwright batch path and returns that rendered decision.
2026-05-28T14:27:56.314940Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py:
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----
2026-05-28T14:28:00.465927Z ERROR codex_core::tools::router: error=`"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\scrapers.py -TotalCount 30 | ForEach-Object { '[' + "'$_ + '"']' }"` rejected: blocked by policy
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\scrapers.py -TotalCount 30 | ForEach-Object { '[' + "'$_ + '"']' }" in C:\Users\gucch\projects\claude\tools\ebay-manager
 declined in 0ms:
`"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\scrapers.py -TotalCount 30 | ForEach-Object { '[' + "'$_ + '"']' }"` rejected: blocked by policy
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -Path monitor\\scrapers.py -TotalCount 35" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 461ms:
"""
莉募・蜈・し繧､繝医・蝨ｨ蠎ｫ繝√ぉ繝・け・・laywright + httpx 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ・・URL縺ｫ逶ｴ謗･繧｢繧ｯ繧ｻ繧ｹ縺励※蝨ｨ蠎ｫ繝・く繧ｹ繝医ｒ讀懷・縺吶ｋ譁ｹ蠑・"""
import asyncio
import logging
import random
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----

def _check_with_httpx(
    url: str,
    in_stock_texts: list[str],
    sold_out_texts: list[str],
    no_page_texts: list[str],
) -> Optional[str]:
    """httpx 縺ｧ HTML 繧貞叙蠕励＠繧ｭ繝ｼ繝ｯ繝ｼ繝画､懃ｴ｢縲ょ愛螳壻ｸ崎・縺ｪ繧・None縲・""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    }
    try:

2026-05-28T14:28:15.368362Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in C:\Users\gucch\projects\claude\tools\ebay-manager\monitor\scrapers.py:
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


# ---- httpx 繝吶・繧ｹ縺ｮ繝√ぉ繝・け・磯ｫ倬溘・霆ｽ驥擾ｼ・----
2026-05-28T14:28:21.583096Z ERROR codex_core::tools::router: error=patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings
codex
The repository is mounted read-only and approvals are disabled, so the patch attempt was rejected before writing. I can’t apply file edits in this session; I’ll still give the exact minimal patch and verification steps so it can be applied in a writable run.
2026-05-28T14:28:30.147578Z ERROR codex_core::tools::router: error=`"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Select-String -Path data\\tmp\\rakuten_357_raw.html -Pattern 'itemprop=\"availability\"|schema.org/InStock|schema.org/OutOfStock|itemprop=\"price\"|itemprop=\"offerCount\"|かごに追加' -Context 2,2"` rejected: blocked by policy
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Select-String -Path data\\tmp\\rakuten_357_raw.html -Pattern 'itemprop=\"availability\"|schema.org/InStock|schema.org/OutOfStock|itemprop=\"price\"|itemprop=\"offerCount\"|かごに追加' -Context 2,2" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "git diff -- monitor\\scrapers.py tests\\test_w183_ec_direct_url.py monitor\\database.py" in C:\Users\gucch\projects\claude\tools\ebay-manager
 declined in 0ms:
`"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Select-String -Path data\\tmp\\rakuten_357_raw.html -Pattern 'itemprop=\"availability\"|schema.org/InStock|schema.org/OutOfStock|itemprop=\"price\"|itemprop=\"offerCount\"|かごに追加' -Context 2,2"` rejected: blocked by policy
 succeeded in 302ms:
diff --git a/tools/ebay-manager/monitor/database.py b/tools/ebay-manager/monitor/database.py
index ba692f4..b304d63 100644
--- a/tools/ebay-manager/monitor/database.py
+++ b/tools/ebay-manager/monitor/database.py
@@ -67,11 +67,12 @@ DEFAULT_SITE_CONFIGS = [
     {
         "site_name": "楽天市場",
         "url_keyword": "item.rakuten",
-        "in_stock_text1": "かごに追加",
+        # W183 (2026-05-28): schema.org microdata で在庫判定 (migration v55 と同値に統一).
+        # 旧 'かごに追加'/'売り切れ' は売切ページにも disabled で残り誤判定 (Codex 実機調査).
+        # 詳細: .company/engineering/migration/codex-ec-direct-url-design.md
+        "in_stock_text1": 'itemprop="availability" content="http://schema.org/InStock"',
         "in_stock_text2": "",
-        # H10 fix (W120 / 2026-05-12): DEFAULT_SITE_CONFIGS と v38 migration UPDATE の整合.
-        # 旧 default は空文字で判定不能、楽天商品で頻出する標準表記を設定.
-        "sold_out_text": "売り切れ",
+        "sold_out_text": 'itemprop="availability" content="http://schema.org/OutOfStock"',
         "no_page_text": "ご指定のページは見つかりません",
         "common_url": "https://x.gd/",
         "convert_url": "ebayRT_",
@@ -99,8 +100,10 @@ DEFAULT_SITE_CONFIGS = [
     {
         "site_name": "Amazon",
         "url_keyword": "www.amazon.co.jp",
-        "in_stock_text1": "カートに入れる",
-        "in_stock_text2": "今すぐ買う",
+        # W183 (2026-05-28): add-to-cart-button で主ボタン特定 (migration v55 と同値に統一).
+        # 旧 'カートに入れる' は nav / 関連商品にも出て誤判定 (Codex 実機調査).
+        "in_stock_text1": 'id="add-to-cart-button"',
+        "in_stock_text2": 'name="submit.add-to-cart"',
         "sold_out_text": "現在在庫切れ",
         "no_page_text": "この商品は現在お取り扱いできません",
         "common_url": "https://www.amazon.co.jp/dp/",
@@ -2557,6 +2560,98 @@ def init_db():
                     conn.execute("PRAGMA user_version = 52")
                     logger.info("[init_db v52] schema_ver bumped to 52")
 
+        # v54 (W182, 2026-05-28): supplier_candidates に availability check カラム追加.
+        # sold_out 商品を candidate として登録する bug の恒久対策 (Codex 2026-05-28 調査).
+        # 詳細: .company/engineering/migration/codex-supplier-bug-investigation.md
+        # v53 は W139-revisit Phase 1 (coverage_anomaly_log) 予約済のため v54 を採番.
+        if schema_ver < 54:
+            _W182_V54_SC_COLS = {
+                "availability_status": "TEXT",
+                "availability_checked_at": "TIMESTAMP",
+                "availability_signal": "TEXT",
+            }
+            for _col, _type in _W182_V54_SC_COLS.items():
+                try:
+                    conn.execute(
+                        f"ALTER TABLE supplier_candidates ADD COLUMN "
+                        f"{_col} {_type}"
+                    )
+                    logger.info(
+                        f"[init_db v54] supplier_candidates.{_col} added"
+                    )
+                except sqlite3.OperationalError:
+                    pass
+            _w182v54_post = set(
+                r[1] for r in conn.execute(
+                    "PRAGMA table_info(supplier_candidates)"
+                ).fetchall()
+            )
+            if set(_W182_V54_SC_COLS).issubset(_w182v54_post):
+                conn.execute("PRAGMA user_version = 54")
+                logger.info("[init_db v54] schema_ver bumped to 54")
+
+        # v55 (W183, 2026-05-28): EC サイト直接 URL 無在庫出品対応 + 楽天/Amazon 在庫判定修正.
+        # source_url_manual=1 で SKU 同期による source_url 上書きを防ぎ、Amazon/楽天等の
+        # SKU 規則性のない EC サイトを商品管理で直接 URL 設定して無在庫監視できるようにする.
+        # 併せて楽天 (schema.org microdata) / Amazon (add-to-cart-button) の在庫判定 signal を
+        # 実 HTML と一致する値に修正 (Codex 2026-05-28 実機調査).
+        # 詳細: .company/engineering/migration/codex-ec-direct-url-design.md
+        if schema_ver < 55:
+            _W183_V55_COLS = {
+                "ebay_listings": {
+                    "source_url_manual": "INTEGER NOT NULL DEFAULT 0",
+                    "source_url_updated_at": "TIMESTAMP",
+                },
+                "monitored_items": {
+                    "source_url_manual": "INTEGER NOT NULL DEFAULT 0",
+                    "source_url_updated_at": "TIMESTAMP",
+                },
+            }
+            for _tbl, _cols in _W183_V55_COLS.items():
+                for _col, _type in _cols.items():
+                    try:
+                        conn.execute(
+                            f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_type}"
+                        )
+                        logger.info(f"[init_db v55] {_tbl}.{_col} added")
+                    except sqlite3.OperationalError:
+                        pass
+            # 楽天 / Amazon の在庫判定 signal を実 HTML 一致値に修正 (冪等 UPDATE).
+            # 楽天: schema.org microdata (InStock/OutOfStock 排他)。旧 'かごに追加' は
+            #       売切ページでも disabled button として残るため誤判定 (Codex 検証).
+            # Amazon: id="add-to-cart-button" で主ボタン特定。旧 'カートに入れる' は
+            #         nav / 関連商品にも出て誤判定 (Codex 検証)。CAPTCHA は scraper 側で unknown 化.
+            try:
+                conn.execute(
+                    "UPDATE site_configs SET "
+                    "in_stock_text1=?, in_stock_text2='', sold_out_text=? "
+                    "WHERE convert_url='ebayRT_' AND url_keyword='item.rakuten'",
+                    ('itemprop="availability" content="http://schema.org/InStock"',
+                     'itemprop="availability" content="http://schema.org/OutOfStock"'),
+                )
+                conn.execute(
+                    "UPDATE site_configs SET "
+                    "in_stock_text1=?, in_stock_text2=?, sold_out_text=? "
+                    "WHERE convert_url='ebayAM_' AND url_keyword='www.amazon.co.jp'",
+                    ('id="add-to-cart-button"', 'name="submit.add-to-cart"', '現在在庫切れ'),
+                )
+                logger.info("[init_db v55] 楽天/Amazon site_configs signal 更新")
+            except sqlite3.OperationalError:
+                pass
+            # 全列が揃った後でのみ user_version bump (冪等性: 部分適用で bump しない)
+            _v55_ok = True
+            for _tbl, _cols in _W183_V55_COLS.items():
+                _post = set(
+                    r[1] for r in conn.execute(
+                        f"PRAGMA table_info({_tbl})"
+                    ).fetchall()
+                )
+                if not set(_cols).issubset(_post):
+                    _v55_ok = False
+            if _v55_ok:
+                conn.execute("PRAGMA user_version = 55")
+                logger.info("[init_db v55] schema_ver bumped to 55")
+
 
 # ---- サイト設定 ----
 
@@ -2624,6 +2719,86 @@ def build_source_url(sku: str) -> Optional[str]:
     return common + item_id if common else None
 
 
+def find_site_config_by_url(url: str) -> Optional[dict]:
+    """URL から site_config を検索 (SKU prefix 非依存、W183).
+
+    url_keyword の部分一致で判定。Amazon/楽天等を直接 URL で監視する際、
+    SKU prefix に頼らず site の在庫判定文字列 (in_stock/sold_out/no_page) を引く。
+    """
+    if not url:
+        return None
+    for cfg in get_site_configs():
+        kw = cfg.get("url_keyword", "")
+        if kw and kw in url:
+            return cfg
+    return None
+
+
+def set_listing_source_url_manual(
+    ebay_item_id: str, source_url: str, manual: bool = True
+) -> bool:
+    """listing の source_url を手動設定し SKU 同期上書きから保護する (W183).
+
+    manual=True : source_url を直接設定 + source_url_manual=1 で固定。以後
+                  upsert_item / upsert_ebay_listing / _sync_monitored_items_sku は
+                  この URL を SKU 派生で上書きしない。
+    manual=False: 固定解除 (source_url_manual=0)。SKU 派生に戻る。
+
+    listing 識別は ebay_item_id (sku-rules.md 準拠)。ebay_listings を更新し、
+    同 ebay_item_id の monitored_items があれば同期。site_config_id は URL から解決。
+    Returns: ebay_listings を更新できたら True / listing 不在で False.
+    """
+    if not ebay_item_id:
+        return False
+    src = (source_url or "").strip()
+    cfg = find_site_config_by_url(src) if src else None
+    site_config_id = cfg["id"] if cfg else None
+    manual_flag = 1 if manual else 0
+    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
+    with get_conn() as conn:
+        # 既存 listing 取得 (不在なら False / URL 変更検知 / 監視台帳新規作成用)
+        row = conn.execute(
+            "SELECT source_url, sku, title FROM ebay_listings WHERE ebay_item_id=?",
+            (ebay_item_id,),
+        ).fetchone()
+        if row is None:
+            return False
+        url_changed = (row[0] or "") != (src or "")
+        if url_changed:
+            # URL が変わったら旧在庫判定は無効 → 次回 inventory_check が再評価
+            # (upsert_ebay_listing 非 manual 経路と同 semantics)。
+            conn.execute(
+                "UPDATE ebay_listings SET source_url=?, source_url_manual=?, "
+                "source_url_updated_at=?, source_status='unknown', "
+                "source_last_checked=NULL WHERE ebay_item_id=?",
+                (src or None, manual_flag, now, ebay_item_id),
+            )
+        else:
+            conn.execute(
+                "UPDATE ebay_listings SET source_url=?, source_url_manual=?, "
+                "source_url_updated_at=? WHERE ebay_item_id=?",
+                (src or None, manual_flag, now, ebay_item_id),
+            )
+        upd = conn.execute(
+            "UPDATE monitored_items SET source_url=?, source_url_manual=?, "
+            "source_url_updated_at=?, site_config_id=? WHERE ebay_item_id=?",
+            (src or None, manual_flag, now, site_config_id, ebay_item_id),
+        )
+        # W183 HIGH-1 (code-reviewer 2026-05-28): manual=True で監視台帳に未登録なら
+        # 新規 INSERT。これが無いと ensure_monitor_coverage が後で SKU 派生 URL で
+        # monitored_items 行を作り、手動 URL が誤 URL に汚染される (W139 同型の
+        # 仕入先 OOS 見逃し → 履行不能)。listing 識別は ebay_item_id (sku-rules)。
+        if manual and (upd.rowcount or 0) == 0:
+            conn.execute(
+                "INSERT INTO monitored_items (ebay_item_id, title, sku, source_url, "
+                "site_config_id, source_url_manual, source_url_updated_at, is_active) "
+                "VALUES (?,?,?,?,?,?,?,1)",
+                (ebay_item_id, row[2] or "", row[1] or "", src or None,
+                 site_config_id, manual_flag, now),
+            )
+    return True
+
+
 # ---- 監視アイテム ----
 
 def upsert_item(sku: str, ebay_item_id: str = "", title: str = "") -> int:
@@ -2661,12 +2836,26 @@ def upsert_item(sku: str, ebay_item_id: str = "", title: str = "") -> int:
             ).fetchone()
 
         if existing:
-            conn.execute(
-                """UPDATE monitored_items SET title=?, sku=?, source_url=?,
-                   site_config_id=?, is_active=1, ebay_item_id=COALESCE(NULLIF(?, ''), ebay_item_id)
-                   WHERE id=?""",
-                (title, sku, source_url, site_config_id, ebay_item_id, existing["id"]),
-            )
+            # W183 (2026-05-28): source_url_manual=1 の行は手動設定 URL を維持し、
+            # SKU 派生 source_url で上書きしない (EC 直接 URL 無在庫監視の保護).
+            _manual_row = conn.execute(
+                "SELECT COALESCE(source_url_manual, 0) FROM monitored_items WHERE id=?",
+                (existing["id"],),
+            ).fetchone()
+            if _manual_row and int(_manual_row[0]) == 1:
+                conn.execute(
+                    """UPDATE monitored_items SET title=?, sku=?, is_active=1,
+                       ebay_item_id=COALESCE(NULLIF(?, ''), ebay_item_id)
+                       WHERE id=?""",
+                    (title, sku, ebay_item_id, existing["id"]),
+                )
+            else:
+                conn.execute(
+                    """UPDATE monitored_items SET title=?, sku=?, source_url=?,
+                       site_config_id=?, is_active=1, ebay_item_id=COALESCE(NULLIF(?, ''), ebay_item_id)
+                       WHERE id=?""",
+                    (title, sku, source_url, site_config_id, ebay_item_id, existing["id"]),
+                )
             return existing["id"]
 
         conn.execute(
@@ -2773,8 +2962,17 @@ def upsert_ebay_listing(ebay_item_id: str, sku: str, title: str = "",
         if existing:
             existing_sku = existing["sku"] or ""
             sku_changed = (sku or "") != existing_sku
+            # W183 (2026-05-28): source_url_manual=1 の listing は手動設定 URL を保護.
+            # SKU が変わっても source_url / source_status / source_last_checked を
+            # SKU 派生で上書きしない (EC 直接 URL 無在庫監視の継続性確保).
+            _manual_row = conn.execute(
+                "SELECT COALESCE(source_url_manual, 0) FROM ebay_listings "
+                "WHERE ebay_item_id=?",
+                (ebay_item_id,),
+            ).fetchone()
+            is_manual = bool(_manual_row and int(_manual_row[0]) == 1)
 
-            if sku_changed:
+            if sku_changed and not is_manual:
                 # 2026-05-20 Codex 指摘 HIGH 対応: 旧 `if sku_changed and sku:`
                 # は eBay 側で SKU が空文字に変わった場合 (W139 後の filter 解除で
                 # SKU 空 listing も DB に流入するように変更) を skip して旧 SKU
@@ -2802,6 +3000,19 @@ def upsert_ebay_listing(ebay_item_id: str, sku: str, title: str = "",
                 # 2026-05-20: sku='' でも追従 (旧 sku の monitored_items 行が
                 # 残ると find_coverage_gaps が誤判定するため)。
                 _sync_monitored_items_sku(conn, ebay_item_id, sku)
+            elif sku_changed and is_manual:
+                # W183: 手動 URL listing は sku のみ追従、source_url / source_status /
+                # source_last_checked / risk_confirmed は維持 (手動 URL は不変なので
+                # 在庫状態を reset する必要なし)。
+                conn.execute(
+                    """UPDATE ebay_listings SET
+                          sku=?, title=?, current_price=?, quantity_ebay=?,
+                          shipping_cost=?, last_synced_at=?
+                       WHERE ebay_item_id=?""",
+                    (sku, title, current_price, quantity_ebay, shipping_cost, now,
+                     ebay_item_id),
+                )
+                _sync_monitored_items_sku(conn, ebay_item_id, sku)
             else:
                 conn.execute(
                     """UPDATE ebay_listings SET title=?, current_price=?, quantity_ebay=?,
@@ -3948,10 +4159,15 @@ def add_supplier_candidate(
     alt_listing_possible: int = 0,
     alt_listing_note: Optional[str] = None,
     eval_model: Optional[str] = None,
+    availability_status: Optional[str] = None,
+    availability_checked_at: Optional[str] = None,
+    availability_signal: Optional[str] = None,
 ) -> Optional[int]:
     """
     仕入先候補を登録（同一 sku + candidate_url の重複は無視）。
     eval_model: AI 評価に使った model (claude-opus-4-7 / claude-haiku-4-5 等).
+    availability_*: W182 (2026-05-28) 在庫 gate を通過した時点の判定結果.
+        - status='available' 以外は呼び出し側で reject 済の想定 (二重防御として記録).
     Returns: 挿入された行のid、重複なら None
     """
     with get_conn() as conn:
@@ -3961,13 +4177,15 @@ def add_supplier_candidate(
                 candidate_price_jpy, candidate_title, match_score,
                 match_reasoning, profit_jpy, profitable, discovered_via,
                 junk_likely_untested, alt_listing_possible, alt_listing_note,
-                eval_model)
-               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
+                eval_model, availability_status, availability_checked_at,
+                availability_signal)
+               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (sku, ebay_item_id, source_platform, candidate_url,
              candidate_price_jpy, candidate_title, match_score,
              match_reasoning, profit_jpy, profitable, discovered_via,
              junk_likely_untested, alt_listing_possible, alt_listing_note,
-             eval_model),
+             eval_model, availability_status, availability_checked_at,
+             availability_signal),
         )
         return cur.lastrowid if cur.rowcount else None
 
@@ -4119,11 +4337,17 @@ def _sync_monitored_items_sku(conn, ebay_item_id: str, new_sku: str) -> None:
                       or (build_source_url(new_sku) if new_sku else None))
     cfg = find_site_config_by_sku(new_sku) if new_sku else None
     site_config_id = cfg["id"] if cfg else None
+    # W183 (2026-05-28): source_url_manual=1 の monitored_items 行は手動 URL を維持
+    # (sku のみ追従、source_url / site_config_id は SKU 派生で上書きしない).
     conn.execute(
         """UPDATE monitored_items
               SET sku=?,
-                  source_url=COALESCE(?, source_url),
-                  site_config_id=?
+                  source_url=CASE WHEN COALESCE(source_url_manual, 0)=1
+                                  THEN source_url
+                                  ELSE COALESCE(?, source_url) END,
+                  site_config_id=CASE WHEN COALESCE(source_url_manual, 0)=1
+                                      THEN site_config_id
+                                      ELSE ? END
             WHERE ebay_item_id=? AND ebay_item_id IS NOT NULL
               AND ebay_item_id <> ''""",
         (new_sku, new_source_url, site_config_id, ebay_item_id),
@@ -4143,18 +4367,30 @@ def update_ebay_listing_sku(ebay_item_id: str, new_sku: str):
     new_source_url = _build_source_url_from_sku(new_sku)
     now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
     with get_conn() as conn:
-        conn.execute(
-            """UPDATE ebay_listings SET
-                  sku=?,
-                  source_url=COALESCE(?, source_url),
-                  source_status='unknown',
-                  source_last_checked=NULL,
-                  source_out_of_stock_since=NULL,
-                  risk_confirmed=0,
-                  last_synced_at=?
-               WHERE ebay_item_id=?""",
-            (new_sku, new_source_url, now, ebay_item_id),
-        )
+        # W183 (2026-05-28): source_url_manual=1 の listing は手動 URL + 在庫状態を維持
+        # (手動 URL は SKU 変更で変わらないので source_* reset 不要).
+        _mrow = conn.execute(
+            "SELECT COALESCE(source_url_manual,0) FROM ebay_listings WHERE ebay_item_id=?",
+            (ebay_item_id,),
+        ).fetchone()
+        if _mrow and int(_mrow[0]) == 1:
+            conn.execute(
+                "UPDATE ebay_listings SET sku=?, last_synced_at=? WHERE ebay_item_id=?",
+                (new_sku, now, ebay_item_id),
+            )
+        else:
+            conn.execute(
+                """UPDATE ebay_listings SET
+                      sku=?,
+                      source_url=COALESCE(?, source_url),
+                      source_status='unknown',
+                      source_last_checked=NULL,
+                      source_out_of_stock_since=NULL,
+                      risk_confirmed=0,
+                      last_synced_at=?
+                   WHERE ebay_item_id=?""",
+                (new_sku, new_source_url, now, ebay_item_id),
+            )
         # W139-fix (2026-05-18): 同一 conn で monitored_items も ebay_item_id
         # キーで追従 (原子的)。これがないと find_coverage_gaps が phantom gap 化。
         _sync_monitored_items_sku(conn, ebay_item_id, new_sku)
diff --git a/tools/ebay-manager/monitor/scrapers.py b/tools/ebay-manager/monitor/scrapers.py
index 963de13..141958b 100644
--- a/tools/ebay-manager/monitor/scrapers.py
+++ b/tools/ebay-manager/monitor/scrapers.py
@@ -43,6 +43,13 @@ def _check_with_httpx(
             return None
 
         html = resp.text
+        # W183 (2026-05-28): Amazon 等の anti-bot ページ (Robot Check / CAPTCHA)
+        # は在庫判定不能 = unknown 扱い (None で Playwright fallback)。在庫切れと
+        # 誤認すると不要な値下げ / 出品停止に直結するため必ず unknown に倒す。
+        low = html.lower()
+        if "robot check" in low or "validatecaptcha" in low:
+            logger.debug(f"anti-bot page (captcha) -> unknown: {url}")
+            return None
         return _detect_status_single(html, in_stock_texts, sold_out_texts, no_page_texts, strict=True)
     except httpx.TimeoutException:
         logger.debug(f"httpx timeout: {url}")
@@ -341,19 +348,38 @@ def check_item_by_config(item: dict, site_config: dict) -> str:
 
 
 def prepare_batch_items(items: list[dict], configs_by_prefix: dict) -> list[dict]:
-    """DB アイテムリストをバッチチェック用の形式に変換"""
+    """DB アイテムリストをバッチチェック用の形式に変換。
+
+    W183 (2026-05-28): SKU prefix に一致しない直接 URL 監視 (source_url_manual=1 の
+    Amazon/楽天 等、SKU 規則性の無い EC) は source_url の url_keyword で site_config を
+    解決する fallback を追加。除外したものは件数と理由をログに残す (Q0 silent-skip 防止)。
+    """
     batch = []
+    dropped_no_url = 0
+    dropped_no_config: list[dict] = []
     for item in items:
         sku = item.get("sku", "")
         source_url = item.get("source_url", "")
         if not source_url:
+            dropped_no_url += 1
             continue
         cfg = None
+        # 1) SKU prefix 一致 (従来の無在庫 ebay**_ SKU)
         for prefix, c in configs_by_prefix.items():
-            if sku.startswith(prefix):
+            if prefix and sku.startswith(prefix):
                 cfg = c
                 break
-        if not cfg:
+        # 2) W183 fallback: prefix 不一致は source_url の url_keyword で site 解決
+        if cfg is None:
+            for c in configs_by_prefix.values():
+                kw = c.get("url_keyword", "")
+                if kw and kw in source_url:
+                    cfg = c
+                    break
+        if cfg is None:
+            dropped_no_config.append(
+                {"id": item.get("id"), "sku": sku, "url": source_url}
+            )
             continue
         batch.append({
             "id": item["id"],
@@ -362,4 +388,142 @@ def prepare_batch_items(items: list[dict], configs_by_prefix: dict) -> list[dict
             "sold_out": [cfg.get("sold_out_text", "")],
             "no_page": [cfg.get("no_page_text", "")],
         })
+    if dropped_no_url or dropped_no_config:
+        logger.info(
+            "[prepare_batch_items] 除外: no_source_url=%d site_config_missing_url=%d (対象 %d 件)",
+            dropped_no_url, len(dropped_no_config), len(items),
+        )
+        for d in dropped_no_config[:20]:
+            logger.warning(
+                "[prepare_batch_items] site_config_missing_url id=%s sku=%r url=%s",
+                d["id"], d["sku"], d["url"],
+            )
     return batch
+
+
+# ============================================================================
+# W182 (2026-05-28): 候補 URL の在庫 gate
+# ============================================================================
+# sold_out 商品を supplier_candidates に登録する bug の恒久対策。
+# task_supplier_candidate_search.py + task_supplier_sweep.py の発見ロジックから
+# 評価 / 登録の前に呼ぶ。raw HTML レベルで sold_out signal を確実に拾うため、
+# PayPay / Yahoo Auctions は専用 logic、他は既存 site_configs を流用。
+#
+# 設計根拠 (Codex 2026-05-28 調査):
+# - PayPay フリマは raw HTML に "InStock" (古い ld+json) と "SoldOut" が混在
+# - 既存 site_configs の `関連商品をアプリで探す` は JS 描画後にしか出ない
+# - raw HTML で確実に検出できる signal: 購入日時 (購入済の確定 signal)、SoldOut
+# 詳細: .company/engineering/migration/codex-supplier-bug-investigation.md
+# ============================================================================
+
+from datetime import datetime, timezone
+
+
+_AVAILABILITY_HTTPX_TIMEOUT = 10
+_AVAILABILITY_HEADERS_BASE = {
+    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
+    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
+}
+
+
+def _avail_headers() -> dict:
+    h = dict(_AVAILABILITY_HEADERS_BASE)
+    h["User-Agent"] = random.choice(USER_AGENTS)
+    return h
+
+
+def check_candidate_availability(url: str, timeout_sec: int = _AVAILABILITY_HTTPX_TIMEOUT) -> dict:
+    """
+    候補 URL の在庫状態を判定し、availability dict を返す。
+
+    Returns: {
+        'status':       'available' | 'unavailable' | 'not_found' | 'unknown',
+        'signal':       検出 signal (debug 用),
+        'checked_at':   ISO8601 UTC,
+    }
+
+    呼び出し側は `status in ('unavailable', 'not_found')` で reject する想定。
+    'unknown' は判定保留 (現状は通過 = 既存挙動と互換、後段の AI 評価でカバー)。
+    """
+    checked_at = datetime.now(timezone.utc).isoformat()
+    if not url:
+        return {'status': 'unknown', 'signal': 'empty url', 'checked_at': checked_at}
+    if 'paypayfleamarket.yahoo.co.jp' in url:
+        return _check_paypay_availability(url, timeout_sec, checked_at)
+    if 'auctions.yahoo.co.jp' in url:
+        return _check_yahoo_auctions_availability(url, timeout_sec, checked_at)
+    # mercari / fril / 他は既存 site_configs ベース
+    return _check_via_site_configs(url, timeout_sec, checked_at)
+
+
+def _check_paypay_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
+    """PayPay フリマ raw HTML 判定 (W182、Codex 2026-05-28 検証ベース)。"""
+    try:
+        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
+    except httpx.TimeoutException:
+        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
+    except httpx.HTTPError as e:
+        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
+    if resp.status_code == 404:
+        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
+    if resp.status_code != 200:
+        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
+    html = resp.text
+    if 'この商品は存在しません' in html:
+        return {'status': 'not_found', 'signal': 'no_page_text', 'checked_at': checked_at}
+    # sold_out signals (Codex 検証で raw HTML に必ず入る): 優先順
+    if '購入日時' in html:
+        return {'status': 'unavailable', 'signal': '購入日時 in HTML', 'checked_at': checked_at}
+    if '"SoldOut"' in html or "'SoldOut'" in html:
+        return {'status': 'unavailable', 'signal': 'SoldOut JSON-LD', 'checked_at': checked_at}
+    if '関連商品をアプリで探す' in html:
+        return {'status': 'unavailable', 'signal': 'related items text', 'checked_at': checked_at}
+    # in_stock 確認
+    if '購入手続きへ' in html:
+        return {'status': 'available', 'signal': '購入手続きへ', 'checked_at': checked_at}
+    return {'status': 'unknown', 'signal': 'no signal matched', 'checked_at': checked_at}
+
+
+def _check_yahoo_auctions_availability(url: str, timeout_sec: int, checked_at: str) -> dict:
+    """ヤフオク (auctions.yahoo.co.jp) raw HTML 判定 (W182)。"""
+    try:
+        resp = httpx.get(url, headers=_avail_headers(), timeout=timeout_sec, follow_redirects=True)
+    except httpx.TimeoutException:
+        return {'status': 'unknown', 'signal': 'httpx timeout', 'checked_at': checked_at}
+    except httpx.HTTPError as e:
+        return {'status': 'unknown', 'signal': f'httpx error: {type(e).__name__}', 'checked_at': checked_at}
+    if resp.status_code == 404:
+        return {'status': 'not_found', 'signal': 'HTTP 404', 'checked_at': checked_at}
+    if resp.status_code != 200:
+        return {'status': 'unknown', 'signal': f'HTTP {resp.status_code}', 'checked_at': checked_at}
+    html = resp.text
+    if 'このオークションは終了' in html or 'このオークションは存在しません' in html:
+        return {'status': 'not_found', 'signal': 'auction ended/missing', 'checked_at': checked_at}
+    if '入札する' in html or '今すぐ落札' in html:
+        return {'status': 'available', 'signal': 'bid available', 'checked_at': checked_at}
+    return {'status': 'unknown', 'signal': 'no signal matched', 'checked_at': checked_at}
+
+
+def _check_via_site_configs(url: str, timeout_sec: int, checked_at: str) -> dict:
+    """site_configs から URL に一致する site を引いて httpx 判定 (W182、mercari / fril / 他)。"""
+    try:
+        from monitor.database import get_conn
+        with get_conn() as conn:
+            rows = conn.execute(
+                "SELECT site_name, url_keyword, in_stock_text1, in_stock_text2, "
+                "       sold_out_text, no_page_text FROM site_configs"
+            ).fetchall()
+    except Exception as e:
+        return {'status': 'unknown', 'signal': f'site_configs read error: {type(e).__name__}', 'checked_at': checked_at}
+    for r in rows:
+        if r[1] and r[1] in url:
+            in_stock = [x for x in (r[2], r[3]) if x]
+            sold_out = [r[4]] if r[4] else []
+            no_page = [r[5]] if r[5] else []
+            status = _check_with_httpx(url, in_stock, sold_out, no_page)
+            return {
+                'status': status or 'unknown',
+                'signal': f'site_config: {r[0]}',
+                'checked_at': checked_at,
+            }
+    return {'status': 'unknown', 'signal': 'no matching site_config', 'checked_at': checked_at}
warning: in the working copy of 'tools/ebay-manager/monitor/database.py', LF will be replaced by CRLF the next time Git touches it
warning: in the working copy of 'tools/ebay-manager/monitor/scrapers.py', LF will be replaced by CRLF the next time Git touches it

exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "rg -n \"def _check_with_httpx|def _detect_status\\(|def check_items_batch|def test_w183_rakuten_schema_org_detection|def test_w183_amazon_captcha_is_unknown\" monitor\\scrapers.py tests\\test_w183_ec_direct_url.py" in C:\Users\gucch\projects\claude\tools\ebay-manager
exec
"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "rg -n \"OutOfStock|InStock|offerCount|price|かごに追加\" data\\tmp\\rakuten_357_raw.html" in C:\Users\gucch\projects\claude\tools\ebay-manager
 succeeded in 313ms:
23:span.lossleader_price1 { color: #C20000; font-size: 80%; }
24:span.lossleader_price2 { color: #C20000; font-size: 80%; }
240: &quot;price&quot;: ,
254: &quot;price&quot;: 69883,
397:    background: url(https://image.rakuten.co.jp/tuzukiya/cabinet/pricedown_btn.jpg) no-repeat 0 0 !important;
667:<meta itemprop="offerCount" content="1">
668:<meta itemprop="price" content="69883">
669:<meta itemprop="priceCurrency" content="JPY">
670:<meta itemprop="availability" content="http://schema.org/OutOfStock">
674:<div id="priceCalculationConfig" data-price="69883" data-free-shipping="1"></div>
853:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">319円
863:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">319円
873:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">1,073円
883:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">389円
893:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">1,769円
903:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">429円
913:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">1,405円
923:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">1,079円
933:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">385円
943:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">289円
953:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">212円
963:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">805円
973:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">178円
983:<td align="center"><span class="lossleader_price1">価格</span><span class="lossleader_price2">125円
1131: price:[69883],
1205: "data": {"topicsList":[],"itemStaticResource":{"superDealBanner":"\u003c!--  App1.0_Active --\u003e\n\u003c!-- deal banner --\u003e\n\u003cstyle\u003e#itemFestival{background-color:#bf0000!important;text-align:center!important;filter:alpha(opacity\u003d100)!important;text-decoration:none!important;opacity:1!important;z-index:99999;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}#itemFestival .itemFestivalLink,#itemFestival .itemFestivalLink .itemFestivalImg{filter:alpha(opacity\u003d100)!important;text-decoration:none!important;opacity:1!important;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}#itemFestival .itemFestivalLink .itemFestivalImg{vertical-align:bottom}#itemFestival .itemFestivalLink:hover{filter:alpha(opacity\u003d100)!important;text-decoration:none!important;opacity:1!important;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}#itemFestival .itemFestivalLink .itemFestivalImg:hover{filter:alpha(opacity\u003d100)!important;opacity:1!important;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}\u003c/style\u003e\u003cdiv id\u003d\"itemFestival\" class\u003d\"pc-item-page-header\" data-ratId\u003d\"DealHeader\" data-ratEvent\u003d\"pv,appear\" data-ratparam\u003d\"all\"\u003e\u003ca href\u003d\"https://event.rakuten.co.jp/superdeal/?l-id\u003ddeal_item_headbelt_PC\" class\u003d\"itemFestivalLink\"\u003e\u003cimg src\u003d\"https://r.r10s.jp/com/img/item/hd_banner_pc_item_202209.jpg\" width\u003d\"950\" height\u003d\"50\" alt\u003d\"\" border\u003d\"0\" class\u003d\"itemFestivalImg\"\u003e\u003c/a\u003e\u003c/div\u003e\n\u003c!-- /deal banner --\u003e"},"itemInfoSku":{"shopId":"198680","manageNumber":"m20-5806","notification":false,"itemId":19012937,"is39Shop":true,"newItemNumber":"M20-5806","title":"ミツトヨ リニヤスケール用カウンター KA-212 174-183 1個","noshi":false,"pcFields":{"rCategoryId":"204172","images":[{"type":"CABINET","location":"/0/1/818/m20-5806.jpg","alt":"ミツトヨリニヤスケール用カウンターKA-212174-1831個"}],"itemNumber":"M20-5806","productDescription":" \u003cdiv class\u003d\"item_description\" clearfix\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e  \u003ch4\u003e特徴\u003c/h4\u003e \u003cp\u003e【特長】\u003c/p\u003e \u003cp\u003e●さまざまな機器の移動量を正確に捉えることに貢献\u003c/p\u003e \u003cp\u003e●フライス盤、旋盤専用機能を標準装備した高性能表示ユニットです。\u003c/p\u003e \u003cp\u003e●小形化･軽量化を実現しました。\u003c/p\u003e \u003cp\u003e●外部インターフェイスとしてRS-232C機能(オプション)を搭載可能です。\u003c/p\u003e \u003cp\u003e【仕様】\u003c/p\u003e \u003cp\u003e●最小表示量:AT100シリーズ接続時0.05〜0.0001mm、AT715接続時0.01〜0.001mm (パラメータにより変更可能)\u003c/p\u003e \u003cp\u003e●表示軸数:2軸\u003c/p\u003e \u003cp\u003e●表示方式/桁数:メイン表示/符号を含む9桁表示、サブ表示/操作･座標8桁表示\u003c/p\u003e \u003cp\u003e●電源電圧:AC100V〜240V(50/60Hz)\u003c/p\u003e \u003cp\u003e●出力(オプション):RS-232C\u003c/p\u003e \u003cbr\u003e \u003c/div\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e \u003ch4\u003e仕様\u003c/h4\u003e \u003ctable cellpadding\u003d\"0\" cellspacing\u003d\"0\" border\u003d\"0\"\u003e \u003ctr\u003e \u003ctd class\u003d\"item_td_style01\"\u003e重量\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1.25kg\u003c/td\u003e \u003ctd class\u003d\"item_td_style01\"\u003e入数\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1個\u003c/td\u003e \u003c/tr\u003e \u003c/table\u003e \u003c/div\u003e \u003c/div\u003e \u003cp\u003e\u003ciframe src\u003d\"https://www.rakuten.ne.jp/gold/tuzukiya/rank_r.html?sys\u003dC3010725\" name\u003d\"カテゴリランキング\" id\u003d\"cat_ranking\" width\u003d\"100%\" height\u003d\"700\" scrolling\u003d\"no\" frameborder\u003d\"0\" style\u003d\"margin: 0; padding: 0;\"\u003eIFRAME 機能を有効にして下さい。\u003c/iframe\u003e\u003c/p\u003e\u003cbr\u003e174183\u003cbr\u003e\u003cstyle\u003e\u003c!-- a.imghover:hover img {filter:alpha(opacity\u003d80);opacity:0.8;} --\u003e\u003c/style\u003e \u003cdiv style\u003d\"width:600px; padding:123px 0 0; margin:15px 0 0; background:url(https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn.jpg) no-repeat;\"\u003e \u003ca href\u003d\"https://order.my.rakuten.co.jp/\" target\u003d\"_blank\" class\u003d\"imghover\" style\u003d\"margin:0;\"\u003e\u003cimg src\u003d\"https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn2.jpg\" border\u003d\"0\" width\u003d\"600\" alt\u003d\"レビューを投稿する\" style\u003d\"margin:0;\"\u003e\u003c/a\u003e \u003c/div\u003e\u003c/div\u003e\u003c!-- カート下インフォここまで --\u003e"},"itemNameEnc":"%A5%DF%A5%C4%A5%C8%A5%E8+%A5%EA%A5%CB%A5%E4%A5%B9%A5%B1%A1%BC%A5%EB%CD%D1%A5%AB%A5%A6%A5%F3%A5%BF%A1%BC+KA-212+174-183+1%B8%C4","newProductDescription":" \u003cdiv class\u003d\"item_description\" clearfix\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e  \u003ch4\u003e特徴\u003c/h4\u003e \u003cp\u003e【特長】\u003c/p\u003e \u003cp\u003e●さまざまな機器の移動量を正確に捉えることに貢献\u003c/p\u003e \u003cp\u003e●フライス盤、旋盤専用機能を標準装備した高性能表示ユニットです。\u003c/p\u003e \u003cp\u003e●小形化･軽量化を実現しました。\u003c/p\u003e \u003cp\u003e●外部インターフェイスとしてRS-232C機能(オプション)を搭載可能です。\u003c/p\u003e \u003cp\u003e【仕様】\u003c/p\u003e \u003cp\u003e●最小表示量:AT100シリーズ接続時0.05〜0.0001mm、AT715接続時0.01〜0.001mm (パラメータにより変更可能)\u003c/p\u003e \u003cp\u003e●表示軸数:2軸\u003c/p\u003e \u003cp\u003e●表示方式/桁数:メイン表示/符号を含む9桁表示、サブ表示/操作･座標8桁表示\u003c/p\u003e \u003cp\u003e●電源電圧:AC100V〜240V(50/60Hz)\u003c/p\u003e \u003cp\u003e●出力(オプション):RS-232C\u003c/p\u003e \u003cbr\u003e \u003c/div\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e \u003ch4\u003e仕様\u003c/h4\u003e \u003ctable cellpadding\u003d\"0\" cellspacing\u003d\"0\" border\u003d\"0\"\u003e \u003ctr\u003e \u003ctd class\u003d\"item_td_style01\"\u003e重量\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1.25kg\u003c/td\u003e \u003ctd class\u003d\"item_td_style01\"\u003e入数\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1個\u003c/td\u003e \u003c/tr\u003e \u003c/table\u003e \u003c/div\u003e \u003c/div\u003e \u003cp\u003e\u003ciframe src\u003d\"https://www.rakuten.ne.jp/gold/tuzukiya/rank_r.html?sys\u003dC3010725\" name\u003d\"カテゴリランキング\" id\u003d\"cat_ranking\" width\u003d\"100%\" height\u003d\"700\" scrolling\u003d\"no\" frameborder\u003d\"0\" style\u003d\"margin: 0; padding: 0;\"\u003eIFRAME 機能を有効にして下さい。\u003c/iframe\u003e\u003c/p\u003e\u003cbr\u003e174183\u003cbr\u003e\u003cstyle\u003e\u003c!-- a.imghover:hover img {filter:alpha(opacity\u003d80);opacity:0.8;} --\u003e\u003c/style\u003e \u003cdiv style\u003d\"width:600px; padding:123px 0 0; margin:15px 0 0; background:url(https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn.jpg) no-repeat;\"\u003e \u003ca href\u003d\"https://order.my.rakuten.co.jp/\" target\u003d\"_blank\" class\u003d\"imghover\" style\u003d\"margin:0;\"\u003e\u003cimg src\u003d\"https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn2.jpg\" border\u003d\"0\" width\u003d\"600\" alt\u003d\"レビューを投稿する\" style\u003d\"margin:0;\"\u003e\u003c/a\u003e \u003c/div\u003e\u003c/div\u003e\u003c!-- カート下インフォここまで --\u003e","superSaleDiscount":0,"media":{"images":[{"location":"https://tshop.r10s.jp/tuzukiya/cabinet/0/1/818/m20-5806.jpg","alt":"ミツトヨリニヤスケール用カウンターKA-212174-1831個"}]},"oldImage":"https://tshop.r10s.jp/tuzukiya/cabinet/0/1/818/m20-5806.jpg","picImageUrl":"@0_mall/tuzukiya/cabinet/0/1/818/m20-5806.jpg","tags":[5002409],"articleNumber":{"value":"4946368693507"},"hideItem":false,"draftItem":false,"unlimitedInventoryFlag":false,"features":{"searchVisibility":"ALWAYS_VISIBLE","displayNormalCartButton":true,"displaySubscriptionCartButton":false,"inventoryDisplay":"HIDDEN_STOCK","shopContact":false,"review":"SHOP_SETTING","displayManufacturerContents":false,"socialGift":{"isNew":true,"isSocialGift":false,"receiverDeadlineDate":"2026/06/04(木)"}},"isRestricted":1,"accessControl":{"adultItem":false},"taxIncludedPrice":69883.0,"rCategoryId":"204172","referencePrice":{"displayType":"SHOP_SETTING"},"payment":{"taxIncluded":false,"cashOnDeliveryFeeIncluded":false},"shipping":{"postageIncluded":true,"singleItemShipping":0},"pointCampaignInfo":[{"itemSellType":"normal","pointCampaign":{},"isPointOptimization":false}],"superDeal":false,"isMnoFlag":false,"itemDisplaySequence":999999998,"layout":{"itemLayoutId":5,"navigationId":0,"layoutSequenceId":0,"smallDescriptionId":0,"largeDescriptionId":0,"showcaseId":0},"newTagId":"5002409","isTelecom":false,"inventoryType":"single","inventoryTypeId":"1","unit":0,"showDeliveryExcuse":true,"sellType":"NORMAL","identicalVariants":{},"identicalVariantsSubscription":{},"sku":[],"normalDeliveryDateId":13,"backOrderDeliveryDateId":13,"variantId":"m20-5806","genreInfo":{"prohibitedReviewStatus":0,"ancestorGenreId":100005},"purchaseInfo":{"purchaseBySellType":{"purchaseCondition":"enabled","basketSettings":{"basketDomain":"https://basket.step.rakuten.co.jp/rms/mall/bs/cartadd/set","basketDomainSubscription":"https://reserve.step.rakuten.co.jp/","directDomain":"https://direct.step.rakuten.co.jp/rms/mall/cartAdd/","cartSet":"https://basket.step.rakuten.co.jp/rms/mall/bs/cartadd/set","cartAll":"https://basket.step.rakuten.co.jp/rms/mall/bs/cartall/","directCartAdd":"https://direct.step.rakuten.co.jp/rms/mall/cartAdd/","directCartAddBulk":"https://direct.step.rakuten.co.jp/rms/mall/cartAddBulk/","timeout":1000,"retry":2},"bundle":true,"normalPurchase":{"price":{"minPrice":69883.0},"preTaxPrice":63530.0}},"newPurchaseSku":{"deliveryMessage":"約2日程度で発送(土日祝除く)","quantity":0},"variantMappedInventories":[{"sku":"m20-5806","inventoryId":"7bc17800-6488-11ed-84ff-0242ac110007","quantity":0}],"doublePrice":{"displayType":"SHOP_SETTING","referencePriceVerified":false}},"breadcrumbs":{"shopCategoryBreadcrumbs":[{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009718/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013184/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009716/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013182/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009709/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013129/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/52/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/62/"}],"newShopCategoryBreadcrumbs":[{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009718/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013184/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009716/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013182/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009709/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013129/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/52/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/62/"}],"genreBreadcrumbs":[{"name":"花・ガーデン・DIY","url":"https://www.rakuten.co.jp/category/100005/"},{"name":"DIY・工具","url":"https://www.rakuten.co.jp/category/100890/"},{"name":"計測工具","url":"https://www.rakuten.co.jp/category/210418/"},{"name":"その他","url":"https://www.rakuten.co.jp/category/204172/"}]},"featureSectionInfo":{"room":true,"campaignPlatformDisclaimer":true,"shopRecommend":true,"shopRanking":true,"cashlessPointBack":false,"buildToOrder":true,"fitsme":false,"viber":true,"line":true,"campaignPlatformContent":true,"breadCrumbs":true,"fitsOrigin":true,"coupon":true,"appIndexingSetting":true,"floatingCart":true,"addtocart":true,"isFirstPriority":false,"searchBar":true,"SPUX":true,"shopContact":false,"fbAppId":"157315820947832","socialGift":{"isSocialGift":false,"receiverDeadlineDate":"2026/06/04(木)"}},"itemReviewInfo":{"summary":{}},"additionalBanner":{"cardPromotionBanner":{"html":"\u003ca href\u003d\"https://www.rakuten-card.co.jp/campaign/rakuten_card/instant/item/?scid\u003dwi_ich_rc_item_except_businesshour_pc\"\u003e楽天カード新規入会で当日から使える2,000ポイントゲット！\u003c/a\u003e ","isLink":true}},"shippingCarriers":[{"carrierName":{"jaJP":"その他","enUS":"Other"},"carrierId":"00"},{"carrierName":{"jaJP":"ヤマト運輸","enUS":"Yamato Transport"},"carrierId":"01"},{"carrierName":{"jaJP":"佐川急便","enUS":"Sagawa Express"},"carrierId":"02"},{"carrierName":{"jaJP":"日本郵便","enUS":"Japan Post"},"carrierId":"03"},{"carrierName":{"jaJP":"西濃運輸","enUS":"Seino Transportation"},"carrierId":"04"},{"carrierName":{"jaJP":"セイノースーパーエクスプレス","enUS":"Seino Super Express"},"carrierId":"05"},{"carrierName":{"jaJP":"福山通運","enUS":"Fukuyama Transporting"},"carrierId":"06"},{"carrierName":{"jaJP":"名鉄運輸","enUS":"Meitetsu Transport"},"carrierId":"07"},{"carrierName":{"jaJP":"トナミ運輸","enUS":"Tonami Transport"},"carrierId":"08"},{"carrierName":{"jaJP":"第一貨物","enUS":"Daiichi Freight System"},"carrierId":"09"},{"carrierName":{"jaJP":"新潟運輸","enUS":"Niigata Transport"},"carrierId":"10"},{"carrierName":{"jaJP":"中越運送","enUS":"Chuetsu Transport"},"carrierId":"11"},{"carrierName":{"jaJP":"岡山県貨物運送","enUS":"Okayamaken Freight Transport"},"carrierId":"12"},{"carrierName":{"jaJP":"久留米運送","enUS":"Kurume Transport"},"carrierId":"13"},{"carrierName":{"jaJP":"山陽自動車運送","enUS":"Sanyo Jidosha Transport"},"carrierId":"14"},{"carrierName":{"jaJP":"NXトランスポート","enUS":"NX Transport"},"carrierId":"15"},{"carrierName":{"jaJP":"エコ配","enUS":"Ecohai"},"carrierId":"16"},{"carrierName":{"jaJP":"EMS","enUS":"EMS"},"carrierId":"17"},{"carrierName":{"jaJP":"DHL","enUS":"DHL"},"carrierId":"18"},{"carrierName":{"jaJP":"FedEx","enUS":"FedEx"},"carrierId":"19"},{"carrierName":{"jaJP":"UPS","enUS":"UPS"},"carrierId":"20"},{"carrierName":{"jaJP":"日本通運","enUS":"Nippon Express"},"carrierId":"21"},{"carrierName":{"jaJP":"TNT","enUS":"TNT"},"carrierId":"22"},{"carrierName":{"jaJP":"OCS","enUS":"OCS"},"carrierId":"23"},{"carrierName":{"jaJP":"USPS","enUS":"USPS"},"carrierId":"24"},{"carrierName":{"jaJP":"SFエクスプレス","enUS":"SF Express"},"carrierId":"25"},{"carrierName":{"jaJP":"Aramex","enUS":"Aramex"},"carrierId":"26"},{"carrierName":{"jaJP":"SGHグローバル・ジャパン","enUS":"SGH Global Japan"},"carrierId":"27"},{"carrierName":{"jaJP":"Rakuten EXPRESS","enUS":"Rakuten EXPRESS"},"carrierId":"28"},{"carrierName":{"jaJP":"日本郵便 楽天倉庫出荷","enUS":"Japan Post from Rakuten Warehouse"},"carrierId":"29"},{"carrierName":{"jaJP":"ヤマト運輸 クロネコゆうパケット","enUS":"Yamato Kuroneko Yu-Packet"},"carrierId":"30"},{"carrierName":{"jaJP":"名鉄NX運輸","enUS":"Meitetsu NX Transport"},"carrierId":"31"}],"makerCreativeInfo":{"sku":[]},"attributes":[{"title":"ブランド名","value":"Mitutoyo / ミツトヨ"},{"title":"メーカー型番","value":"174-183"}],"shopStatus":15,"genreKaimawari":{"isKaimawariCampaignPeriod":false},"hometownTaxEligible":false,"installPaymentInfo":{"installPaymentEnabled":false}},"mnoPromotion":{"button":{"staticTextContent":"楽天モバイルの詳細・お申し込み","url":"https://ac.ebis.ne.jp/tr_set.php?argument\u003dZMhPE4GP\u0026ai\u003drgp_mnocoupon50030"},"image":{"alt":"楽天モバイル（Rakuten最強プラン）ご契約後、楽天市場のお買い物がポイント+４倍","url":"https://www.rakuten.co.jp/com/inc/item/mno/img/mobile_pr.png"},"link":{"staticTextContent":"ポイント倍率UPの詳細はこちら","url":"https://ac.ebis.ne.jp/tr_set.php?argument\u003dZMhPE4GP\u0026ai\u003drgp_mno_007097"},"disclaimerText":"※要エントリー、楽天モバイル（Rakuten最強プラン）利用開始月からの楽天市場のお買い物（初日〜末日まで）がポイントアップ対象になります。（月間獲得上限2,000ポイント、期間限定ポイント）"}}
1207: "newApi": {"topicsList":[],"itemStaticResource":{"superDealBanner":"\u003c!--  App1.0_Active --\u003e\n\u003c!-- deal banner --\u003e\n\u003cstyle\u003e#itemFestival{background-color:#bf0000!important;text-align:center!important;filter:alpha(opacity\u003d100)!important;text-decoration:none!important;opacity:1!important;z-index:99999;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}#itemFestival .itemFestivalLink,#itemFestival .itemFestivalLink .itemFestivalImg{filter:alpha(opacity\u003d100)!important;text-decoration:none!important;opacity:1!important;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}#itemFestival .itemFestivalLink .itemFestivalImg{vertical-align:bottom}#itemFestival .itemFestivalLink:hover{filter:alpha(opacity\u003d100)!important;text-decoration:none!important;opacity:1!important;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}#itemFestival .itemFestivalLink .itemFestivalImg:hover{filter:alpha(opacity\u003d100)!important;opacity:1!important;margin:0!important;padding:0!important;position:relative!important;top:0!important;left:0!important}\u003c/style\u003e\u003cdiv id\u003d\"itemFestival\" class\u003d\"pc-item-page-header\" data-ratId\u003d\"DealHeader\" data-ratEvent\u003d\"pv,appear\" data-ratparam\u003d\"all\"\u003e\u003ca href\u003d\"https://event.rakuten.co.jp/superdeal/?l-id\u003ddeal_item_headbelt_PC\" class\u003d\"itemFestivalLink\"\u003e\u003cimg src\u003d\"https://r.r10s.jp/com/img/item/hd_banner_pc_item_202209.jpg\" width\u003d\"950\" height\u003d\"50\" alt\u003d\"\" border\u003d\"0\" class\u003d\"itemFestivalImg\"\u003e\u003c/a\u003e\u003c/div\u003e\n\u003c!-- /deal banner --\u003e"},"itemInfoSku":{"shopId":"198680","manageNumber":"m20-5806","notification":false,"itemId":19012937,"is39Shop":true,"newItemNumber":"M20-5806","title":"ミツトヨ リニヤスケール用カウンター KA-212 174-183 1個","noshi":false,"pcFields":{"rCategoryId":"204172","images":[{"type":"CABINET","location":"/0/1/818/m20-5806.jpg","alt":"ミツトヨリニヤスケール用カウンターKA-212174-1831個"}],"itemNumber":"M20-5806","productDescription":" \u003cdiv class\u003d\"item_description\" clearfix\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e  \u003ch4\u003e特徴\u003c/h4\u003e \u003cp\u003e【特長】\u003c/p\u003e \u003cp\u003e●さまざまな機器の移動量を正確に捉えることに貢献\u003c/p\u003e \u003cp\u003e●フライス盤、旋盤専用機能を標準装備した高性能表示ユニットです。\u003c/p\u003e \u003cp\u003e●小形化･軽量化を実現しました。\u003c/p\u003e \u003cp\u003e●外部インターフェイスとしてRS-232C機能(オプション)を搭載可能です。\u003c/p\u003e \u003cp\u003e【仕様】\u003c/p\u003e \u003cp\u003e●最小表示量:AT100シリーズ接続時0.05〜0.0001mm、AT715接続時0.01〜0.001mm (パラメータにより変更可能)\u003c/p\u003e \u003cp\u003e●表示軸数:2軸\u003c/p\u003e \u003cp\u003e●表示方式/桁数:メイン表示/符号を含む9桁表示、サブ表示/操作･座標8桁表示\u003c/p\u003e \u003cp\u003e●電源電圧:AC100V〜240V(50/60Hz)\u003c/p\u003e \u003cp\u003e●出力(オプション):RS-232C\u003c/p\u003e \u003cbr\u003e \u003c/div\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e \u003ch4\u003e仕様\u003c/h4\u003e \u003ctable cellpadding\u003d\"0\" cellspacing\u003d\"0\" border\u003d\"0\"\u003e \u003ctr\u003e \u003ctd class\u003d\"item_td_style01\"\u003e重量\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1.25kg\u003c/td\u003e \u003ctd class\u003d\"item_td_style01\"\u003e入数\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1個\u003c/td\u003e \u003c/tr\u003e \u003c/table\u003e \u003c/div\u003e \u003c/div\u003e \u003cp\u003e\u003ciframe src\u003d\"https://www.rakuten.ne.jp/gold/tuzukiya/rank_r.html?sys\u003dC3010725\" name\u003d\"カテゴリランキング\" id\u003d\"cat_ranking\" width\u003d\"100%\" height\u003d\"700\" scrolling\u003d\"no\" frameborder\u003d\"0\" style\u003d\"margin: 0; padding: 0;\"\u003eIFRAME 機能を有効にして下さい。\u003c/iframe\u003e\u003c/p\u003e\u003cbr\u003e174183\u003cbr\u003e\u003cstyle\u003e\u003c!-- a.imghover:hover img {filter:alpha(opacity\u003d80);opacity:0.8;} --\u003e\u003c/style\u003e \u003cdiv style\u003d\"width:600px; padding:123px 0 0; margin:15px 0 0; background:url(https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn.jpg) no-repeat;\"\u003e \u003ca href\u003d\"https://order.my.rakuten.co.jp/\" target\u003d\"_blank\" class\u003d\"imghover\" style\u003d\"margin:0;\"\u003e\u003cimg src\u003d\"https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn2.jpg\" border\u003d\"0\" width\u003d\"600\" alt\u003d\"レビューを投稿する\" style\u003d\"margin:0;\"\u003e\u003c/a\u003e \u003c/div\u003e\u003c/div\u003e\u003c!-- カート下インフォここまで --\u003e"},"itemNameEnc":"%A5%DF%A5%C4%A5%C8%A5%E8+%A5%EA%A5%CB%A5%E4%A5%B9%A5%B1%A1%BC%A5%EB%CD%D1%A5%AB%A5%A6%A5%F3%A5%BF%A1%BC+KA-212+174-183+1%B8%C4","newProductDescription":" \u003cdiv class\u003d\"item_description\" clearfix\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e  \u003ch4\u003e特徴\u003c/h4\u003e \u003cp\u003e【特長】\u003c/p\u003e \u003cp\u003e●さまざまな機器の移動量を正確に捉えることに貢献\u003c/p\u003e \u003cp\u003e●フライス盤、旋盤専用機能を標準装備した高性能表示ユニットです。\u003c/p\u003e \u003cp\u003e●小形化･軽量化を実現しました。\u003c/p\u003e \u003cp\u003e●外部インターフェイスとしてRS-232C機能(オプション)を搭載可能です。\u003c/p\u003e \u003cp\u003e【仕様】\u003c/p\u003e \u003cp\u003e●最小表示量:AT100シリーズ接続時0.05〜0.0001mm、AT715接続時0.01〜0.001mm (パラメータにより変更可能)\u003c/p\u003e \u003cp\u003e●表示軸数:2軸\u003c/p\u003e \u003cp\u003e●表示方式/桁数:メイン表示/符号を含む9桁表示、サブ表示/操作･座標8桁表示\u003c/p\u003e \u003cp\u003e●電源電圧:AC100V〜240V(50/60Hz)\u003c/p\u003e \u003cp\u003e●出力(オプション):RS-232C\u003c/p\u003e \u003cbr\u003e \u003c/div\u003e \u003cdiv class\u003d\"item_spec_sp clearfix\"\u003e \u003ch4\u003e仕様\u003c/h4\u003e \u003ctable cellpadding\u003d\"0\" cellspacing\u003d\"0\" border\u003d\"0\"\u003e \u003ctr\u003e \u003ctd class\u003d\"item_td_style01\"\u003e重量\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1.25kg\u003c/td\u003e \u003ctd class\u003d\"item_td_style01\"\u003e入数\u003c/td\u003e \u003ctd class\u003d\"item_td_style02\"\u003e1個\u003c/td\u003e \u003c/tr\u003e \u003c/table\u003e \u003c/div\u003e \u003c/div\u003e \u003cp\u003e\u003ciframe src\u003d\"https://www.rakuten.ne.jp/gold/tuzukiya/rank_r.html?sys\u003dC3010725\" name\u003d\"カテゴリランキング\" id\u003d\"cat_ranking\" width\u003d\"100%\" height\u003d\"700\" scrolling\u003d\"no\" frameborder\u003d\"0\" style\u003d\"margin: 0; padding: 0;\"\u003eIFRAME 機能を有効にして下さい。\u003c/iframe\u003e\u003c/p\u003e\u003cbr\u003e174183\u003cbr\u003e\u003cstyle\u003e\u003c!-- a.imghover:hover img {filter:alpha(opacity\u003d80);opacity:0.8;} --\u003e\u003c/style\u003e \u003cdiv style\u003d\"width:600px; padding:123px 0 0; margin:15px 0 0; background:url(https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn.jpg) no-repeat;\"\u003e \u003ca href\u003d\"https://order.my.rakuten.co.jp/\" target\u003d\"_blank\" class\u003d\"imghover\" style\u003d\"margin:0;\"\u003e\u003cimg src\u003d\"https://image.rakuten.co.jp/tuzukiya/cabinet/00002158/review_title600bn2.jpg\" border\u003d\"0\" width\u003d\"600\" alt\u003d\"レビューを投稿する\" style\u003d\"margin:0;\"\u003e\u003c/a\u003e \u003c/div\u003e\u003c/div\u003e\u003c!-- カート下インフォここまで --\u003e","superSaleDiscount":0,"media":{"images":[{"location":"https://tshop.r10s.jp/tuzukiya/cabinet/0/1/818/m20-5806.jpg","alt":"ミツトヨリニヤスケール用カウンターKA-212174-1831個"}]},"oldImage":"https://tshop.r10s.jp/tuzukiya/cabinet/0/1/818/m20-5806.jpg","picImageUrl":"@0_mall/tuzukiya/cabinet/0/1/818/m20-5806.jpg","tags":[5002409],"articleNumber":{"value":"4946368693507"},"hideItem":false,"draftItem":false,"unlimitedInventoryFlag":false,"features":{"searchVisibility":"ALWAYS_VISIBLE","displayNormalCartButton":true,"displaySubscriptionCartButton":false,"inventoryDisplay":"HIDDEN_STOCK","shopContact":false,"review":"SHOP_SETTING","displayManufacturerContents":false,"socialGift":{"isNew":true,"isSocialGift":false,"receiverDeadlineDate":"2026/06/04(木)"}},"isRestricted":1,"accessControl":{"adultItem":false},"taxIncludedPrice":69883.0,"rCategoryId":"204172","referencePrice":{"displayType":"SHOP_SETTING"},"payment":{"taxIncluded":false,"cashOnDeliveryFeeIncluded":false},"shipping":{"postageIncluded":true,"singleItemShipping":0},"pointCampaignInfo":[{"itemSellType":"normal","pointCampaign":{},"isPointOptimization":false}],"superDeal":false,"isMnoFlag":false,"itemDisplaySequence":999999998,"layout":{"itemLayoutId":5,"navigationId":0,"layoutSequenceId":0,"smallDescriptionId":0,"largeDescriptionId":0,"showcaseId":0},"newTagId":"5002409","isTelecom":false,"inventoryType":"single","inventoryTypeId":"1","unit":0,"showDeliveryExcuse":true,"sellType":"NORMAL","identicalVariants":{},"identicalVariantsSubscription":{},"sku":[],"normalDeliveryDateId":13,"backOrderDeliveryDateId":13,"variantId":"m20-5806","genreInfo":{"prohibitedReviewStatus":0,"ancestorGenreId":100005},"purchaseInfo":{"purchaseBySellType":{"purchaseCondition":"enabled","basketSettings":{"basketDomain":"https://basket.step.rakuten.co.jp/rms/mall/bs/cartadd/set","basketDomainSubscription":"https://reserve.step.rakuten.co.jp/","directDomain":"https://direct.step.rakuten.co.jp/rms/mall/cartAdd/","cartSet":"https://basket.step.rakuten.co.jp/rms/mall/bs/cartadd/set","cartAll":"https://basket.step.rakuten.co.jp/rms/mall/bs/cartall/","directCartAdd":"https://direct.step.rakuten.co.jp/rms/mall/cartAdd/","directCartAddBulk":"https://direct.step.rakuten.co.jp/rms/mall/cartAddBulk/","timeout":1000,"retry":2},"bundle":true,"normalPurchase":{"price":{"minPrice":69883.0},"preTaxPrice":63530.0}},"newPurchaseSku":{"deliveryMessage":"約2日程度で発送(土日祝除く)","quantity":0},"variantMappedInventories":[{"sku":"m20-5806","inventoryId":"7bc17800-6488-11ed-84ff-0242ac110007","quantity":0}],"doublePrice":{"displayType":"SHOP_SETTING","referencePriceVerified":false}},"breadcrumbs":{"shopCategoryBreadcrumbs":[{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009718/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013184/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009716/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013182/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009709/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013129/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/52/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/62/"}],"newShopCategoryBreadcrumbs":[{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009718/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013184/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009716/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013182/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000009709/"},{"name":"その他測定用品","url":"https://item.rakuten.co.jp/tuzukiya/c/0000013129/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/52/"},{"name":"測定･計測用品","url":"https://item.rakuten.co.jp/tuzukiya/s/62/"}],"genreBreadcrumbs":[{"name":"花・ガーデン・DIY","url":"https://www.rakuten.co.jp/category/100005/"},{"name":"DIY・工具","url":"https://www.rakuten.co.jp/category/100890/"},{"name":"計測工具","url":"https://www.rakuten.co.jp/category/210418/"},{"name":"その他","url":"https://www.rakuten.co.jp/category/204172/"}]},"featureSectionInfo":{"room":true,"campaignPlatformDisclaimer":true,"shopRecommend":true,"shopRanking":true,"cashlessPointBack":false,"buildToOrder":true,"fitsme":false,"viber":true,"line":true,"campaignPlatformContent":true,"breadCrumbs":true,"fitsOrigin":true,"coupon":true,"appIndexingSetting":true,"floatingCart":true,"addtocart":true,"isFirstPriority":false,"searchBar":true,"SPUX":true,"shopContact":false,"fbAppId":"157315820947832","socialGift":{"isSocialGift":false,"receiverDeadlineDate":"2026/06/04(木)"}},"itemReviewInfo":{"summary":{}},"additionalBanner":{"cardPromotionBanner":{"html":"\u003ca href\u003d\"https://www.rakuten-card.co.jp/campaign/rakuten_card/instant/item/?scid\u003dwi_ich_rc_item_except_businesshour_pc\"\u003e楽天カード新規入会で当日から使える2,000ポイントゲット！\u003c/a\u003e ","isLink":true}},"shippingCarriers":[{"carrierName":{"jaJP":"その他","enUS":"Other"},"carrierId":"00"},{"carrierName":{"jaJP":"ヤマト運輸","enUS":"Yamato Transport"},"carrierId":"01"},{"carrierName":{"jaJP":"佐川急便","enUS":"Sagawa Express"},"carrierId":"02"},{"carrierName":{"jaJP":"日本郵便","enUS":"Japan Post"},"carrierId":"03"},{"carrierName":{"jaJP":"西濃運輸","enUS":"Seino Transportation"},"carrierId":"04"},{"carrierName":{"jaJP":"セイノースーパーエクスプレス","enUS":"Seino Super Express"},"carrierId":"05"},{"carrierName":{"jaJP":"福山通運","enUS":"Fukuyama Transporting"},"carrierId":"06"},{"carrierName":{"jaJP":"名鉄運輸","enUS":"Meitetsu Transport"},"carrierId":"07"},{"carrierName":{"jaJP":"トナミ運輸","enUS":"Tonami Transport"},"carrierId":"08"},{"carrierName":{"jaJP":"第一貨物","enUS":"Daiichi Freight System"},"carrierId":"09"},{"carrierName":{"jaJP":"新潟運輸","enUS":"Niigata Transport"},"carrierId":"10"},{"carrierName":{"jaJP":"中越運送","enUS":"Chuetsu Transport"},"carrierId":"11"},{"carrierName":{"jaJP":"岡山県貨物運送","enUS":"Okayamaken Freight Transport"},"carrierId":"12"},{"carrierName":{"jaJP":"久留米運送","enUS":"Kurume Transport"},"carrierId":"13"},{"carrierName":{"jaJP":"山陽自動車運送","enUS":"Sanyo Jidosha Transport"},"carrierId":"14"},{"carrierName":{"jaJP":"NXトランスポート","enUS":"NX Transport"},"carrierId":"15"},{"carrierName":{"jaJP":"エコ配","enUS":"Ecohai"},"carrierId":"16"},{"carrierName":{"jaJP":"EMS","enUS":"EMS"},"carrierId":"17"},{"carrierName":{"jaJP":"DHL","enUS":"DHL"},"carrierId":"18"},{"carrierName":{"jaJP":"FedEx","enUS":"FedEx"},"carrierId":"19"},{"carrierName":{"jaJP":"UPS","enUS":"UPS"},"carrierId":"20"},{"carrierName":{"jaJP":"日本通運","enUS":"Nippon Express"},"carrierId":"21"},{"carrierName":{"jaJP":"TNT","enUS":"TNT"},"carrierId":"22"},{"carrierName":{"jaJP":"OCS","enUS":"OCS"},"carrierId":"23"},{"carrierName":{"jaJP":"USPS","enUS":"USPS"},"carrierId":"24"},{"carrierName":{"jaJP":"SFエクスプレス","enUS":"SF Express"},"carrierId":"25"},{"carrierName":{"jaJP":"Aramex","enUS":"Aramex"},"carrierId":"26"},{"carrierName":{"jaJP":"SGHグローバル・ジャパン","enUS":"SGH Global Japan"},"carrierId":"27"},{"carrierName":{"jaJP":"Rakuten EXPRESS","enUS":"Rakuten EXPRESS"},"carrierId":"28"},{"carrierName":{"jaJP":"日本郵便 楽天倉庫出荷","enUS":"Japan Post from Rakuten Warehouse"},"carrierId":"29"},{"carrierName":{"jaJP":"ヤマト運輸 クロネコゆうパケット","enUS":"Yamato Kuroneko Yu-Packet"},"carrierId":"30"},{"carrierName":{"jaJP":"名鉄NX運輸","enUS":"Meitetsu NX Transport"},"carrierId":"31"}],"makerCreativeInfo":{"sku":[]},"attributes":[{"title":"ブランド名","value":"Mitutoyo / ミツトヨ"},{"title":"メーカー型番","value":"174-183"}],"shopStatus":15,"genreKaimawari":{"isKaimawariCampaignPeriod":false},"hometownTaxEligible":false,"installPaymentInfo":{"installPaymentEnabled":false}},"mnoPromotion":{"button":{"staticTextContent":"楽天モバイルの詳細・お申し込み","url":"https://ac.ebis.ne.jp/tr_set.php?argument\u003dZMhPE4GP\u0026ai\u003drgp_mnocoupon50030"},"image":{"alt":"楽天モバイル（Rakuten最強プラン）ご契約後、楽天市場のお買い物がポイント+４倍","url":"https://www.rakuten.co.jp/com/inc/item/mno/img/mobile_pr.png"},"link":{"staticTextContent":"ポイント倍率UPの詳細はこちら","url":"https://ac.ebis.ne.jp/tr_set.php?argument\u003dZMhPE4GP\u0026ai\u003drgp_mno_007097"},"disclaimerText":"※要エントリー、楽天モバイル（Rakuten最強プラン）利用開始月からの楽天市場のお買い物（初日〜末日まで）がポイントアップ対象になります。（月間獲得上限2,000ポイント、期間限定ポイント）"}},

 succeeded in 317ms:
tests\test_w183_ec_direct_url.py:187:def test_w183_rakuten_schema_org_detection():
tests\test_w183_ec_direct_url.py:215:def test_w183_amazon_captcha_is_unknown():
monitor\scrapers.py:24:def _check_with_httpx(
monitor\scrapers.py:97:def _detect_status(
monitor\scrapers.py:235:def check_items_batch(items_with_config: list[dict]) -> dict[int, str]:

codex
I could not apply the fix in this session: the workspace is mounted `read-only` and approvals are disabled, so `apply_patch` was rejected. I also could not live-render Rakuten because the Playwright/network command was blocked by policy.

**Root Cause**
`monitor/scrapers.py::_check_with_httpx()` treats raw Rakuten HTML as authoritative. For `https://item.rakuten.co.jp/tuzukiya/m20-5806/`, saved raw HTML confirms the only microdata availability is:

`<meta itemprop="availability" content="http://schema.org/OutOfStock">`

at `data/tmp/rakuten_357_raw.html:667-670`, next to `offerCount=1` and `price=69883`. Because `_check_with_httpx()` returns `"unavailable"`, `check_items_batch()` never reaches Playwright.

**Minimal Fix To Apply**
In [monitor/scrapers.py](/C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/scrapers.py:24), add helpers and make Rakuten raw `OutOfStock` defer:

```python
def _is_rakuten_item_url(url: str) -> bool:
    return "item.rakuten" in (url or "")


def _contains_any(content: str, texts: list[str]) -> bool:
    return any(t and t in content for t in texts)


def _has_raw_rakuten_oos_only(url, content, in_stock_texts, sold_out_texts, no_page_texts) -> bool:
    return (
        _is_rakuten_item_url(url)
        and _contains_any(content, sold_out_texts)
        and not _contains_any(content, in_stock_texts)
        and not _contains_any(content, no_page_texts)
    )
```

Inside `_check_with_httpx()`, after CAPTCHA handling and before `_detect_status_single(...)`:

```python
if _has_raw_rakuten_oos_only(url, html, in_stock_texts, sold_out_texts, no_page_texts):
    logger.info("Rakuten raw OutOfStock only; deferring to Playwright: %s", url)
    return None
```

No `site_configs` change is needed, so no new migration is required.

**Regression Test**
Add to [tests/test_w183_ec_direct_url.py](/C:/Users/gucch/projects/claude/tools/ebay-manager/tests/test_w183_ec_direct_url.py:215):

```python
def test_w183_rakuten_raw_oos_only_defers_to_playwright():
    from monitor.scrapers import _check_with_httpx, check_items_batch

    raw_oos = '<html><meta itemprop="availability" content="http://schema.org/OutOfStock"></html>'
    fake = MagicMock(status_code=200, text=raw_oos)
    url = "https://item.rakuten.co.jp/tuzukiya/m20-5806/"
    in_stock = ['itemprop="availability" content="http://schema.org/InStock"']
    sold_out = ['itemprop="availability" content="http://schema.org/OutOfStock"']
    no_page = ["ご指定のページは見つかりません"]

    with patch("monitor.scrapers.httpx.get", return_value=fake):
        assert _check_with_httpx(url, in_stock, sold_out, no_page) is None

    item = {"id": 1, "url": url, "in_stock": in_stock, "sold_out": sold_out, "no_page": no_page}
    with patch("monitor.scrapers.httpx.get", return_value=fake), \
         patch("monitor.scrapers._run_playwright_batch", return_value={1: "available"}) as pw:
        assert check_items_batch([item]) == {1: "available"}
        pw.assert_called_once()
```

Verification I could perform: saved raw HTML matches the failure evidence exactly. Verification I could not perform: live Playwright confirmation or pytest execution, due read-only/policy blocks.
tokens used
88,927
I could not apply the fix in this session: the workspace is mounted `read-only` and approvals are disabled, so `apply_patch` was rejected. I also could not live-render Rakuten because the Playwright/network command was blocked by policy.

**Root Cause**
`monitor/scrapers.py::_check_with_httpx()` treats raw Rakuten HTML as authoritative. For `https://item.rakuten.co.jp/tuzukiya/m20-5806/`, saved raw HTML confirms the only microdata availability is:

`<meta itemprop="availability" content="http://schema.org/OutOfStock">`

at `data/tmp/rakuten_357_raw.html:667-670`, next to `offerCount=1` and `price=69883`. Because `_check_with_httpx()` returns `"unavailable"`, `check_items_batch()` never reaches Playwright.

**Minimal Fix To Apply**
In [monitor/scrapers.py](/C:/Users/gucch/projects/claude/tools/ebay-manager/monitor/scrapers.py:24), add helpers and make Rakuten raw `OutOfStock` defer:

```python
def _is_rakuten_item_url(url: str) -> bool:
    return "item.rakuten" in (url or "")


def _contains_any(content: str, texts: list[str]) -> bool:
    return any(t and t in content for t in texts)


def _has_raw_rakuten_oos_only(url, content, in_stock_texts, sold_out_texts, no_page_texts) -> bool:
    return (
        _is_rakuten_item_url(url)
        and _contains_any(content, sold_out_texts)
        and not _contains_any(content, in_stock_texts)
        and not _contains_any(content, no_page_texts)
    )
```

Inside `_check_with_httpx()`, after CAPTCHA handling and before `_detect_status_single(...)`:

```python
if _has_raw_rakuten_oos_only(url, html, in_stock_texts, sold_out_texts, no_page_texts):
    logger.info("Rakuten raw OutOfStock only; deferring to Playwright: %s", url)
    return None
```

No `site_configs` change is needed, so no new migration is required.

**Regression Test**
Add to [tests/test_w183_ec_direct_url.py](/C:/Users/gucch/projects/claude/tools/ebay-manager/tests/test_w183_ec_direct_url.py:215):

```python
def test_w183_rakuten_raw_oos_only_defers_to_playwright():
    from monitor.scrapers import _check_with_httpx, check_items_batch

    raw_oos = '<html><meta itemprop="availability" content="http://schema.org/OutOfStock"></html>'
    fake = MagicMock(status_code=200, text=raw_oos)
    url = "https://item.rakuten.co.jp/tuzukiya/m20-5806/"
    in_stock = ['itemprop="availability" content="http://schema.org/InStock"']
    sold_out = ['itemprop="availability" content="http://schema.org/OutOfStock"']
    no_page = ["ご指定のページは見つかりません"]

    with patch("monitor.scrapers.httpx.get", return_value=fake):
        assert _check_with_httpx(url, in_stock, sold_out, no_page) is None

    item = {"id": 1, "url": url, "in_stock": in_stock, "sold_out": sold_out, "no_page": no_page}
    with patch("monitor.scrapers.httpx.get", return_value=fake), \
         patch("monitor.scrapers._run_playwright_batch", return_value={1: "available"}) as pw:
        assert check_items_batch([item]) == {1: "available"}
        pw.assert_called_once()
```

Verification I could perform: saved raw HTML matches the failure evidence exactly. Verification I could not perform: live Playwright confirmation or pytest execution, due read-only/policy blocks.
