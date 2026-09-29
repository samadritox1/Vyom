# VYOM-MARINE v4 — Real Data Implementation Audit

## Meaning of status

- **Implemented**: code path exists, normalized schema exists, and is covered by compilation/tests or deterministic parser tests.
- **Implemented / configuration required**: connector exists, but operator must provide credentials, verified dataset IDs, files, or feed URLs.
- **External dependency**: no program can obtain the data until the provider grants access or an operator supplies the data product.

| Capability | Status | Notes |
|---|---|---|
| IMD port warnings | Implemented | Public API connector |
| IMD sea-area bulletin | Implemented | Public API connector |
| IMD coastal bulletin | Implemented | Public API connector |
| IMD cyclone track | Implemented | Official-context connector |
| IMD cyclone wind | Implemented | Official-context connector |
| IMD cone of uncertainty | Implemented | Official-context connector |
| IMD fishermen warning | Implemented / endpoint config | `IMD_FISHERMEN_ENDPOINT` |
| NOAA NDBC observations | Implemented | Automatic nearest-station discovery + real-time parser |
| NOAA CO-OPS observations | Implemented | Automatic station discovery + data API |
| INCOIS WW3 | Implemented / NetCDF required | Reads operator-synced local NetCDF |
| INCOIS HYCOM | Implemented / NetCDF required | Reads operator-synced local NetCDF |
| INCOIS RSMC latest filename discovery | Implemented | Does not guess unstable direct file URL |
| INCOIS ERDDAP | Implemented / verified dataset mapping required | Declarative dataset mapping |
| GEBCO bathymetry | Implemented / grid required | Screening only; not navigation grade |
| AISHub AIS | Implemented / credentials required | Area-based AIS |
| MarineTraffic AIS | Implemented / key required | Explicit MMSI/IMO lookup adapter |
| Copernicus Marine | Implemented / account + dataset config required | Uses official Python toolbox |
| CAP warning feeds | Implemented / feed URLs required | Polygons + timestamps + severity |
| Cyclone ensemble JSON | Implemented / upstream track processor required | Provider-independent normalized interface |
| Sea-ice NetCDF/OPeNDAP | Implemented / product URL/file required | Risk context only |
| Provenance store | Implemented | SQLite prototype |
| Source health | Implemented | Last attempt/success/error/records |
| Forecast store | Implemented | Cycle, valid time, lead, model, member retained |
| Observation store | Implemented | QC, source quality, sigma, timestamps retained |
| Forecast-observation matcher | Implemented | Space/time matching |
| Circular direction error | Implemented | Wind/wave/current/swell directions |
| Regional calibration | Implemented | 5° → 10° → basin → global hierarchy |
| Seasonal calibration | Implemented | Season key retained |
| Lead-time calibration | Implemented | Lead-band cells |
| Variable-specific calibration | Implemented | Separate cells per variable |
| Model-specific calibration | Implemented | Separate cells per model |
| Bias / MAE / RMSE | Implemented | Recomputed from matched pairs |
| Brier score | Implemented for configured event thresholds | Probability/event verification |
| Reliability | Implemented as calibration diagnostic | Improves as sample size grows |
| Full ensemble CRPS | Requires full retained ensemble distribution | Do not claim CRPS from deterministic single values |
| Hierarchical shrinkage | Implemented | Sparse cells shrink toward parent region |
| Dynamic learned model weights | Implemented | Calibration exported to engine |
| Drift monitoring | Implemented | Recent vs baseline RMSE |
| Bayesian observation assimilation | Implemented in marine engine | Real observations mapped into sensor schema |
| Official-warning priority | Implemented | Caller/authority warning cannot be silently downgraded |
| Warning polygon relevance | Implemented | Point-in-polygon when geometry exists |
| Named-area warning relevance | Implemented | Conservative basin/coastal matching |
| AIS → CPA/TCPA | Implemented | Existing Ocean Guardian traffic engine consumes contacts |
| Cyclone ensemble → route exposure | Implemented | Normalized tracks supplied to engine |
| Sea ice → vessel risk context | Implemented | Requires configured product and vessel capability |
| Vessel prediction logging | Implemented | Risk predictions retained with engine version |
| Verified vessel outcome labels | Implemented | Admin-gated only |
| Vessel-class calibration | Implemented | Separate from environmental model calibration |
| Continuous worker | Implemented | Separate long-running worker recommended |
| API health endpoint | Implemented | `/marine/data/health` |
| API collection endpoint | Implemented | `/marine/data/collect` |
| API recalibration endpoint | Implemented | `/marine/data/calibrate` |
| API verified vessel-case endpoint | Implemented | `/marine/data/vessel-case` |

## Deliberately not fabricated

The implementation does not manufacture provider access, paid AIS credentials, official hydrographic depth, verified INCOIS dataset IDs, vessel RAOs, certified stability limits, or GMDSS/METAREA feed URLs that have not been supplied by the operator.

Those are external data/authorization dependencies, not missing mathematical code.
