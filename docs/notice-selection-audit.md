# Notice classification implementation audit

Reviewed 10 October 2026 against a SQLite backup made through a read-only connection. Application source was tested in an isolated development-container copy. The review did not change live settings, current notices, historical decisions or transmit anything.

## Collected data

| Service | Retained notices | Current snapshot records | History rows | Unrecognized or missing retained values |
| --- | ---: | ---: | ---: | ---: |
| BOM | 1 | 1 | 117 | 0 |
| RFS | 471 | 30 | 972 | 0 |
| TRAFFIC | 909 | 507 | 1750 | 0 |

All recorded historical classification fields also resolve to catalogue entries. Old absent metadata is omitted from the omission count; it is not evidence that a provider failed to supply a value. Current means present in the saved snapshot, not necessarily an active hazard.

## Before/after review

The proposed migration changes no selection decisions among retained notices in this snapshot. History contains one excluded **Severe Thunderstorm Warning - Canberra** record that would match the selected thunderstorm family after explicit adoption. Its geographic eligibility and delivery history are separate; this historical comparison does not enqueue or resend it.

**Detailed Severe Thunderstorm Warning** already matches the legacy Warning suffix. Its seven historical rows merge into the same family, retaining Detailed as a subtype. **Warning to Sheep Graziers** also already matches the legacy special Warning-to prefix rule; its 33 historical rows remain included. Marine Wind Warning, Road Weather Alert and the nine ordinary thunderstorm rows keep their classifications.

**NEW ENGLAND HWY, KENTUCKY** (RFS incident 680884) is Grass Fire, Not Applicable, Under control, with provider council Tamworth. Geography can match Tamworth/New England while the selected levels exclude Not Applicable. The new current-page eligibility explanation separates these decisions. Migration does not automatically select Not Applicable or Planned Burn.

## Reviewed inventory

Observed counts below use distinct provider IDs per entry across retained data and history; repeated revisions do not inflate a value. An ID changing value can appear in more than one entry.

### BOM type

| Classification | Current | Observed distinct IDs | Raw examples |
| --- | ---: | ---: | --- |
| Severe Thunderstorm Warning | 0 | 3 | Detailed Severe Thunderstorm Warning, Severe Thunderstorm Warning, Severe Thunderstorm Warning - Canberra |
| Severe Weather Warning | 0 | 0 | Catalogue only |
| Flood Warning | 0 | 0 | Catalogue only |
| Flood Watch | 0 | 0 | Catalogue only |
| Fire Weather Warning | 0 | 0 | Catalogue only |
| Heatwave Warning | 0 | 0 | Catalogue only |
| Tropical Cyclone Warning | 0 | 0 | Catalogue only |
| Tropical Cyclone Advice | 0 | 0 | Catalogue only |
| Tsunami Warning | 0 | 0 | Catalogue only |
| Tsunami Watch | 0 | 0 | Catalogue only |
| Marine Wind Warning | 1 | 1 | Marine Wind Warning |
| Hazardous Surf Warning | 0 | 0 | Catalogue only |
| Damaging Surf Warning | 0 | 0 | Catalogue only |
| Coastal Hazard Warning | 0 | 0 | Catalogue only |
| Frost Warning | 0 | 0 | Catalogue only |
| Warning to Sheep Graziers | 0 | 1 | Warning to Sheep Graziers |
| Road Weather Alert | 0 | 1 | Road Weather Alert |
| Bush Walkers Weather Alert | 0 | 0 | Catalogue only |
| Unrecognized | 0 | 0 | Catalogue only |
| Not supplied | 0 | 0 | Catalogue only |

### RFS level

| Classification | Current | Observed distinct IDs | Raw examples |
| --- | ---: | ---: | --- |
| Emergency Warning | 0 | 0 | Catalogue only |
| Watch and Act | 0 | 0 | Catalogue only |
| Advice | 8 | 130 | Advice |
| Not Applicable | 19 | 371 | Not Applicable |
| Planned Burn | 3 | 10 | Planned Burn |
| Unrecognized | 0 | 0 | Catalogue only |
| Not supplied | 0 | 0 | Catalogue only |

### RFS kind

| Classification | Current | Observed distinct IDs | Raw examples |
| --- | ---: | ---: | --- |
| Grass Fire | 7 | 104 | Grass Fire |
| MVA/Transport | 1 | 80 | MVA/Transport |
| Bush Fire | 10 | 79 | Bush Fire |
| Other | 0 | 45 | Other |
| Fire Alarm | 0 | 30 | Fire Alarm |
| Burn off | 3 | 27 | Burn off |
| Assist Other Agency | 1 | 24 | Assist Other Agency |
| Car Fire | 0 | 19 | Car Fire |
| Structure Fire | 0 | 21 | Structure Fire |
| Vehicle/Equipment Fire | 0 | 15 | Vehicle/Equipment Fire |
| Medical | 0 | 11 | Medical |
| Flood/Storm/Tree Down | 1 | 11 | Flood/Storm/Tree Down |
| Hazard Reduction | 3 | 10 | Hazard Reduction |
| Planned Event | 4 | 10 | Planned Event |
| Truck Fire | 0 | 3 | Truck Fire |
| Fire Had Occurred | 0 | 3 | Fire Had Occurred |
| HAZMAT | 0 | 2 | HAZMAT |
| Search/Rescue | 0 | 1 | Search/Rescue |
| Rescue Road Crash | 0 | 1 | Rescue Road Crash |
| Haystack fire | 0 | 1 | Haystack fire |
| Unrecognized | 0 | 0 | Catalogue only |
| Not supplied | 0 | 0 | Catalogue only |

### TRAFFIC category

| Classification | Current | Observed distinct IDs | Raw examples |
| --- | ---: | ---: | --- |
| SCHEDULED ROADWORK | 171 | 195 | SCHEDULED ROADWORK |
| BREAKDOWN | 32 | 191 | BREAKDOWN |
| CRASH | 42 | 168 | CRASH |
| CHANGED TRAFFIC CONDITIONS | 107 | 136 | CHANGED TRAFFIC CONDITIONS |
| HAZARD | 78 | 103 | HAZARD |
| ADVERSE WEATHER | 26 | 27 | ADVERSE WEATHER |
| SPECIAL EVENT | 16 | 18 | SPECIAL EVENT |
| TRAFFIC LIGHTS FLASHING YELLOW | 1 | 15 | TRAFFIC LIGHTS FLASHING YELLOW |
| OPEN TO 4WD ONLY | 15 | 15 | OPEN TO 4WD ONLY |
| FLOODING | 9 | 11 | FLOODING |
| TRAFFIC LIGHTS BLACKED OUT | 1 | 10 | TRAFFIC LIGHTS BLACKED OUT |
| EMERGENCY ROADWORK | 5 | 9 | EMERGENCY ROADWORK |
| SMOKE | 2 | 5 | SMOKE |
| FERRY OUT OF SERVICE | 1 | 3 | FERRY OUT OF SERVICE |
| POLICE OPERATION | 1 | 3 | POLICE OPERATION |
| HOLIDAY TRAFFIC | 0 | 3 | HOLIDAY TRAFFIC |
| GRASS FIRE | 0 | 2 | GRASS FIRE |
| HEAVY TRAFFIC | 0 | 1 | HEAVY TRAFFIC |
| BUSHFIRE | 0 | 2 | BUSHFIRE |
| BURST WATER MAIN | 0 | 1 | BURST WATER MAIN |
| BUILDING FIRE | 0 | 1 | BUILDING FIRE |
| TRAFFIC LIGHTS | 0 | 1 | TRAFFIC LIGHTS |
| Unrecognized | 0 | 0 | Catalogue only |
| Not supplied | 0 | 0 | Catalogue only |

## Completion checks

| Planned requirement | Implemented and checked |
| --- | --- |
| Complete service dimensions | 18 BOM families; five RFS levels and 20 types; Traffic feeds plus 22 categories |
| Unknown and missing | Independently selectable per classification dimension; tested in normal processing and replay |
| Raw and normalized evidence | Raw labels, subtype/qualifier, saved catalogue snapshot; actual BOM severity/urgency/certainty only |
| Stable explicit selections | IDs, catalogue versions, aliases and reviewed patterns retained in saved snapshots; update adoption preview |
| Safe migration | Legacy rules remain active; exact exclusions retained; new type/category dimensions default All; feeds and Roadwork gate retained |
| Unified evaluation | Normal polling, replay/resend, queue guards, settings preview and current eligibility |
| Geographic separation | Jurisdiction/coverage and occurrence exclusions preserved; previously transmitted terminal exceptions retained |
| No duplicate sends | Provider revision hashes unchanged; completed delivery suppression checked through exclusion/re-inclusion |
| Operator controls | Searchable grouped All/Selected controls, current/observed counts, variants, read-only preview and diagnostics JSON |
| Historical integrity | New recorded evidence displayed; no retroactive rewriting or fabricated missing historical classifications |
| Tests | 734 passed (136 new tests); catalogue, migration, guard, replay, closure and special-value coverage |

Final checks: 734 tests passed in 22.25 seconds in the Python 3.12 development container. The existing Starlette/httpx deprecation warning remains. All 80 Python source/test files parsed successfully; the notice-selection widget passed `node --check`; `git diff --check` passed.

Physical MeshCore hardware and deployment on the separate test device were not part of this local audit. See [operator documentation](notice-selection.md).
