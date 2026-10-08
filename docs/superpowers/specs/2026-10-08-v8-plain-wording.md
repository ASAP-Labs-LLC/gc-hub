# v8.0.0: findings and conclusion in plain words

2026-10-08. Code: `analysis_core.py` (`render_bullets`, `deviation_conclusion`).
Replaces the wording sections of `2026-10-07-v7-conclusions.md`; the detection,
each range's verdict (higher / lower / mixed), severity and position are v7's,
unchanged.

## Why

Ryan: "Those bullet points and comments are soo long … I dont need exact
retention times, I dont need signal strength numbers. Imagine middle school
grade understanding, keep it stupid simple. We who use the graphs understand
the depth but the customer doesn't care."

The report is read by the customer. The charts carry the detail; the words say
what is different and what it could mean.

## Rules

- No retention times, no signal sizes, no percentages in the findings or the conclusion.
- Short sentences, everyday words: "more light material", "a different pattern", "mixed in".
- Severity is still the tag on each finding (marginal / moderate / significant) and still decides "slightly more" / "more" / "much more" in the conclusion.

## Findings (one line per range, plus the outside line)

`• <label> (Cx–Cy[, partly checked]): <direction> than <std> — <severity>[, plus N sharp peaks]`

| case | text after the head |
|---|---|
| higher / lower | `higher than Diesel #2 — moderate` |
| mixed | `both higher and lower than Diesel #2 — moderate` |
| sharp peaks only | `2 sharp peaks above Diesel #2 — moderate` (`above and below` when both) |
| with sharp peaks too | `… — moderate, plus 3 sharp peaks` |
| not evaluated | `not checked, outside this run` |
| no difference | `No differences found.` / `No differences found in the ranges.` |

The `(Cx–Cy…):` head and the first `— <severity>` are kept: `report_layout.finding_rows` and Compare's `splitLine` parse them.

## Conclusion

Up to three range findings (most severe first), each followed by what it could
mean (a meaning already said is not repeated); then "Other ranges differ too
(see the findings)." when there are more, the light and heavy ends together, a
difference outside the ranges, the sharp peaks, and the closing line.

| finding | sentence | could mean |
|---|---|---|
| higher, light end | `… has more light material in the gas range.` | `This could mean a lighter fuel, such as gasoline, was mixed in.` ("such as gasoline" only for a range ending at or below C12) |
| higher, heavy end | `… more heavy material in the oil range.` | `This could mean a heavier product, such as oil, was mixed in.` ("such as oil" only from C20 up) |
| higher, main body | `… more material in the … range.` | `This could be a different fuel or a blend.` |
| lower, light end | `… less light material …` | `The lightest parts may have evaporated, or it may be a heavier fuel.` |
| lower, heavy end | `… less heavy material …` | `This could mean a lighter fuel was mixed in.` |
| lower, main body | `… less material …` | `This could mean another fuel was mixed in.` |
| mixed | `… a different pattern in the gas range (some parts higher, some lower).` | `This could be a different fuel or a blend.` |

- Together: lower light + higher heavy → `Overall, it is heavier than <std>.`; the reverse → `lighter`; both higher → `It may be a mix of a lighter and a heavier product.`
- Outside the ranges: `It also differs from <std> outside the ranges.`
- Sharp peaks: `It also has 2 sharp peaks above <std>. These could be something added.` (below: `something missing`).
- No difference: `This sample closely matches <std> in every range.` (no ranges: `This sample closely matches <std>.`)
- No calibration: `The ranges could not be checked because this sample has no usable calibration.`
- Closing line: `This is a screening result only. It does not prove what is in the sample.`

Example: `Compared to Diesel #2, this sample has more light material in the gas range. This could mean a lighter fuel, such as gasoline, was mixed in. This is a screening result only. It does not prove what is in the sample.`
