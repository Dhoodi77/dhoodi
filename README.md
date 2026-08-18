# Vera & Fold — Shopify Theme (Online Store 2.0)

This is a working theme skeleton implementing the structure from the strategy
spec: header/footer, homepage sections, product page, collection page, cart
drawer + cart page, search, 404, generic page template.

## What's real vs. what's a placeholder

- **Structure, Liquid, CSS, JS, schema: real and functional** against Shopify's
  Online Store 2.0 conventions as of this build.
- **Copy, prices, images: placeholder/example**, matching the strategy doc's
  proposals. Replace via the theme editor once approved.
- **Ingredients, how-to-use, certifications, reviews: intentionally empty**
  until real product/formulation data exists — the theme renders a visible
  "pending" state rather than fabricating content (see `main-product.liquid`).
- This was built and validated as files, not pushed to a live Shopify store —
  I don't have Shopify CLI / theme-repo access from this chat. Deployment is
  the one step you or a developer has to run (steps below).

## Before you deploy: admin setup required

The theme references these and will silently no-op or show fallback text
until they exist in your store's admin:

1. **Metaobject definition**: `routine_step_definition`
   Fields: `icon` (file), `title` (single line text), `description` (multi-line text).
   Used by `sections/routine-builder.liquid`.
2. **Product metafields** (namespace `custom`):
   - `custom.how_to_use` (rich text)
   - `custom.ingredients_inci` (multi-line text)
   - `custom.key_benefits` (list of single-line text)
   - `custom.routine_step` (integer) — used for the "Step N" tag on product cards
3. **Reviews**: `product.metafields.reviews.rating` is the standard namespace
   used by Shopify's own Product Reviews app and most third-party review apps.
   If you use a different app, check its docs for the metafield/snippet it expects.
4. **Menus**: create a `main-menu` navigation menu in Settings → Navigation —
   the header section defaults to looking for one named exactly that.
5. **Markets**: set up a US market (USD) and an EU market group (EUR) in
   Settings → Markets. No currency logic is hardcoded in this theme; all
   pricing runs through Shopify's `money` filters and will follow whatever
   Markets configuration you set.

## Deployment

```bash
# from the theme's root directory (this folder)
shopify theme dev            # local preview against your dev store
shopify theme push            # push to a new unpublished theme on your store
shopify theme push --live     # publish (only once you've reviewed it)
```

Requires the Shopify CLI authenticated against your store
(`shopify auth login`). Full CLI docs: https://shopify.dev/docs/api/shopify-cli

## Known limitations / next passes needed

- No visual QA has been done inside an actual Shopify theme preview (only a
  static CSS sanity check outside Shopify) — check real rendering once pushed,
  particularly the sticky product bar and cart drawer AJAX flow.
- Cart drawer re-render after "Add to cart" currently just updates the item
  count; wire it to Shopify's Section Rendering API
  (`/?sections=cart-drawer`) so the drawer's line items refresh without a
  full page reload — noted inline in `assets/theme.js`.
- Product recommendations section assumes Shopify's native
  `recommendations` object; confirm it's populated as expected once real
  products/collections exist (it needs sales data or manual related-products
  settings to return results).
- No app-specific integrations are wired (reviews app, subscription app,
  loyalty, etc.) — add per whichever apps you actually install.
- Structured data currently covers Organization only; add Product and
  BreadcrumbList JSON-LD once real product data exists (flagged in the
  strategy doc, not yet added here to avoid emitting structured data with
  placeholder values).
- Accessibility and performance were designed-in (semantic HTML, alt text
  fields, lazy loading, responsive `srcset`, focus states) but not run through
  an automated audit (Lighthouse/axe) — do that once live.
