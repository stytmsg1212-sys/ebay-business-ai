"""eBaymag GraphQL クライアント (CDP ログイン済 page 経由、money-direct)。

eBaymag は `https://ebaymag.com/graphql`。認証は cookie (credentials:include) +
`x-csrf-token` (meta[name=csrf-token])。本 module は page.evaluate の fetch で叩く。

提供:
  - gql(page, op, query, variables): 生 GraphQL 呼び出し (CSRF 自動付与)
  - list_profiles(page): 全ポリシー {id,title,numberOfProducts}
  - read_profile(page, profile_id): 単一プロファイル詳細 (shippingEbayProfiles 含む)
  - get_fx(page): {code: rate} (USD→現地通貨 換算用)

mutation (upsertProfile) は別途 build + 呼び出し (money-direct、read-back 必須)。
契約詳細: .claude/rule-snippets/browser-ui-native-input.md / memory
feedback_ebaymag_native_playwright_input_works。
"""
from __future__ import annotations

# CSRF を meta から読み、cookie 付きで graphql を叩く JS。
_GQL_JS = r"""async (args) => {
  const meta = document.querySelector('meta[name=csrf-token]');
  const csrf = meta ? meta.content : '';
  const r = await fetch('https://ebaymag.com/graphql', {
    method: 'POST',
    headers: {'content-type': 'application/json', 'x-csrf-token': csrf},
    credentials: 'include',
    body: JSON.stringify({operationName: args.op, query: args.q, variables: args.v}),
  });
  return {status: r.status, json: await r.json()};
}"""

LIST_QUERY = """query ShippingProfilesList($first: Int) {
  profiles(first: $first) {
    nodes { id title color dispatchTime numberOfProducts __typename }
    __typename
  }
}"""

PROFILE_QUERY = """query ShippingProfile($id: ID!) {
  profile(id: $id) {
    id title color dispatchTime numberOfProducts type returnsWithin
    returnsPaidByBuyer excludedCountries country city postalCode
    tariffs { prices { currency price additionalPrice __typename } timeMax locations __typename }
    domesticShippingServices { siteId key description carrier category min max __typename }
    shippingEbayProfiles { id title siteId showcaseId payload generatedByOriginal managedByUser __typename }
    __typename
  }
}"""

FX_QUERY = """query ShippingProfileAdditional {
  currencies { code rate __typename }
  viewer { id currency __typename }
}"""

SAVE_MUTATION = """mutation ShippingProfileSave($input: upsertProfileInput!) {
  upsertProfile(input: $input) {
    profile { id title __typename }
    success
    errors { message fields __typename }
    __typename
  }
}"""

# W317: 全商品 1 ページ分 (Relay Connection)。id + 各サイト listing の
# publicationUrl から eBay item id を抽出して product_id map を作る (呼出側)。
# ⚠️ filters は必ず null を渡す (省略と等価で archived 込み全件 = Phase0 実測 823)。
# 空 object {} を渡すと暗黙 archived:false (218 件) に絞られ map が欠ける。
PRODUCTS_QUERY = """query Products($first: Int, $after: String, $filters: ProductFilterInput) {
  products(first: $first, after: $after, filters: $filters) {
    totalCount
    pageInfo { endCursor hasNextPage }
    nodes { id listings { site { id } publicationUrl } }
  }
}"""


class EbaymagGraphQLError(RuntimeError):
    pass


def gql(page, op: str, query: str, variables: dict) -> dict:
    res = page.evaluate(_GQL_JS, {"op": op, "q": query, "v": variables})
    if res.get("status") != 200:
        raise EbaymagGraphQLError(f"{op}: HTTP {res.get('status')}")
    body = res.get("json") or {}
    if body.get("errors"):
        raise EbaymagGraphQLError(f"{op}: GraphQL errors {body['errors']}")
    return body.get("data") or {}


def list_profiles(page, first: int = 50) -> list[dict]:
    data = gql(page, "ShippingProfilesList", LIST_QUERY, {"first": first})
    return (data.get("profiles") or {}).get("nodes") or []


def read_profile(page, profile_id: str) -> dict:
    data = gql(page, "ShippingProfile", PROFILE_QUERY, {"id": profile_id})
    prof = data.get("profile")
    if not prof:
        raise EbaymagGraphQLError(f"profile {profile_id} が読めない")
    return prof


def get_fx(page) -> dict[str, float]:
    data = gql(page, "ShippingProfileAdditional", FX_QUERY, {})
    return {c["code"]: c["rate"] for c in (data.get("currencies") or []) if c.get("code")}


def list_products(page, first: int = 200, after: str | None = None) -> dict:
    """全商品の 1 ページ分を返す (Relay Connection、W317)。

    Relay pagination のループ (pageInfo.hasNextPage + after) は呼出側が回す。
    戻り値 = products connection dict ({totalCount, pageInfo{endCursor,hasNextPage}, nodes}).

    ⚠️ filters は必ず None を渡す (変数省略と等価 = archived 込み全件)。空 object {} を
    渡すと暗黙に archived:false へ絞られて map が欠ける (Phase0 実測)。
    """
    data = gql(
        page, "Products", PRODUCTS_QUERY,
        {"first": first, "after": after, "filters": None},
    )
    return data.get("products") or {}
