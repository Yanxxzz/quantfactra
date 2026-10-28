# ECB reference-rate snapshot

Source: European Central Bank, euro foreign exchange reference rates.

- [Original series and purpose](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
- [ECB copyright and reuse conditions](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html)
- Exact bounded query, retrieval time and hashes: `source.json`.

The information is available free of charge from the ECB. The data remain subject to the ECB's reuse conditions, separately from QuantFactra's software MIT license. Reproduction must acknowledge the ECB and remain accurate; transformations must be identified. If incorporated into sold documents, users must be told that the source information is freely available from the ECB. No ECB endorsement is claimed.

`rates.json` reshapes the received CHF, GBP, JPY and USD quote strings into date rows, preserving the original numbers and observation-status codes. Raw quotes are currency units per one EUR. QuantFactra derives EUR per unit by taking the reciprocal. It excludes dates without a positive finite quote for every member of this fixed four-currency basket, records those dates, and does not fill missing values. This source-calendar clarification was recorded before computing factor values or results; the query scope and strategy parameters were unchanged.

These are reference observations, not executable bid/ask prices. The ECB publishes them for information and discourages transaction use. This example's portfolio calculation is hypothetical, ignores currency interest/carry and uses declared transaction-cost scenarios. The snapshot is the historical data published at retrieval time, not a historical-vintage/PIT archive.
