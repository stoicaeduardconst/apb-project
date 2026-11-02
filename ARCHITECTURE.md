# Project C Architecture

## Components and data flow

`src/solution.ts` is the process entry point. It accepts the run `BASE_URL` as its first argument or from `BPA_BASE_URL`; it never reads `BPA_KEY`. The hub client loads context, configuration, items, stock, demand, suppliers, cost centers, exchange rates, and purchase orders. It fetches offers only for active items at or below their reorder points.

`src/procurement.ts` owns the business decisions. It orders work by criticality A, B, C, then SKU text. For each item it computes the stock position from on-hand, reserved, and eligible incoming quantities. It ignores closed/cancelled orders and orders from the current run when computing incoming stock. It then applies the configured reorder point, pack size, MOQ, supplier status, offer dates, lead-time limit, and maximum quantity. The selected price tier is converted with the latest available EUR rate on or before the business date. The offer tie-break is rounded total cost, lead time, supplier on-time rate, then supplier ID.

`src/decimal.ts` configures `decimal.js` for 100-digit precision and half-up rounding. It avoids binary floating-point for demand ceilings, price thresholds, exchange rates, cost comparisons, and budget totals. Only final order costs are rounded to two decimal places.

The full plan is computed before the first write. The solution creates one order per supplier, with lines sorted by SKU, then records one decision per item. A supplier order that receives three HTTP 500 responses becomes `PO_CREATION_FAILED` for each assigned item; other order failures stop the process. Budget is consumed during the canonical planning pass for orderable items and is not refunded after a failed order. After final outcomes are known, the solution sends supplier order notifications and manual-review notifications, then posts the report in the original `/items` order.

## Retries and idempotency

Every POST includes the required `Idempotency-Key` header and reuses the same serialized body for retries. Keys are deterministic: `{supplier_id}:po`, `{sku}:decision`, `{po_number}:po_created`, `{sku}:replenishment_review`, `{sku}:stockout_risk`, and `report`. Requests time out after 10 seconds and start at least 55 ms apart. HTTP 500/502/503/504, network failures, and timeouts get at most three attempts, waiting the response's `Retry-After` value or one second. `429 RATE_LIMITED` waits and retries without consuming an attempt, up to ten waits. `429 RUN_REQUEST_LIMIT` and other unhandled 4xx responses stop the process.

Three HTTP 500 responses for one purchase order are the specified business failure. Three HTTP 500 responses for one notification cause that notification to be logged and skipped. A mixed failure sequence is not treated as either business case and stops the process.

## Restart safety

The solution stores no business state locally. Each start reloads hub state and recomputes the same canonical plan, ignoring purchase orders belonging to this run when calculating incoming stock. It sends writes with their original deterministic key and identical body; the hub replays an already accepted write, including the purchase-order number. It never creates a corrected decision under a second key. This makes a restart converge to the uninterrupted result without duplicate orders, decisions, or notifications. The final report is also always sent with the same `report` key and body.

## Verification

`npm test` compiles TypeScript and checks exact rounding, incoming-stock boundaries, the published EUR/MOQ example, exact price and budget limits, budget behavior after manual review, and canonical work order. A public hub run and its output belong in `public-run.log` before submission.
