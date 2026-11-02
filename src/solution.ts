import {
  canonicalItems,
  compareText,
  decideItem,
  shouldFetchOffers,
  type Decision,
  type Demand,
  type FxRate,
  type InventoryContext,
  type Item,
  type Offer,
  type PurchaseOrder,
  type Supplier,
  type Stock,
} from "./procurement";
import { Decimal } from "./decimal";

const MAX_ATTEMPTS = 3;
const REQUEST_INTERVAL_MS = 55;
const REQUEST_TIMEOUT_MS = 10_000;

interface RunContext {
  run_id: string;
  business_date: string;
}

interface ProcurementConfig {
  horizon_days: number;
  price_increase_review_pct: string;
  max_lead_time_days: Record<"A" | "B" | "C", number>;
  notification_recipients: {
    PROCUREMENT_MANAGER: string;
    WAREHOUSE_MANAGER: string;
  };
}

interface CostCenter {
  cost_center: string;
  budget_ron: string;
}

interface PurchaseOrderResult {
  po_number: string;
}

class ApiError extends Error {
  constructor(
    message: string,
    readonly statusHistory: (number | null)[],
    readonly code?: string,
  ) {
    super(message);
  }
}

class HubClient {
  private lastRequestAt = 0;

  constructor(private readonly baseUrl: string) {}

  async get<T>(path: string): Promise<T> {
    return this.request<T>("GET", path);
  }

  async post<T>(
    path: string,
    body: unknown,
    idempotencyKey: string,
  ): Promise<T> {
    return this.request<T>("POST", path, body, idempotencyKey);
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
    idempotencyKey?: string,
  ): Promise<T> {
    const serializedBody =
      body === undefined ? undefined : JSON.stringify(body);
    const statusHistory: (number | null)[] = [];
    let attempts = 0;
    let rateLimitWaits = 0;

    while (attempts < MAX_ATTEMPTS) {
      await this.waitForRequestSlot();
      attempts += 1;
      const headers: Record<string, string> = { Accept: "application/json" };
      if (serializedBody !== undefined)
        headers["Content-Type"] = "application/json";
      if (idempotencyKey !== undefined)
        headers["Idempotency-Key"] = idempotencyKey;

      let response: Response;
      try {
        response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
          method,
          headers,
          body: serializedBody,
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
      } catch (error) {
        statusHistory.push(null);
        if (attempts === MAX_ATTEMPTS) {
          throw new ApiError(
            `${method} ${path}: network error: ${errorMessage(error)}`,
            statusHistory,
          );
        }
        await this.retry(method, path, attempts, "network error", 1);
        continue;
      }

      if (response.ok) {
        const text = await response.text();
        return (text ? JSON.parse(text) : undefined) as T;
      }

      const errorBody = (await response.json().catch(() => ({}))) as {
        error?: { code?: string; message?: string };
      };
      const code = errorBody.error?.code;
      const message = errorBody.error?.message ?? `HTTP ${response.status}`;

      if (response.status === 429 && code === "RATE_LIMITED") {
        if (rateLimitWaits >= 10) {
          throw new ApiError(
            `${method} ${path}: gave up after 10 RATE_LIMITED waits`,
            statusHistory,
            code,
          );
        }
        rateLimitWaits += 1;
        const delay = retryDelay(response.headers.get("Retry-After"));
        console.log(
          `retry ${rateLimitWaits}: ${method} ${path} after RATE_LIMITED; wait ${delay}s`,
        );
        await sleep(delay * 1_000);
        attempts -= 1;
        continue;
      }

      if (response.status === 429 && code === "RUN_REQUEST_LIMIT") {
        statusHistory.push(response.status);
        throw new ApiError(
          `${method} ${path}: ${code}: ${message}`,
          statusHistory,
          code,
        );
      }

      statusHistory.push(response.status);
      if (
        [500, 502, 503, 504].includes(response.status) &&
        attempts < MAX_ATTEMPTS
      ) {
        const delay = retryDelay(response.headers.get("Retry-After"));
        await this.retry(
          method,
          path,
          attempts,
          `HTTP ${response.status} ${code ?? message}`,
          delay,
        );
        continue;
      }
      if ([500, 502, 503, 504].includes(response.status)) {
        throw new ApiError(
          `${method} ${path}: HTTP ${response.status} ${code ?? message}`,
          statusHistory,
          code,
        );
      }
      throw new ApiError(
        `${method} ${path}: HTTP ${response.status} ${code ?? message}`,
        statusHistory,
        code,
      );
    }

    throw new ApiError(`${method} ${path}: request exhausted`, statusHistory);
  }

  private async waitForRequestSlot(): Promise<void> {
    const elapsed = Date.now() - this.lastRequestAt;
    if (elapsed < REQUEST_INTERVAL_MS)
      await sleep(REQUEST_INTERVAL_MS - elapsed);
    this.lastRequestAt = Date.now();
  }

  private async retry(
    method: string,
    path: string,
    attempt: number,
    cause: string,
    delay: number,
  ): Promise<void> {
    console.log(
      `retry ${attempt}/${MAX_ATTEMPTS}: ${method} ${path} after ${cause}; wait ${delay}s`,
    );
    await sleep(delay * 1_000);
  }
}

async function run(baseUrl: string): Promise<void> {
  const hub = new HubClient(baseUrl);
  const runContext = await hub.get<RunContext>("/context");
  const config = await hub.get<ProcurementConfig>("/config");
  const items = await hub.get<Item[]>("/items");
  const stocks = await hub.get<Stock[]>("/stock");
  const demands = await hub.get<Demand[]>("/demand");
  const suppliers = await hub.get<Supplier[]>("/suppliers");
  const costCenters = await hub.get<CostCenter[]>("/cost-centers");
  const fxRates = await hub.get<FxRate[]>("/fx-rates");
  const purchaseOrders = await hub.get<PurchaseOrder[]>("/purchase-orders");

  const context: InventoryContext = {
    businessDate: runContext.business_date,
    runId: runContext.run_id,
    config,
    stockBySku: new Map(stocks.map((stock) => [stock.sku, stock])),
    demandBySku: new Map(demands.map((demand) => [demand.sku, demand])),
    suppliersById: new Map(
      suppliers.map((supplier) => [supplier.supplier_id, supplier]),
    ),
    costCentersById: new Map(
      costCenters.map((center) => [center.cost_center, center]),
    ),
    purchaseOrders,
    fxRates,
  };

  const orderedItems = canonicalItems(items);
  const offersBySku = new Map<string, Offer[]>();
  for (const item of orderedItems) {
    if (shouldFetchOffers(item, context)) {
      offersBySku.set(
        item.sku,
        await hub.get<Offer[]>(`/offers?sku=${encodeURIComponent(item.sku)}`),
      );
    }
  }

  const spentByCostCenter = new Map<string, Decimal>();
  const decisions = orderedItems.map((item) =>
    decideItem(
      item,
      offersBySku.get(item.sku) ?? [],
      context,
      spentByCostCenter,
    ),
  );

  const ordersBySupplier = groupOrders(decisions);
  const poNumbersBySupplier = new Map<string, string>();
  for (const [supplierId, lines] of [...ordersBySupplier.entries()].sort(
    ([left], [right]) => compareText(left, right),
  )) {
    try {
      const result = await hub.post<PurchaseOrderResult>(
        "/purchase-orders",
        {
          supplier_id: supplierId,
          lines,
        },
        `${supplierId}:po`,
      );
      if (!result?.po_number)
        throw new Error(
          `POST /purchase-orders for ${supplierId} returned no po_number`,
        );
      poNumbersBySupplier.set(supplierId, result.po_number);
      for (const decision of decisions) {
        if (
          decision.outcome === "ORDER_CREATED" &&
          decision.supplier_id === supplierId
        ) {
          decision.po_number = result.po_number;
        }
      }
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.statusHistory.length === MAX_ATTEMPTS &&
        error.statusHistory.every((status) => status === 500)
      ) {
        for (const decision of decisions) {
          if (
            decision.outcome === "ORDER_CREATED" &&
            decision.supplier_id === supplierId
          ) {
            decision.outcome = "MANUAL_REVIEW";
            decision.reason_codes = ["PO_CREATION_FAILED"];
          }
        }
        console.log(
          `purchase order failed for ${supplierId}; assigned items require manual review`,
        );
      } else {
        throw error;
      }
    }
  }

  for (const decision of decisions) {
    await hub.post(
      "/replenishment-decisions",
      decisionBody(decision),
      `${decision.sku}:decision`,
    );
    console.log(
      `${decision.sku}: ${decision.outcome}; reasons=${decision.reason_codes.join(",") || "none"}`,
    );
  }

  const criticalityBySku = new Map(
    items.map((item) => [item.sku, item.criticality]),
  );
  await sendNotifications(
    hub,
    decisions,
    suppliers,
    config,
    poNumbersBySupplier,
    criticalityBySku,
  );

  const decisionsBySku = new Map(
    decisions.map((decision) => [decision.sku, decision]),
  );
  const report = {
    items: items.map((item) => {
      const decision = decisionsBySku.get(item.sku)!;
      return {
        sku: decision.sku,
        outcome: decision.outcome,
        reason_codes: decision.reason_codes,
        po_number: decision.po_number,
      };
    }),
  };
  await hub.post("/report", report, "report");
  console.log(`report submitted for ${items.length} items`);
}

function groupOrders(
  decisions: Decision[],
): Map<string, { sku: string; quantity: number }[]> {
  const grouped = new Map<string, { sku: string; quantity: number }[]>();
  for (const decision of decisions) {
    if (
      decision.outcome !== "ORDER_CREATED" ||
      !decision.supplier_id ||
      decision.quantity === undefined
    )
      continue;
    const lines = grouped.get(decision.supplier_id) ?? [];
    lines.push({ sku: decision.sku, quantity: decision.quantity });
    grouped.set(decision.supplier_id, lines);
  }
  for (const lines of grouped.values())
    lines.sort((left, right) => compareText(left.sku, right.sku));
  return grouped;
}

function decisionBody(decision: Decision): Record<string, unknown> {
  const body: Record<string, unknown> = {
    sku: decision.sku,
    outcome: decision.outcome,
    reason_codes: decision.reason_codes,
  };
  if (decision.supplier_id !== undefined)
    body.supplier_id = decision.supplier_id;
  if (decision.quantity !== undefined) body.quantity = decision.quantity;
  if (decision.unit_price !== undefined) body.unit_price = decision.unit_price;
  if (decision.currency !== undefined) body.currency = decision.currency;
  if (decision.total_cost_ron !== undefined)
    body.total_cost_ron = decision.total_cost_ron;
  if (decision.po_number !== null) body.po_number = decision.po_number;
  return body;
}

async function sendNotifications(
  hub: HubClient,
  decisions: Decision[],
  suppliers: Supplier[],
  config: ProcurementConfig,
  poNumbersBySupplier: Map<string, string>,
  criticalityBySku: Map<string, string>,
): Promise<void> {
  const supplierById = new Map(
    suppliers.map((supplier) => [supplier.supplier_id, supplier]),
  );
  for (const [supplierId, poNumber] of [...poNumbersBySupplier.entries()].sort(
    ([left], [right]) => compareText(left, right),
  )) {
    const supplier = supplierById.get(supplierId)!;
    await sendNotification(
      hub,
      {
        template: "po_created",
        recipient_role: "SUPPLIER",
        recipient: supplier.email,
        po_number: poNumber,
      },
      `${poNumber}:po_created`,
    );
  }

  for (const decision of decisions) {
    if (decision.outcome !== "MANUAL_REVIEW") continue;
    await sendNotification(
      hub,
      {
        template: "replenishment_review",
        recipient_role: "PROCUREMENT_MANAGER",
        recipient: config.notification_recipients.PROCUREMENT_MANAGER,
        sku: decision.sku,
      },
      `${decision.sku}:replenishment_review`,
    );
    if (criticalityBySku.get(decision.sku) === "A") {
      await sendNotification(
        hub,
        {
          template: "stockout_risk",
          recipient_role: "WAREHOUSE_MANAGER",
          recipient: config.notification_recipients.WAREHOUSE_MANAGER,
          sku: decision.sku,
        },
        `${decision.sku}:stockout_risk`,
      );
    }
  }
}

async function sendNotification(
  hub: HubClient,
  body: Record<string, string>,
  key: string,
): Promise<void> {
  try {
    await hub.post("/notifications", body, key);
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.statusHistory.length === MAX_ATTEMPTS &&
      error.statusHistory.every((status) => status === 500)
    ) {
      console.log(`notification ${key} skipped after three HTTP 500 responses`);
      return;
    }
    throw error;
  }
}

function retryDelay(value: string | null): number {
  if (value === null) return 1;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : 1;
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function main(): Promise<void> {
  const baseUrl = process.argv[2] ?? process.env.BPA_BASE_URL;
  if (!baseUrl)
    throw new Error("Pass BASE_URL as the first argument or set BPA_BASE_URL");
  await run(baseUrl);
}

main().catch((error) => {
  console.error(`fatal: ${errorMessage(error)}`);
  process.exitCode = 1;
});
