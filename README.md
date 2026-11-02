# Project C: Procurement and Stock Replenishment

TypeScript solution for the APB Project C hub. It reads all rules and the business date from the run API, computes costs with `decimal.js`, then creates purchase orders, decisions, notifications, and the final report.

## Requirements

- Node.js 22 or newer
- Python 3.10 or newer only for the supplied `bpa_runner.py`

## Run locally

```sh
npm ci
npm test
npm run build
export BPA_HUB=https://apb.majordom.tech
export BPA_KEY=<your-personal-key>
python bpa_runner.py --project C --suite public -- node dist/solution.js {base_url}
```

Keep `BPA_KEY` out of source files and logs. The runner removes it before starting the solution; the solution receives only the run's `BASE_URL`.

## Cum se ruleaza la notare

```text
image: node:22-slim
setup: npm ci && npm run build
command: node dist/solution.js {base_url}
```

## Files

- `src/decimal.ts`: `decimal.js` precision and rounding configuration.
- `src/procurement.ts`: inventory position, canonical ordering, eligibility, offer choice, price review, and budget rules.
- `src/solution.ts`: hub requests, retries, purchase-order creation, decisions, notifications, and report.
- `ARCHITECTURE.md`: phase 1 architecture and restart behavior.
- `test/procurement.test.cjs`: focused boundary tests.

Run `npm test` before submission. Save the output from a successful public run as `public-run.log` in the repository.
