# Services and data sources

[Back to installation and setup](../README.md).

## BOM current warnings

BOM, RFS and Live Traffic NSW each have their own enable control and polling interval in minutes (minimum 5). Their geographic coverage is configured together on **Settings → Geographic Coverage** (`/settings/geography`). Coverage uses **All NSW OR selected council OR additional location term**. Other service filters still apply. Source pages show geographic decisions and match evidence; broadcast History remains separate.

## NSW RFS Fires Near Me

NoticeEcho can monitor the official NSW RFS current-incidents GeoJSON feed as a separate source. Open **NSW RFS settings** to enable monitoring and select alert levels. Configure coverage on the universal Geographic Coverage page. Emergency Warning and Watch and Act are selected initially; Advice is optional. The council filter applies to every level. No RFS messages are sent until monitoring and applicable geographic coverage are selected. The global Dry Run setting also applies to RFS.

RFS incidents have their own live view; BOM and RFS broadcast history appear together in History with a source filter. NoticeEcho checks the RFS feed at its configured interval (minimum 5 minutes); the RFS says incident data is updated every 30 minutes. Incident locations may be approximate. Source: © State of New South Wales (NSW Rural Fire Service). For current information go to [rfs.nsw.gov.au](https://www.rfs.nsw.gov.au/).

## Live Traffic NSW

Live Traffic NSW is a separate, disabled-by-default service. Configure monitoring and hazard feeds under **Settings → Live Traffic NSW**, and location coverage under **Settings → Geographic Coverage**. It uses keyless Transport for NSW public GeoJSON feeds and the global Dry Run setting. Incident, flood and local council feeds are selected initially; scheduled roadworks require an explicit Roadwork selection. Future and expired notices are recorded without broadcasting active warnings; explicit provider-ended updates can be sent for previously transmitted notices. The selected Fire feed remains enabled alongside RFS monitoring and provides fire-related road impacts. Existing items on the first live poll are recorded as a baseline without transmitting; expanded geographic coverage can then send previously excluded current matching items. Feed disappearance is recorded after two successful polls without claiming a road has reopened.

State-road coordinates are matched locally against a simplified NSW Spatial Services council-boundary snapshot (`app/traffic/nsw_lga.geojson.gz`, obtained 1 October 2026); regional council names are a fallback. Boundary locations are approximate, especially near borders. Source: © Transport for NSW, [Live Traffic Hazards dataset](https://data.nsw.gov.au/data/dataset/2-live-traffic-hazards). Verify current conditions at [livetraffic.com](https://www.livetraffic.com/).

## BOM feed behavior

The application uses BOM's public NSW warning RSS feed, including the warning link and product identifier supplied by BOM. RSS items are normalized into provider-neutral alerts before filtering and deduplication. BOM's feed documentation notes that RSS should not be the sole source of warning information and requires links back to the full BOM warning product.

Recent warning revisions and the History page record each distinct BOM version received from the NSW feed, including warnings excluded by broadcast filters. An unchanged warning is not added again on every poll. The Events log records each dry-run attempt, so it can grow while History stays the same. The unofficial verification message is queued after warning parts and sent at most once per five minutes on the live channel; its last successful send time survives restarts. Dry-run shows the same five-minute cadence.

The default broadcast policy includes all BOM warning products. Settings can instead select individual Australian warning products, plus additional products such as Flood Watch, Tropical Cyclone Advice, Road Weather Alert and Bush Walkers Weather Alert. Timestamped RSS titles and `Marine Wind Warning Summary` items are normalized before filtering; qualified flood products and numbered tropical cyclone products match their corresponding product selection. Council matching uses BOM warning polygons when supplied, or complete typed LGA names or explicitly administrative council names. Broad forecast districts, marine coasts, and incomplete location descriptions are marked unknown rather than treated as outside a selected council. Existing non-NSW current snapshots and rows with proven non-NSW provenance are removed on upgrade; older History without reliable source provenance is retained. For marine wind warnings whose RSS item contains only a statewide summary, NoticeEcho resolves the linked BOM product ID and reads the warning detail API. Strong Wind Warning areas and cancellations are sent as separately labelled parts. If detail is unavailable, it falls back to the RSS summary.
