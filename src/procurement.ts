import { Decimal } from "./decimal";

export type Outcome = "ORDER_CREATED" | "NO_ACTION" | "MANUAL_REVIEW";
export type ReasonCode =
  | "ITEM_DISCONTINUED"
  | "ABOVE_REORDER_POINT"
  | "NO_ELIGIBLE_SUPPLIER"
  | "PRICE_INCREASE_REVIEW"
  | "BUDGET_EXCEEDED"
  | "PO_CREATION_FAILED";

export interface Item {
  sku: string;
  pack_size: number;
  safety_stock: number;
  max_stock: number;
  reference_lead_time_days: number;
  criticality: "A" | "B" | "C";
  discontinued: boolean;
  last_purchase_price_ron: string | null;
  cost_center: string;
}

export interface Stock {
  sku: string;
  on_hand: number;
  reserved: number;
}

export interface Demand {
  sku: string;
  avg_daily_demand: string;
}

export interface Supplier {
  supplier_id: string;
  status: "ACTIVE" | "INACTIVE" | "BLOCKED";
  on_time_rate: string;
  email: string;
}

export interface Offer {
  supplier_id: string;
  currency: "RON" | "EUR";
  moq: number;
  max_qty: number;
  lead_time_days: number;
  valid_from: string;
  valid_to: string | null;
  price_tiers: { min_qty: number; unit_price: string }[];
}

export interface PurchaseOrder {
  po_number: string;
  supplier_id: string;
  status: "OPEN" | "CLOSED" | "CANCELLED";
  run_id: string | null;
  lines: {
    sku: string;
    quantity: number;
    received_quantity: number;
    eta: string;
  }[];
}

export interface CostCenter {
  cost_center: string;
  budget_ron: string;
}

export interface FxRate {
  date: string;
  currency: string;
  rate_ron: string;
}

export interface ProcurementConfig {
  horizon_days: number;
  price_increase_review_pct: string;
  max_lead_time_days: Record<"A" | "B" | "C", number>;
}

export interface Decision {
  sku: string;
  outcome: Outcome;
  reason_codes: ReasonCode[];
  po_number: string | null;
  supplier_id?: string;
  quantity?: number;
  unit_price?: string;
  currency?: "RON" | "EUR";
  total_cost_ron?: string;
}

export interface InventoryContext {
  businessDate: string;
  runId: string;
  config: ProcurementConfig;
  stockBySku: Map<string, Stock>;
  demandBySku: Map<string, Demand>;
  suppliersById: Map<string, Supplier>;
  costCentersById: Map<string, CostCenter>;
  purchaseOrders: PurchaseOrder[];
  fxRates: FxRate[];
}

export function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function canonicalItems(items: Item[]): Item[] {
  const priority = { A: 0, B: 1, C: 2 };
  return [...items].sort(
    (left, right) =>
      priority[left.criticality] - priority[right.criticality] ||
      compareText(left.sku, right.sku),
  );
}

export function inventoryPosition(
  item: Item,
  context: InventoryContext,
): number {
  const stock = context.stockBySku.get(item.sku);
  const onHand = stock?.on_hand ?? 0;
  const reserved = stock?.reserved ?? 0;
  const horizonEnd = addDays(context.businessDate, context.config.horizon_days);
  const incoming = context.purchaseOrders
    .filter(
      (order) => order.status === "OPEN" && order.run_id !== context.runId,
    )
    .flatMap((order) => order.lines)
    .filter((line) => line.sku === item.sku && line.eta <= horizonEnd)
    .reduce((sum, line) => sum + line.quantity - line.received_quantity, 0);
  return onHand - reserved + incoming;
}

export function shouldFetchOffers(
  item: Item,
  context: InventoryContext,
): boolean {
  if (item.discontinued) return false;
  const demand = new Decimal(
    context.demandBySku.get(item.sku)?.avg_daily_demand ?? "0",
  );
  const reorderPoint = demand
    .times(new Decimal(item.reference_lead_time_days))
    .ceil()
    .plus(item.safety_stock);
  return new Decimal(inventoryPosition(item, context)).lessThanOrEqualTo(
    reorderPoint,
  );
}

export function decideItem(
  item: Item,
  offers: Offer[],
  context: InventoryContext,
  spentByCostCenter: Map<string, Decimal>,
): Decision {
  if (item.discontinued) {
    return noAction(item.sku, "ITEM_DISCONTINUED");
  }

  const position = inventoryPosition(item, context);
  const demand = new Decimal(
    context.demandBySku.get(item.sku)?.avg_daily_demand ?? "0",
  );
  const reorderPoint = demand
    .times(new Decimal(item.reference_lead_time_days))
    .ceil()
    .plus(item.safety_stock);
  if (new Decimal(position).greaterThan(reorderPoint)) {
    return noAction(item.sku, "ABOVE_REORDER_POINT");
  }

  const baseQuantity = ceilToPack(item.max_stock - position, item.pack_size);
  const supplierOffers: EvaluatedOffer[] = [];
  for (const offer of offers) {
    const supplier = context.suppliersById.get(offer.supplier_id);
    const quantity = Math.max(
      baseQuantity,
      ceilToPack(offer.moq, item.pack_size),
    );
    if (
      !supplier ||
      supplier.status !== "ACTIVE" ||
      offer.valid_from > context.businessDate ||
      (offer.valid_to !== null && offer.valid_to < context.businessDate) ||
      offer.lead_time_days >
        context.config.max_lead_time_days[item.criticality] ||
      quantity > offer.max_qty
    ) {
      continue;
    }

    const tier = offer.price_tiers
      .filter((candidate) => candidate.min_qty <= quantity)
      .reduce<
        { min_qty: number; unit_price: string } | undefined
      >((best, candidate) => (!best || candidate.min_qty > best.min_qty ? candidate : best), undefined);
    if (!tier) continue;

    const rate =
      offer.currency === "RON"
        ? new Decimal("1")
        : findRate(context.fxRates, context.businessDate);
    const unitPrice = new Decimal(tier.unit_price);
    const totalCost = new Decimal(quantity).times(unitPrice).times(rate);
    supplierOffers.push({
      offer,
      supplier,
      quantity,
      unitPrice: tier.unit_price,
      unitPriceRon: unitPrice.times(rate),
      totalCostRon: new Decimal(totalCost.toFixed(2)),
    });
  }

  if (supplierOffers.length === 0) {
    return {
      sku: item.sku,
      outcome: "MANUAL_REVIEW",
      reason_codes: ["NO_ELIGIBLE_SUPPLIER"],
      po_number: null,
    };
  }

  supplierOffers.sort(compareOffers);
  const selected = supplierOffers[0];
  const decision: Decision = {
    sku: item.sku,
    outcome: "ORDER_CREATED",
    reason_codes: [],
    po_number: null,
    supplier_id: selected.offer.supplier_id,
    quantity: selected.quantity,
    unit_price: selected.unitPrice,
    currency: selected.offer.currency,
    total_cost_ron: selected.totalCostRon.toFixed(2),
  };

  const reasonCodes: ReasonCode[] = [];
  if (item.last_purchase_price_ron !== null) {
    const threshold = new Decimal(item.last_purchase_price_ron).times(
      new Decimal("1").plus(
        new Decimal(context.config.price_increase_review_pct).dividedBy(100),
      ),
    );
    if (selected.unitPriceRon.comparedTo(threshold) > 0) {
      reasonCodes.push("PRICE_INCREASE_REVIEW");
    }
  }

  const spent = spentByCostCenter.get(item.cost_center) ?? new Decimal("0");
  const budget = new Decimal(
    context.costCentersById.get(item.cost_center)!.budget_ron,
  );
  if (
    spent.plus(new Decimal(decision.total_cost_ron!)).comparedTo(budget) > 0
  ) {
    reasonCodes.push("BUDGET_EXCEEDED");
  }

  if (reasonCodes.length > 0) {
    decision.outcome = "MANUAL_REVIEW";
    decision.reason_codes = reasonCodes;
  } else {
    spentByCostCenter.set(
      item.cost_center,
      spent.plus(new Decimal(decision.total_cost_ron!)),
    );
  }
  return decision;
}

function noAction(sku: string, reasonCode: ReasonCode): Decision {
  return {
    sku,
    outcome: "NO_ACTION",
    reason_codes: [reasonCode],
    po_number: null,
  };
}

function ceilToPack(amount: number, packSize: number): number {
  const quantity = BigInt(amount);
  const pack = BigInt(packSize);
  return Number(((quantity + pack - 1n) / pack) * pack);
}

function findRate(rates: FxRate[], businessDate: string): Decimal {
  const latest = rates
    .filter((rate) => rate.currency === "EUR" && rate.date <= businessDate)
    .sort((left, right) => compareText(right.date, left.date))[0];
  if (!latest)
    throw new Error(`No EUR exchange rate at or before ${businessDate}`);
  return new Decimal(latest.rate_ron);
}

function compareOffers(left: EvaluatedOffer, right: EvaluatedOffer): number {
  return (
    left.totalCostRon.comparedTo(right.totalCostRon) ||
    left.offer.lead_time_days - right.offer.lead_time_days ||
    new Decimal(right.supplier.on_time_rate).comparedTo(
      new Decimal(left.supplier.on_time_rate),
    ) ||
    compareText(left.offer.supplier_id, right.offer.supplier_id)
  );
}

interface EvaluatedOffer {
  offer: Offer;
  supplier: Supplier;
  quantity: number;
  unitPrice: string;
  unitPriceRon: Decimal;
  totalCostRon: Decimal;
}

function addDays(date: string, days: number): string {
  const result = new Date(`${date}T00:00:00Z`);
  result.setUTCDate(result.getUTCDate() + days);
  return result.toISOString().slice(0, 10);
}
