# Sample Strategy

Manual publishing samples should use Korean product families with language
versions, not generic page-level documents.

## Current Manual Samples

Keep:

- `zdx-u60`
  - Source file: `ZDX-U60.pdf`
  - Source language: Korean
  - Target language versions: Korean, English, Spanish
- `haven-zd-r90`
  - Source file: `Haven_Quick_user_guide_ZD-R90.pdf`
  - Source language: Korean / English mixed quick guide
  - Target language versions: Korean, English, Spanish

Discard for this MVP sample set:

- `SHP-P52HBXC-RM.pdf`
- `SHP-P71+퀵매뉴얼+02549A+230201.pdf`
- `SHP-DP951.pdf`

Those files are useful OCR stress tests, but their font encoding and dense
layout make them poor first samples for the product-family publishing flow.

## Translation Pipeline

For each manual page and target language:

1. Translator Agent: Korean source to draft translation.
2. Technical Accuracy Editor: verify numbers, units, model names, button labels,
   warnings, and step order against the source.
3. Native Review Editor: make the translation natural for customers without
   changing technical meaning.
4. Human Review: final edit and publish.

Raw OCR text is internal material. Customer-facing pages should use reviewed
published text.
