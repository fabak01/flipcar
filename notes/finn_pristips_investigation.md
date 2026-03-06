# FINN Pristips API investigation (2026-03-06)

## Scope
Investigated `https://www.finn.no/mobility/insights/price-valuation` for embedded data and backend endpoints.

## Method summary
- Tried direct `httpx` from the shell environment (blocked by proxy for this host).
- Used Playwright network logging to capture live fetch/XHR calls.
- Pulled and inspected `client-root.js` route map for API endpoints.
- Probed the discovered endpoints with and without required headers.

## HTML/source observations
- The page returns server-rendered HTML (`200`) and static assets.
- No obvious `__NEXT_DATA__` / `NEXT_DATA` payload was observed in the HTML body during inspection.
- The app JS bundle is loaded from:
  - `https://assets.finn.no/pkg/motor-price-valuation/0.0.145/client-root.js`

## Discovered route map (from JS bundle)
The minified bundle contains this endpoint map:

```js
je={lookup:it("/vehicles/:registrationNumber"),
lookupPriceValuation:it("/vehicles/price-valuation/:registrationNumber"),
pricePercentile:it("/ads/price/percentile"),
registrationHistory:it("/vehicles/:registrationNumber/history"),
distributionAds:it("/ads"),
totalDistributionAds:it("/ads/count"),
priceRanges:it("/ads/distribution/price/ranges"),
priceSummary:it("/ads/distribution/price/summary"),
mileageRanges:it("/ads/distribution/mileage/ranges"),
mileageSummary:it("/ads/distribution/mileage/summary"),
wheelDrives:it("/car/wheel-drives"),
equipment:it("/car/equipments"),
engineFuels:it("/car/engine-fuels"),
sold:it("/ads/sold"),
publishingTimeHistory:it("/ads/distribution/publishing-time/summary"),
adsActive:it("/ads/active"),
bodyTypes:it("/car/body-types"),
transmissions:it("/car/transmissions"),
models:it("/car/models"),
makes:it("/car/makes"),
registrationClasses:it("/car/registration-classes"),
registerLookupAccess:it("/lookup-access/register"),
lookupAccessQuotas:it("/lookup-access/quotas"),
priceValuation:it("/ads/price/valuation")}
```

Base prefix used by `it(...)` is:

```text
/mobility/insights/price-valuation/api
```

## Concrete test: BMW i3 2017 (EK45405, 96843 km)

### Endpoint that worked
`GET /mobility/insights/price-valuation/api/vehicles/price-valuation/EK45405?mileage=96843`

Required header:
- `X-Client-Id: motor-price-valuation`

Without `X-Client-Id`, backend responds:

```json
{"error":"X-Client-Id header is required"}
```

### Raw successful response sample (truncated)

```json
{
  "model": {"id": 2000264, "isPredicted": false, "text": "i3", "isOfvCorrect": true},
  "chassisNumber": {"value": "WBY1Z6105H7A09518", "isPredicted": false, "isOfvCorrect": true},
  "wheelDrive": {"id": 1, "isPredicted": false, "text": "Bakhjulsdrift", "isOfvCorrect": true},
  "engineFuel": {"id": 4, "isPredicted": false, "text": "El", "isOfvCorrect": true},
  "mileage": {"value": 96843, "isPredicted": false, "isOfvCorrect": false},
  "modelYear": {"value": 2017, "isPredicted": false, "isOfvCorrect": true},
  "registrationClass": {"id": 1, "isPredicted": false, "text": "Personbil", "isOfvCorrect": true},
  "transmission": {"id": 2, "isPredicted": false, "text": "Automat", "isOfvCorrect": true}
}
```

### Other endpoint behavior
- `GET /api/ads/count` works with `X-Client-Id` (returned `541307` in this run).
- `GET /api/ads/price/valuation` returned `400` with tested query combinations (likely requires specific parameter contract not brute-forced here).
- `POST /api/ads/price/valuation` returned `405 Method Not Allowed`.

## URL variant check
Tested variant:

`https://www.finn.no/mobility/insights/price-valuation?registration_number=EK45405`

No direct server-side JSON response was observed from that query param alone; the app still relies on API calls from frontend logic.

## GraphQL / REST conclusion
- No GraphQL endpoint usage was observed.
- Observed integration is REST-style under:
  - `/mobility/insights/price-valuation/api/...`

## Auth/session/cookies requirements
- For the tested vehicle lookup endpoint, no login was needed.
- Header `X-Client-Id: motor-price-valuation` was required.
- Cookies/session were not required for this specific GET lookup in our test.

## JS rendering requirement
- Not strictly required if you call the REST endpoints directly with correct headers.
- JS/browser inspection is useful to discover endpoint paths and required headers.

## Most reliable automation approach
1. Discover/track endpoint schema from JS bundle (`client-root.js`) because FINN may version/rename assets.
2. Call REST endpoint directly:
   - `GET /mobility/insights/price-valuation/api/vehicles/price-valuation/{regnr}?mileage={km}`
   - Header `X-Client-Id: motor-price-valuation`
3. Add retries, status checks, and schema validation in case of FINN backend changes.
4. Keep a browser fallback path (Playwright) if endpoint contracts change.

## Environment note
- In this container, direct shell `httpx/curl` to FINN was blocked by proxy (`CONNECT tunnel failed, response 403`).
- Playwright browser networking succeeded and allowed endpoint discovery/verification.
