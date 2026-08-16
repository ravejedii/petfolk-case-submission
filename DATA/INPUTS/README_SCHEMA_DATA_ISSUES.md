# Petfolk Medical & Operations — Take-Home Dataset

This is the dataset referenced in your AI Strategy Lead, Medical & Operations take-home challenge.

Use any tools, languages, or libraries you like. The data is synthetic but built to behave like real Pet Care Center operating data — including the parts that misbehave.

---

## Files in this folder

| File | Rows | Description |
|---|---|---|
| `locations.csv` | 47 | One row per Pet Care Center, including its leadership assignment |
| `clinic_weekly.csv` | ~2,200 | One row per clinic per week — the core operating panel |
| `provider_weekly.csv` | ~5,600 | One row per doctor per week, most recent 26 weeks only |
| `action_plans.csv` | 28 | Open and closed improvement plans with owners, targets, and status |
| `README.md` | — | This file |

All files are UTF-8 CSV and read into pandas/polars/Excel without configuration.

---

## Reference date

Treat **Monday, 2026-05-04** as "today." You are writing the digest that lands in a leader's inbox this morning.

The weekly panel covers 52 weeks. `week_start` is always a Monday; the most recent complete week begins **2026-04-27**. Provider-level data exists only for the most recent 26 weeks.

---

## Quick start

```python
import pandas as pd

loc = pd.read_csv('locations.csv', parse_dates=['opened_date'])
cw  = pd.read_csv('clinic_weekly.csv', parse_dates=['week_start'])
pw  = pd.read_csv('provider_weekly.csv', parse_dates=['week_start'])
ap  = pd.read_csv('action_plans.csv', parse_dates=['opened_date','due_date','last_status_update'])

# Your leader owns 11 of the 47 centers
mine = loc[loc.rmp_name == 'Dr. Priya Raghunathan']
print(len(mine), 'centers')
```

---

## `locations.csv`

| Column | Type | Notes |
|---|---|---|
| `location_id` | string | Unique. Format: `PCC_001` |
| `location_name` | string | Display name of the Pet Care Center |
| `metro_area` | string | Metro market |
| `state` | string | Two-letter state code |
| `region` | string | One of four operating regions |
| `rmp_name` | string | Regional Medical Partner who owns this center |
| `rop_name` | string | Regional Operations Partner who owns this center |
| `opened_date` | date | When the center opened. Some opened during the data window. |
| `maturity_tier` | category | `new` (<12mo), `ramping` (12–24mo), `mature` (>24mo) |
| `exam_rooms` | int | Physical capacity |
| `doctor_fte_target` | float | Budgeted doctor FTE for this center |

---

## `clinic_weekly.csv`

One row per center per week. A center has no rows for weeks before it opened.

### Keys

| Column | Type | Notes |
|---|---|---|
| `location_id` | string | Joins to `locations.location_id` |
| `week_start` | date | Monday of the operating week |

### Volume & capacity

| Column | Type | Notes |
|---|---|---|
| `doctor_hours_scheduled` | float | Total scheduled doctor hours for the week |
| `appts_completed` | int | Appointments seen |
| `appts_no_show` | int | Booked, did not arrive |
| `appts_cancelled` | int | Cancelled by the client |
| `appts_per_doctor_hour` | float | Throughput. `appts_completed / doctor_hours_scheduled` |

### Medical quality

| Column | Type | Notes |
|---|---|---|
| `recheck_compliance_pct` | float | % of visits requiring a recheck where the recheck was scheduled before the client left |
| `record_completion_24h_pct` | float | % of medical records closed within 24 hours of the visit |
| `callback_compliance_pct` | float | % of required post-visit client callbacks completed on time |

### Client experience

| Column | Type | Notes |
|---|---|---|
| `client_csat` | float | 1–5 mean satisfaction score. ~2% missing. |
| `csat_responses` | int | Number of survey responses behind that score |
| `avg_wait_time_min` | float | Mean client wait. A few rows are negative — see data quality notes. |

### Commercial

| Column | Type | Notes |
|---|---|---|
| `revenue_per_appt` | float | USD. ~1% missing. |
| `membership_conversion_pct` | float | % of eligible visits converting to a care membership — **see data quality note 5** |

### Staffing

| Column | Type | Notes |
|---|---|---|
| `staff_call_outs` | int | Unplanned absences that week |
| `open_dvm_requisitions` | int | Unfilled doctor roles at week end |

---

## `provider_weekly.csv`

One row per doctor per week, **most recent 26 weeks only**. A doctor has no row for a week they weren't scheduled.

| Column | Type | Notes |
|---|---|---|
| `provider_id` | string | Format: `DVM_0001` |
| `location_id` | string | Primary center. Joins to `locations.location_id`. |
| `week_start` | date | Monday of the operating week |
| `employment_type` | category | `full_time`, `part_time`, `relief`. Capitalization is inconsistent in some rows. |
| `tenure_months` | int | Months at Petfolk |
| `scheduled_hours` | float | Hours scheduled that week |
| `appts_completed` | int | Appointments seen by this doctor |
| `recheck_compliance_pct` | float | Same definition as the clinic metric, at doctor level |
| `record_completion_24h_pct` | float | Same definition as the clinic metric, at doctor level |
| `avg_appt_duration_min` | float | Mean appointment length. ~2% missing. |

Clinic-level `recheck_compliance_pct` and `record_completion_24h_pct` are the hours-weighted average of their providers for weeks where both tables overlap.

---

## `action_plans.csv`

Improvement plans opened by regional leaders against a specific metric at a specific center.

| Column | Type | Notes |
|---|---|---|
| `plan_id` | string | Format: `AP_001` |
| `location_id` | string | Center the plan targets |
| `owner_name` | string | Leader accountable |
| `target_metric` | string | Column name in `clinic_weekly.csv` this plan is trying to move |
| `baseline_value` | float | Value when the plan opened |
| `target_value` | float | Goal |
| `opened_date` | date | |
| `due_date` | date | Some are in the past |
| `status` | category | `not_started`, `on_track`, `at_risk`, `complete` — **self-reported by the owner** |
| `last_status_update` | date | When the owner last touched the record |
| `notes` | string | Mostly empty |

---

## Joining the tables

- `locations.location_id` → `clinic_weekly.location_id` (one-to-many)
- `locations.location_id` → `provider_weekly.location_id` (one-to-many)
- `locations.location_id` → `action_plans.location_id` (one-to-many)
- `provider_weekly` and `clinic_weekly` join on `location_id` + `week_start`

`locations.rmp_name` is what defines a leader's span. Every center has exactly one Regional Medical Partner and one Regional Operations Partner.

---

## Data quality notes

These are real things you'd hit in production. Don't assume the data is clean.

1. **Duplicate rows** — a small number of exact duplicate rows exist in `clinic_weekly.csv` from a double-run of the weekly load.
2. **Missing values** in `client_csat` (~2%), `revenue_per_appt` (~1%), and `avg_appt_duration_min` (~2%).
3. **Negative values** in `avg_wait_time_min` for a handful of rows, from a known clock-sync bug. Treat as missing.
4. **Inconsistent capitalization** in `provider_weekly.employment_type`.
5. **`membership_conversion_pct` changed definition on 2026-02-09.** The denominator was widened to include urgent care visits, which previously were excluded. Values before and after that date are not directly comparable.
6. **Centers that opened mid-window** have partial history and ramping denominators. Trend comparisons that assume a full 52 weeks will break on these.
7. **Small denominators.** Several centers are new or small. Weekly rates built on few appointments or few survey responses move a lot on their own.

---

## Questions

If something in the data doesn't make sense or seems contradictory, document the question in your submission and proceed with a stated assumption. We'd rather see how you handle ambiguity than get a clean answer to the wrong question.

Good luck.
