# Preferences. Working defaults, edit me.

These are working defaults prefilled from the current strategy notes. Luke edits
this file and the scorer treats it as the single source of truth on what he
wants. Capability lives in the CV. Desire lives here. The tracked copy at
config/preferences.template.md and the live copy at
config/profile/preferences.md start identical. Edit the live copy.

## Engagement types, in priority order.

1. Fractional or interim software director for medtech and IVD companies.
2. IEC 62304 and ISO 13485 consulting and audit preparation.
3. The fixed scope IEC 62304 gap assessment.
4. Specialist medtech test consulting through LKeoghQA.

Generic testing roles and generic software roles score low regardless of pay.

## Rates.

Director level day rates in line with the current engagement, and a salary
floor for permanent posts. The two machine-readable lines below are read by
the scorer and by the gating code, so changing a number here changes it
everywhere at once. A day rate meets the day floor, an hourly rate times
eight meets the day floor, and an annual salary meets the salary floor as
stated. Nothing converts between them.

day_rate_floor_gbp: 750
salary_floor_gbp: 100000

A stated figure under its floor is one question away, would they move on the
rate. An unstated rate passes and is the first question to ask.

## Location and travel.

Home base West Sussex. Comfortable with Belgium and wider European travel in a
hybrid pattern, with an established rhythm around Ghent and Leuven. Fully
remote works well. Full time relocation is a deal breaker.

## Sectors.

Software driven medical devices and IVD first, photonics and diagnostics
especially. Adjacent regulated sectors, defence, aerospace, utilities, are
acceptable as bridge work but score below medtech. Unregulated sectors score
lowest.

## Scoring thresholds.

Two machine-readable lines, read by the gating code.

cv_pass_score: 70
cv_floor_score: 40

## Title and keywords.

Lines the dashboard's scoring panel manages in place. The scorer reads
them as part of this file, so what is written here shapes want_match.

target_title: Fractional software director, medtech and IVD
keywords: IEC 62304, ISO 13485, IVD, medical devices, fractional, interim
