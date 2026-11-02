import DecimalJs from "decimal.js";

DecimalJs.set({ precision: 100, rounding: DecimalJs.ROUND_HALF_UP });

export { DecimalJs as Decimal };
