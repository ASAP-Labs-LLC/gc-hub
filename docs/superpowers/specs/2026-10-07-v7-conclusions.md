# v7.0.0 lane V1: findings and the automatic conclusion

2026-10-07. Code: `analysis_core.py` (`_assess`, `range_position`,
`standard_profile`, `render_bullets`, `deviation_conclusion`) and
`static/js/compare_logic.js` (`findingsView`). Supersedes the bullet and
conclusion wording of the phase 3+4 spec
(`2026-09-29-phase3-4-bullets-comments-design.md`); the detection itself
(trend and spike channels, thresholds, windows, ranges + 1 lines) is unchanged.

## Why

Ryan: "The gas conclusion always says higher even if the deviation is lower."
In v6, `_assess` set `elevated` for a range whose bullet was LOWER when it had
any sharp peak above the standard or a moderate higher run, and
`deviation_conclusion` worded every elevated range as "elevated intensity …
consistent with possible <range> contamination". A range that was only lower
was left out, so a sample clearly lower in the gas range got "no significant
deviation … consistent with the reference standard", and a deviation outside
the ranges never reached the conclusion at all.

## The rule: one verdict, used by both

Each range (and the outside) gets a **verdict** from its broad (trend) runs,
which the bullet leads with and the conclusion says:

| verdict | when |
|---|---|
| `higher` / `lower` | the dominant direction by area, no moderate run the other way |
| `mixed` | a run the other way also reaches moderate (the v6 "also" clause) |

Sharp peaks never change the verdict. They are reported on their own, in the
bullet's numbers and in a separate sentence of the conclusion. A range with
sharp peaks only reads "sharp peaks above/below <std>".

Item fields (additive; `/api/analysis` `items`): `verdict`, `position`,
`broad_severity` (trend runs only; the conclusion's adverb), `dominant_severity`.
Each spike also gets a `severity`. `mixed` now means `verdict == "mixed"`;
`direction` is still the dominant direction by area. `severity` (the bullet's
tag) is the largest |diff| in the dominant direction, sharp peaks included, or
for a mixed range the larger of the two directions. `elevated` keeps its v6
meaning (the bullet reports something higher) and no longer drives the conclusion.

## Light end, main body, heavy end

`position` comes from the range's evaluated carbons compared with the
standard's own distribution, never from the label. `standard_profile` takes the
carbon numbers at 10/50/90% of the standard's area (above its 2nd-percentile
floor, after the 0.35 min solvent window, up to the x-axis limit).
`range_position` compares the range's middle carbon with them: below C10% is
`light`, above C90% is `heavy`, otherwise `middle`. Without a profile it uses a
typical middle distillate (C10 / C16 / C22). So C5–C11 is the light end of a
diesel (measured C10.6 / C15.4 / C21.0 on the regression fixture) but the main
body of a gasoline.

Within a position, the boiling-range name comes from absolute carbons. A light
range ending at or below C12 adds "(gasoline-range)". A heavy range starting at
or above C20 says "lube/oil-range material".

## Bullets

`• <label> (Cx–Cy[, evaluated …]): <direction> than <std> — <severity>[, <part> <change>] (<numbers>)`

- direction: `higher` / `lower` / `mixed, higher and lower` / `sharp peaks above|below|above and below`
- part + change: `light end` / `main body` / `heavy end`, then `elevated`, `reduced`, or `differs in shape` when mixed. Ranges only, not the outside line.
- numbers: `max ±N at t min`; `N% of range beyond the marginal threshold`; the sharp peaks. A mixed range starts with `mostly <dir>, <sev>; <other dir> <sev> at <spans>`.
- The head and severity stay parseable. `report_layout.finding_rows` sets the text up to the first `(Cx–Cy…):` in bold and reads the tag from the first `— <severity>`. Compare's `splitLine` splits at the item's label. The line count is still at most ranges + 1.
- This keeps the v6 head `Gas (C5–C11):` and does not use Ryan's sketch `Gas C5–C11: lower than Diesel, moderate — …`. The `(Cx–Cy):` head and `— <severity>` are the contract both the PDF and Compare parse (and lane V3 owns `report_layout.py`). The plain-language direction now comes right after the head.

## Conclusion

No "Conclusion: " prefix any more. The report's box and Compare's section are
both already titled "Conclusion", and only tests depended on the prefix.
Sentences, in order:

1. **Range findings, by severity** (significant first, then range order). The first opens with "Compared to <std>, this sample shows …", the next ones with "It also shows …". Each finding reads "<slightly|moderately|significantly> <elevated|lower> intensity in the <name> range (Cx–Cy): <what it means>, consistent with <cause>":
   - higher, light: "more light-end material than <std>, consistent with possible light-end (gasoline-range) contamination"
   - higher, middle: "more material in the main body of the distribution than <std>, consistent with a different blend or product"
   - higher, heavy: "more heavy-end material than <std>, consistent with heavier components such as lube/oil-range material" (else "(a heavier cut or blend)")
   - lower, light: "the light end is reduced compared with <std>, consistent with a heavier cut or loss of light components (weathering/evaporation)"
   - lower, middle: "the main body of the distribution is reduced compared with <std>, consistent with dilution by a lighter or heavier product"
   - lower, heavy: "the heavy end is reduced compared with <std>, consistent with a lighter cut or dilution with a lighter product"
   - mixed: "<slight|moderate|significant> deviations in both directions in the … range: the <part> differs in shape from <std> rather than only in amount (mostly <dir>; <other> at <spans>), consistent with a different product or a blend"

   The adverb grades the broad deviation (`broad_severity`), because sharp peaks get their own sentence. Ranges with the same carbon span and verdict are named together ("the gas / gasoline range"). A plain-word label is lower-cased ("Gas" becomes "gas"); anything else ("GRO") is kept as written, and a trailing "range" is dropped.
2. **Together.** When there is one light-end and one heavy-end verdict, the conclusion says what they mean together: a heavier, lighter or narrower distribution, or "a blend of a lighter and a heavier product (possible mixed contamination)".
3. **Outside.** "Outside the defined ranges it is <adverb> <higher|lower> than <std> at <spans>."
4. **Sharp peaks.** "It also shows isolated sharp peaks above the standard at … (in the gas range; up to +N, <severity>), which may indicate specific added components". Peaks below the standard read "which may indicate specific components missing from the sample". Peaks are located in at most 3 named ranges ("in N of the defined ranges" past that), or "outside the defined ranges". With no broad range finding this becomes "… shows no broad deviation in the defined ranges, but isolated sharp peaks …".
5. "These findings are indicative only and do not confirm specific substances." This ends every deviating conclusion.

Special cases. Nothing deviating gives "… no significant deviation in the
defined ranges. The chromatographic profile is consistent with the reference
standard." (with no ranges: "across the run"). No calibration keeps the v6
sentence. With no ranges and a deviation, it reads "… is moderately higher
across the run, at a–b min. No ranges were defined to attribute the deviation."

**Length.** The conclusion is capped at 1,500 characters (`comments.CONCLUSION_MAX`).
The first 3 range findings are written in full, then up to 4 in short
("Further deviations: moderately lower in the kero range (C9–C16); …; and N
more (see the findings)"). If the text is still too long, fewer findings are
written in full and fewer are named in short.

## Examples

These are the actual outputs, against "Diesel #2" with ranges Gas C5–C11 and
Oil C20–C44 on the test ladder (`tests/test_analysis_bullets.py` pins each one).

**Gas higher**
```
• Gas (C5–C11): higher than Diesel #2 — moderate, light end elevated (max +620 at 1.00 min; 20% of range beyond the marginal threshold)
```
> Compared to Diesel #2, this sample shows moderately elevated intensity in the gas range (C5–C11): more light-end material than Diesel #2, consistent with possible light-end (gasoline-range) contamination. These findings are indicative only and do not confirm specific substances.

**Gas lower**
```
• Gas (C5–C11): lower than Diesel #2 — moderate, light end reduced (max -620 at 1.00 min; 20% of range beyond the marginal threshold)
```
> Compared to Diesel #2, this sample shows moderately lower intensity in the gas range (C5–C11): the light end is reduced compared with Diesel #2, consistent with a heavier cut or loss of light components (weathering/evaporation). These findings are indicative only and do not confirm specific substances.

**Gas mixed**
```
• Gas (C5–C11): mixed, higher and lower than Diesel #2 — moderate, light end differs in shape (mostly higher, moderate; lower moderate at 2.00–2.30 min; max +600 at 0.60 min; 43% of range beyond the marginal threshold)
```
> Compared to Diesel #2, this sample shows moderate deviations in both directions in the gas range (C5–C11): the light end differs in shape from Diesel #2 rather than only in amount (mostly higher; lower at 2.00–2.30 min), consistent with a different product or a blend. These findings are indicative only and do not confirm specific substances.

**Oil higher**
```
• Oil (C20–C44, evaluated to C28): higher than Diesel #2 — moderate, heavy end elevated (max +620 at 6.50 min; 25% of range beyond the marginal threshold)
```
> Compared to Diesel #2, this sample shows moderately elevated intensity in the oil range (C20–C44): more heavy-end material than Diesel #2, consistent with heavier components such as lube/oil-range material. These findings are indicative only and do not confirm specific substances.

**Two ranges, different directions**
```
• Gas (C5–C11): lower than Diesel #2 — significant, light end reduced (max -2500 at 1.00 min; 20% of range beyond the marginal threshold)
• Oil (C20–C44, evaluated to C28): higher than Diesel #2 — moderate, heavy end elevated (max +620 at 6.50 min; 25% of range beyond the marginal threshold)
```
> Compared to Diesel #2, this sample shows significantly lower intensity in the gas range (C5–C11): the light end is reduced compared with Diesel #2, consistent with a heavier cut or loss of light components (weathering/evaporation). It also shows moderately elevated intensity in the oil range (C20–C44): more heavy-end material than Diesel #2, consistent with heavier components such as lube/oil-range material. Together, a reduced light end and an elevated heavy end indicate a heavier overall distribution than Diesel #2. These findings are indicative only and do not confirm specific substances.

**Sharp peaks only**
```
• Gas (C5–C11): sharp peaks above Diesel #2 — moderate (2 sharp peaks above the standard at 0.84, 1.40 min; no broad deviation above the marginal threshold)
```
> Compared to Diesel #2, this sample shows no broad deviation in the defined ranges, but isolated sharp peaks above the standard at 0.84, 1.40 min (in the gas range; up to +950, moderate), which may indicate specific added components. These findings are indicative only and do not confirm specific substances.

**Outside only**
```
No deviations above the marginal threshold within the defined ranges.
• Outside the defined ranges: higher than Diesel #2 — moderate at 3.70–3.90 min (max +700 at 3.70 min)
```
> Compared to Diesel #2, this sample shows no significant deviation in the defined ranges. Outside the defined ranges it is moderately higher than Diesel #2 at 3.70–3.90 min. These findings are indicative only and do not confirm specific substances.

**All within**
```
No deviations above the marginal threshold.
```
> Compared to Diesel #2, this sample shows no significant deviation in the defined ranges. The chromatographic profile is consistent with the reference standard.

**Ryan's case: lower overall, sharp peaks above** (v6: "elevated intensity in the gas range … contamination")
```
• Gas (C5–C11): lower than Diesel #2 — moderate, light end reduced (max -700 at 3.00 min; 33% of range beyond the marginal threshold; 2 sharp peaks above, 1 below the standard at 2.00, 2.40, 3.00 min)
```
> Compared to Diesel #2, this sample shows slightly lower intensity in the gas range (C5–C11): the light end is reduced compared with Diesel #2, consistent with a heavier cut or loss of light components (weathering/evaporation). It also shows isolated sharp peaks above the standard at 2.00, 2.40 min (in the gas range; up to +1200, moderate), which may indicate specific added components, and an isolated sharp peak below the standard at 3.00 min (in the gas range; -700, moderate), which may indicate a specific component missing from the sample. These findings are indicative only and do not confirm specific substances.

## Compare

`compare_logic.findingsView`: the badge reads `<Severity> · <verdict>`, or
`· sharp peaks` for a range with sharp peaks only. A row's `direction` is the
verdict. A v6 answer without `verdict` falls back to mixed, else its direction.
`compare_logic.js` is now `?v=4`.

## Compatibility

The display changes, but nothing is recomputed or stored differently, which
makes this part of the v7 MAJOR release. `report_log` rows keep whatever
conclusion they printed. The replay (`tests/replay/`) compares results-CSV
columns only and never reads bullets or conclusions.
