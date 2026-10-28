# Kenneth French momentum case: source and use

This project reads the [official monthly Mom CSV ZIP](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_CSV.zip) and follows the publisher's [factor definition](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor.html).

Source: Kenneth R. French Data Library; underlying research portfolios use CRSP data. Credit the publisher and original researchers. Public download access does not make the dataset part of QuantFactra's MIT software license. The repository and Pages package contain derived statistics, annually sampled figures, code and provenance hashes; they do **not** redistribute the ZIP or monthly source rows. Download the original from its publisher for your own reproduction and consult the [publisher's institution's terms](https://www.dartmouth.edu/copyright/) for other uses.

The source is a current, revised research-return vintage. French's library changed from CRSP FIZ to CIZ in January 2025; historical values can change. The exact 1927-2016 slice is hash-bound. A new archive that appends months but leaves this slice unchanged is accepted; changed historical returns stop reproduction before creating output. Keep your downloaded archive locally.

This is portfolio-return-level replay of a known anomaly. QuantFactra does not recreate CRSP constituents, corporate actions, stock-level weights or trades, and does not claim a new factor, point-in-time stock database, investable return or clean out-of-sample discovery. A year after publication is not automatically clean out-of-sample.

The factor has a long and a short leg. Compounding its monthly spread creates an illustrative index on one unit of reference capital, with 200% gross notional. Cash/collateral return, borrow, financing, transaction costs, impact and taxes are not available. Fixed 10/25 bps monthly deductions are illustrative sensitivity assumptions, not measured trading costs.
